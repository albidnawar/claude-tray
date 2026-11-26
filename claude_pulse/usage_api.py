"""
Usage API Client
================

Reads a Claude Code OAuth login from a config directory and asks
``api.anthropic.com`` for the account's usage.  This is the only module that
touches credentials.  The approach (token from ``.credentials.json``, the
``/api/oauth/usage`` endpoint, ``claude update`` to renew an expired token)
is the one proven by Usage Monitor for Claude (MIT, Jens Duttke).

The access token is used only in the Authorization header of requests to
``api.anthropic.com``; it is never logged, stored or sent anywhere else.

Requests use the standard library only (``urllib`` and ``ssl``).  Server
certificates are verified against the Windows certificate store, which is where
a company proxy's root certificate is installed, and the system proxy settings
apply.  Staying off third-party HTTP libraries keeps about 17 MB of memory free.
"""
from __future__ import annotations

import http.client
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ['Login', 'FetchOutcome', 'read_login', 'fetch_usage', 'fetch_profile', 'refresh_login', 'plan_name']

API_URL_USAGE = 'https://api.anthropic.com/api/oauth/usage'
API_URL_PROFILE = 'https://api.anthropic.com/api/oauth/profile'
_FALLBACK_USER_AGENT = 'claude-code/2.1.332'
_REQUEST_TIMEOUT = 10
_REFRESH_TIMEOUT = 60
_EXPIRY_MARGIN_SECONDS = 60

_ssl_context: ssl.SSLContext | None = None
_ssl_lock = threading.Lock()


@dataclass(frozen=True)
class Login:
    """What the credentials file says about an account (the token itself stays private to this module)."""

    access_token: str
    expires_at: float | None
    subscription: str | None
    tier: str | None

    def is_expired(self) -> bool:
        return self.expires_at is not None and self.expires_at - _EXPIRY_MARGIN_SECONDS <= time.time()


@dataclass
class FetchOutcome:
    """Result of one usage request: either ``usage`` or an ``error`` kind."""

    usage: dict[str, Any] | None = None
    error: str | None = None  # no_token | auth | rate_limit | network | server
    retry_after: int | None = None


def read_login(config_dir: Path) -> Login | None:
    """Parse ``<config_dir>/.credentials.json``; None when it is missing or unreadable."""
    try:
        raw = json.loads((config_dir / '.credentials.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None

    oauth = raw.get('claudeAiOauth') if isinstance(raw, dict) else None
    if not isinstance(oauth, dict) or not oauth.get('accessToken'):
        return None

    expires = oauth.get('expiresAt')
    return Login(
        access_token=str(oauth['accessToken']),
        expires_at=expires / 1000 if isinstance(expires, (int, float)) else None,
        subscription=oauth.get('subscriptionType') if isinstance(oauth.get('subscriptionType'), str) else None,
        tier=oauth.get('rateLimitTier') if isinstance(oauth.get('rateLimitTier'), str) else None,
    )


def plan_name(login: Login | None) -> str:
    """Human plan name from the credentials: ``'Max 20x'``, ``'Pro'``, ``'Team'``."""
    if login is None or not login.subscription:
        return ''

    name = login.subscription.replace('_', ' ').title()
    multiplier = re.search(r'(\d+)x', login.tier or '')
    return f'{name} {multiplier.group(1)}x' if multiplier else name


def fetch_usage(config_dir: Path) -> FetchOutcome:
    """Fetch the usage response for the account stored in ``config_dir``."""
    login = read_login(config_dir)
    if login is None:
        return FetchOutcome(error='no_token')

    try:
        status, headers, body = _send(API_URL_USAGE, _headers(login))
    except (OSError, http.client.HTTPException):
        return FetchOutcome(error='network')

    if status == 401:
        return FetchOutcome(error='auth')
    if status == 429:
        return FetchOutcome(error='rate_limit', retry_after=_retry_after(headers))
    if status >= 500:
        return FetchOutcome(error='server')
    if status >= 400:
        return FetchOutcome(error='network')

    try:
        payload = json.loads(body)
    except ValueError:
        return FetchOutcome(error='server')

    return FetchOutcome(usage=payload) if isinstance(payload, dict) else FetchOutcome(error='server')


def fetch_profile(config_dir: Path) -> dict[str, Any] | None:
    """Account profile (display name); None on any failure, since it is only cosmetic."""
    login = read_login(config_dir)
    if login is None:
        return None

    try:
        status, _headers_unused, body = _send(API_URL_PROFILE, _headers(login))
        if status >= 400:
            return None

        payload = json.loads(body)
    except (OSError, http.client.HTTPException, ValueError):
        return None

    return payload if isinstance(payload, dict) else None


def refresh_login(config_dir: Path) -> bool:
    """Ask the Claude CLI to renew this account's token (``claude update``); True if the token changed."""
    before = read_login(config_dir)
    executable = shutil.which('claude')
    if executable is None:
        return False

    environment = {**os.environ, 'CLAUDE_CONFIG_DIR': str(config_dir)}
    flags = subprocess.CREATE_NO_WINDOW if sys.platform == 'win32' else 0
    try:
        subprocess.run(
            [executable, 'update'], env=environment, capture_output=True, timeout=_REFRESH_TIMEOUT,
            creationflags=flags, stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False

    after = read_login(config_dir)
    return after is not None and (before is None or after.access_token != before.access_token)


def _headers(login: Login) -> dict[str, str]:
    return {
        'Authorization': f'Bearer {login.access_token}',
        'Content-Type': 'application/json',
        'User-Agent': _FALLBACK_USER_AGENT,
        'anthropic-beta': 'oauth-2025-04-20',
    }


def _retry_after(headers: Any) -> int | None:
    try:
        return max(int(headers.get('Retry-After', '')), 0)
    except (ValueError, TypeError):
        return None


def _context() -> ssl.SSLContext:
    """One shared TLS context (certificates from the Windows store, verification on), built on first use."""
    global _ssl_context
    with _ssl_lock:
        if _ssl_context is None:
            _ssl_context = ssl.create_default_context()

        return _ssl_context


def _send(url: str, headers: dict[str, str]) -> tuple[int, Any, bytes]:
    """GET ``url``; returns ``(status, response headers, body)`` for any HTTP status, raises OSError when there is no response."""
    request = urllib.request.Request(url, headers=headers, method='GET')
    try:
        with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT, context=_context()) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        try:
            body = error.read()
        except (OSError, http.client.HTTPException):
            body = b''

        return error.code, error.headers, body

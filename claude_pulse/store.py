"""
Usage Store
===========

Holds the latest usage of every account and keeps it fresh.  Network work
runs on short-lived background threads; results come back to the Qt thread
through a queued signal.  Polling is adaptive: quick while something is on
screen, slow otherwise, with exponential backoff after errors and 429s.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .quotas import Quota, pick_session, pick_weekly, quotas_from_usage
from .settings import AccountConfig, Settings
from .usage_api import FetchOutcome, fetch_profile, fetch_usage, plan_name, read_login, refresh_login

__all__ = ['AccountState', 'UsageStore']

POLL_WATCHED_SECONDS = 90
POLL_IDLE_SECONDS = 240
MIN_GAP_SECONDS = 20
HOVER_MAX_AGE_SECONDS = 45
STALE_AFTER_SECONDS = 600
REFRESH_COOLDOWN_SECONDS = 300
PROFILE_TTL_SECONDS = 6 * 3600
_MAX_BACKOFF_SECONDS = 900
_SCHEDULER_TICK_MS = 10_000


@dataclass
class AccountState:
    """Everything the UI needs to draw one account."""

    config: AccountConfig
    label: str
    full_name: str = ''
    plan: str = ''
    quotas: list[Quota] = field(default_factory=list)
    error: str | None = None
    updated_at: float | None = None

    @property
    def id(self) -> str:
        return self.config.id

    @property
    def session(self) -> Quota | None:
        return pick_session(self.quotas)

    @property
    def weekly(self) -> Quota | None:
        return pick_weekly(self.quotas)

    @property
    def extras(self) -> list[Quota]:
        main = {quota.key for quota in (self.session, self.weekly) if quota is not None}
        return [quota for quota in self.quotas if quota.key not in main]

    @property
    def has_data(self) -> bool:
        return bool(self.quotas)

    def age_seconds(self) -> float | None:
        return None if self.updated_at is None else max(0.0, time.time() - self.updated_at)

    def is_stale(self) -> bool:
        age = self.age_seconds()
        return age is None or age > STALE_AFTER_SECONDS


@dataclass
class _Runtime:
    """Scheduling bookkeeping for one account (never shown)."""

    last_attempt: float = 0.0
    blocked_until: float = 0.0
    failures: int = 0
    in_flight: bool = False
    last_refresh_attempt: float = 0.0
    profile_at: float = 0.0


@dataclass
class _Result:
    account_id: str
    outcome: FetchOutcome
    profile: dict[str, Any] | None
    plan: str


class UsageStore(QObject):
    """Latest usage for all accounts plus the currently selected one."""

    changed = Signal()
    _delivered = Signal(object)

    def __init__(self, accounts: list[AccountConfig], settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.accounts = [AccountState(config=config, label=_fallback_label(index)) for index, config in enumerate(accounts)]
        self._runtime = {state.id: _Runtime() for state in self.accounts}
        self.watching = False
        self.offline = False

        wanted = settings.active_account
        self.active_id = wanted if any(state.id == wanted for state in self.accounts) else (self.accounts[0].id if self.accounts else '')

        self._delivered.connect(self._apply)
        self._timer = QTimer(self)
        self._timer.setInterval(_SCHEDULER_TICK_MS)
        self._timer.timeout.connect(self._schedule)

    @property
    def active(self) -> AccountState | None:
        return self.account(self.active_id)

    def account(self, account_id: str) -> AccountState | None:
        return next((state for state in self.accounts if state.id == account_id), None)

    @property
    def refreshing(self) -> bool:
        """True while any account is being fetched."""
        return any(runtime.in_flight for runtime in self._runtime.values())

    def start(self) -> None:
        self._timer.start()
        self._schedule()

    def select(self, account_id: str) -> None:
        if account_id == self.active_id or self.account(account_id) is None:
            return

        self.active_id = account_id
        self.settings.active_account = account_id
        self.changed.emit()

    def cycle(self) -> None:
        """Select the next account."""
        if len(self.accounts) < 2:
            return

        ids = [state.id for state in self.accounts]
        self.select(ids[(ids.index(self.active_id) + 1) % len(ids)])

    def ensure_fresh(self) -> None:
        """Fetch any account whose data is older than a hover is willing to show."""
        now = time.time()
        for state in self.accounts:
            age = state.age_seconds()
            if age is None or age > HOVER_MAX_AGE_SECONDS:
                self._maybe_fetch(state, now)

    def refresh_now(self) -> None:
        """Manual refresh: every account, still honouring the minimum gap and any backoff."""
        now = time.time()
        for state in self.accounts:
            self._maybe_fetch(state, now)

    def _schedule(self) -> None:
        interval = POLL_WATCHED_SECONDS if self.watching else POLL_IDLE_SECONDS
        now = time.time()
        for state in self.accounts:
            runtime = self._runtime[state.id]
            if now - runtime.last_attempt >= interval:
                self._maybe_fetch(state, now)

    def _maybe_fetch(self, state: AccountState, now: float) -> None:
        runtime = self._runtime[state.id]
        if self.offline or runtime.in_flight or now < runtime.blocked_until or now - runtime.last_attempt < MIN_GAP_SECONDS:
            return

        runtime.in_flight = True
        runtime.last_attempt = now
        may_refresh = now - runtime.last_refresh_attempt > REFRESH_COOLDOWN_SECONDS
        want_profile = state.full_name == '' or now - runtime.profile_at > PROFILE_TTL_SECONDS
        threading.Thread(target=self._work, args=(state.config, may_refresh, want_profile), daemon=True).start()

    def _work(self, config: AccountConfig, may_refresh: bool, want_profile: bool) -> None:
        refreshed_attempted = False
        login = read_login(config.config_dir)
        if login is not None and login.is_expired() and may_refresh:
            refresh_login(config.config_dir)
            refreshed_attempted = True

        outcome = fetch_usage(config.config_dir)
        if outcome.error == 'auth' and may_refresh and not refreshed_attempted:
            refreshed_attempted = True
            if refresh_login(config.config_dir):
                outcome = fetch_usage(config.config_dir)

        profile = fetch_profile(config.config_dir) if want_profile and outcome.usage is not None else None
        if refreshed_attempted:
            self._runtime[config.id].last_refresh_attempt = time.time()

        self._delivered.emit(_Result(config.id, outcome, profile, plan_name(read_login(config.config_dir))))

    def _apply(self, result: _Result) -> None:
        state = self.account(result.account_id)
        if state is None:
            return

        runtime = self._runtime[state.id]
        runtime.in_flight = False
        outcome = result.outcome
        now = time.time()

        if result.plan:
            state.plan = result.plan

        if outcome.usage is not None:
            runtime.failures = 0
            runtime.blocked_until = 0.0
            state.quotas = quotas_from_usage(outcome.usage)
            state.error = None
            state.updated_at = now
            if result.profile is not None:
                runtime.profile_at = now
                state.full_name = _display_name(result.profile)
        else:
            runtime.failures += 1
            state.error = outcome.error
            runtime.blocked_until = now + _backoff(outcome, runtime.failures)

        self.relabel()
        self.changed.emit()

    def relabel(self) -> None:
        """Short, unique segment labels: the plan name when plans differ, else the first name, else a number."""
        plans = [state.plan.split(' ')[0] for state in self.accounts]
        names = [state.full_name.split(' ')[0] for state in self.accounts]
        for index, state in enumerate(self.accounts):
            if state.config.label:
                state.label = state.config.label
            elif plans[index] and plans.count(plans[index]) == 1:
                state.label = plans[index]
            elif names[index] and names.count(names[index]) == 1:
                state.label = names[index]
            else:
                state.label = _fallback_label(index)


def _backoff(outcome: FetchOutcome, failures: int) -> float:
    if outcome.error == 'rate_limit':
        return min(_MAX_BACKOFF_SECONDS, max(float(outcome.retry_after or 0), 120.0 * 2 ** (failures - 1)))
    if outcome.error in ('auth', 'no_token'):
        return 60.0

    return min(300.0, 30.0 * 2 ** (failures - 1))


def _display_name(profile: dict[str, Any]) -> str:
    account = profile.get('account')
    if not isinstance(account, dict):
        return ''

    name = account.get('display_name') or account.get('full_name')
    return name if isinstance(name, str) else ''


def _fallback_label(index: int) -> str:
    return f'Account {index + 1}'

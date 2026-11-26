"""
Settings & Account Discovery
============================

A tiny JSON file in ``%APPDATA%\\ClaudePulse`` remembers which account is
active and whether the island is on.  Accounts are found automatically:
the default Claude config directory plus every directory under
``~/.claude-profiles`` that holds a ``.credentials.json``.  A ``accounts``
list in the settings file overrides discovery.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ['AccountConfig', 'Settings', 'ISLAND_MODES', 'ISLAND_STYLES', 'discover_accounts', 'settings_path']

MAX_ACCOUNTS = 3
ISLAND_MODES = ('off', 'hover', 'always')
ISLAND_STYLES = ('floating', 'attached')
_CREDENTIALS_NAME = '.credentials.json'


@dataclass(frozen=True)
class AccountConfig:
    """Where one Claude account keeps its login."""

    id: str
    config_dir: Path
    label: str | None = None


def settings_path() -> Path:
    base = os.environ.get('APPDATA')
    root = Path(base) if base else Path.home() / '.config'
    return root / 'ClaudePulse' / 'settings.json'


class Settings:
    """Persistent user preferences; every write goes straight to disk."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or settings_path()
        self._data: dict[str, Any] = self._load()

    @property
    def active_account(self) -> str | None:
        value = self._data.get('active_account')
        return value if isinstance(value, str) else None

    @active_account.setter
    def active_account(self, value: str) -> None:
        self._set('active_account', value)

    @property
    def island_mode(self) -> str:
        """``'off'``, ``'hover'`` (drops down from the top edge) or ``'always'``; reads the old on/off flag too."""
        value = self._data.get('island_mode')
        if value in ISLAND_MODES:
            return str(value)

        return 'always' if self._data.get('island_enabled') else 'off'

    @island_mode.setter
    def island_mode(self, value: str) -> None:
        if value in ISLAND_MODES:
            self._set('island_mode', value)

    @property
    def island_style(self) -> str:
        """``'attached'`` (grows out of the top edge of the screen) or ``'floating'`` (a pill with a gap above it)."""
        value = self._data.get('island_style')
        return str(value) if value in ISLAND_STYLES else 'attached'

    @island_style.setter
    def island_style(self, value: str) -> None:
        if value in ISLAND_STYLES:
            self._set('island_style', value)

    @property
    def show_activity(self) -> bool:
        return bool(self._data.get('show_activity', True))

    @show_activity.setter
    def show_activity(self, value: bool) -> None:
        self._set('show_activity', bool(value))

    @property
    def account_overrides(self) -> list[dict[str, Any]]:
        value = self._data.get('accounts')
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def _load(self) -> dict[str, Any]:
        try:
            loaded = json.loads(self.path.read_text(encoding='utf-8-sig'))
        except (OSError, ValueError):
            return {}

        return loaded if isinstance(loaded, dict) else {}

    def _set(self, key: str, value: Any) -> None:
        if self._data.get(key) == value:
            return

        self._data[key] = value
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self._data, indent=2), encoding='utf-8')
        except OSError:
            pass


def discover_accounts(settings: Settings, home: Path | None = None) -> list[AccountConfig]:
    """Accounts from the settings override, or discovered from the standard Claude locations."""
    home = home or Path.home()

    configured = _accounts_from_overrides(settings.account_overrides)
    if configured:
        return configured[:MAX_ACCOUNTS]

    found: list[AccountConfig] = []
    default_dir = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude')
    if (default_dir / _CREDENTIALS_NAME).is_file():
        found.append(AccountConfig('default', default_dir))

    profiles = home / '.claude-profiles'
    if profiles.is_dir():
        for child in sorted(profiles.iterdir()):
            if child.is_dir() and (child / _CREDENTIALS_NAME).is_file() and child != default_dir:
                found.append(AccountConfig(child.name, child))

    return found[:MAX_ACCOUNTS]


def _accounts_from_overrides(overrides: list[dict[str, Any]]) -> list[AccountConfig]:
    accounts: list[AccountConfig] = []
    for index, item in enumerate(overrides):
        directory = item.get('dir')
        if not isinstance(directory, str) or not directory:
            continue

        label = item.get('label')
        identifier = item.get('id')
        accounts.append(AccountConfig(
            id=identifier if isinstance(identifier, str) and identifier else f'account{index + 1}',
            config_dir=Path(os.path.expandvars(directory)).expanduser(),
            label=label if isinstance(label, str) and label else None,
        ))

    return accounts

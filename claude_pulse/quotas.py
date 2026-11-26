"""
Quota Parsing
=============

Turns a raw ``/api/oauth/usage`` response into a short, ordered list of
quotas.  Quota fields are never hardcoded: any dict entry with a non-null
``utilization`` and a field name of the form ``<number word>_<unit>[_<variant>]``
is a quota, so new model-scoped limits show up without a code change.

This module is pure (no Qt, no network) so it can be unit tested directly.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

__all__ = ['Quota', 'quotas_from_usage', 'parse_field_name', 'pick_session', 'pick_weekly']

_NUMBER_WORDS = {
    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5, 'six': 6, 'seven': 7,
    'eight': 8, 'nine': 9, 'ten': 10, 'twelve': 12, 'fourteen': 14, 'thirty': 30,
}
_UNIT_SECONDS = {'hour': 3600, 'day': 86400, 'week': 7 * 86400}
_UNIT_SHORT = {'hour': 'h', 'day': 'd', 'week': 'w'}
_SESSION_SECONDS = 5 * 3600
_WEEK_SECONDS = 7 * 86400


@dataclass(frozen=True)
class Quota:
    """One rate-limit window of an account."""

    key: str
    label: str
    short: str
    percent: float
    resets_at: datetime | None
    period_seconds: int | None
    variant: str | None = None

    def elapsed_fraction(self, now: datetime | None = None) -> float | None:
        """Fraction (0..1) of the window that has already passed, or None when unknown."""
        if self.resets_at is None or not self.period_seconds:
            return None

        now = now or datetime.now(timezone.utc)
        remaining = (self.resets_at - now).total_seconds()
        return min(1.0, max(0.0, 1.0 - remaining / self.period_seconds))

    def seconds_until_reset(self, now: datetime | None = None) -> float | None:
        """Seconds until the window resets (never negative), or None when unknown."""
        if self.resets_at is None:
            return None

        now = now or datetime.now(timezone.utc)
        return max(0.0, (self.resets_at - now).total_seconds())


def parse_field_name(field: str) -> tuple[int, str, str | None] | None:
    """Split ``'seven_day_sonnet'`` into ``(7, 'day', 'sonnet')``; None if it is not a period name."""
    parts = field.split('_', 2)
    if len(parts) < 2:
        return None

    number = _NUMBER_WORDS.get(parts[0])
    if number is None or parts[1] not in _UNIT_SECONDS:
        return None

    return number, parts[1], (parts[2] if len(parts) > 2 else None)


def quotas_from_usage(data: Any) -> list[Quota]:
    """Extract every active quota from a usage response, session first, then weekly, then the rest."""
    if not isinstance(data, dict):
        return []

    quotas: list[Quota] = []
    for field, value in data.items():
        if not isinstance(value, dict) or not isinstance(value.get('utilization'), (int, float)):
            continue

        parsed = parse_field_name(field)
        if parsed is None:
            continue

        number, unit, variant = parsed
        quotas.append(_build_quota(field, number, unit, variant, float(value['utilization']), _parse_time(value.get('resets_at'))))

    quotas.extend(_scoped_limit_quotas(data, {quota.key for quota in quotas}))
    quotas.sort(key=_sort_key)
    return quotas


def pick_session(quotas: list[Quota]) -> Quota | None:
    """The unscoped five-hour window, if the plan has one."""
    return next((quota for quota in quotas if quota.variant is None and quota.period_seconds == _SESSION_SECONDS), None)


def pick_weekly(quotas: list[Quota]) -> Quota | None:
    """The unscoped seven-day window, if the plan has one."""
    return next((quota for quota in quotas if quota.variant is None and quota.period_seconds == _WEEK_SECONDS), None)


def _build_quota(key: str, number: int, unit: str, variant: str | None, percent: float, resets_at: datetime | None) -> Quota:
    period = number * _UNIT_SECONDS[unit]
    short = f'{number}{_UNIT_SHORT[unit]}'

    if variant is None and period == _SESSION_SECONDS:
        label = 'Session'
    elif variant is None and period == _WEEK_SECONDS:
        label = 'Weekly'
    else:
        name = variant.replace('_', ' ').title() if variant else ''
        label = f'{name} {short}'.strip()

    return Quota(key=key, label=label, short=short, percent=percent, resets_at=resets_at, period_seconds=period, variant=variant)


def _scoped_limit_quotas(data: dict[str, Any], known_keys: set[str]) -> list[Quota]:
    """Model-scoped weekly limits that only exist inside the ``limits`` array."""
    limits = data.get('limits')
    if not isinstance(limits, list):
        return []

    weekly_period = _WEEK_SECONDS
    result: list[Quota] = []
    for limit in limits:
        if not isinstance(limit, dict):
            continue

        scope = limit.get('scope')
        model = scope.get('model') if isinstance(scope, dict) else None
        name = model.get('display_name') if isinstance(model, dict) else None
        percent = limit.get('percent')
        if not name or not isinstance(percent, (int, float)) or limit.get('group') != 'weekly':
            continue

        slug = ''.join(char if char.isalnum() else '_' for char in name.lower()).strip('_')
        key = f'seven_day_{slug}'
        if key in known_keys:
            continue

        result.append(Quota(
            key=key, label=f'{name} 7d', short='7d', percent=float(percent),
            resets_at=_parse_time(limit.get('resets_at')), period_seconds=weekly_period, variant=slug,
        ))

    return result


def _sort_key(quota: Quota) -> tuple[int, int, str]:
    if quota.variant is None and quota.period_seconds == _SESSION_SECONDS:
        return 0, 0, quota.key
    if quota.variant is None and quota.period_seconds == _WEEK_SECONDS:
        return 1, 0, quota.key

    return 2, quota.period_seconds or 0, quota.key


def _parse_time(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

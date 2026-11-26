"""
Text Formatting
===============

Small pure helpers that turn numbers and timestamps into the short strings
the UI shows.  No Qt, no I/O.
"""
from __future__ import annotations

from datetime import datetime

__all__ = ['percent_text', 'countdown', 'reset_clock', 'clock_text', 'age_text', 'severity']

_WEEKDAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')

SEVERITY_WARN = 70.0
SEVERITY_CRITICAL = 90.0


def percent_text(percent: float | None) -> str:
    """``42.4`` -> ``'42%'``; unknown -> an em-free placeholder."""
    if percent is None:
        return '--'

    return f'{round(percent):d}%'


def severity(percent: float | None) -> str:
    """Bucket a percentage into ``'ok'``, ``'warn'`` or ``'critical'``."""
    if percent is None or percent < SEVERITY_WARN:
        return 'ok'

    return 'warn' if percent < SEVERITY_CRITICAL else 'critical'


def countdown(seconds: float | None) -> str:
    """Compact remaining time: ``'2h 14m'``, ``'4d 3h'``, ``'<1m'``."""
    if seconds is None:
        return '--'

    minutes = int(seconds // 60)
    if minutes < 1:
        return '<1m'
    if minutes < 60:
        return f'{minutes}m'

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f'{hours}h {minutes:02d}m' if minutes else f'{hours}h'

    days, hours = divmod(hours, 24)
    return f'{days}d {hours}h' if hours else f'{days}d'


def reset_clock(moment: datetime | None, use_24h: bool, now: datetime | None = None) -> str:
    """Local wall-clock text for a reset: ``'14:00'`` today, ``'Tue 14:00'`` on another day."""
    if moment is None:
        return '--'

    local = moment.astimezone()
    clock = clock_text(moment, use_24h)

    reference = (now or datetime.now().astimezone()).astimezone()
    if local.date() == reference.date():
        return clock

    return f'{_WEEKDAYS[local.weekday()]} {clock}'


def clock_text(moment: datetime | None, use_24h: bool) -> str:
    """Local time of day only: ``'17:30'`` or ``'5:30 PM'``."""
    if moment is None:
        return '--'

    local = moment.astimezone()
    if use_24h:
        return f'{local.hour:02d}:{local.minute:02d}'

    hour = local.hour % 12 or 12
    return f'{hour}:{local.minute:02d} {"AM" if local.hour < 12 else "PM"}'


def age_text(seconds: float | None) -> str:
    """How long ago the data was fetched: ``'just now'``, ``'3m ago'``."""
    if seconds is None:
        return 'never'
    if seconds < 15:
        return 'just now'
    if seconds < 60:
        return f'{int(seconds)}s ago'

    minutes = int(seconds // 60)
    if minutes < 60:
        return f'{minutes}m ago'

    return f'{minutes // 60}h ago'

"""
Demo Data
=========

Realistic fake accounts and Claude sessions for ``--demo`` and for the preview
renderer, so the interface can be tried (and screenshotted) without a login or
a network.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .activity import DONE, IDLE, NEEDS_INPUT, RUNNING, STOPPED, SessionActivity, SessionMonitor
from .quotas import quotas_from_usage
from .store import AccountState, UsageStore

__all__ = ['apply_demo_data', 'demo_accounts', 'demo_sessions']


def apply_demo_data(
    store: UsageStore, session: tuple[float, float] = (42, 18), weekly: tuple[float, float] = (63, 34), activity: SessionMonitor | None = None,
) -> None:
    """Fill the store with fake usage; ``session``/``weekly`` hold the percentages for each of the two accounts."""
    now = datetime.now(timezone.utc)
    samples = [
        ('Alex Morgan', 'Team', session[0], weekly[0], timedelta(hours=2, minutes=14), timedelta(days=4, hours=3)),
        ('Alex Morgan', 'Pro', session[1], weekly[1], timedelta(hours=3, minutes=41), timedelta(days=2, hours=19)),
    ]
    store.offline = True
    for state, (name, plan, five, seven, five_left, seven_left) in zip(store.accounts, samples):
        _fill(state, name, plan, five, seven, (now + five_left).isoformat(), (now + seven_left).isoformat())

    store.relabel()
    if activity is not None:
        activity.set_fixed(demo_sessions([state.id for state in store.accounts]))

    store.changed.emit()


def demo_sessions(account_ids: list[str], states: tuple[str, ...] = (RUNNING, DONE, IDLE)) -> list[SessionActivity]:
    """Fake sessions in the given states, spread over the accounts."""
    now = time.time()
    catalogue = {
        RUNNING: ('Add island activity panel', 'claude-tray', 'Editing island.py', now - 3, (3, 5)),
        DONE: ('Refactor the landing page', 'bakery-site', 'Finished', now - 140, None),
        NEEDS_INPUT: ('Choose a database', 'Data_Vision_Final', 'Waiting for your answer', now - 20, None),
        STOPPED: ('Migrate the old scripts', 'Python in 30 days', 'Stopped', now - 300, None),
        IDLE: ('Fix the signup form', 'portfolio', '', now - 3 * 3600, None),
    }
    sessions = []
    for index, state in enumerate(states):
        title, project, detail, stamp, progress = catalogue[state]
        sessions.append(SessionActivity(
            account_id=account_ids[index % len(account_ids)], session_id=f'demo-{index}', project=project, title=title,
            state=state, detail=detail, updated_at=stamp, progress=progress,
        ))

    return sessions


def _fill(state: AccountState, name: str, plan: str, five: float, seven: float, five_reset: str, seven_reset: str) -> None:
    state.full_name = name
    state.plan = plan
    state.quotas = quotas_from_usage({
        'five_hour': {'utilization': five, 'resets_at': five_reset},
        'seven_day': {'utilization': seven, 'resets_at': seven_reset},
    })
    state.updated_at = time.time() - 12
    state.error = None


def demo_accounts() -> list[Path]:
    return [Path('demo-team'), Path('demo-pro')]

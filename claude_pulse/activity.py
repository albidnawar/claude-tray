"""
Claude Activity
===============

Shows what your Claude Code sessions are doing right now.  Claude Code appends
every step of a session to a transcript file under ``<config dir>/projects``.
This module reads the *end* of the newest transcripts (read-only, local, no
network) and works out for each session whether it is running, waiting for you,
finished or stopped, and in one short phrase what it is doing.

``summarize()`` is pure and unit tested; ``SessionMonitor`` does the file access
on a background thread and tells the UI when something changed.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QTimer, Signal

from .settings import AccountConfig

__all__ = [
    'SessionActivity', 'SessionMonitor', 'summarize', 'describe_tool', 'read_tail_entries',
    'RUNNING', 'NEEDS_INPUT', 'DONE', 'STOPPED', 'IDLE',
]

RUNNING = 'running'
NEEDS_INPUT = 'needs_input'
DONE = 'done'
STOPPED = 'stopped'
IDLE = 'idle'

POLL_SECONDS = 2.0
MAX_SESSIONS = 3
_PER_ACCOUNT = 3
_TAIL_BYTES = 600_000
_LARGE_TAIL_BYTES = 4_000_000
_RUNNING_STALE_SECONDS = 600.0
_DONE_FRESH_SECONDS = 600.0
_RECENT_SECONDS = 24 * 3600.0
_FILE_LIST_TTL_SECONDS = 15.0
_TITLE_LIMIT = 70

_END_STOPS = {'end_turn', 'stop_sequence', 'max_tokens', 'refusal'}
_INPUT_TOOLS = {'AskUserQuestion', 'ExitPlanMode'}
_NOISE_PREFIXES = ('<command-name>', '<command-message>', '<local-command-', '<system-reminder>')
_INTERRUPT_MARKER = 'Request interrupted by user'


@dataclass(frozen=True)
class SessionActivity:
    """One Claude Code session, reduced to what the island shows."""

    account_id: str
    session_id: str
    project: str
    title: str
    state: str
    detail: str
    updated_at: float | None
    progress: tuple[int, int] | None = None

    def age_seconds(self, now: float | None = None) -> float | None:
        if self.updated_at is None:
            return None

        return max(0.0, (now if now is not None else time.time()) - self.updated_at)


def summarize(entries: list[dict[str, Any]], account_id: str, session_id: str, fallback_project: str = '', now: float | None = None) -> SessionActivity:
    """Work out a session's state from the tail of its transcript (entries in file order)."""
    project = fallback_project
    title = ''
    prompt = ''
    for entry in entries:
        cwd = entry.get('cwd')
        if isinstance(cwd, str) and cwd:
            project = _basename(cwd)

        kind = entry.get('type')
        if kind == 'ai-title' and isinstance(entry.get('aiTitle'), str):
            title = entry['aiTitle']
        elif kind == 'last-prompt' and isinstance(entry.get('lastPrompt'), str):
            prompt = entry['lastPrompt']

    state, detail, stamp = _latest_state(entries)
    progress, current = _latest_progress(entries)
    if state == RUNNING and current and detail in ('Thinking', 'Working'):
        detail = current

    label = _clean_line(title) or _clean_line(prompt) or project or 'Claude session'
    activity = SessionActivity(
        account_id=account_id, session_id=session_id, project=project, title=label, state=state, detail=detail,
        updated_at=stamp, progress=progress if state == RUNNING else None,
    )
    return apply_staleness(activity, now)


def apply_staleness(activity: SessionActivity, now: float | None = None) -> SessionActivity:
    """Sessions that stopped writing long ago are idle, whatever their last entry said."""
    age = activity.age_seconds(now)
    if age is None:
        return replace(activity, state=IDLE, detail='')

    if activity.state in (RUNNING, NEEDS_INPUT) and age > _RUNNING_STALE_SECONDS:
        return replace(activity, state=IDLE, detail='', progress=None)
    if activity.state in (DONE, STOPPED) and age > _DONE_FRESH_SECONDS:
        return replace(activity, state=IDLE, detail='', progress=None)

    return activity


def describe_tool(name: str, params: Any) -> str:
    """One short phrase for a tool call, such as ``'Editing island.py'``."""
    data = params if isinstance(params, dict) else {}
    target = _basename(str(data.get('file_path') or data.get('notebook_path') or data.get('path') or ''))

    if name in ('Bash', 'PowerShell', 'BashOutput'):
        return _clean_line(str(data.get('description') or '')) or 'Running a command'
    if name == 'Read':
        return f'Reading {target}' if target else 'Reading files'
    if name == 'Write':
        return f'Writing {target}' if target else 'Writing a file'
    if name in ('Edit', 'MultiEdit', 'NotebookEdit'):
        return f'Editing {target}' if target else 'Editing a file'
    if name in ('Glob', 'Grep'):
        return 'Searching the code'
    if name == 'WebSearch':
        return 'Searching the web'
    if name == 'WebFetch':
        return 'Reading a web page'
    if name in ('Task', 'Agent'):
        return _clean_line(str(data.get('description') or '')) or 'Running a sub-agent'
    if name in ('TodoWrite', 'TaskCreate', 'TaskUpdate'):
        return 'Planning the work'
    if name == 'AskUserQuestion':
        return 'Waiting for your answer'
    if name == 'ExitPlanMode':
        return 'Plan ready for your approval'
    if name.startswith('mcp__'):
        parts = name.split('__')
        return f'{parts[1]}: {parts[-1]}'.replace('_', ' ') if len(parts) >= 3 else name

    return name or 'Working'


def read_tail_entries(path: Path, max_bytes: int = _TAIL_BYTES) -> list[dict[str, Any]]:
    """The last ``max_bytes`` of a JSONL transcript as parsed entries; a cut-off first line is dropped."""
    try:
        size = path.stat().st_size
        with open(path, 'rb') as handle:
            start = max(0, size - max_bytes)
            handle.seek(start)
            chunk = handle.read()
    except OSError:
        return []

    lines = chunk.split(b'\n')
    if start > 0:
        lines = lines[1:]

    entries: list[dict[str, Any]] = []
    for raw in lines:
        if not raw.strip():
            continue

        try:
            parsed = json.loads(raw)
        except ValueError:
            continue

        if isinstance(parsed, dict):
            entries.append(parsed)

    return entries


def _latest_state(entries: list[dict[str, Any]]) -> tuple[str, str, float | None]:
    for entry in reversed(entries):
        if entry.get('isSidechain'):
            continue

        kind = entry.get('type')
        if kind == 'system':
            if entry.get('subtype') == 'turn_duration':
                return DONE, 'Finished', _timestamp(entry)
            continue

        if kind not in ('user', 'assistant') or entry.get('isMeta'):
            continue

        message = entry.get('message') if isinstance(entry.get('message'), dict) else {}
        content = message.get('content')
        stamp = _timestamp(entry)

        if kind == 'assistant':
            blocks = [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []
            tool = next((block for block in reversed(blocks) if block.get('type') == 'tool_use'), None)
            if tool is not None:
                name = str(tool.get('name') or '')
                return (NEEDS_INPUT if name in _INPUT_TOOLS else RUNNING), describe_tool(name, tool.get('input')), stamp
            if message.get('stop_reason') in _END_STOPS:
                return DONE, 'Finished', stamp
            return RUNNING, 'Thinking', stamp

        text = _user_text(content)
        if text.startswith(_NOISE_PREFIXES):
            continue
        if _INTERRUPT_MARKER in text:
            return STOPPED, 'Stopped', stamp
        if isinstance(content, list) and any(isinstance(block, dict) and block.get('type') == 'tool_result' for block in content):
            return RUNNING, 'Working', stamp
        return RUNNING, 'Thinking', stamp

    return IDLE, '', None


def _latest_progress(entries: list[dict[str, Any]]) -> tuple[tuple[int, int] | None, str]:
    """Completed/total of the newest task list and the title of the task in progress."""
    for entry in reversed(entries):
        if entry.get('type') != 'assistant':
            continue

        message = entry.get('message') if isinstance(entry.get('message'), dict) else {}
        content = message.get('content')
        if not isinstance(content, list):
            continue

        for block in reversed(content):
            if not (isinstance(block, dict) and block.get('type') == 'tool_use' and block.get('name') == 'TodoWrite'):
                continue

            items = (block.get('input') or {}).get('todos') if isinstance(block.get('input'), dict) else None
            if not isinstance(items, list) or not items:
                return None, ''

            todos = [item for item in items if isinstance(item, dict)]
            done = sum(1 for item in todos if item.get('status') == 'completed')
            current = next((item for item in todos if item.get('status') == 'in_progress'), None)
            text = _clean_line(str(current.get('activeForm') or current.get('content') or '')) if current else ''
            return ((done, len(todos)) if done < len(todos) else None), text

    return None, ''


def _user_text(content: Any) -> str:
    if isinstance(content, str):
        return content.lstrip()

    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get('type') == 'text' and isinstance(block.get('text'), str):
                return block['text'].lstrip()

    return ''


def _timestamp(entry: dict[str, Any]) -> float | None:
    raw = entry.get('timestamp')
    if not isinstance(raw, str):
        return None

    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None

    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def _basename(path: str) -> str:
    return path.replace('\\', '/').rstrip('/').rsplit('/', 1)[-1]


def _clean_line(text: str) -> str:
    line = ' '.join(text.split())
    return line if len(line) <= _TITLE_LIMIT else line[: _TITLE_LIMIT - 1].rstrip() + '…'


_PRIORITY = {NEEDS_INPUT: 0, RUNNING: 1, DONE: 2, STOPPED: 3, IDLE: 4}


class SessionMonitor(QObject):
    """Polls the newest transcripts of every account and keeps the latest ``sessions``."""

    changed = Signal()
    _delivered = Signal(object)

    def __init__(self, accounts: list[AccountConfig], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._accounts = accounts
        self._sessions: list[SessionActivity] = []
        self._cache: dict[Path, tuple[float, int, SessionActivity]] = {}
        self._file_list: dict[str, tuple[float, list[Path]]] = {}
        self._in_flight = False
        self._frozen = False

        self._delivered.connect(self._apply)
        self._timer = QTimer(self)
        self._timer.setInterval(round(POLL_SECONDS * 1000))
        self._timer.timeout.connect(self._poll)

    @property
    def sessions(self) -> list[SessionActivity]:
        return list(self._sessions)

    def set_active(self, active: bool) -> None:
        """Poll only while something on screen shows the result."""
        if self._frozen:
            return

        if active and not self._timer.isActive():
            self._timer.start()
            self._poll()
        elif not active:
            self._timer.stop()

    def set_fixed(self, sessions: list[SessionActivity]) -> None:
        """Use fixed sessions and never touch the disk (demo mode)."""
        self._frozen = True
        self._timer.stop()
        self._sessions = list(sessions)
        self.changed.emit()

    def primary(self) -> SessionActivity | None:
        return self._sessions[0] if self._sessions else None

    def _poll(self) -> None:
        if self._in_flight or self._frozen:
            return

        self._in_flight = True
        threading.Thread(target=self._scan, daemon=True).start()

    def _scan(self) -> None:
        now = time.time()
        found: list[SessionActivity] = []
        try:
            for account in self._accounts:
                for path in self._recent_files(account, now):
                    activity = self._read(path, account, now)
                    if activity is not None:
                        found.append(activity)
        except Exception:  # a transcript being rewritten must never take the monitor down
            found = []

        found.sort(key=lambda item: (_PRIORITY[item.state], -(item.updated_at or 0.0)))
        self._delivered.emit(found[:MAX_SESSIONS])

    def _apply(self, sessions: list[SessionActivity]) -> None:
        self._in_flight = False
        if self._frozen or sessions == self._sessions:
            return

        self._sessions = sessions
        self.changed.emit()

    def _recent_files(self, account: AccountConfig, now: float) -> list[Path]:
        cached = self._file_list.get(account.id)
        if cached is not None and now - cached[0] < _FILE_LIST_TTL_SECONDS:
            return cached[1]

        found: list[tuple[float, Path]] = []
        projects = account.config_dir / 'projects'
        try:
            with os.scandir(projects) as project_entries:
                for project in project_entries:
                    if not project.is_dir():
                        continue

                    with os.scandir(project.path) as files:
                        for file in files:
                            if file.name.endswith('.jsonl') and file.is_file():
                                modified = file.stat().st_mtime
                                if now - modified < _RECENT_SECONDS:
                                    found.append((modified, Path(file.path)))
        except OSError:
            pass

        found.sort(reverse=True)
        paths = [path for _modified, path in found[:_PER_ACCOUNT]]
        self._file_list[account.id] = (now, paths)
        return paths

    def _read(self, path: Path, account: AccountConfig, now: float) -> SessionActivity | None:
        try:
            stat = path.stat()
        except OSError:
            return None

        cached = self._cache.get(path)
        if cached is not None and cached[0] == stat.st_mtime and cached[1] == stat.st_size:
            return apply_staleness(cached[2], now)

        entries = read_tail_entries(path)
        if not any(entry.get('type') in ('user', 'assistant') for entry in entries):
            entries = read_tail_entries(path, _LARGE_TAIL_BYTES)

        activity = summarize(entries, account.id, path.stem, _basename(path.parent.name), now)
        self._cache[path] = (stat.st_mtime, stat.st_size, activity)
        return activity

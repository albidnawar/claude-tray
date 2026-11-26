"""Tests for the Claude session activity parser and monitor, using synthetic transcripts."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from claude_pulse import activity  # noqa: E402
from claude_pulse.activity import DONE, IDLE, NEEDS_INPUT, RUNNING, STOPPED, SessionMonitor, describe_tool, summarize  # noqa: E402
from claude_pulse.settings import AccountConfig  # noqa: E402

NOW = datetime(2026, 11, 26, 12, 0, 0, tzinfo=timezone.utc).timestamp()


def iso(seconds_before_now: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_before_now, tz=timezone.utc).isoformat().replace('+00:00', 'Z')


def user(content, age: float = 5, **extra):
    return {'type': 'user', 'timestamp': iso(age), 'cwd': 'C:\\Projects\\Demo App', 'message': {'role': 'user', 'content': content}, **extra}


def assistant(blocks, stop='tool_use', age: float = 5, **extra):
    return {'type': 'assistant', 'timestamp': iso(age), 'message': {'role': 'assistant', 'stop_reason': stop, 'content': blocks}, **extra}


def tool_use(name, **params):
    return {'type': 'tool_use', 'id': 't1', 'name': name, 'input': params}


def result(entries, **kwargs):
    return summarize(entries, 'acct', 'sess', now=NOW, **kwargs)


class StateTests(unittest.TestCase):
    def test_finished_turn(self) -> None:
        entries = [
            user('please fix the bug', age=60),
            assistant([tool_use('Edit', file_path='C:\\x\\island.py')], age=50),
            user([{'type': 'tool_result', 'tool_use_id': 't1'}], age=45),
            assistant([{'type': 'text', 'text': 'done'}], stop='end_turn', age=20),
            {'type': 'system', 'subtype': 'turn_duration', 'timestamp': iso(19), 'isMeta': False},
            {'type': 'last-prompt', 'lastPrompt': 'please fix the bug'},
        ]
        session = result(entries)
        self.assertEqual((session.state, session.detail), (DONE, 'Finished'))
        self.assertAlmostEqual(session.age_seconds(NOW), 19, delta=1)

    def test_end_turn_without_a_duration_marker_is_still_finished(self) -> None:
        session = result([user('hi'), assistant([{'type': 'text', 'text': 'hello'}], stop='end_turn')])
        self.assertEqual(session.state, DONE)

    def test_pending_tool_call_says_what_is_running(self) -> None:
        cases = {
            ('Edit', 'island.py'): 'Editing island.py',
            ('Write', 'notes.md'): 'Writing notes.md',
            ('Read', 'app.py'): 'Reading app.py',
        }
        for (name, filename), expected in cases.items():
            session = result([user('go'), assistant([tool_use(name, file_path=f'C:\\code\\{filename}')])])
            self.assertEqual((session.state, session.detail), (RUNNING, expected))

    def test_shell_tool_uses_its_description(self) -> None:
        session = result([user('go'), assistant([tool_use('Bash', command='pytest -x', description='Run the unit tests')])])
        self.assertEqual(session.detail, 'Run the unit tests')
        bare = result([user('go'), assistant([tool_use('PowerShell', command='ls')])])
        self.assertEqual(bare.detail, 'Running a command')

    def test_new_prompt_and_tool_result_mean_working(self) -> None:
        self.assertEqual((result([user('do it')]).state, result([user('do it')]).detail), (RUNNING, 'Thinking'))
        waiting = result([user('x'), assistant([tool_use('Read', file_path='a.py')]), user([{'type': 'tool_result', 'tool_use_id': 't1'}])])
        self.assertEqual((waiting.state, waiting.detail), (RUNNING, 'Working'))
        mid_turn = result([user('x'), assistant([{'type': 'thinking', 'thinking': '...'}], stop='tool_use')])
        self.assertEqual((mid_turn.state, mid_turn.detail), (RUNNING, 'Thinking'))

    def test_interrupt_marks_the_session_stopped(self) -> None:
        stopped = result([user('x'), assistant([tool_use('Bash')]), user([{'type': 'text', 'text': '[Request interrupted by user for tool use]'}])])
        self.assertEqual((stopped.state, stopped.detail), (STOPPED, 'Stopped'))

    def test_questions_to_the_user_need_input(self) -> None:
        for name in ('AskUserQuestion', 'ExitPlanMode'):
            session = result([user('x'), assistant([tool_use(name)])])
            self.assertEqual(session.state, NEEDS_INPUT, name)

    def test_sidechain_and_meta_and_command_noise_are_ignored(self) -> None:
        entries = [
            user('real work'),
            assistant([tool_use('Edit', file_path='real.py')]),
            assistant([tool_use('Bash', description='sub-agent thing')], isSidechain=True),
            user('<command-name>/clear</command-name>'),
            user('injected caveat', isMeta=True),
        ]
        session = result(entries)
        self.assertEqual((session.state, session.detail), (RUNNING, 'Editing real.py'))

    def test_empty_transcript_is_idle(self) -> None:
        session = result([])
        self.assertEqual(session.state, IDLE)
        self.assertIsNone(session.updated_at)


class StalenessTests(unittest.TestCase):
    def test_old_running_session_is_idle_but_recent_one_is_not(self) -> None:
        self.assertEqual(result([user('x', age=500)]).state, RUNNING)
        self.assertEqual(result([user('x', age=700)]).state, IDLE)

    def test_finished_state_fades_to_idle_after_ten_minutes(self) -> None:
        done = [user('x', age=700), assistant([{'type': 'text'}], stop='end_turn', age=650)]
        fresh = [user('x', age=100), assistant([{'type': 'text'}], stop='end_turn', age=60)]
        self.assertEqual(result(done).state, IDLE)
        self.assertEqual(result(fresh).state, DONE)

    def test_idle_keeps_its_timestamp_for_the_ago_text(self) -> None:
        session = result([user('x', age=7200)])
        self.assertEqual(session.state, IDLE)
        self.assertAlmostEqual(session.age_seconds(NOW), 7200, delta=1)


class TitleTests(unittest.TestCase):
    def test_title_prefers_ai_title_then_prompt_then_project(self) -> None:
        base = [user('hello')]
        both = result(base + [{'type': 'last-prompt', 'lastPrompt': 'the prompt'}, {'type': 'ai-title', 'aiTitle': 'The AI title'}])
        prompt_only = result(base + [{'type': 'last-prompt', 'lastPrompt': 'the prompt'}])
        neither = result(base)
        self.assertEqual(both.title, 'The AI title')
        self.assertEqual(prompt_only.title, 'the prompt')
        self.assertEqual(neither.title, 'Demo App')

    def test_project_comes_from_the_working_directory_on_any_platform(self) -> None:
        self.assertEqual(result([user('x')]).project, 'Demo App')
        posix = result([{**user('x'), 'cwd': '/home/me/projects/site/'}])
        self.assertEqual(posix.project, 'site')

    def test_long_titles_are_shortened(self) -> None:
        title = result([user('x'), {'type': 'ai-title', 'aiTitle': 'word ' * 60}]).title
        self.assertLessEqual(len(title), 70)
        self.assertTrue(title.endswith('\u2026'))

    def test_fallback_when_there_is_nothing_to_show(self) -> None:
        self.assertEqual(summarize([], 'a', 's', now=NOW).title, 'Claude session')


class ProgressTests(unittest.TestCase):
    def _todos(self, *statuses):
        items = [{'content': f'task {i}', 'activeForm': f'Doing task {i}', 'status': status} for i, status in enumerate(statuses)]
        return assistant([tool_use('TodoWrite', todos=items)], age=30)

    def test_progress_and_current_task_while_working(self) -> None:
        entries = [user('x', age=40), self._todos('completed', 'in_progress', 'pending'), user([{'type': 'tool_result'}], age=2)]
        session = result(entries)
        self.assertEqual(session.progress, (1, 3))
        self.assertEqual(session.detail, 'Doing task 1')

    def test_tool_description_wins_over_the_task_title_while_a_tool_runs(self) -> None:
        entries = [user('x', age=40), self._todos('completed', 'in_progress'), assistant([tool_use('Edit', file_path='a.py')], age=2)]
        session = result(entries)
        self.assertEqual(session.detail, 'Editing a.py')
        self.assertEqual(session.progress, (1, 2))

    def test_finished_list_shows_no_progress(self) -> None:
        entries = [user('x', age=40), self._todos('completed', 'completed'), user([{'type': 'tool_result'}], age=2)]
        self.assertIsNone(result(entries).progress)

    def test_no_progress_for_a_finished_session(self) -> None:
        entries = [user('x', age=90), self._todos('completed', 'pending'), assistant([{'type': 'text'}], stop='end_turn', age=10)]
        self.assertIsNone(result(entries).progress)


class DescribeToolTests(unittest.TestCase):
    def test_known_and_unknown_tools(self) -> None:
        self.assertEqual(describe_tool('Grep', {'pattern': 'x'}), 'Searching the code')
        self.assertEqual(describe_tool('WebSearch', {}), 'Searching the web')
        self.assertEqual(describe_tool('Task', {'description': 'Explore the repo'}), 'Explore the repo')
        self.assertEqual(describe_tool('Write', None), 'Writing a file')
        self.assertEqual(describe_tool('mcp__figma__get_design_context', {}), 'figma: get design context')
        self.assertEqual(describe_tool('SomethingNew', {}), 'SomethingNew')
        self.assertEqual(describe_tool('', {}), 'Working')


class TailReadingTests(unittest.TestCase):
    def test_partial_first_line_is_dropped_and_garbage_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 's.jsonl'
            lines = [json.dumps({'type': 'user', 'n': index, 'pad': 'x' * 50}) for index in range(40)]
            lines.insert(30, 'this is not json')
            path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
            entries = activity.read_tail_entries(path, max_bytes=500)
            self.assertTrue(entries)
            self.assertEqual(entries[-1]['n'], 39)
            self.assertTrue(all(isinstance(entry, dict) for entry in entries))
            self.assertLess(len(entries), 40)

    def test_missing_file_and_empty_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            self.assertEqual(activity.read_tail_entries(Path(folder) / 'nope.jsonl'), [])
            empty = Path(folder) / 'e.jsonl'
            empty.write_text('', encoding='utf-8')
            self.assertEqual(activity.read_tail_entries(empty), [])


class MonitorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _write(self, directory: Path, project: str, name: str, entries: list, age: float = 0) -> Path:
        folder = directory / 'projects' / project
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f'{name}.jsonl'
        path.write_text('\n'.join(json.dumps(entry) for entry in entries) + '\n', encoding='utf-8')
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def _entries(self, finished: bool) -> list:
        now = time.time()

        def stamp(offset: float) -> str:
            return datetime.fromtimestamp(now - offset, tz=timezone.utc).isoformat()

        entries = [
            {'type': 'user', 'timestamp': stamp(30), 'cwd': 'C:\\p\\Site', 'message': {'role': 'user', 'content': 'go'}},
            {'type': 'assistant', 'timestamp': stamp(5), 'message': {'role': 'assistant', 'stop_reason': 'tool_use', 'content': [tool_use('Edit', file_path='a.py')]}},
        ]
        if finished:
            entries.append({'type': 'system', 'subtype': 'turn_duration', 'timestamp': stamp(2)})

        return entries

    def test_running_sessions_sort_before_finished_ones_across_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / 'one', Path(folder) / 'two'
            self._write(first, 'proj', 'done-session', self._entries(finished=True))
            self._write(second, 'proj', 'live-session', self._entries(finished=False))
            monitor = SessionMonitor([AccountConfig('one', first), AccountConfig('two', second)])
            monitor._scan()
            sessions = monitor.sessions
            self.assertEqual([(s.account_id, s.state) for s in sessions], [('two', RUNNING), ('one', DONE)])
            self.assertEqual(monitor.primary().session_id, 'live-session')

    def test_old_transcripts_and_missing_directories_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            account = Path(folder) / 'acct'
            self._write(account, 'proj', 'ancient', self._entries(finished=True), age=3 * 86400)
            monitor = SessionMonitor([AccountConfig('a', account), AccountConfig('ghost', Path(folder) / 'does-not-exist')])
            monitor._scan()
            self.assertEqual(monitor.sessions, [])

    def test_only_the_newest_few_per_account_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            account = Path(folder) / 'acct'
            for index in range(6):
                self._write(account, 'proj', f's{index}', self._entries(finished=True), age=index * 10)
            monitor = SessionMonitor([AccountConfig('a', account)])
            monitor._scan()
            self.assertEqual(len(monitor.sessions), activity.MAX_SESSIONS)
            self.assertEqual({s.session_id for s in monitor.sessions}, {'s0', 's1', 's2'})

    def test_changed_signal_fires_only_when_something_changed(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            account = Path(folder) / 'acct'
            self._write(account, 'proj', 's', self._entries(finished=False))
            monitor = SessionMonitor([AccountConfig('a', account)])
            fired: list[int] = []
            monitor.changed.connect(lambda: fired.append(1))
            monitor._scan()
            monitor._scan()
            self.assertEqual(len(fired), 1)

            self._write(account, 'proj', 's', self._entries(finished=True))
            monitor._scan()
            self.assertEqual(len(fired), 2)
            self.assertEqual(monitor.sessions[0].state, DONE)

    def test_unchanged_files_are_not_parsed_again(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            account = Path(folder) / 'acct'
            self._write(account, 'proj', 's', self._entries(finished=False))
            monitor = SessionMonitor([AccountConfig('a', account)])
            monitor._scan()
            calls: list[int] = []
            original = activity.read_tail_entries
            activity.read_tail_entries = lambda *args, **kwargs: (calls.append(1), original(*args, **kwargs))[1]
            try:
                monitor._scan()
            finally:
                activity.read_tail_entries = original
            self.assertEqual(calls, [])

    def test_corrupt_transcript_does_not_break_the_scan(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            account = Path(folder) / 'acct'
            folder_path = account / 'projects' / 'proj'
            folder_path.mkdir(parents=True)
            (folder_path / 'bad.jsonl').write_bytes(b'\xff\xfe not json at all \x00\n{broken')
            self._write(account, 'proj', 'good', self._entries(finished=False))
            monitor = SessionMonitor([AccountConfig('a', account)])
            monitor._scan()
            self.assertIn('good', [s.session_id for s in monitor.sessions])

    def test_fixed_sessions_freeze_the_monitor(self) -> None:
        monitor = SessionMonitor([AccountConfig('a', Path('nowhere'))])
        fixed = [activity.SessionActivity('a', 's', 'p', 't', RUNNING, 'x', time.time())]
        monitor.set_fixed(fixed)
        monitor.set_active(True)
        monitor._scan()
        self.assertEqual(monitor.sessions, fixed)


if __name__ == '__main__':
    unittest.main()

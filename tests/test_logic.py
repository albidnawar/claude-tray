"""Unit tests for the pure logic: quota parsing, formatting, settings, API error mapping and backoff."""
from __future__ import annotations

import http.client
import http.server
import json
import os
import ssl
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

os.environ['QT_QPA_PLATFORM'] = 'offscreen'
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtWidgets import QApplication  # noqa: E402

from claude_pulse import formatting, quotas, usage_api  # noqa: E402
from claude_pulse.settings import AccountConfig, Settings, discover_accounts  # noqa: E402
from claude_pulse.store import AccountState, UsageStore, _backoff  # noqa: E402

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    return (NOW + delta).isoformat()


class QuotaParsingTests(unittest.TestCase):
    def test_field_names(self) -> None:
        self.assertEqual(quotas.parse_field_name('five_hour'), (5, 'hour', None))
        self.assertEqual(quotas.parse_field_name('seven_day_sonnet'), (7, 'day', 'sonnet'))
        self.assertIsNone(quotas.parse_field_name('tangelo'))
        self.assertIsNone(quotas.parse_field_name('nimbus_quill'))
        self.assertIsNone(quotas.parse_field_name('seven_parsecs'))

    def test_session_and_weekly_come_first_and_junk_is_ignored(self) -> None:
        data = {
            'seven_day_opus': {'utilization': 5, 'resets_at': _iso(timedelta(days=3))},
            'seven_day': {'utilization': 8, 'resets_at': _iso(timedelta(days=3))},
            'five_hour': {'utilization': 9, 'resets_at': _iso(timedelta(hours=2))},
            'tangelo': None,
            'extra_usage': {'is_enabled': False, 'utilization': None},
            'cinder_cove': {'utilization': 50, 'resets_at': None},
        }
        result = quotas.quotas_from_usage(data)
        self.assertEqual([quota.key for quota in result], ['five_hour', 'seven_day', 'seven_day_opus'])
        self.assertEqual(quotas.pick_session(result).percent, 9)
        self.assertEqual(quotas.pick_weekly(result).percent, 8)
        self.assertEqual(result[0].label, 'Session')
        self.assertEqual(result[1].label, 'Weekly')
        self.assertEqual(result[2].label, 'Opus 7d')

    def test_null_reset_is_tolerated(self) -> None:
        result = quotas.quotas_from_usage({'five_hour': {'utilization': 0, 'resets_at': None}})
        self.assertEqual(len(result), 1)
        self.assertIsNone(result[0].resets_at)
        self.assertIsNone(result[0].elapsed_fraction(NOW))
        self.assertIsNone(result[0].seconds_until_reset(NOW))

    def test_non_dict_response(self) -> None:
        self.assertEqual(quotas.quotas_from_usage(None), [])
        self.assertEqual(quotas.quotas_from_usage([]), [])
        self.assertEqual(quotas.quotas_from_usage({'limits': 'garbage', 'five_hour': 'x'}), [])

    def test_elapsed_fraction(self) -> None:
        quota = quotas.quotas_from_usage({'five_hour': {'utilization': 1, 'resets_at': _iso(timedelta(hours=2, minutes=30))}})[0]
        self.assertAlmostEqual(quota.elapsed_fraction(NOW), 0.5)
        past = quotas.quotas_from_usage({'five_hour': {'utilization': 1, 'resets_at': _iso(timedelta(hours=-1))}})[0]
        self.assertEqual(past.elapsed_fraction(NOW), 1.0)
        self.assertEqual(past.seconds_until_reset(NOW), 0.0)

    def test_model_scoped_limit_from_limits_array(self) -> None:
        data = {
            'five_hour': {'utilization': 1, 'resets_at': _iso(timedelta(hours=1))},
            'limits': [
                {'group': 'weekly', 'percent': 12, 'resets_at': _iso(timedelta(days=2)), 'scope': {'model': {'display_name': 'Fable'}}},
                {'group': 'weekly', 'percent': 7, 'resets_at': None, 'scope': None},
                {'group': 'session', 'percent': 3, 'scope': {'model': {'display_name': 'Ignored'}}},
            ],
        }
        result = quotas.quotas_from_usage(data)
        scoped = [quota for quota in result if quota.variant == 'fable']
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0].percent, 12)
        self.assertEqual(scoped[0].label, 'Fable 7d')
        self.assertEqual(len(result), 2)

    def test_naive_timestamps_are_treated_as_utc(self) -> None:
        result = quotas.quotas_from_usage({'five_hour': {'utilization': 1, 'resets_at': '2026-11-26T14:00:00'}})
        self.assertEqual(result[0].resets_at.tzinfo, timezone.utc)


class FormattingTests(unittest.TestCase):
    def test_percent_and_severity(self) -> None:
        self.assertEqual(formatting.percent_text(42.4), '42%')
        self.assertEqual(formatting.percent_text(None), '--')
        self.assertEqual(formatting.severity(69.9), 'ok')
        self.assertEqual(formatting.severity(70), 'warn')
        self.assertEqual(formatting.severity(89.9), 'warn')
        self.assertEqual(formatting.severity(90), 'critical')
        self.assertEqual(formatting.severity(None), 'ok')

    def test_countdown(self) -> None:
        self.assertEqual(formatting.countdown(None), '--')
        self.assertEqual(formatting.countdown(20), '<1m')
        self.assertEqual(formatting.countdown(59 * 60), '59m')
        self.assertEqual(formatting.countdown(2 * 3600 + 14 * 60), '2h 14m')
        self.assertEqual(formatting.countdown(3 * 3600), '3h')
        self.assertEqual(formatting.countdown(4 * 86400 + 3 * 3600), '4d 3h')
        self.assertEqual(formatting.countdown(2 * 86400), '2d')

    def test_age_text(self) -> None:
        self.assertEqual(formatting.age_text(None), 'never')
        self.assertEqual(formatting.age_text(3), 'just now')
        self.assertEqual(formatting.age_text(40), '40s ago')
        self.assertEqual(formatting.age_text(180), '3m ago')
        self.assertEqual(formatting.age_text(7300), '2h ago')

    def test_reset_clock_today_and_other_day(self) -> None:
        local_now = datetime(2026, 11, 26, 9, 0).astimezone()
        same_day = datetime(2026, 11, 26, 14, 5).astimezone()
        later = datetime(2026, 11, 28, 14, 5).astimezone()
        self.assertEqual(formatting.reset_clock(same_day, True, local_now), '14:05')
        self.assertEqual(formatting.reset_clock(same_day, False, local_now), '2:05 PM')
        self.assertEqual(formatting.reset_clock(later, True, local_now), 'Sat 14:05')
        self.assertEqual(formatting.reset_clock(datetime(2026, 11, 26, 0, 30).astimezone(), False, local_now), '12:30 AM')
        self.assertEqual(formatting.reset_clock(None, True), '--')


class SettingsTests(unittest.TestCase):
    def test_round_trip_and_corrupt_file(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'sub' / 'settings.json'
            settings = Settings(path)
            self.assertEqual(settings.island_mode, 'off')
            self.assertTrue(settings.show_activity)
            settings.island_mode = 'hover'
            settings.show_activity = False
            settings.active_account = 'pro'

            reloaded = Settings(path)
            self.assertEqual(reloaded.island_mode, 'hover')
            self.assertFalse(reloaded.show_activity)
            self.assertEqual(reloaded.active_account, 'pro')

            path.write_text('{not json', encoding='utf-8')
            self.assertEqual(Settings(path).island_mode, 'off')

    def test_island_style_defaults_to_attached_and_ignores_garbage(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            settings = Settings(path)
            self.assertEqual(settings.island_style, 'attached')
            settings.island_style = 'floating'
            settings.island_style = 'sideways'
            self.assertEqual(Settings(path).island_style, 'floating')
            path.write_text('{"island_style": 42}', encoding='utf-8')
            self.assertEqual(Settings(path).island_style, 'attached')

    def test_file_saved_with_a_byte_order_mark_is_still_read(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text('{ "active_account": "pro", "island_enabled": true }', encoding='utf-8-sig')
            settings = Settings(path)
            self.assertEqual(settings.island_mode, 'always')  # the old on/off flag still means "always"
            settings.island_mode = 'bogus'
            self.assertEqual(settings.island_mode, 'always')
            self.assertEqual(settings.active_account, 'pro')

    def test_discovers_default_and_profile_accounts(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            for relative in ('.claude', '.claude-profiles/work', '.claude-profiles/side', '.claude-profiles/empty'):
                (home / relative).mkdir(parents=True)
            for relative in ('.claude', '.claude-profiles/work', '.claude-profiles/side'):
                (home / relative / '.credentials.json').write_text('{}', encoding='utf-8')

            with mock.patch.dict('os.environ', {}, clear=False) as environment:
                environment.pop('CLAUDE_CONFIG_DIR', None)
                found = discover_accounts(Settings(home / 'settings.json'), home)

            self.assertEqual([account.id for account in found], ['default', 'side', 'work'])

    def test_override_wins_over_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'settings.json'
            path.write_text(json.dumps({'accounts': [{'dir': folder, 'label': 'Mine'}, {'nodir': True}]}), encoding='utf-8')
            found = discover_accounts(Settings(path), Path(folder))
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].label, 'Mine')
            self.assertEqual(found[0].id, 'account1')


class UsageApiTests(unittest.TestCase):
    def _login_dir(self, folder: str, **oauth: object) -> Path:
        directory = Path(folder)
        payload = {'accessToken': 'secret-token', 'expiresAt': (time.time() + 3600) * 1000, 'subscriptionType': 'max', 'rateLimitTier': 'default_claude_max_20x'}
        payload.update(oauth)
        (directory / '.credentials.json').write_text(json.dumps({'claudeAiOauth': payload}), encoding='utf-8')
        return directory

    def test_read_login_and_plan_name(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            login = usage_api.read_login(self._login_dir(folder))
            self.assertIsNotNone(login)
            self.assertFalse(login.is_expired())
            self.assertEqual(usage_api.plan_name(login), 'Max 20x')

    def test_expired_and_missing_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            expired = usage_api.read_login(self._login_dir(folder, expiresAt=(time.time() - 10) * 1000))
            self.assertTrue(expired.is_expired())
            self.assertEqual(usage_api.plan_name(None), '')
            self.assertIsNone(usage_api.read_login(Path(folder) / 'missing'))

    def test_plan_name_without_multiplier(self) -> None:
        login = usage_api.Login('t', None, 'team', 'default_raven')
        self.assertEqual(usage_api.plan_name(login), 'Team')

    def _fetch(self, status: int, payload: object = None, headers: dict | None = None) -> usage_api.FetchOutcome:
        body = json.dumps(payload).encode() if payload is not None else b''
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(usage_api, '_send', return_value=(status, headers or {}, body)) as send:
            outcome = usage_api.fetch_usage(self._login_dir(folder))
            url, sent = send.call_args.args
            self.assertEqual(sent['Authorization'], 'Bearer secret-token')
            self.assertEqual(url, usage_api.API_URL_USAGE)
        return outcome

    def test_status_mapping(self) -> None:
        self.assertEqual(self._fetch(200, {'five_hour': {}}).usage, {'five_hour': {}})
        self.assertEqual(self._fetch(401).error, 'auth')
        self.assertEqual(self._fetch(500).error, 'server')
        self.assertEqual(self._fetch(503).error, 'server')
        self.assertEqual(self._fetch(404).error, 'network')
        rate = self._fetch(429, headers={'Retry-After': '120'})
        self.assertEqual((rate.error, rate.retry_after), ('rate_limit', 120))
        self.assertIsNone(self._fetch(429, headers={'Retry-After': 'soon'}).retry_after)
        self.assertIsNone(self._fetch(429).retry_after)
        self.assertEqual(self._fetch(200, ['not', 'a', 'dict']).error, 'server')
        self.assertEqual(self._fetch(200).error, 'server')  # an empty body is not JSON

    def test_network_failure_and_no_token(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = self._login_dir(folder)
            for failure in (ConnectionResetError(), TimeoutError(), ssl.SSLError('bad certificate'), http.client.IncompleteRead(b'x')):
                with mock.patch.object(usage_api, '_send', side_effect=failure):
                    self.assertEqual(usage_api.fetch_usage(directory).error, 'network', repr(failure))
            self.assertEqual(usage_api.fetch_usage(Path(folder) / 'nothing').error, 'no_token')

    def test_profile_is_best_effort(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            directory = self._login_dir(folder)
            good = (200, {}, b'{"account": {"display_name": "Ann"}}')
            with mock.patch.object(usage_api, '_send', return_value=good):
                self.assertEqual(usage_api.fetch_profile(directory), {'account': {'display_name': 'Ann'}})
            for bad in ((401, {}, b''), (200, {}, b'not json'), (200, {}, b'[1, 2]')):
                with mock.patch.object(usage_api, '_send', return_value=bad):
                    self.assertIsNone(usage_api.fetch_profile(directory))
            with mock.patch.object(usage_api, '_send', side_effect=OSError('down')):
                self.assertIsNone(usage_api.fetch_profile(directory))
            self.assertIsNone(usage_api.fetch_profile(Path(folder) / 'nothing'))

    def test_tls_context_verifies_certificates_and_is_shared(self) -> None:
        first = usage_api._context()
        self.assertIs(first, usage_api._context())
        self.assertEqual(first.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(first.check_hostname)

    def test_refresh_without_cli_is_a_clean_failure(self) -> None:
        with tempfile.TemporaryDirectory() as folder, mock.patch('shutil.which', return_value=None):
            self.assertFalse(usage_api.refresh_login(self._login_dir(folder)))


class SendTests(unittest.TestCase):
    """The real HTTP transport, against a throwaway local server (plain HTTP; TLS is covered by the context test)."""

    @classmethod
    def setUpClass(cls) -> None:
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - http.server API
                code = int(self.path.strip('/') or 200)
                body = json.dumps({'path': self.path, 'auth': self.headers.get('Authorization'), 'agent': self.headers.get('User-Agent')}).encode()
                self.send_response(code)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                if code == 429:
                    self.send_header('Retry-After', '45')
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def _get(self, path: str) -> tuple:
        return usage_api._send(f'http://127.0.0.1:{self.port}/{path}', {'Authorization': 'Bearer abc', 'User-Agent': 'claude-code/test'})

    def test_success_returns_status_headers_and_body(self) -> None:
        status, headers, body = self._get('200')
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual((payload['auth'], payload['agent']), ('Bearer abc', 'claude-code/test'))

    def test_error_statuses_are_returned_not_raised(self) -> None:
        for code in (401, 404, 429, 500, 503):
            status, headers, body = self._get(str(code))
            self.assertEqual(status, code)
            self.assertTrue(body)
        self.assertEqual(usage_api._retry_after(self._get('429')[1]), 45)

    def test_connection_refused_raises_oserror(self) -> None:
        with socket_closed_port() as port, self.assertRaises(OSError):
            usage_api._send(f'http://127.0.0.1:{port}/', {})


def socket_closed_port():
    import contextlib
    import socket

    @contextlib.contextmanager
    def manager():
        probe = socket.socket()
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
        probe.close()
        yield port

    return manager()


class StoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _store(self, folder: str, count: int = 2) -> UsageStore:
        accounts = [AccountConfig(f'a{index}', Path(folder) / f'a{index}') for index in range(count)]
        return UsageStore(accounts, Settings(Path(folder) / 'settings.json'))

    def test_backoff_rules(self) -> None:
        rate = usage_api.FetchOutcome(error='rate_limit', retry_after=500)
        self.assertEqual(_backoff(rate, 1), 500)
        self.assertEqual(_backoff(usage_api.FetchOutcome(error='rate_limit'), 1), 120)
        self.assertEqual(_backoff(usage_api.FetchOutcome(error='rate_limit'), 3), 480)
        self.assertEqual(_backoff(usage_api.FetchOutcome(error='rate_limit'), 10), 900)
        self.assertEqual(_backoff(usage_api.FetchOutcome(error='network'), 1), 30)
        self.assertEqual(_backoff(usage_api.FetchOutcome(error='network'), 20), 300)
        self.assertEqual(_backoff(usage_api.FetchOutcome(error='auth'), 1), 60)

    def test_labels_prefer_plan_then_name_then_number(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = self._store(folder)
            first, second = store.accounts
            first.plan, second.plan = 'Team', 'Pro'
            store.relabel()
            self.assertEqual((first.label, second.label), ('Team', 'Pro'))

            second.plan = 'Team'
            first.full_name, second.full_name = 'Ann Lee', 'Bob Ray'
            store.relabel()
            self.assertEqual((first.label, second.label), ('Ann', 'Bob'))

            second.full_name = 'Ann Smith'
            store.relabel()
            self.assertEqual((first.label, second.label), ('Account 1', 'Account 2'))

    def test_select_cycle_and_persist(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            store = self._store(folder)
            self.assertEqual(store.active_id, 'a0')
            store.select('a1')
            self.assertEqual(Settings(Path(folder) / 'settings.json').active_account, 'a1')
            store.cycle()
            self.assertEqual(store.active_id, 'a0')
            store.select('does-not-exist')
            self.assertEqual(store.active_id, 'a0')
            self.assertEqual(self._store(folder).active_id, 'a0')

    def test_apply_success_then_failure_keeps_data(self) -> None:
        from claude_pulse.store import _Result

        with tempfile.TemporaryDirectory() as folder:
            store = self._store(folder, 1)
            ok = usage_api.FetchOutcome(usage={'five_hour': {'utilization': 20, 'resets_at': _iso(timedelta(hours=1))}})
            store._apply(_Result('a0', ok, {'account': {'display_name': 'Ann Lee'}}, 'Pro'))
            state = store.accounts[0]
            self.assertEqual(state.full_name, 'Ann Lee')
            self.assertEqual(state.session.percent, 20)
            self.assertIsNone(state.error)

            store._apply(_Result('a0', usage_api.FetchOutcome(error='network'), None, 'Pro'))
            self.assertEqual(state.error, 'network')
            self.assertEqual(state.session.percent, 20)
            self.assertGreater(store._runtime['a0'].blocked_until, time.time())

    def test_stale_detection(self) -> None:
        state = AccountState(config=AccountConfig('x', Path('x')), label='x')
        self.assertTrue(state.is_stale())
        state.updated_at = time.time() - 5
        self.assertFalse(state.is_stale())
        state.updated_at = time.time() - 3600
        self.assertTrue(state.is_stale())


if __name__ == '__main__':
    unittest.main()

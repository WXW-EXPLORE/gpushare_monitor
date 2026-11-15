"""Authentication expiry and polling regressions; no external connections."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from gpushare_monitor import (
    AuthenticationError, ProbeError, cycle, fetch_one_json,
    health_mail_if_needed, main, poll_delay, validate, validate_market_page,
)


def config():
    return json.loads((Path(__file__).resolve().parent.parent /
                       'config.my-instance.example.json').read_text())


def auth_failure(_source):
    raise AuthenticationError('rejected')


class HealthTests(unittest.TestCase):
    def test_http_401_403_are_authentication_failures_without_server_reason(self):
        for code in (401, 403):
            with self.subTest(code=code), patch('gpushare_monitor.build_opener') as opener:
                opener.return_value.open.side_effect = HTTPError(
                    'https://api.gpushare.com/', code, 'private-server-detail', {}, None)
                with self.assertRaises(AuthenticationError) as error:
                    fetch_one_json(config()['source'])
                self.assertNotIn('private-server-detail', str(error.exception))

    def test_business_401_403_fail_before_reading_inventory(self):
        for code in (401, 403, '401', '403'):
            with self.subTest(code=code), self.assertRaises(AuthenticationError):
                validate_market_page({'status': code})
        with self.assertRaises(ProbeError) as error:
            validate_market_page({'status': 500})
        self.assertNotIsInstance(error.exception, AuthenticationError)

    def test_first_auth_failure_alerts_immediately_and_rearms_after_empty_success(self):
        cfg = config()
        messages = []
        notify = lambda title, body: messages.append((title, body))
        state, message = cycle(cfg, {}, probe=auth_failure)
        self.assertEqual(state['errors'], 1)
        self.assertTrue(message.startswith('AUTH_FAILED'))
        self.assertNotIn('free_gpus', state)
        health_mail_if_needed(state, notify=notify, now=100000)
        self.assertEqual(len(messages), 1)
        self.assertIn('Token', messages[0][0])
        state, _ = cycle(cfg, state, probe=auth_failure)
        health_mail_if_needed(state, notify=notify, now=100001)
        self.assertEqual(len(messages), 1)
        health_mail_if_needed(state, notify=notify, now=121600)
        self.assertEqual(len(messages), 2)
        state, _ = cycle(cfg, state, probe=lambda _: {
            'status': 0, 'data': {'list': [], 'total': 0}})
        self.assertFalse(state['auth_failure_alerted'])
        state, _ = cycle(cfg, state, probe=auth_failure)
        health_mail_if_needed(state, notify=notify, now=121601)
        self.assertEqual(len(messages), 3)

    def test_failed_auth_mail_is_retried_and_network_failure_does_not_rearm(self):
        state, _ = cycle(config(), {}, probe=auth_failure)
        with self.assertRaises(OSError):
            health_mail_if_needed(state, notify=lambda *a: (_ for _ in ()).throw(OSError()), now=100000)
        self.assertFalse(state.get('auth_failure_alerted', False))
        messages = []
        health_mail_if_needed(state, notify=lambda *a: messages.append(a), now=100001)
        def network_failure(_):
            raise ProbeError('network unavailable')
        state, _ = cycle(config(), state, probe=network_failure)
        self.assertTrue(state['auth_failure_alerted'])
        state, _ = cycle(config(), state, probe=auth_failure)
        health_mail_if_needed(state, notify=lambda *a: messages.append(a), now=100002)
        self.assertEqual(len(messages), 1)

    def test_other_errors_keep_delayed_health_alert(self):
        messages = []
        health_mail_if_needed({'errors': 4, 'last_status': 'unknown'}, notify=lambda *a: messages.append(a), now=100000)
        self.assertEqual(messages, [])
        health_mail_if_needed({'errors': 5, 'last_status': 'unknown'}, notify=lambda *a: messages.append(a), now=100000)
        self.assertEqual(len(messages), 1)

    def test_once_reports_authentication_failure_as_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'config.json'
            path.write_text(json.dumps(config()))
            with patch('gpushare_monitor.cycle', return_value=(
                    {'last_status': 'auth_failed', 'errors': 1}, 'AUTH_FAILED')):
                with patch('gpushare_monitor.health_mail_if_needed', side_effect=lambda state: state):
                    code = main(['once', '--config', str(path), '--state', str(Path(folder) / 'state.json')])
            self.assertEqual(code, 1)

    def test_30_second_configuration_and_processing_time_compensation(self):
        cfg = config()
        cfg['poll_seconds'] = 30
        validate(cfg)
        for jitter, expected in ((0, 26), (10, 36)):
            with patch('gpushare_monitor.random.uniform', return_value=jitter):
                self.assertEqual(poll_delay(cfg, {}, elapsed=4), expected)
        with patch('gpushare_monitor.random.uniform', return_value=0):
            self.assertEqual(poll_delay(cfg, {}, elapsed=50), 1)
            self.assertEqual(poll_delay(cfg, {'last_status': 'auth_failed', 'errors': 1}, elapsed=4), 56)
            self.assertEqual(poll_delay(cfg, {'last_status': 'unknown', 'errors': 10}, elapsed=4), 896)
        cfg['poll_seconds'] = 29
        with self.assertRaises(ValueError):
            validate(cfg)


if __name__ == '__main__':
    unittest.main()

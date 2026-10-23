import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from gpushare_monitor import ProbeError, cycle, evaluate, load_state, save_state, validate, env_headers, inspect_json, NoRedirect


BASE = {
    'target': {'machine_id': 'host-2', 'gpu_model': 'RTX 4090', 'gpu_count': 2, 'console_url': 'https://gpushare.com/'},
    'poll_seconds': 60,
    'source': {'confirmed_read_only': True, 'url': 'https://api.gpushare.com/app/api/market/matching/machine',
               'records_path': 'data.machines', 'id_path': 'id', 'model_path': 'model', 'free_path': 'free'}
}


def result(free):
    return {'data': {'machines': [
        {'id': 'host-1', 'model': 'RTX 4090', 'free': 200},
        {'id': 'host-2', 'model': 'RTX 4090', 'free': free}]}}


class MonitorTests(unittest.TestCase):
    def test_validates_source_and_machine(self):
        cfg = copy.deepcopy(BASE)
        validate(cfg)
        cfg['source']['url'] = 'https://api.gpushare.com/app/api/instance/start'
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg['source']['url'] = 'http://evil.example/data'
        with self.assertRaises(ValueError):
            validate(cfg)
        cfg['source']['confirmed_read_only'] = False
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_exact_id_and_count(self):
        self.assertEqual(evaluate(result(1), BASE), (False, 1))
        self.assertEqual(evaluate(result(2), BASE), (True, 2))

    def test_missing_machine_is_unknown(self):
        data = {'data': {'machines': [{'id': 'another', 'free': 1}]}}
        with self.assertRaises(ProbeError):
            evaluate(data, BASE)

    def test_invalid_free_not_available(self):
        for bad in (True, '2', -1, None):
            with self.subTest(bad=bad), self.assertRaises(ProbeError):
                evaluate(result(bad), BASE)

    def test_missing_path_unknown(self):
        with self.assertRaises(ProbeError):
            evaluate({'data': {}}, BASE)

    def test_model_mismatch_unknown(self):
        data = result(3)
        data['data']['machines'][1]['model'] = 'RTX 3090'
        with self.assertRaises(ProbeError):
            evaluate(data, BASE)

    def test_dedup_rearm_and_unknown(self):
        messages = []
        probe = lambda src: result(2)
        notify = lambda title, body: messages.append(title)
        s, msg = cycle(BASE, {}, probe=probe, notify=notify)
        self.assertEqual(len(messages), 1)
        s, msg = cycle(BASE, s, probe=probe, notify=notify)
        self.assertEqual(len(messages), 1)
        s, msg = cycle(BASE, s, probe=lambda src: result(0), notify=notify)
        self.assertTrue(s['alerted'])
        s, msg = cycle(BASE, s, probe=lambda src: {'oops': 1}, notify=notify)
        self.assertTrue(s['alerted'])
        s, msg = cycle(BASE, s, probe=lambda src: result(0), notify=notify)
        s, msg = cycle(BASE, s, probe=lambda src: result(0), notify=notify)
        self.assertFalse(s['alerted'])
        s, msg = cycle(BASE, s, probe=probe, notify=notify)
        self.assertEqual(len(messages), 2)

    def test_mail_failure_does_not_suppress(self):
        with self.assertRaises(OSError):
            cycle(BASE, {}, probe=lambda src: result(2), notify=lambda *a: (_ for _ in ()).throw(OSError('mail')))
        messages = []
        s, _ = cycle(BASE, {}, probe=lambda src: result(2), notify=lambda *a: messages.append('ok'))
        self.assertEqual(messages, ['ok'])

    def test_file_roundtrip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/'nested'/'state.json'
            save_state(p, {'a': True})
            self.assertEqual(load_state(p), {'a': True})
            self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_screenshot_instance_target(self):
        cfg_path = Path(__file__).resolve().parent.parent/'config.my-instance.example.json'
        cfg = json.loads(cfg_path.read_text(encoding='utf-8'))
        self.assertEqual(cfg['target']['gpu_model'], '4080s-32G')
        self.assertEqual(cfg['target']['gpu_count'], 1)
        self.assertEqual(cfg['target']['machine_id'], 'MACHL7cYdJ32V4WRGwRGsbSN')
        self.assertFalse(cfg['source']['confirmed_read_only'])
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_offline_inspect_json_no_secrets(self):
        data = {'data': {'machines': [
            {'machineId': 'MACHL7cYdJ32V4WRGwRGsbSN', 'idleGpuCount': 1,
             'token': 'secret-never-print'}, {'machineId': 'another', 'idleGpuCount': 5}]}}
        msg = inspect_json(data, 'MACHL7cYdJ32V4WRGwRGsbSN')
        self.assertIn('idleGpuCount', msg)
        self.assertIn('machineId', msg)
        self.assertNotIn('secret-never-print', msg)
        self.assertIn('没有找到', inspect_json(data, 'not-found'))

    def test_no_cross_site_redirects(self):
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, 'Found', {}, 'https://evil.example'))

    def test_header_refs_only(self):
        with self.assertRaises(ProbeError):
            env_headers({'headers': {'Cookie': 'session=secret'}})
        with patch.dict(os.environ, {'GPUSHARE_READ_COOKIE': 'session=secret'}):
            headers = env_headers({'headers': {'Cookie': 'env:GPUSHARE_READ_COOKIE'}})
            self.assertEqual(headers['Cookie'], 'session=secret')


if __name__ == '__main__':
    unittest.main()

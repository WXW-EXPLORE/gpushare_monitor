"""Independent machine notifications, legacy migration and shared control."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from gpushare_monitor import (
    AuthenticationError, ProbeError, cycle, cycle_many, health_mail_if_needed,
    load_state, main, save_state, target_configs, target_signature, validate,
)
from update_token import KEY, update


def config():
    return json.loads((Path(__file__).resolve().parent.parent /
                       'config.two-machines.example.json').read_text())


def response(source, free=1):
    model = parse_qs(urlsplit(source['url']).query)['gpuName'][0]
    target = next(c['target'] for c in target_configs(config())
                  if c['target']['gpu_model'] == model)
    return {'status': 0, 'data': {'total': 1, 'list': [{
        'machineId': target['machine_id'], 'gpuName': model,
        'gpuNum': 8, 'gpuUsed': 8 - free, 'status': 1, 'isOnline': True,
        'regionName': 'test-region', 'skuList': [
            {'skuName': 'payg', 'status': 1, 'enable': 1}],
    }]}}


class MultiTargetTests(unittest.TestCase):
    def test_expansion_preserves_primary_query_and_uses_matching_gpu_filter(self):
        cfg = config()
        original = copy.deepcopy(cfg)
        validate(cfg)
        targets = target_configs(cfg)
        self.assertEqual(cfg, original)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0]['source'], cfg['source'])
        self.assertEqual(targets[0]['target'], cfg['target'])
        for item in targets:
            self.assertEqual(parse_qs(urlsplit(item['source']['url']).query)['gpuName'],
                             [item['target']['gpu_model']])
            self.assertEqual(item['source']['method'], 'GET')
            self.assertNotIn('additional_targets', item)

    def test_duplicate_and_invalid_additional_targets_fail_validation(self):
        cfg = config()
        cfg['additional_targets'] = [copy.deepcopy(cfg['target'])]
        with self.assertRaises(ValueError):
            validate(cfg)
        for extra in (None, {}, ['invalid-target'], [{'gpu_model': 'A6000-48G'}]):
            cfg = config()
            cfg['additional_targets'] = extra
            with self.subTest(extra=extra), self.assertRaises(ValueError):
                validate(cfg)
        cfg = config()
        cfg['source']['method'] = 'POST'
        with self.assertRaises(ValueError):
            validate(cfg)

    def test_independent_dedup_and_relisting_survive_disk_roundtrip(self):
        cfg = config()
        messages = []
        notify = lambda *args: messages.append(args)
        state, _ = cycle_many(cfg, {}, probe=response, notify=notify)
        self.assertEqual(len(messages), 2)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            save_state(path, state)
            state = load_state(path)
        state, _ = cycle_many(cfg, state, probe=response, notify=notify)
        self.assertEqual(len(messages), 2)
        def first_full(source):
            return response(source, 0 if '4090-48G' in source['url'] else 1)
        for _ in range(2):
            state, _ = cycle_many(cfg, state, probe=first_full, notify=notify)
        state, _ = cycle_many(cfg, state, probe=response, notify=notify)
        self.assertEqual(len(messages), 3)
        self.assertIn('4090-48G', messages[-1][0])
        self.assertEqual(state['free_gpus'], 2)

    def test_migration_does_not_renotify_original_machine_or_reset_mail_cursor(self):
        cfg = config()
        primary = target_configs(cfg)[0]
        old, _ = cycle(primary, {}, probe=response, notify=lambda *args: None)
        old.update(paused=False, imap_last_uid=41, imap_uidvalidity='1',
                   used_control_tokens=['old-command-hash'])
        sent = []
        state, _ = cycle_many(cfg, old, probe=response, notify=lambda *args: sent.append(args))
        self.assertEqual(len(sent), 1)
        self.assertIn('A6000-48G', sent[0][0])
        for key in ('paused', 'imap_last_uid', 'imap_uidvalidity', 'used_control_tokens'):
            self.assertEqual(state[key], old[key])
        self.assertTrue(state['target_states'][target_signature(primary)]['alerted'])
        self.assertNotIn('signature', state)

    def test_failed_query_does_not_skip_or_renotify_the_other_machine(self):
        def one_failure(source):
            if '4090-48G' in source['url']:
                raise ProbeError('temporary network failure')
            return response(source)
        sent = []
        state, _ = cycle_many(config(), {}, probe=one_failure,
                              notify=lambda *args: sent.append(args))
        self.assertEqual(state['last_status'], 'unknown')
        self.assertEqual(len(sent), 1)
        self.assertIn('A6000-48G', sent[0][0])
        state, _ = cycle_many(config(), state, probe=response,
                              notify=lambda *args: sent.append(args))
        self.assertEqual(len(sent), 2)
        self.assertIn('4090-48G', sent[-1][0])
        self.assertEqual(state['errors'], 0)

    def test_failed_mail_retries_only_that_machine(self):
        sent = []
        def first_mail_fails(subject, body):
            if '4090-48G' in subject:
                raise OSError('smtp failure')
            sent.append((subject, body))
        state, _ = cycle_many(config(), {}, probe=response, notify=first_mail_fails)
        self.assertEqual(state['last_status'], 'notification_failed')
        self.assertEqual(len(sent), 1)
        state, _ = cycle_many(config(), state, probe=response,
                              notify=lambda *args: sent.append(args))
        self.assertEqual(len(sent), 2)
        self.assertIn('4090-48G', sent[-1][0])

    def test_authentication_alert_is_shared_even_if_other_query_succeeds(self):
        def one_auth_failure(source):
            if '4090-48G' in source['url']:
                raise AuthenticationError('rejected')
            return response(source, 0)
        sent = []
        state = {}
        for now in (100000, 100001):
            state, _ = cycle_many(config(), state, probe=one_auth_failure,
                                  notify=lambda *args: self.fail('no inventory is available'))
            health_mail_if_needed(state, notify=lambda *args: sent.append(args), now=now)
        self.assertEqual(len(sent), 1)
        self.assertEqual(state['last_status'], 'auth_failed')
        state, _ = cycle_many(config(), state, probe=lambda s: response(s, 0))
        self.assertFalse(state['auth_failure_alerted'])
        state, _ = cycle_many(config(), state, probe=one_auth_failure)
        health_mail_if_needed(state, notify=lambda *args: sent.append(args), now=100002)
        self.assertEqual(len(sent), 2)

    def test_pause_prevents_all_inventory_queries(self):
        original = {'paused': True, 'imap_last_uid': 42,
                    'target_states': {'old': {'alerted': True}}}
        result, message = cycle_many(config(), original,
            probe=lambda *args: self.fail('paused inventory query'),
            notify=lambda *args: self.fail('paused notification'))
        self.assertTrue(message.startswith('PAUSED'))
        self.assertEqual(result, original)

    def test_check_command_checks_both_without_state_or_notifications(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg_path = Path(folder) / 'config.json'
            cfg_path.write_text(json.dumps(config()))
            state_path = Path(folder) / 'state.json'
            with patch('gpushare_monitor.fetch_json', side_effect=response) as probe:
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    code = main(['check', '--config', str(cfg_path), '--state', str(state_path)])
            self.assertEqual(code, 0)
            self.assertEqual(probe.call_count, 2)
            self.assertIn('4090-48G', output.getvalue())
            self.assertIn('A6000-48G', output.getvalue())
            self.assertFalse(state_path.exists())

    def test_token_update_verifies_both_before_replacing_local_value(self):
        with tempfile.TemporaryDirectory() as folder:
            env_path = Path(folder) / '.env'
            original = KEY + '="Bearer old-dummy"\n'
            env_path.write_text(original)
            cfg = config()
            cfg['source']['headers'] = {'Authorization': 'env:' + KEY}
            cfg_path = Path(folder) / 'config.json'
            cfg_path.write_text(json.dumps(cfg))
            def second_rejects(source):
                if 'A6000-48G' in source['url']:
                    raise AuthenticationError('rejected')
                return response(source)
            with patch.dict(os.environ, {KEY: 'Bearer existing-dummy'}):
                with patch('update_token.fetch_json', side_effect=second_rejects) as probe:
                    with self.assertRaises(AuthenticationError):
                        update(env_path, cfg_path, 'new-dummy')
                    self.assertEqual(probe.call_count, 2)
                self.assertEqual(env_path.read_text(), original)
                self.assertEqual(os.environ[KEY], 'Bearer existing-dummy')


if __name__ == '__main__':
    unittest.main()

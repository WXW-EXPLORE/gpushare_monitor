"""Offline tests: never connect to GPUShare, SMTP, or a real mailbox."""
import copy
from email.message import EmailMessage
from email_control import (TOKEN_TTL_SECONDS, MailControlError,
                           control_section, make_token, verify_token, mailto_link,
                           read_control_config, parse_control_message,
                           scan_mail_controls)
from gpushare_monitor import cycle
import os
from urllib.parse import parse_qs, urlparse
import unittest

ENV = {'IMAP_HOST': 'imap.example.test', 'IMAP_PORT': '993',
       'IMAP_USER': 'monitor@example.test', 'IMAP_PASSWORD': 'dummy-app-password',
       'CONTROL_AUTHORIZED_FROM': 'owner@example.test',
       'CONTROL_SECRET': 'x' * 48}


def config():
    return read_control_config(ENV)


def make_header(action, token, sender='owner@example.test', separator=' '):
    msg = EmailMessage()
    msg['From'] = sender
    msg['To'] = 'monitor@example.test'
    msg['Subject'] = separator.join(('GPUCTRL', action.upper(), token))
    msg.set_content('Send control')
    return msg.as_bytes().split(b'\n\n', 1)[0] + b'\n\n'


class FakeIMAP:
    validity = b'1001'
    def __init__(self, host, port, timeout):
        self.messages = {}
    def login(self, user, password):
        return 'OK', [b'logged in']
    def select(self, mailbox, readonly):
        assert readonly
        return 'OK', [b'2']
    def response(self, name):
        return 'UIDVALIDITY', [self.validity]
    def uid(self, command, _, criteria):
        if command == 'SEARCH':
            if criteria == 'ALL':
                keys = list(self.messages)
            else:
                floor = int(criteria.split()[1].split(':')[0])
                keys = [uid for uid in self.messages if uid >= floor]
            return 'OK', [b' '.join(str(x).encode('ascii') for x in sorted(keys))]
        if command == 'FETCH':
            uid = int(_)
            return 'OK', [(b'header', self.messages[uid])]
        raise AssertionError(command)
    def logout(self):
        return 'BYE', []


class ControlTests(unittest.TestCase):
    def test_default_disabled(self):
        self.assertIsNone(read_control_config({}))

    def test_missing_credentials_rejected(self):
        with self.assertRaises(MailControlError):
            read_control_config({'IMAP_HOST': 'example.test'})
        with self.assertRaises(MailControlError):
            read_control_config({**ENV, 'CONTROL_SECRET': 'too-short'})

    def test_token_and_expiry(self):
        sec = config()['secret']
        tok = make_token('pause', sec, now=100000)
        self.assertTrue(verify_token('pause', tok, sec, now=100000))
        self.assertFalse(verify_token('resume', tok, sec, now=100000))
        self.assertFalse(verify_token('pause', tok, sec, now=100000 + TOKEN_TTL_SECONDS + 1))
        self.assertFalse(verify_token('pause', tok[:-4] + 'abcd', sec, now=100000))

    def test_mailto_is_addressed_to_monitor_with_subject(self):
        url = mailto_link('pause', config(), now=100000)
        parsed = urlparse(url)
        self.assertEqual(parsed.scheme, 'mailto')
        self.assertEqual(parsed.path, 'monitor@example.test')
        self.assertIn('subject=GPUCTRL%20PAUSE%20', parsed.query)
        self.assertNotIn('+', parsed.query)
        subject = parse_qs(parsed.query)['subject'][0]
        self.assertTrue(subject.startswith('GPUCTRL PAUSE '))
        self.assertIn('请保持邮件主题', parse_qs(parsed.query)['body'][0])

    def test_parse_headers_sender_token_required(self):
        token = make_token('pause', config()['secret'], now=100000)
        self.assertEqual(parse_control_message(make_header('pause', token), config(), now=100000), 'pause')
        self.assertIsNone(parse_control_message(make_header('pause', token, 'stranger@example.test'), config(), now=100000))
        self.assertIsNone(parse_control_message(make_header('resume', token), config(), now=100000))
        self.assertIsNone(parse_control_message(make_header('pause', token), config(), now=100000 + TOKEN_TTL_SECONDS + 1))

    def test_controls_are_not_gpu_actions(self):
        self.assertIn('暂停监测', control_section('pause', config()))
        self.assertIn('恢复监测', control_section('resume', config()))

    def test_legacy_plus_subject_requires_valid_sender_signature_and_no_replay(self):
        cfg = config()
        token = make_token('pause', cfg['secret'], now=100000)
        header = make_header('pause', token, separator='+')
        self.assertEqual(parse_control_message(header, cfg, now=100000), 'pause')
        self.assertIsNone(parse_control_message(
            make_header('pause', token, sender='other@example.test', separator='+'), cfg, now=100000))
        self.assertIsNone(parse_control_message(
            make_header('resume', token, separator='+'), cfg, now=100000))
        fake = FakeIMAP('x', 993, 15)
        state = {'imap_uidvalidity': '1001', 'imap_last_uid': 0}
        fake.messages[1] = header
        state, actions = scan_mail_controls(state, cfg, client_factory=lambda *a, **k: fake, now=100000)
        self.assertEqual(actions, ['pause'])
        self.assertTrue(state['paused'])
        fake.messages[2] = header
        state, actions = scan_mail_controls(state, cfg, client_factory=lambda *a, **k: fake, now=100000)
        self.assertEqual(actions, [])

    def test_imap_first_poll_skips_old_messages(self):
        fake = FakeIMAP('x', 993, 15)
        fake.messages = {10: make_header('pause', make_token('pause', config()['secret'], now=100000))}
        st, actions = scan_mail_controls({}, config(), client_factory=lambda *args, **kw: fake, now=100000)
        self.assertEqual(actions, [])
        self.assertEqual(st['imap_last_uid'], 10)
        self.assertFalse(st.get('paused', False))

    def test_pause_resume_and_replay_guard(self):
        fake = FakeIMAP('x', 993, 15)
        client = lambda *args, **kw: fake
        st, actions = scan_mail_controls({}, config(), client_factory=client, now=100000)
        self.assertEqual((actions, st['imap_last_uid']), ([], 0))
        pause_token = make_token('pause', config()['secret'], now=100000)
        fake.messages[20] = make_header('pause', pause_token)
        st, actions = scan_mail_controls(st, config(), client_factory=client, now=100000)
        self.assertEqual(actions, ['pause'])
        self.assertTrue(st['paused'])
        resume_token = make_token('resume', config()['secret'], now=100000)
        fake.messages[21] = make_header('resume', resume_token)
        st, actions = scan_mail_controls(st, config(), client_factory=client, now=100000)
        self.assertEqual(actions, ['resume'])
        self.assertFalse(st['paused'])
        self.assertFalse(st['alerted'])
        fake.messages[22] = make_header('pause', pause_token)
        st, actions = scan_mail_controls(st, config(), client_factory=client, now=100000)
        self.assertEqual(actions, [])
        self.assertFalse(st['paused'])
        self.assertEqual(st['imap_last_uid'], 22)

    def test_spoofed_sender_ignored(self):
        fake = FakeIMAP('x', 993, 15)
        st, _ = scan_mail_controls({}, config(), client_factory=lambda *a, **k:fake, now=100000)
        fake.messages[20] = make_header('pause', make_token('pause', config()['secret'], now=100000), sender='notowner@example.test')
        st, actions = scan_mail_controls(st, config(), client_factory=lambda *a, **k:fake, now=100000)
        self.assertEqual(actions, [])
        self.assertFalse(st.get('paused', False))

    def test_resume_rearms_every_machine_without_mutating_previous_state(self):
        fake = FakeIMAP('x', 993, 15)
        original = {'paused': True, 'imap_uidvalidity': '1001', 'imap_last_uid': 0,
                    'target_states': {'first': {'alerted': True}, 'second': {'alerted': True}}}
        fake.messages[1] = make_header('resume', make_token('resume', config()['secret'], now=100000))
        result, actions = scan_mail_controls(original, config(),
            client_factory=lambda *a, **k: fake, now=100000)
        self.assertEqual(actions, ['resume'])
        self.assertFalse(result['paused'])
        self.assertTrue(all(not value['alerted'] for value in result['target_states'].values()))
        self.assertTrue(original['target_states']['first']['alerted'])

    def test_uid_validity_reset_skips_old_commands(self):
        fake = FakeIMAP('x', 993, 15)
        st, _ = scan_mail_controls({}, config(), client_factory=lambda *a, **k:fake, now=100000)
        fake.messages[20] = make_header('pause', make_token('pause', config()['secret'], now=100000))
        fake.validity = b'1002'
        st, actions = scan_mail_controls(st, config(), client_factory=lambda *a, **k:fake, now=100000)
        self.assertEqual(actions, [])
        self.assertEqual(st['imap_last_uid'], 20)

    def test_alert_cycle_keeps_mail_cursor(self):
        cfg = {'target': {'gpu_model': '4080s-32G', 'machine_id': 'host-A', 'gpu_count': 1},
               'source': {'url': 'https://api.gpushare.com/sample', 'records_path': 'data.list',
                          'id_path': 'id', 'free_path': 'free'}}
        state = {'imap_uidvalidity': '1001', 'imap_last_uid': 123, 'paused': False,
                 'used_control_tokens': ['hashed-token']}
        probe = lambda _src: {'data': {'list': [{'id': 'host-A', 'free': 0}]}}
        result, _ = cycle(cfg, state, probe=probe)
        self.assertEqual(result['imap_last_uid'], 123)
        self.assertEqual(result['used_control_tokens'], ['hashed-token'])
        self.assertFalse(result['paused'])


if __name__ == '__main__':
    unittest.main()

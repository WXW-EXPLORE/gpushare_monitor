"""Use dummy credentials and temporary files; never read the real .env."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from gpushare_monitor import AuthenticationError
from update_token import KEY, normalize_token, replace_authorization, update, write_env


class TokenUpdateTests(unittest.TestCase):
    def test_accepts_browser_header_or_token_and_rejects_multiline_shell_values(self):
        for value in ('dummy-token', 'Bearer dummy-token', 'Authorization: Bearer dummy-token'):
            self.assertEqual(normalize_token(value), 'Bearer dummy-token')
        for value in ('', 'abc\nSMTP_PASSWORD=changed', '$(bad)', 'a"b', 'a b'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_token(value)

    def test_replaces_duplicate_active_assignments_and_preserves_mail_configuration(self):
        text = ('# token comment\nSMTP_PASSWORD="dummy mail value"\n'
                f'{KEY}="Bearer old"\nexport {KEY}=another-old\nIMAP_PORT=993\n')
        result = replace_authorization(text, 'new-dummy-token')
        self.assertEqual(result.count(KEY + '='), 1)
        self.assertNotIn('another-old', result)
        self.assertIn('SMTP_PASSWORD="dummy mail value"\n', result)
        self.assertTrue(result.endswith('IMAP_PORT=993\n'))
        self.assertEqual(replace_authorization('IMAP_PORT=993', 'dummy'),
                         'IMAP_PORT=993\n' + KEY + '="Bearer dummy"\n')

    def test_atomic_update_is_private_and_cleans_temporary_files(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / '.env'
            path.write_text(f'{KEY}="Bearer old"\nSMTP_HOST=unchanged\n')
            path.chmod(0o644)
            write_env(path, 'dummy')
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn('SMTP_HOST=unchanged', path.read_text())
            self.assertEqual(list(Path(folder).glob('.env-update-*')), [])

    def test_invalid_token_leaves_file_and_process_environment_untouched(self):
        with tempfile.TemporaryDirectory() as folder:
            env_path = Path(folder) / '.env'
            original = f'{KEY}="Bearer old"\nSMTP_HOST=unchanged\n'
            env_path.write_text(original)
            config_path = Path(folder) / 'config.json'
            cfg = json.loads((Path(__file__).resolve().parent.parent /
                              'config.my-instance.example.json').read_text())
            cfg['source']['headers'] = {'Authorization': 'env:' + KEY}
            config_path.write_text(json.dumps(cfg))
            with patch.dict(os.environ, {KEY: 'Bearer existing'}):
                with patch('update_token.fetch_json', side_effect=AuthenticationError()):
                    with self.assertRaises(AuthenticationError):
                        update(env_path, config_path, 'new-dummy')
                self.assertEqual(env_path.read_text(), original)
                self.assertEqual(os.environ[KEY], 'Bearer existing')
                def checked_fetch(_source):
                    self.assertEqual(os.environ[KEY], 'Bearer new-dummy')
                    return {'status': 0, 'data': {'list': [], 'total': 0}}
                with patch('update_token.fetch_json', side_effect=checked_fetch):
                    update(env_path, config_path, 'new-dummy')
                self.assertIn('Bearer new-dummy', env_path.read_text())
                self.assertEqual(os.environ[KEY], 'Bearer existing')


if __name__ == '__main__':
    unittest.main()

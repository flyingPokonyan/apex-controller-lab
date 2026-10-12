"""The shipped runtime must work without a Forge checkout or machine secrets."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

WINDOWS = Path(__file__).resolve().parents[2] / 'windows'
sys.path.insert(0, str(WINDOWS))
from apex_registration.config import config_path, load_config, environment
from apex_registration.setup import save_config


class RegistrationConfigTests(unittest.TestCase):
    def test_missing_config_has_no_signup_proxy_or_embedded_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            data = load_config(Path(directory) / 'registration.private.json')
        self.assertEqual(data['bitbrowserUrl'], 'http://127.0.0.1:54345')
        self.assertEqual([data[k] for k in ('bitbrowserApiKey', 'proxyUrl', 'steamCaptchaKey')], ['', '', ''])
        self.assertEqual(environment(data)['FORGE_PROXY'], 'direct')

    def test_identity_replacement_preserves_registration_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            identity = Path(directory) / 'account-cycle.private.json'
            identity.write_text('{"providerToken":"old"}')
            path = config_path(identity)
            save_config(path, dict(proxyUrl='socks5://fixture:secret@proxy.example:1080', bitbrowserApiKey='fixture-key'))
            before = path.read_bytes()
            identity.write_text('{"providerToken":"new"}')
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(environment(load_config(path))['GAME_ACCOUNTS_BIT_API_KEY'], 'fixture-key')

    def test_invalid_configuration_reports_field_without_secret(self):
        cases = [('proxyUrl', 'fixture-secret'), ('bitbrowserUrl', 'fixture-secret'),
                 ('forgeProxy', 'fixture-secret'), ('timeoutSeconds', -1)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'config.json'
            for key, value in cases:
                with self.subTest(key=key):
                    path.write_text(json.dumps({key:value}))
                    with self.assertRaises(ValueError) as raised:
                        load_config(path)
                    self.assertIn(key, str(raised.exception))
                    self.assertNotIn('fixture-secret', str(raised.exception))

    def test_atomic_save_failure_keeps_previous_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'registration.private.json'
            save_config(path, {'proxyUrl':'http://old.example:8080'})
            before = path.read_bytes()
            with patch('apex_registration.setup.os.replace', side_effect=OSError('locked')):
                with self.assertRaises(OSError):
                    save_config(path, {'proxyUrl':'http://new.example:8080'})
            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_runtime_imports_in_fresh_process_without_forge_on_path(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, '-m', 'apex_registration', '--check-runtime'],
                cwd=directory, env=dict(os.environ, PYTHONPATH=str(WINDOWS)),
                text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('ready', result.stdout.lower())


if __name__ == '__main__':
    unittest.main()

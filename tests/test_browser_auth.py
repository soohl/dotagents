"""Check local credential management and the browser exposure boundary."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from cryptography.exceptions import InvalidKey
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
import browser_auth


class BrowserAuthTests(unittest.TestCase):
    def test_secret_updates_preserve_other_values_and_restrict_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / '.env').write_text('# local settings\nOTHER="keep $this literal"\nSECRET=old\n')
            browser_auth.save_settings(root, {'SECRET': 'new', 'ADDED': 'value'})
            self.assertEqual(browser_auth.settings(root),
                             {'OTHER': 'keep $this literal', 'SECRET': 'new', 'ADDED': 'value'})
            self.assertEqual((root / '.env').stat().st_mode & 0o777, 0o600)

    def test_credentials_require_a_local_terminal(self):
        with patch.object(browser_auth.sys.stdin, 'isatty', return_value=False), \
                patch.object(browser_auth, 'read_users') as read:
            for action in (browser_auth.password, browser_auth.notification):
                with self.assertRaisesRegex(ValueError, 'terminal'):
                    action(ROOT)
            read.assert_not_called()

    def test_password_hash_accepts_only_the_correct_password(self):
        digest = browser_auth.password_hash('disposable test password')
        Argon2id.verify_phc_encoded(b'disposable test password', digest)
        with self.assertRaises(InvalidKey):
            Argon2id.verify_phc_encoded(b'wrong password', digest)

    def test_enrollment_code_matches_recipient_and_expiry(self):
        now = datetime.now(timezone.utc)
        def message(time, recipient='owner@example.invalid'):
            return f'Date: {time:%Y-%m-%d %H:%M:%S}.123 +0000 UTC\nRecipient: {{Owner {recipient}}}\nSubject: Verify\n' + '-' * 40 + '\n\nABCD1234\n\n' + '-' * 40
        self.assertEqual(browser_auth.verification_code(message(now), 'owner@example.invalid'), 'ABCD1234')
        for body in (message(now, 'other@example.invalid'), message(now-timedelta(minutes=6)), '',
                     message(now).replace('ABCD1234', 'bad')):
            with self.assertRaises(ValueError):
                browser_auth.verification_code(body, 'owner@example.invalid')

    def test_tunnel_uses_authelia_without_publishing_backends(self):
        environment = dict(os.environ, FUNNEL_HOSTNAME='test.example.ts.net',
                           **{key: 'test' for key in browser_auth.SECRET_KEYS})
        result = subprocess.run(['docker', 'compose', '--env-file', '/dev/null',
                                 '-f', str(ROOT / 'config/compose.harness.yaml'),
                                 '-f', str(ROOT / 'config/compose.harness-remote.yaml'),
                                 'config', '--format', 'json'], env=environment,
                                capture_output=True, text=True, check=True)
        services = json.loads(result.stdout)['services']
        for name in ('pocket-id', 'passkey-proxy', 'keycloak', 'identity-db', 'auth-proxy'):
            self.assertNotIn(name, services)
        self.assertNotIn('remote-edge', services)
        for name in ('authelia', 'caddy'):
            self.assertFalse(services[name].get('ports'))
        for name in ('chat', 'agent'):
            self.assertTrue(all(p['host_ip'] == '127.0.0.1' for p in services[name]['ports']))
        self.assertEqual(services['caddy']['network_mode'], 'service:tailscale')
        self.assertEqual([(p['host_ip'], p['published'], p['target']) for p in services['tailscale']['ports']],
                         [('127.0.0.1', '3000', 8080)])
        self.assertTrue(services['authelia']['read_only'])
        self.assertEqual(services['authelia']['user'], '1000:1000')
        for name, service in services.items():
            if name != 'authelia':
                self.assertFalse(set(browser_auth.SECRET_KEYS) & service.get('environment', {}).keys())


if __name__ == '__main__':
    unittest.main()

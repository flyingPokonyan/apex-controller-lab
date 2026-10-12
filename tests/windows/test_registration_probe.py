import unittest
from unittest.mock import Mock, patch
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_registration.utils import proxy_probe


class ProxyProbeTests(unittest.TestCase):
    def test_socks_uses_remote_dns_and_ignores_system_proxy(self):
        session = Mock()
        session.get.return_value.text = '{"country":"US"}'
        with patch.object(proxy_probe.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            self.assertEqual(proxy_probe.probe_exit_country('socks5://user:secret@proxy.example:3000'), ('US', None))
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies['https'], 'socks5h://user:secret@proxy.example:3000')

    def test_errors_keep_endpoint_and_type_but_never_credentials(self):
        session = Mock()
        session.get.side_effect = proxy_probe.requests.exceptions.ProxyError('socks5://user:secret@proxy.example:3000')
        with patch.object(proxy_probe.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            country, error = proxy_probe.probe_exit_country('socks5://user:secret@proxy.example:3000')
        self.assertIsNone(country)
        self.assertIn('ipinfo.io: ProxyError', error)
        self.assertIn('ip-api.com: ProxyError', error)
        self.assertNotIn('secret', error)
        self.assertNotIn('user', error)

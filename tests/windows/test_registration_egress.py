import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.parse import unquote, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_registration import cli
from apex_registration.utils.proxy import ProxyExit
from apex_registration.utils import proxy_probe
from apex_registration.utils.egress import exit_fields, public_exit
from apex_registration.flows.steam import try_steam_once as steam


class RegistrationEgressTests(unittest.TestCase):
    def test_creation_exit_survives_a_new_country_on_resume_and_supervisor_upload(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(cli.os.environ, {
                'EA_PROXY_VENDOR': '711', 'APEX_REGISTRATION_ATTEMPT': '1'}, clear=True):
            output = Path(directory)
            cli.remember_exit(output, ProxyExit(country='HK', ip='192.0.2.1'))
            cli.remember_creation_exit(output, {'ea_registered': False})
            self.assertNotIn('creationExit', exit_fields(output))
            cli.remember_creation_exit(output, {'ea_creation_submitted': True})
            cli.os.environ.update(APEX_REGISTRATION_ATTEMPT='2', SKIP_EA_REGISTER='1')
            cli.remember_exit(output, ProxyExit(country='US', ip='192.0.2.2'))
            cli.remember_creation_exit(output, {'ea_registered': True})
            facts = exit_fields(output)
            self.assertEqual(facts['creationExit']['country'], 'HK')
            self.assertEqual(facts['creationExit']['ip'], '192.0.2.1')
            self.assertEqual(facts['exit']['country'], 'US')
            (output / 'ea_creation_exit.json').unlink()
            cli.remember_creation_exit(output, {'ea_registered': True})
            self.assertNotIn('creationExit', exit_fields(output))

    def test_selected_exit_and_retry_vendor_are_saved_without_proxy_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(cli.os.environ, {
                'EA_PROXY_VENDOR': '1024', 'APEX_REGISTRATION_ATTEMPT': '1', 'EA_REGISTER_REGION': 'US'}, clear=True):
            output = Path(directory)
            with patch.object(proxy_probe, 'probe_exit', side_effect=[
                    (ProxyExit(country='JP', ip='192.0.2.1'), None),
                    (ProxyExit(country='US', ip='192.0.2.2', asn=1234, org='Example ISP'), None)]):
                cli.select_sticky_proxy(output, 'http://user:secret@us.1024proxy.io:3000', 'fixture')
            cli.os.environ.update(EA_PROXY_VENDOR='711', APEX_REGISTRATION_ATTEMPT='2')
            with patch.object(proxy_probe, 'probe_exit', return_value=(ProxyExit(country='US', ip='2001:db8::2'), None)):
                cli.select_sticky_proxy(output, 'http://user:secret@proxy.trustsource.live:10000', 'fixture')
            facts = exit_fields(output)
            self.assertEqual([(x['attempt'], x['provider'], x['ip']) for x in facts['exits']],
                [(1, '1024', '192.0.2.2'), (2, '711', '2001:db8::2')])
            self.assertNotIn('secret', json.dumps(facts))
            self.assertNotIn('192.0.2.1', json.dumps(facts))
            # A failed new round cannot reuse the last round's IP.
            cli.os.environ.update(EA_PROXY_VENDOR='1024', APEX_REGISTRATION_ATTEMPT='3')
            with patch.object(proxy_probe, 'probe_exit', return_value=(None, 'ConnectTimeout')):
                with self.assertRaises(RuntimeError):
                    cli.select_sticky_proxy(output, 'http://user:secret@proxy.example', 'fixture')
            self.assertEqual(exit_fields(output)['exit']['provider'], '1024')
            self.assertEqual(exit_fields(output)['exit']['ip'], '')
            self.assertEqual(len(exit_fields(output)['exits']), 3)

    def test_incognito_never_reports_an_unused_proxy_ip(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(cli.os.environ, {
                'GAME_ACCOUNTS_DESKTOP_BROWSER': 'incognito', 'EA_PROXY_VENDOR': '711'}, clear=True):
            cli.remember_exit(Path(directory), ProxyExit(country='US', ip='192.0.2.2'))
            fact = exit_fields(directory)['exit']
            self.assertEqual((fact['provider'], fact['ip'], fact['country']), ('direct', '', ''))

    def test_sync_uses_saved_facts_even_with_a_different_machine_vendor(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(cli.os.environ, {
                'FORGE_TOKEN': 'fixture', 'FORGE_API_URL': 'https://forge.example', 'EA_PROXY_VENDOR': '1024'}, clear=True):
            output = Path(directory)
            cli.write_private_json(output / 'windows_trial.json', {'task_id': 'owned-task'})
            cli.write_private_json(output / 'proxy_exit.json', {'provider': '711', 'ip': '192.0.2.2',
                'attempt': 2, 'proxyUrl': 'http://user:secret@proxy.example'})
            response = Mock()
            response.__enter__ = Mock(return_value=response)
            response.__exit__ = Mock(return_value=False)
            response.read.return_value = b'{"task_id":"owned-task","state":"running"}'
            with patch.object(cli.urllib.request, 'urlopen', return_value=response) as send:
                cli.sync_result(output, finished=False)
            payload = json.loads(send.call_args.args[0].data)
            self.assertEqual(payload['exit']['provider'], '711')
            self.assertNotIn('secret', json.dumps(payload))

    def test_public_exit_rejects_credentials_and_invalid_ip(self):
        fact = public_exit({'provider': '711', 'ip': 'http://u:p@host', 'country': 'usa', 'asn': True,
                            'org': 'token=private', 'proxyUrl': 'secret'})
        self.assertEqual((fact['ip'], fact['country'], fact['asn'], fact['org']), ('', '', None, ''))

    def test_probe_keeps_geo_and_fallback_remote_dns(self):
        session = Mock()
        session.get.side_effect = [proxy_probe.requests.exceptions.ConnectTimeout('secret'), Mock(text=
            '{"countryCode":"US","query":"192.0.2.2","as":"AS1234 ISP","org":"ISP"}')]
        with patch.object(proxy_probe.requests, 'Session') as factory:
            factory.return_value.__enter__.return_value = session
            geo, error = proxy_probe.probe_exit('socks5://user:secret@proxy.example')
        self.assertIsNone(error)
        self.assertEqual((geo.ip, geo.asn, geo.org), ('192.0.2.2', 1234, 'ISP'))
        self.assertTrue(session.proxies['https'].startswith('socks5h:'))

    def test_steam_mail_uses_explicit_proxy_and_direct_mode_ignores_system_proxy(self):
        with patch.dict(cli.os.environ, {'STEAM_PROXY': 'socks5://fixture:secret@proxy.example:3000',
                'HTTPS_PROXY': 'http://ambient.example', 'NO_PROXY': 'graph.microsoft.com'}, clear=True):
            with steam.graph_client() as client:
                self.assertFalse(client.trust_env)
                self.assertEqual(client.proxies['https'], 'socks5h://fixture:secret@proxy.example:3000')
            cli.os.environ['ACCOUNT_FLOW_DIRECT'] = '1'
            with steam.graph_client() as client:
                self.assertFalse(client.trust_env)
                self.assertEqual(client.proxies, {})

    def test_both_steam_mail_calls_use_the_assigned_proxy_client(self):
        with patch.object(steam, 'graph_client') as factory:
            factory.return_value.post.return_value.json.return_value = {'access_token': 'access'}
            factory.return_value.get.return_value.status_code = 200
            factory.return_value.get.return_value.json.return_value = {'value': []}
            self.assertEqual(steam.graph_access_token({'client_id': 'client', 'refresh_token': 'refresh'}), 'access')
            self.assertIsNone(steam.graph_find_steam_link('access', after_ts=0))
            self.assertEqual(factory.call_count, 2)

    def test_steam_exit_rotation_keeps_the_task_country(self):
        for raw, country in (
                ('http://login-region-US-sid-abc12345-t-60:pw@us.1024proxy.io:3000', 'US'),
                ('http://acct-zone-custom-region-JP-session-12345678-sessTime-60:pw@global.rotgb.trustsource.live:6000', 'JP')):
            rotated = steam._rotate_sticky_proxy(raw, 'another-seed')
            user = unquote(urlparse(rotated).username)
            self.assertNotEqual(rotated, raw)
            self.assertIn(f'-region-{country}', user)
            self.assertNotIn('-region-HK', user)

    def test_three_captcha_rejections_move_to_one_new_exit_in_the_same_country(self):
        raw = 'http://login-region-US-sid-abc12345-t-60:pw@us.1024proxy.io:3000'
        session = Mock()
        session.cookies.get_dict.return_value = {}

        def post(url, **_):
            response = Mock()
            response.json.return_value = ({'sitekey': 'key', 'gid': '1'} if 'refreshcaptcha' in url
                                          else {'success': 101})
            return response

        session.post.side_effect = post
        joins = []

        def join(proxy_url, email):
            joins.append(proxy_url)
            return session, Mock(content=b'<input id="init_id" value="init-1">'), steam.parse_proxy(proxy_url)

        with tempfile.TemporaryDirectory() as tmp, \
                patch.dict(steam.os.environ, {'EA_PROXY': raw, 'STEAM_PROXY': raw}, clear=True), \
                patch.multiple(steam, ARTIFACT_ROOT=Path(tmp), OUT_LOG=Path(tmp) / 'log', OUT_ERR=Path(tmp) / 'err',
                               OUT_RESULT=Path(tmp) / 'result.json', PROXY_FILE=Path(tmp) / 'proxy',
                               load_captcha_provider=Mock(return_value='captchaly'),
                               load_captchaly_key=Mock(return_value='key'),
                               load_email_line=Mock(return_value={'email': 'fixture@example.com'}),
                               load_mailbox_pool=lambda primary: [primary], open_steam_join=join,
                               captchaly_bind_proxy=Mock(return_value=None),
                               solve_hcaptcha_enterprise=Mock(return_value=SimpleNamespace(
                                   token='token', deducted=1, duration=1))):
            self.assertEqual(steam.main(), 2)
            result = json.loads((Path(tmp) / 'result.json').read_text())
            rotated = steam.os.environ['EA_PROXY']
        self.assertEqual((result['error_code'], result['exit_rotations']), ('steam_captcha_rejected', 1))
        self.assertEqual(len(joins), 2)
        verify_calls = [c for c in session.post.call_args_list if 'ajaxverifyemail' in c.args[0]]
        self.assertEqual(len(verify_calls), 6)
        self.assertNotEqual(rotated, raw)
        self.assertIn('-region-US', unquote(urlparse(rotated).username))
        self.assertEqual(steam.os.environ.get('STEAM_PROXY'), None)  # patch.dict restored the environment

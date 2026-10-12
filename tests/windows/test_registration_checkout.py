import sys
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_registration import cli as windows


class WindowsCheckoutTests(unittest.TestCase):
    def test_managed_checkout_uses_runner_task_and_never_allocates_another(self):
        from unittest.mock import Mock
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = json.dumps(self.bundle()).encode()
        with patch.dict(windows.os.environ, {'FORGE_RUNNER_TASK_ID': 'owned-task'}), \
                patch.object(windows.urllib.request, 'urlopen', return_value=response) as open_url:
            windows.checkout_bundle('https://forge.example', 'runner-token', 'unused')
        req = open_url.call_args.args[0]
        self.assertEqual(req.full_url, 'https://forge.example/v1/runner/registration-tasks/owned-task')
        self.assertEqual(req.method, 'GET')
        self.assertIsNone(req.data)

    def bundle(self):
        return {"task_id":"task-1","hold_task_id":"task-1","steam_hold_task_id":None,
                "ea_mailbox":{"id":7,"email":"ea@example.com","client_id":"client","refresh_token":"refresh"},
                "steam_mailbox":{"id":8,"email":"steam@example.com","client_id":"client","refresh_token":"refresh"},
                "steam":{"source":"inventory","id":9,"username":"SteamPerson","password":"Steam-password","email":"steam@example.com"}}

    def test_checkout_timeout_keeps_request_id_before_response_is_saved(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.dict(windows.os.environ,{"FORGE_API_URL":"https://forge.example","FORGE_TOKEN":"token"}):
                with patch.object(windows,"checkout_bundle",side_effect=[RuntimeError("response lost"),self.bundle()]) as checkout:
                    with self.assertRaises(RuntimeError):
                        windows.prepare_bundle(output)
                    request_id = json.loads((output / "forge_request.json").read_text())["request_id"]
                    self.assertEqual(windows.prepare_bundle(output),"inventory")
                    self.assertEqual([call.args[2] for call in checkout.call_args_list],[request_id,request_id])

    def test_sync_only_never_starts_registration_and_preserves_pending_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            windows.write_private_json(output/"windows_trial.json",self.bundle())
            windows.write_private_json(output/"ea_full_setup_result.json",{"ea_email":"ea@example.com","stage":"complete",
                "ea_registered":True,"account_login_verified":True,"account_profile_completed":True,"steam_link":True,"totp":True,"totp_secret":"seed"})
            with patch.dict(windows.os.environ,{"FORGE_TOKEN":"token","FORGE_API_URL":"https://forge.example"}):
                with patch.object(windows,"prepare_bundle",side_effect=AssertionError("must not checkout")), patch.object(windows,"_refuse_unless_windows",side_effect=AssertionError("must not start browser")):
                    with patch.object(windows.urllib.request,"urlopen",side_effect=urllib.error.URLError("offline")):
                        self.assertEqual(windows.main(["--sync-only","--output-dir",directory]),3)
            self.assertTrue((output/"forge_pending_result.json").exists())
            self.assertTrue((output/"ea_full_setup_result.json").exists())

    def test_registration_creation_record_is_not_silently_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            windows.write_private_json(Path(directory)/"ea_full_setup_result.json",{"ea_creation_submitted":True,"account_login_verified":False})
            with patch.object(windows,"prepare_bundle",side_effect=AssertionError("must not take another email")):
                self.assertEqual(windows.main(["--output-dir",directory]),2)

    def test_register_bundle_writes_both_mailboxes_and_no_steam_file(self):
        payload = {
            "hold_task_id": "hold-ea",
            "steam_hold_task_id": "hold-steam",
            "ea_mailbox": {
                "id": 7,
                "email": "ea@example.com",
                "password": "",
                "client_id": "client",
                "refresh_token": "ea-refresh",
                "mail_type": "outlook",
            },
            "steam_mailbox": {
                "id": 8,
                "email": "steam@example.com",
                "password": "",
                "client_id": "client",
                "refresh_token": "steam-refresh",
                "mail_type": "outlook",
            },
            "steam": {"source": "register", "id": None, "username": None, "email": "steam@example.com"},
        }

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return json.dumps(payload).encode()

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            with patch.dict(windows.os.environ, {"FORGE_API_URL": "https://forge.example", "FORGE_TOKEN": "token"}, clear=False):
                with patch.object(windows.urllib.request, "urlopen", return_value=Response()):
                    source = windows.prepare_bundle(output)
                self.assertEqual(source, "register")
                self.assertEqual(windows.os.environ["EA_EMAIL"], "ea@example.com")
                self.assertNotIn("SKIP_STEAM_LINK", windows.os.environ)
                with patch.object(windows.urllib.request, "urlopen", side_effect=AssertionError("second checkout")):
                    self.assertEqual(windows.prepare_bundle(output), "register")
            pool = json.loads((output / "mailboxes.json").read_text())
            self.assertEqual([item["email"] for item in pool], ["ea@example.com", "steam@example.com"])
            self.assertFalse((output / "steam_account.txt").exists())

    def test_mailbox_refresh_hooks_the_link_module_token_name(self):
        class LinkModule:
            def graph_token(self, record):
                record["refresh_token"] = "rotated"
                return "access"

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            windows.write_private_json(output / "windows_trial.json", self.bundle())
            module = LinkModule()
            windows.capture_mailbox_refresh(module, output)
            self.assertEqual(module.graph_token({"email": "ea@example.com", "refresh_token": "refresh"}), "access")
            saved = json.loads((output / "windows_trial.json").read_text())
        self.assertEqual(saved["mailbox_updates"][0]["refresh_token"], "rotated")
        self.assertEqual(saved["ea_mailbox"]["refresh_token"], "rotated")

    def test_account_card_keeps_the_totp_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "ea_full_setup_result.json").write_text(json.dumps({
                "ea_email": "ea@example.com",
                "ea_password": "pw",
                "ea_origin_id": "PlayerOne",
                "totp_secret": "JBSWY3DPEHPK3PXP",
                "backup_codes": ["111"],
                "steam_user": "steam_user",
                "steam_link": True,
                "totp": True,
            }))
            path = windows.write_account_card(output)
            card = json.loads(path.read_text())
        self.assertEqual(card["totp_secret"], "JBSWY3DPEHPK3PXP")
        self.assertEqual(card["ea_password"], "pw")
        self.assertEqual(card["backup_codes"], ["111"])

    def test_region_proxy_keeps_only_a_matching_exit(self):
        seen = []

        def probe(url):
            seen.append(url)
            if len(seen) == 1:
                return "ID", None
            return "US", None

        with patch.object(windows.random, "choice", side_effect=["JP", "US"]):
            url = windows.choose_region_proxy(
                "socks5://user-region-HK-sid-base-t-30:secret@us.1024proxy.io:3000",
                "ea@example.com",
                probe=probe,
            )
        self.assertEqual(windows.os.environ["EA_REGISTER_COUNTRY"], "US")
        self.assertIn("-region-US-", url)
        self.assertEqual(len(seen), 2)

    def test_assigned_runtime_overrides_old_machine_proxy_and_survives_restart(self):
        payload = self.bundle()
        payload['registration'] = {'proxyVendor': '711', 'proxyUrl': 'http://fixture:secret@proxy.trustsource.live:10000',
                                   'steamCaptchaKey': 'fixture-captcha'}
        with tempfile.TemporaryDirectory() as directory, patch.dict(windows.os.environ, {'EA_PROXY': 'old', 'CAPTCHALY_KEY': 'old'}):
            output = Path(directory)
            windows.apply_bundle(output, payload)
            self.assertEqual(windows.os.environ['EA_PROXY'], payload['registration']['proxyUrl'])
            self.assertEqual(windows.os.environ['CAPTCHALY_KEY'], 'fixture-captcha')
            windows.os.environ['EA_PROXY'] = 'different'
            with patch.object(windows, 'checkout_bundle', side_effect=AssertionError('must reuse assigned task')):
                windows.prepare_bundle(output)
            self.assertEqual(windows.os.environ['EA_PROXY'], payload['registration']['proxyUrl'])

    def test_proxy_is_explicit_and_does_not_randomly_switch_vendor(self):
        configured = 'http://configured:secret@proxy.example:8080'
        with patch.dict(windows.os.environ, {'EA_PROXY': configured, 'EA_PROXY_VENDOR': '711'}, clear=True):
            self.assertEqual(windows.configured_proxy(), configured)
        with patch.dict(windows.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, 'Forge'):
                windows.configured_proxy()

    def test_proxy_failure_is_saved_before_final_upload(self):
        from unittest.mock import Mock
        from apex_registration.utils import bitbrowser, proxy_probe
        with tempfile.TemporaryDirectory() as directory, patch.dict(windows.os.environ,
                {'EA_PROXY': 'socks5://user:secret@us.1024proxy.io:3000', 'EA_REGISTER_REGION': 'US'}, clear=True):
            output = Path(directory)
            windows.write_private_json(output / 'windows_trial.json', self.bundle())
            with patch.object(windows, '_refuse_unless_windows'), patch.object(windows, 'acquire_game_lock'), \
                    patch.object(bitbrowser.BitBrowserClient, 'health', return_value=True), \
                    patch.object(windows, 'prepare_bundle', return_value='inventory'), \
                    patch.object(proxy_probe, 'probe_exit_country', return_value=(None, 'ipinfo.io: ConnectTimeout')), \
                    patch.object(windows, 'try_sync_result', return_value=True) as sync:
                self.assertEqual(windows.main(['--output-dir', directory]), 2)
            result = windows.read_json(output, 'ea_full_setup_result.json')
            self.assertEqual(result['stage'], 'proxy_check')
            self.assertEqual(result['error_code'], 'proxy_check_failed')
            self.assertIn('ConnectTimeout', result['error_message'])
            self.assertNotIn('secret', result['error_message'])
            sync.assert_called_once_with(output, finished=True, exit_code=2)

    def test_resin_proxy_uses_the_matching_ytoo_platform(self):
        seen = []

        def probe(url):
            seen.append(url)
            if "YtooJP" in url:
                return "US", None
            return "HK", None

        with patch.object(windows.random, "choice", side_effect=["JP", "HK"]):
            url = windows.choose_resin_proxy(
                "http://Default:secret@127.0.0.1:12260",
                "ea@example.com",
                probe=probe,
            )
        self.assertEqual(windows.os.environ["EA_REGISTER_COUNTRY"], "HK")
        self.assertIn("YtooHK.", url)
        self.assertNotIn("Default", url.split("@", 1)[0])
        self.assertTrue(url.endswith("@resin.pokonyan.com:2260"))
        self.assertEqual(len(seen), 2)


if __name__ == "__main__":
    unittest.main()

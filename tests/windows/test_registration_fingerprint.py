"""One registration claims one Windows Chrome, with language following the exit."""
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))

from apex_registration.flows.steam import try_steam_once as steam
from apex_registration.utils import bitbrowser
from apex_registration.utils.browser_profile import (
    CHROME_MAJOR,
    SEC_CH_UA,
    chrome_headers,
    current_country,
    ea_juno_create_url,
    ea_register_url,
    exit_utc_offset,
    impersonation_target,
    playwright_context_options,
    sec_ch_ua,
    steam_timezone_cookie,
)
from apex_registration.utils.proxy import parse_proxy_exit


CHROME_130 = '"Chromium";v="130", "Google Chrome";v="130", "Not?A_Brand";v="99"'


class FingerprintConsistencyTests(unittest.TestCase):
    def test_sec_ch_ua_follows_the_real_grease_rule_for_the_kernel(self):
        self.assertEqual(sec_ch_ua(130), CHROME_130)
        self.assertNotIn("142", sec_ch_ua(130))

    def test_tls_impersonation_does_not_claim_a_newer_chrome_than_the_kernel(self):
        available = ["chrome99", "chrome120", "chrome124", "chrome131", "chrome133a", "safari17_0"]
        self.assertEqual(impersonation_target(130, available), "chrome124")
        self.assertEqual(impersonation_target(120, available), "chrome120")

    def test_bitbrowser_is_windows_chrome_with_proxy_webrtc(self):
        with patch.dict(os.environ, {"GAME_ACCOUNTS_BIT_CORE": ""}, clear=False):
            os.environ.pop("GAME_ACCOUNTS_BIT_CORE", None)
            finger = bitbrowser._fingerprint()
        self.assertEqual(finger["os"], "Win32")
        self.assertEqual(finger["osVersion"], "11,10")
        self.assertEqual(finger["webRTC"], "0")
        self.assertEqual((finger["coreVersion"], finger["version"]), ("130", "130"))
        self.assertIn("Chrome/130.0.0.0", finger["userAgent"])
        self.assertNotIn("MacIntel", str(finger))
        with patch.dict(os.environ, {"GAME_ACCOUNTS_BIT_CORE": "124"}):
            pinned = bitbrowser._fingerprint()
        self.assertEqual((pinned["coreVersion"], pinned["version"]), ("124", "124"))
        self.assertIn("Chrome/124.0.0.0", pinned["userAgent"])

    def test_opened_kernel_must_match_the_claimed_chrome(self):
        self.assertIsNone(bitbrowser.kernel_mismatch("130.0.6723.117", 130))
        self.assertIsNone(bitbrowser.kernel_mismatch("Chrome/130.0.6723.117", 130))
        self.assertIsNone(bitbrowser.kernel_mismatch("", 130))
        message = bitbrowser.kernel_mismatch("136.0.7103.49", 130)
        self.assertIn("Chrome 136", message)
        self.assertIn("bitbrowserCore", message)

    def test_ea_ids_take_several_real_shapes_and_keep_ea_rules(self):
        import re
        from apex_registration.utils.browser_profile import rand_gamer_id
        samples = [rand_gamer_id() for _ in range(400)]
        for value in samples:
            self.assertRegex(value, r"^[A-Za-z][A-Za-z0-9_]{7,15}$")
            self.assertFalse(any(word in value.lower() for word in ("ea", "origin", "admin", "support")), value)
            self.assertIsNone(re.fullmatch(r"[A-Za-z2-7]{16}", value), value)
            self.assertIsNone(re.search(r"(19|20)\d\d", value), value)
        shapes = {("_" in v, bool(re.search(r"\d", v)), v != v.lower()) for v in samples}
        self.assertGreaterEqual(len(shapes), 5)
        self.assertLess(sum(1 for v in samples if re.fullmatch(r"[a-z]+_[a-z]+", v)), 80)
        self.assertGreater(len(set(samples)), 380)

    def test_protocol_language_and_steam_locale_follow_the_exit(self):
        with patch.dict(os.environ, {"EA_REGISTER_COUNTRY": "JP"}, clear=False):
            os.environ.pop("EA_REGISTER_UTC_OFFSET", None)
            headers = chrome_headers()
            self.assertTrue(headers["Accept-Language"].startswith("ja"))
            self.assertEqual(headers["sec-ch-ua"], SEC_CH_UA)
            self.assertIn(f'v="{CHROME_MAJOR}"', headers["sec-ch-ua"])
            self.assertEqual(steam.steam_join_headers()["Accept-Language"], headers["Accept-Language"])
            self.assertEqual(steam.register_locale().steam_elang, "10")
            self.assertEqual(steam_timezone_cookie(), "32400,0")
            options = playwright_context_options("seed")
            self.assertEqual(options["locale"], "ja")
            self.assertEqual(options["timezone_id"], "Asia/Tokyo")
            self.assertNotIn("geolocation", options)
        with patch.dict(os.environ, {"EA_REGISTER_COUNTRY": "US"}, clear=False):
            os.environ.pop("EA_REGISTER_UTC_OFFSET", None)
            os.environ.pop("EA_REGISTER_TIMEZONE", None)
            self.assertIsNone(steam_timezone_cookie())
            self.assertNotIn("timezone_id", playwright_context_options("seed"))
            self.assertEqual(chrome_headers()["Accept-Language"].split(",")[0], "en-US")
        with patch.dict(os.environ, {"EA_REGISTER_COUNTRY": "US", "EA_REGISTER_UTC_OFFSET": "-18000"}):
            self.assertEqual(steam_timezone_cookie(), "-18000,0")
        with patch.dict(os.environ, {"EA_REGISTER_COUNTRY": "HK"}, clear=False):
            self.assertEqual(current_country(), "HK")
            self.assertEqual(steam_timezone_cookie(), "28800,0")
            self.assertEqual(playwright_context_options("seed")["timezone_id"], "Asia/Hong_Kong")
            self.assertEqual(ea_register_url(), "https://www.ea.com/zh-hk/register")
        with patch.dict(os.environ, {"EA_REGISTER_COUNTRY": "JP"}, clear=False):
            self.assertEqual(ea_register_url(), "https://www.ea.com/ja/register")
            self.assertIn("locale=ja_JP", ea_juno_create_url())
        with patch.dict(os.environ, {"EA_REGISTER_COUNTRY": "US"}, clear=False):
            self.assertEqual(ea_register_url(), "https://www.ea.com/en-us/register")
            self.assertIn("locale=en_US", ea_juno_create_url())

    def test_us_offset_comes_from_the_exit_timezone_and_nowhere_else(self):
        geo = SimpleNamespace(timezone="America/New_York", utc_offset=None)
        offset = exit_utc_offset(geo)
        self.assertIn(offset, (-18000, -14400))
        self.assertIsNone(exit_utc_offset(SimpleNamespace(timezone="", utc_offset=None)))
        parsed = parse_proxy_exit('{"country":"US","timezone":"America/Los_Angeles","ip":"192.0.2.9"}')
        self.assertEqual(parsed.timezone, "America/Los_Angeles")
        self.assertEqual(parse_proxy_exit('{"country":"US","timezone":"not a zone"}').timezone, "")

    def test_webdriver_is_not_redefined(self):
        source = (
            Path(__file__).resolve().parents[2]
            / "windows"
            / "apex_registration"
            / "flows"
            / "ea"
            / "ea_pw_register.py"
        )
        self.assertNotIn("webdriver", source.read_text())


if __name__ == "__main__":
    unittest.main()

"""Window recovery and the EA update banner from the October 9 incident."""
import ctypes
from dataclasses import replace
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ea_app import EaAppAutomationError, EaIdentityFact, EaUiState
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver, SW_RESTORE
from apex_automation.ea_pages import EaPage
from apex_automation.ocr_obstacles import OcrToken


class EaWindowRecoveryTest(unittest.TestCase):
    def driver(self, windows):
        driver = object.__new__(WindowsEaHybridDriver)
        clock = [0.0]
        restores = []

        def rect(hwnd, pointer):
            pointer._obj.left, pointer._obj.top, pointer._obj.right, pointer._obj.bottom = windows[hwnd]["rect"]
            return True

        def process_id(hwnd, pointer):
            pointer._obj.value = hwnd
            return 1

        def restore(hwnd, command):
            restores.append((hwnd, command))
            windows[hwnd].update(visible=True, iconic=False, rect=(100, 100, 900, 900))

        driver.user32 = SimpleNamespace(
            EnumWindows=lambda callback, _value: [callback(hwnd, 0) for hwnd in windows],
            GetWindowThreadProcessId=process_id, GetWindowRect=rect,
            IsWindowVisible=lambda hwnd: windows[hwnd]["visible"],
            IsIconic=lambda hwnd: windows[hwnd]["iconic"], ShowWindow=restore,
        )
        driver._process_name = lambda pid: windows[pid].get("process", "eadesktop.exe")
        driver.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        self.enterContext(patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: clock[0]))
        # No Windows ABI is needed for a deterministic enumeration fixture.
        self.enterContext(patch.object(ctypes, "WINFUNCTYPE", lambda *_args: lambda function: function, create=True))
        return driver, restores

    def test_minimized_ea_is_restored_before_main_window_size_filter(self):
        driver, restores = self.driver({7: {"visible": True, "iconic": True, "rect": (-32000, -32000, -31840, -31972)}})
        self.assertEqual(driver._ea_window(), 7)
        self.assertEqual(restores, [(7, SW_RESTORE)])

    def test_hidden_main_ea_window_is_restored(self):
        driver, restores = self.driver({7: {"visible": False, "iconic": False, "rect": (100, 100, 900, 900)}})
        self.assertEqual(driver._ea_window(), 7)
        self.assertEqual(restores, [(7, SW_RESTORE)])

    def test_other_apps_and_small_ea_helper_windows_are_not_restored(self):
        driver, restores = self.driver({
            7: {"visible": True, "iconic": True, "rect": (0, 0, 160, 28), "process": "other.exe"},
            8: {"visible": False, "iconic": False, "rect": (0, 0, 100, 100)},
        })
        with self.assertRaises(EaAppAutomationError):
            driver._ea_window()
        self.assertEqual(restores, [])


class EaUpdateBannerTest(unittest.TestCase):
    def setUp(self):
        self.clear = EaObservation((0, 0, 1920, 1080), np.zeros((1, 1), dtype=np.uint8),
            (OcrToken("Browse", .99, (40, 180, 120, 200)), OcrToken("Library", .99, (40, 230, 120, 250))), EaPage.SIGNED_IN)
        self.banner = replace(self.clear, tokens=self.clear.tokens + (
            OcrToken("The EA app requires an update.", .99, (300, 70, 1000, 100)),
            OcrToken("Restart app", .99, (1100, 70, 1240, 100)),
        ))

    def driver(self, observations):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._observe = Mock(side_effect=observations)
        driver._ea_window = lambda: 7
        driver._record = Mock()
        driver._click_point = Mock()
        driver._identity = lambda _hwnd: EaIdentityFact("fixture-player", "test", True)
        driver._dismiss_expired_session = lambda _hwnd: False
        driver._dismiss_library_tour = lambda _hwnd, observation: observation
        driver._dismiss_account_ban = lambda _hwnd, observation: observation
        driver._open_account_menu = Mock(return_value=(self.clear, (700, 500)))
        driver._process_running = Mock(return_value=False)
        driver.restart_app = Mock(return_value=EaUiState.SIGNED_IN)
        driver.notify = Mock()
        driver.sleep = lambda _seconds: None
        return driver

    def test_banner_uses_only_exact_restart_action_not_the_whole_notice(self):
        self.assertEqual(WindowsEaHybridDriver._app_restart_point(self.banner), (1170, 85))
        whole = replace(self.banner, tokens=(OcrToken(
            "The EA app requires an update. To access the latest features, restart the app within 16h 36m. Restart app",
            .99, (200, 70, 1600, 100)),))
        self.assertTrue(WindowsEaHybridDriver._app_restart_required(whole))
        self.assertIsNone(WindowsEaHybridDriver._app_restart_point(whole))

    def test_game_update_and_unrelated_restart_text_are_not_ea_update_evidence(self):
        for text in ("Restart app", "Apex Legends update required"):
            observation = replace(self.clear, tokens=(OcrToken(text, .99, (300, 70, 1000, 100)),))
            self.assertFalse(WindowsEaHybridDriver._app_restart_required(observation))
        low = replace(self.banner, tokens=tuple(replace(t, confidence=.4) for t in self.banner.tokens))
        self.assertFalse(WindowsEaHybridDriver._app_restart_required(low))

    def test_direct_banner_restart_uses_its_link(self):
        driver = self.driver([self.banner])
        driver._request_restart_app(7)
        driver._click_point.assert_called_once_with(7, 1170, 85)

    def test_running_apex_defers_app_restart(self):
        driver = self.driver([])
        driver._process_running.return_value = True
        self.assertEqual(driver._handle_app_update(7, self.banner), (7, self.banner))
        driver.restart_app.assert_not_called()

    def test_signout_restarts_app_then_still_requires_the_login_page(self):
        login = replace(self.clear, page=EaPage.EMAIL, tokens=())
        driver = self.driver([self.banner, self.clear, login])
        self.assertTrue(driver.sign_out())
        driver.restart_app.assert_called_once()
        driver._open_account_menu.assert_called_once()
        driver._click_point.assert_called_once_with(7, 700, 500)

    def test_restart_does_not_itself_prove_signout(self):
        driver = self.driver([self.banner, self.clear])
        driver._open_account_menu.return_value = None
        self.assertFalse(driver._sign_out_once())
        driver.restart_app.assert_called_once()

    def test_preflight_handles_update_and_stops_repeating_a_persistent_banner(self):
        driver = self.driver([self.banner, self.clear])
        self.assertEqual(driver.preflight(), EaUiState.SIGNED_IN)
        driver.restart_app.assert_called_once()
        driver = self.driver([self.banner, self.banner])
        with self.assertRaisesRegex(EaAppAutomationError, "重启后仍显示"):
            driver.preflight()
        driver.restart_app.assert_called_once()
        self.assertFalse(driver._update_restart_in_progress)


if __name__ == "__main__":
    unittest.main()

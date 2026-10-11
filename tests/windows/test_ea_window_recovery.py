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
from apex_automation.ea_app import EaAppAutomationError, EaCaptureUnavailable, EaIdentityFact, EaUiState
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver, SW_RESTORE
from apex_automation.ea_pages import EaPage
from apex_automation.ocr_obstacles import OcrToken


class EaWindowRecoveryTest(unittest.TestCase):
    def test_observing_an_ea_popup_does_not_restore_and_dismiss_it(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._live = lambda hwnd: hwnd
        # The popup is owned by the EA main window, with a different handle.
        driver.user32 = Mock()
        driver.user32.GetForegroundWindow.return_value = 8
        driver.user32.GetParent.return_value = 0
        driver.user32.GetWindow.return_value = 7
        driver._focus(7)
        driver.user32.ShowWindow.assert_not_called()
        driver.user32.BringWindowToTop.assert_not_called()
        driver.user32.SetForegroundWindow.assert_not_called()

    def test_same_pid_sibling_is_not_the_requested_window_or_its_popup(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver.user32 = Mock()
        driver.user32.GetParent.return_value = 0
        driver.user32.GetWindow.return_value = 0
        driver.user32.GetWindowThreadProcessId.side_effect = lambda h, p: setattr(p._obj, "value", 42)
        self.assertFalse(driver._window_belongs_to_ea(7, 8))
        self.assertTrue(driver._window_belongs_to_ea(7, 7))

    def test_focus_brings_the_requested_sibling_to_the_front(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._live = lambda hwnd: hwnd
        foreground = [8]
        driver.user32 = Mock()
        driver.kernel32 = Mock()
        driver.kernel32.GetCurrentThreadId.return_value = 2
        driver.user32.GetParent.return_value = 0
        driver.user32.GetWindow.return_value = 0
        def same_process(_hwnd, pointer):
            pointer._obj.value = 42
            return 2
        driver.user32.GetWindowThreadProcessId.side_effect = same_process
        driver.user32.GetForegroundWindow.side_effect = lambda: foreground[0]
        driver.user32.SetForegroundWindow.side_effect = lambda hwnd: foreground.__setitem__(0, hwnd)
        driver.sleep = Mock()
        driver._focus(7)
        self.assertEqual(foreground[0], 7)
        driver.user32.SetForegroundWindow.assert_called_once_with(7)

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
        driver._focus = Mock()
        frame = np.zeros((1800, 2000, 3), np.uint8)
        frame[400:800, 200:800] = 255
        driver._frame = lambda: frame
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

    def test_capture_failure_does_not_select_an_unverified_window(self):
        driver, _restores = self.driver({
            1: {"visible": True, "iconic": False, "rect": (0, 0, 600, 800)},
            2: {"visible": True, "iconic": False, "rect": (0, 0, 1425, 750)},
        })
        driver._frame = Mock(side_effect=EaCaptureUnavailable("capture unavailable"))
        with self.assertRaises(EaCaptureUnavailable):
            driver._ea_window()

    def test_occluded_valid_window_is_sampled_after_focusing_it(self):
        driver, _ = self.driver({
            1: {"visible": True, "iconic": False, "rect": (0, 0, 1425, 1000)},
            2: {"visible": True, "iconic": False, "rect": (100, 100, 660, 900)},
        })
        foreground = [1]
        driver._focus = lambda hwnd: foreground.__setitem__(0, hwnd)
        black = np.zeros((1080, 1920, 3), np.uint8)
        content = black.copy()
        content[300:700, 150:600] = 200
        driver._frame = lambda: black if foreground[0] == 1 else content
        self.assertEqual(driver._ea_window(), 2)
        self.assertEqual(foreground[0], 2)

    def test_unfocusable_candidate_cannot_borrow_covering_window_pixels(self):
        driver, _ = self.driver({
            1: {"visible": True, "iconic": False, "rect": (0, 0, 1425, 900)},
            2: {"visible": True, "iconic": False, "rect": (100, 100, 660, 900)},
        })
        driver._focus = Mock(side_effect=[EaAppAutomationError("not foreground"), None])
        self.assertEqual(driver._ea_window(), 2)

    def test_identity_wait_does_not_reuse_a_rejected_blank_handle(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._ea_window = Mock(side_effect=EaAppAutomationError("only blank windows"))
        driver._alive = Mock(return_value=True)
        with self.assertRaises(EaAppAutomationError):
            driver._ea_window_for_identity(1)

    def test_blank_window_loses_to_a_smaller_window_with_content(self):
        frame = np.zeros((1600, 1800, 3), np.uint8)
        frame[1000:1400, 80:400] = (30, 180, 220)
        driver, restores = self.driver({
            1: {"visible": True, "iconic": False, "rect": (0, 0, 1425, 750)},
            2: {"visible": True, "iconic": False, "rect": (0, 800, 560, 1600)},
        })
        driver._frame = lambda: frame
        self.assertEqual(driver._ea_window(), 2)
        self.assertEqual(restores, [])

    def test_front_window_with_content_beats_a_larger_window_behind_it(self):
        frame = np.zeros((1600, 1800, 3), np.uint8)
        frame[200:700, 80:600] = (10, 20, 200)
        frame[1000:1400, 80:400] = (200, 180, 20)
        driver, _restores = self.driver({
            2: {"visible": True, "iconic": False, "rect": (0, 800, 560, 1600)},
            1: {"visible": True, "iconic": False, "rect": (0, 0, 1425, 900)},
        })
        driver._frame = lambda: frame
        self.assertEqual(driver._ea_window(), 2)

    def test_only_blank_ea_windows_are_not_selected(self):
        driver, _restores = self.driver({
            1: {"visible": True, "iconic": False, "rect": (0, 0, 1425, 750)},
        })
        driver._frame = lambda: np.zeros((1600, 1800, 3), np.uint8)
        with self.assertRaisesRegex(EaAppAutomationError, "空白窗口"):
            driver._ea_window()

    def test_flat_interior_is_blank_and_a_varied_interior_is_not(self):
        flat = np.zeros((200, 200, 3), np.uint8)
        varied = flat.copy()
        varied[80:150, 40:160] = (20, 200, 240)
        self.assertTrue(WindowsEaHybridDriver._interior_is_blank(flat, (0, 0, 200, 200)))
        self.assertFalse(WindowsEaHybridDriver._interior_is_blank(varied, (0, 0, 200, 200)))


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

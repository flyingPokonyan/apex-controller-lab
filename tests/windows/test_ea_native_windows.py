"""Real Win32 window ownership/visibility checks on the Windows CI desktop."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ea_app_win32 import WindowsEaHybridDriver
from apex_automation.ea_app import EaCaptureUnavailable
from apex_automation.ocr_obstacles import OcrToken


@unittest.skipUnless(sys.platform == "win32", "requires Win32 window manager")
class NativeEaWindowsTest(unittest.TestCase):
    def setUp(self):
        frame = np.zeros((1400, 2000, 3), np.uint8)
        frame[250:550, 250:550] = 240
        self.driver = WindowsEaHybridDriver(capture_source=SimpleNamespace(grab=lambda: frame))
        self.driver._ea_installation = None
        # Restrict enumeration to this test's windows, never other CI apps.
        self.driver._process_name = lambda pid: "eadesktop.exe" if pid == os.getpid() else "other.exe"
        self.api = self.driver.user32
        self.api.CreateWindowExW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p]
        self.api.CreateWindowExW.restype = wintypes.HWND
        self.api.DestroyWindow.argtypes = [wintypes.HWND]
        self.api.DestroyWindow.restype = wintypes.BOOL

    def window(self, title, *, visible=False, owner=None, ex_style=0, class_name="STATIC"):
        style = 0x00CF0000 | (0x10000000 if visible else 0)  # OVERLAPPEDWINDOW / VISIBLE
        hwnd = self.api.CreateWindowExW(ex_style, class_name, title, style,
            100, 100, 800, 700, owner, None, None, None)
        self.assertTrue(hwnd, f"CreateWindowExW failed: {ctypes.GetLastError()}")
        self.addCleanup(self.api.DestroyWindow, hwnd)
        return hwnd

    def test_restore_main_and_consolidate_old_exposed_roots(self):
        tray = self.window("QTrayIconMessageWindow", visible=True)
        scratch = self.window("", visible=True)
        main = self.window("EA")
        self.assertEqual(self.driver._ea_window(), main)
        self.assertTrue(self.api.IsWindowVisible(main))
        self.assertFalse(self.api.IsWindowVisible(tray))
        self.assertFalse(self.api.IsWindowVisible(scratch))

    def test_main_transition_preserves_its_owned_popup(self):
        main = self.window("EA", visible=True)
        popup = self.window("", visible=True, owner=main, ex_style=0x80)
        self.assertEqual(self.driver._ea_window(), main)
        self.assertTrue(self.api.IsWindowVisible(main))
        self.assertTrue(self.api.IsWindowVisible(popup))

    def test_hidden_receivers_stay_hidden_when_restoring_minimized_main(self):
        tray = self.window("QTrayIconMessageWindow")
        scratch = self.window("")
        main = self.window("EA", visible=True)
        self.api.ShowWindow(main, 6)  # MINIMIZE
        self.assertEqual(self.driver._ea_window(), main)
        self.assertFalse(self.api.IsIconic(main))
        self.assertFalse(self.api.IsWindowVisible(tray))
        self.assertFalse(self.api.IsWindowVisible(scratch))

    def test_login_tool_window_is_interactive_and_preserves_its_owner(self):
        main = self.window("EA", visible=True)
        login = self.window("EA", visible=True, owner=main, ex_style=0x80)
        self.assertEqual(self.driver._ea_window(), login)
        self.assertTrue(self.api.IsWindowVisible(login))
        self.assertTrue(self.api.IsWindowVisible(main))

    def test_native_capture_source_rejects_another_root_even_in_the_same_pid(self):
        main = self.window("EA", visible=True, class_name="BUTTON")
        other = self.window("Console", visible=True, class_name="BUTTON")
        self.driver._focus(main)
        source = self.driver._observation_source(main)
        self.assertEqual(source[0], os.getpid())
        token = OcrToken('Your account has been banned', .99, (200, 300, 500, 320))
        self.assertTrue(self.driver._token_belongs_to_window(main, token))
        self.driver._focus(other)
        self.assertFalse(self.driver._token_belongs_to_window(main, token))
        with self.assertRaises(EaCaptureUnavailable):
            self.driver._observation_source(main)
        self.api.ShowWindow(main, 6)
        self.assertFalse(self.driver._alive(main))


if __name__ == "__main__":
    unittest.main()

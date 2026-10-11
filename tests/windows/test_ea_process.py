"""Recovery when EA has disappeared, and verification after process launch."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ea_process import EaDesktopInstallation, verified_ea_executable
from apex_automation.ea_app import EaAppAutomationError, EaUiRecoveryExhausted, EaUiState
from apex_automation.ea_app_win32 import WindowsEaHybridDriver


class InstallationTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.exe = self.root / "custom-install/EADesktop.exe"
        self.exe.parent.mkdir()
        self.exe.touch()
        self.cache = self.root / "managed/ea-app.json"
        self.installation = EaDesktopInstallation(self.cache)

    def test_actual_process_path_survives_a_new_worker_without_ea_window(self):
        self.installation.remember(self.exe)
        with patch("apex_automation.ea_process.registered_ea_executables", return_value=[]):
            self.assertEqual(EaDesktopInstallation(self.cache).resolve(), self.exe)

    def test_missing_relative_and_other_executable_are_never_launched(self):
        other = self.root / "unrelated.exe"
        other.touch()
        for path in (other, Path("EADesktop.exe"), self.root / "missing/EADesktop.exe"):
            self.assertIsNone(verified_ea_executable(path))
            self.assertIsNone(self.installation.remember(path))
        self.assertFalse(self.cache.exists())

    def test_corrupt_cache_falls_back_to_registered_installation(self):
        self.cache.parent.mkdir()
        self.cache.write_text("{partial", encoding="utf-8")
        with patch("apex_automation.ea_process.registered_ea_executables", return_value=[str(self.exe)]):
            self.assertEqual(self.installation.resolve(), self.exe)

    def test_absent_window_and_registry_use_existing_standard_installation(self):
        standard = self.root / "Electronic Arts/EA Desktop/EA Desktop/EADesktop.exe"
        standard.parent.mkdir(parents=True)
        standard.touch()
        with patch("apex_automation.ea_process.registered_ea_executables", return_value=[]), \
                patch.dict("os.environ", {"ProgramW6432": str(self.root)}, clear=True):
            self.assertEqual(self.installation.resolve(), standard)

    def test_unwritable_cache_does_not_break_a_verified_window(self):
        with patch("apex_automation.ea_process.replace_with_retry", side_effect=PermissionError):
            self.assertEqual(self.installation.remember(self.exe), self.exe)
        self.assertEqual(self.installation.resolve(), self.exe)


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.exe = self.root / "EADesktop.exe"
        self.exe.touch()
        self.driver = object.__new__(WindowsEaHybridDriver)
        self.driver._ea_installation = EaDesktopInstallation(self.root / "ea-app.json")
        self.driver._ea_installation.remember(self.exe)
        self.driver._process_running = Mock(return_value=False)
        self.driver._process_path = Mock(return_value=str(self.exe))
        self.driver._ea_window = Mock(return_value=7)
        self.driver.user32 = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd, pointer: setattr(pointer._obj, "value", 321))
        self.driver.preflight = Mock(return_value=EaUiState.LOGIN)
        self.driver.notify = Mock()
        self.driver.sleep = Mock()
        self.kill = self.enterContext(patch("apex_automation.ea_app_win32.subprocess.run", return_value=Mock(returncode=0)))
        self.launch = self.enterContext(patch("apex_automation.ea_app_win32.subprocess.Popen"))
        self.enterContext(patch("apex_automation.ea_app_win32.desktop_app_creationflags", return_value=0x01000000))

    def test_present_window_restarts_only_verified_pid_and_launches_outside_job(self):
        self.assertEqual(self.driver._restart_ea_process(), EaUiState.LOGIN)
        self.assertEqual(self.kill.call_args.args[0], ["taskkill", "/PID", "321", "/T", "/F"])
        self.assertEqual(self.launch.call_args.args[0], [str(self.exe)])
        self.assertEqual(self.launch.call_args.kwargs["creationflags"], 0x01000000)
        self.driver.preflight.assert_called_once()

    def test_closed_window_reopens_installation_without_killing_unknown_processes(self):
        self.driver._ea_window.side_effect = EaAppAutomationError("window absent")
        self.assertEqual(self.driver._restart_ea_process(), EaUiState.LOGIN)
        self.kill.assert_not_called()
        self.launch.assert_called_once()

    def test_active_apex_prevents_both_kill_and_launch(self):
        self.driver._process_running.return_value = True
        with self.assertRaises(EaUiRecoveryExhausted):
            self.driver._restart_ea_process()
        self.kill.assert_not_called()
        self.launch.assert_not_called()

    def test_unknown_installation_stops_before_launch(self):
        self.driver._ea_window.side_effect = EaAppAutomationError("window absent")
        self.driver._ea_installation.resolve = Mock(return_value=None)
        with self.assertRaisesRegex(EaUiRecoveryExhausted, "没有找到"):
            self.driver._restart_ea_process()
        self.kill.assert_not_called()
        self.launch.assert_not_called()

    def test_process_launch_alone_does_not_prove_ea_recovered(self):
        self.driver.preflight.side_effect = EaAppAutomationError("unrecognizable")
        with patch("apex_automation.ea_app_win32.time.monotonic", side_effect=[0, 1, 61]):
            with self.assertRaisesRegex(EaUiRecoveryExhausted, "没有可识别页面"):
                self.driver._restart_ea_process()
        self.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

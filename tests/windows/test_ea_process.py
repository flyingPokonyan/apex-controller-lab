"""Recovery when EA has disappeared, and verification after process launch."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ea_process import (
    EaDesktopInstallation, listed_ea_pids, verified_ea_executable,
)
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


    def test_tasklist_keeps_only_ea_desktop_and_background_service(self):
        stdout = "\n".join((
            '"EADesktop.exe","321","Console","1","1 K"',
            '"EABackgroundService.exe","654","Services","0","1 K"',
            '"notepad.exe","999","Console","1","1 K"',
            "INFO: No tasks are running which match the specified criteria.",
            '"EADesktop.exe","321","Console","1","1 K"',
        ))
        self.assertEqual(listed_ea_pids(stdout), [321, 654])
        self.assertEqual(listed_ea_pids(""), [])


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
        self.calls = []
        self.snapshots = [""]
        self.enterContext(patch("apex_automation.ea_app_win32.subprocess.run", side_effect=self.subprocess))
        self.launch = self.enterContext(patch("apex_automation.ea_app_win32.subprocess.Popen"))
        self.enterContext(patch("apex_automation.ea_app_win32.desktop_app_creationflags", return_value=0x01000000))

    def subprocess(self, args, **_kwargs):
        self.calls.append(list(args))
        if args[0] == "tasklist":
            index = min(sum(command[0] == "tasklist" for command in self.calls) - 1, len(self.snapshots) - 1)
            return Mock(returncode=0, stdout=self.snapshots[index], stderr="")
        if args[0] == "taskkill":
            return Mock(returncode=0, stdout="", stderr="")
        raise AssertionError(args)

    def pids_killed(self):
        return [int(command[2]) for command in self.calls if command[:2] == ["taskkill", "/PID"]]

    def images_killed(self):
        return [command[2] for command in self.calls if command[:2] == ["taskkill", "/IM"]]

    def test_present_window_stops_every_ea_process_then_launches_one_outside_job(self):
        self.snapshots = [
            '"EADesktop.exe","321","Console","1","1 K"\n"EABackgroundService.exe","654","Services","0","1 K"\n',
            "",
        ]
        self.assertEqual(self.driver._restart_ea_process(), EaUiState.LOGIN)
        self.assertEqual(self.images_killed(), ["EADesktop.exe", "EABackgroundService.exe"])
        self.assertEqual(self.pids_killed(), [321, 321, 654])
        self.assertEqual(self.launch.call_args.args[0], [str(self.exe)])
        self.assertEqual(self.launch.call_args.kwargs["creationflags"], 0x01000000)
        self.launch.assert_called_once()
        self.driver.preflight.assert_called_once()
        self.driver._ea_window.assert_called_once_with(require_content=False)

    def test_closed_window_stops_leftovers_without_killing_unrelated_processes(self):
        self.driver._ea_window.side_effect = EaAppAutomationError("window absent")
        self.snapshots = [
            "\n".join((
                '"EADesktop.exe","321","Console","1","1 K"',
                '"EABackgroundService.exe","654","Services","0","1 K"',
                '"notepad.exe","999","Console","1","1 K"',
            )),
            "",
        ]
        self.assertEqual(self.driver._restart_ea_process(), EaUiState.LOGIN)
        self.assertEqual(self.pids_killed(), [321, 654])
        self.assertNotIn(999, self.pids_killed())
        self.launch.assert_called_once()

    def test_restart_does_not_launch_a_second_instance_while_ea_remains(self):
        self.snapshots = ['"EADesktop.exe","321","Console","1","1 K"\n']
        clock = {"now": 0.0}

        def monotonic():
            clock["now"] += 10
            return clock["now"]

        with patch("apex_automation.ea_app_win32.time.monotonic", side_effect=monotonic):
            with self.assertRaisesRegex(EaUiRecoveryExhausted, "未全部退出"):
                self.driver._restart_ea_process()
        self.launch.assert_not_called()
        self.assertIn(321, self.pids_killed())

    def test_active_apex_prevents_both_kill_and_launch(self):
        self.driver._process_running.return_value = True
        with self.assertRaises(EaUiRecoveryExhausted):
            self.driver._restart_ea_process()
        self.assertEqual(self.calls, [])
        self.launch.assert_not_called()

    def test_unreadable_process_list_does_not_launch_another_instance(self):
        def broken(args, **_kwargs):
            if args[0] == "tasklist":
                return Mock(returncode=1, stdout="", stderr="denied")
            return Mock(returncode=0, stdout="", stderr="")

        with patch("apex_automation.ea_app_win32.subprocess.run", side_effect=broken):
            with self.assertRaisesRegex(EaUiRecoveryExhausted, "无法确认"):
                self.driver._restart_ea_process()
        self.launch.assert_not_called()

    def test_unknown_installation_stops_before_launch(self):
        self.driver._ea_window.side_effect = EaAppAutomationError("window absent")
        self.driver._ea_installation.resolve = Mock(return_value=None)
        with self.assertRaisesRegex(EaUiRecoveryExhausted, "没有找到"):
            self.driver._restart_ea_process()
        self.assertEqual(self.calls, [])
        self.launch.assert_not_called()

    def test_process_launch_alone_does_not_prove_ea_recovered(self):
        self.driver.preflight.side_effect = EaAppAutomationError("unrecognizable")
        clock = {"now": 0.0}

        def monotonic():
            clock["now"] += 30
            return clock["now"]

        with patch("apex_automation.ea_app_win32.time.monotonic", side_effect=monotonic):
            with self.assertRaisesRegex(EaUiRecoveryExhausted, "没有可识别页面"):
                self.driver._restart_ea_process()
        self.launch.assert_called_once()


if __name__ == "__main__":
    unittest.main()

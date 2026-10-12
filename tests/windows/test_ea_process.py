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
from apex_automation.ea_app import EaAppAutomationError, EaApexStartFailed, EaUiRecoveryExhausted, EaUiState
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


    def test_tasklist_keeps_only_ea_desktop_in_the_runner_session(self):
        stdout = "\n".join((
            '"EADesktop.exe","321","Console","1","1 K"',
            '"EABackgroundService.exe","654","Services","0","1 K"',
            '"EADesktop.exe","777","Console","2","1 K"',
            '"notepad.exe","999","Console","1","1 K"',
            "INFO: No tasks are running which match the specified criteria.",
            '"EADesktop.exe","321","Console","1","1 K"',
        ))
        self.assertEqual(listed_ea_pids(stdout, session_id=1), [321])
        self.assertEqual(listed_ea_pids("", session_id=1), [])

    def test_unreadable_desktop_session_is_not_treated_as_absent(self):
        with self.assertRaises(ValueError):
            listed_ea_pids('"EADesktop.exe","321","Console","unknown"', session_id=1)


class RestartTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.exe = self.root / "EADesktop.exe"
        self.exe.touch()
        self.driver = object.__new__(WindowsEaHybridDriver)
        self.driver._ea_installation = EaDesktopInstallation(self.root / "ea-app.json")
        self.driver._ea_installation.remember(self.exe)
        self.driver._process_running = Mock(return_value=False)
        self.driver._runner_session_id = Mock(return_value=1)
        self.driver._process_path = Mock(return_value=str(self.exe))
        self.driver._ea_window = Mock(return_value=7)
        self.driver.user32 = SimpleNamespace(GetWindowThreadProcessId=lambda _hwnd, pointer: setattr(pointer._obj, "value", 321))
        self.driver.preflight = Mock(return_value=EaUiState.LOGIN)
        self.driver.notify = Mock()
        self.driver.evidence = None
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

    def test_present_window_stops_verified_desktop_then_launches_one_outside_job(self):
        self.snapshots = [
            '"EADesktop.exe","321","Console","1","1 K"\n"EABackgroundService.exe","654","Services","0","1 K"\n',
            "",
        ]
        self.assertEqual(self.driver._restart_ea_process(), EaUiState.LOGIN)
        self.assertEqual(self.images_killed(), [])
        self.assertEqual(self.pids_killed(), [321])
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
        self.assertEqual(self.pids_killed(), [321])
        self.assertNotIn(999, self.pids_killed())
        self.launch.assert_called_once()

    def test_background_service_and_another_session_do_not_block_relaunch(self):
        self.snapshots = ['"EABackgroundService.exe","654","Services","0","1 K"\n'
                          '"EADesktop.exe","777","Console","2","1 K"']
        self.driver._restart_ea_process()
        self.assertEqual(self.pids_killed(), [])
        self.assertEqual(self.images_killed(), [])
        self.launch.assert_called_once()

    def test_no_window_still_requires_verified_path_before_killing_leftovers(self):
        self.driver._ea_window.side_effect = EaAppAutomationError("window absent")
        self.snapshots = ['"EADesktop.exe","321","Console","1","1 K"']
        self.driver._process_path.return_value = str(self.root / "other/EADesktop.exe")
        with self.assertRaisesRegex(EaUiRecoveryExhausted, "安装路径"):
            self.driver._restart_ea_process()
        self.assertEqual(self.pids_killed(), [])
        self.launch.assert_not_called()

    def test_unknown_runner_session_does_not_kill_or_launch(self):
        self.driver._runner_session_id.side_effect = EaUiRecoveryExhausted("会话未知")
        with self.assertRaises(EaUiRecoveryExhausted):
            self.driver._restart_ea_process()
        self.assertEqual(self.calls, [])
        self.launch.assert_not_called()

    def test_delayed_desktop_reappearance_is_stopped_before_one_launch(self):
        self.snapshots = ["", '"EADesktop.exe","321","Console","1","1 K"', "", ""]
        self.driver._restart_ea_process()
        self.assertEqual(self.pids_killed(), [321])
        self.assertEqual(sum(command[0] == "tasklist" for command in self.calls), 4)
        self.launch.assert_called_once()

    def test_game_starting_during_cleanup_blocks_relaunch(self):
        self.driver._process_running.side_effect = [False, False, True]
        with self.assertRaisesRegex(EaUiRecoveryExhausted, "Apex 尚未退出"):
            self.driver._restart_ea_process()
        self.launch.assert_not_called()

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

    def test_unreadable_pid_path_does_not_kill_or_launch(self):
        self.driver._ea_window.side_effect = EaAppAutomationError("window absent")
        self.snapshots = ['"EADesktop.exe","321","Console","1","1 K"']
        self.driver._process_path.return_value = ""
        with self.assertRaisesRegex(EaUiRecoveryExhausted, "安装路径"):
            self.driver._restart_ea_process()
        self.assertEqual(self.pids_killed(), [])
        self.launch.assert_not_called()

    def test_desktop_exiting_between_listing_and_path_read_is_rechecked(self):
        self.driver._ea_window.side_effect = EaAppAutomationError("window absent")
        self.snapshots = ['"EADesktop.exe","321","Console","1","1 K"', "", "", ""]
        self.driver._process_path.return_value = ""
        self.driver._restart_ea_process()
        self.assertEqual(self.pids_killed(), [])
        self.launch.assert_called_once()

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
            clock["now"] += 1
            return clock["now"]

        with patch("apex_automation.ea_app_win32.time.monotonic", side_effect=monotonic):
            with self.assertRaisesRegex(EaUiRecoveryExhausted, "没有可识别页面"):
                self.driver._restart_ea_process()
        self.launch.assert_called_once()


class MenuRestartTest(unittest.TestCase):
    def setUp(self):
        self.driver = object.__new__(WindowsEaHybridDriver)
        self.driver._process_running = Mock(return_value=False)
        self.driver._ea_window = Mock(return_value=7)
        self.driver._request_restart_app = Mock()
        self.driver.preflight = Mock(return_value=EaUiState.SIGNED_IN)
        self.driver._observe = Mock()
        self.driver._record = Mock()
        self.now = 0.0
        self.driver.sleep = lambda seconds: setattr(self, "now", self.now + seconds)
        self.enterContext(patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: self.now))

    def test_unchanged_readable_page_does_not_prove_restart(self):
        self.driver._alive = Mock(return_value=True)
        with self.assertRaisesRegex(EaApexStartFailed, "原窗口仍未退出"):
            self.driver.restart_app()
        self.driver.preflight.assert_not_called()
        self.driver._record.assert_not_called()
        self.assertEqual(self.now, 15.0)

    def test_window_transition_still_requires_a_recognized_page(self):
        self.driver._alive = Mock(return_value=False)
        self.assertEqual(self.driver.restart_app(), EaUiState.SIGNED_IN)
        self.driver.preflight.assert_called_once()
        self.driver._record.assert_called_once()
        self.driver.preflight.side_effect = EaAppAutomationError("blank page")
        with self.assertRaises(EaApexStartFailed):
            self.driver.restart_app()

    def test_running_game_blocks_even_the_menu_restart(self):
        self.driver._process_running.return_value = True
        with self.assertRaises(EaUiRecoveryExhausted):
            self.driver.restart_app()
        self.driver._request_restart_app.assert_not_called()


if __name__ == "__main__":
    unittest.main()

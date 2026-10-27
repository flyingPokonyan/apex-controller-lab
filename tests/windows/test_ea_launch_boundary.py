"""Asynchronous game launch, account evidence, and verified cleanup boundaries."""
import subprocess
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ea_app import EaAppAutomationError
from apex_automation.ea_app_win32 import APEX_LAUNCH_TIMEOUT_S, WindowsEaHybridDriver


class LaunchCleanupTest(unittest.TestCase):
    def driver(self, running):
        driver = object.__new__(WindowsEaHybridDriver)
        self.now = 0.0
        driver.sleep = lambda seconds: setattr(self, "now", self.now + seconds)
        driver.notify = Mock()
        driver._process_running = lambda name: name == "r5apex.exe" and running(self.now)
        self.enterContext(patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: self.now))
        self.kill = self.enterContext(patch("apex_automation.ea_app_win32.subprocess.run"))
        return driver

    def test_pending_game_that_appears_after_first_empty_poll_is_closed_before_signout(self):
        stopped = []
        driver = self.driver(lambda now: now >= 12 and not stopped)
        driver._apex_launch_pending_until = 20.0
        self.kill.side_effect = lambda *a, **k: stopped.append(self.now)
        result = driver.stop_apex()
        self.assertEqual(stopped, [12.0])
        self.assertTrue(result.all_processes_exited)
        self.assertTrue(result.requested_normal_close)
        self.assertGreaterEqual(self.now, 20)

    def test_worker_restart_reconstructs_launch_barrier_from_checkpoint_phase(self):
        driver = self.driver(lambda now: False)
        result = driver.stop_apex(settle_launch=True)
        self.assertTrue(result.all_processes_exited)
        self.assertEqual(self.now, APEX_LAUNCH_TIMEOUT_S)
        self.kill.assert_not_called()

    def test_normal_close_escalates_if_game_does_not_exit(self):
        stopped = []
        driver = self.driver(lambda now: not stopped)
        def kill(args, **kwargs):
            if "/F" in args:
                stopped.append(self.now)
        self.kill.side_effect = kill
        self.assertTrue(driver.stop_apex().all_processes_exited)
        self.assertEqual(stopped, [5.0])
        self.assertNotIn("/F", self.kill.call_args_list[0].args[0])

    def test_failed_kill_is_not_cleanup_evidence(self):
        driver = self.driver(lambda now: True)
        self.assertFalse(driver.stop_apex().all_processes_exited)
        self.assertEqual(self.now, 30)

    def test_uncertain_launch_click_still_has_a_cleanup_barrier(self):
        driver = self.driver(lambda now: False)
        driver._click_point = Mock(side_effect=EaAppAutomationError("input uncertain"))
        with self.assertRaises(EaAppAutomationError):
            driver._request_apex_launch(7, (1, 2))
        self.assertEqual(driver._apex_launch_pending_until, APEX_LAUNCH_TIMEOUT_S)
        self.assertTrue(driver.stop_apex().all_processes_exited)
        self.assertEqual(self.now, APEX_LAUNCH_TIMEOUT_S)

    def test_play_transition_to_empty_library_waits_for_delayed_game_not_a_ban(self):
        from test_ea_pages import EaLaunchRecoveryTest as Fixture
        from apex_automation.ea_app import EaAccountBanned
        for explicit_ban in (False, True):
            with self.subTest(explicit_ban=explicit_ban):
                now = [0.0]
                stopped = []
                entry = Fixture.observation(("Apex Legends", 120, 460))
                play = Fixture.observation(("Apex Legends", 800, 280), ("PLAY", 1150, 700))
                after = Fixture().banned_overlay() if explicit_ban else Fixture.observation(("Your library is empty", 960, 360))
                frames = [entry, play, after]
                clicks, records = [], []
                driver = Fixture.driver(frames, clicks, records)
                driver._observe = lambda _hwnd: frames.pop(0) if frames else after
                driver.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
                driver._process_running = lambda name: name == "r5apex.exe" and now[0] >= 12 and not stopped
                with patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: now[0]), \
                     patch("apex_automation.ea_app_win32.subprocess.run", side_effect=lambda *a, **k: stopped.append(now[0])):
                    if explicit_ban:
                        with self.assertRaises(EaAccountBanned):
                            driver.start_apex()
                        self.assertTrue(driver.stop_apex().all_processes_exited)
                        self.assertEqual(len(stopped), 1)
                        self.assertGreaterEqual(stopped[0], 12.0)
                        self.assertLess(stopped[0], 15.0)
                        self.assertGreaterEqual(now[0], 90)
                    else:
                        driver.start_apex()
                        self.assertEqual(now[0], 12)
                        self.assertNotIn("account-banned", records)
                        self.assertEqual(stopped, [])
                self.assertEqual(clicks, [(120, 460), (1150, 700)] + ([(1100, 700)] if explicit_ban else []))

    def launch(self, game_starts_at):
        from test_ea_pages import EaLaunchRecoveryTest as Fixture
        now, clicks, records = [0.0], [], []
        entry = Fixture.observation(("Apex Legends", 120, 460))
        play = Fixture.observation(("Apex Legends", 800, 280), ("PLAY", 1150, 700))
        frames = [entry]
        driver = Fixture.driver(frames, clicks, records)
        driver._observe = lambda _hwnd: frames.pop(0) if frames else play
        driver.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        driver._process_running = lambda name: name == "r5apex.exe" and now[0] >= game_starts_at
        self.enterContext(patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: now[0]))
        return driver, now, clicks, records

    def test_play_shown_again_is_clicked_once_more_in_the_same_session(self):
        driver, now, clicks, records = self.launch(game_starts_at=100)
        driver.start_apex()
        self.assertEqual(clicks, [(120, 460), (1150, 700), (1150, 700)])
        self.assertEqual(records.count("apex-play-again"), 1)
        self.assertGreaterEqual(now[0], 100)

    def test_a_slow_first_launch_is_not_given_up_at_ninety_seconds(self):
        driver, now, clicks, _records = self.launch(game_starts_at=150)
        driver.start_apex()
        self.assertGreaterEqual(now[0], 150)

    def test_second_launch_is_the_last_before_cleanup(self):
        from apex_automation.ea_app import EaApexLaunchStalled
        driver, now, clicks, records = self.launch(game_starts_at=float("inf"))
        with self.assertRaisesRegex(EaApexLaunchStalled, "未发现 Apex 进程"):
            driver.start_apex()
        self.assertEqual(clicks.count((1150, 700)), 2)
        self.assertEqual(records.count("apex-play-again"), 1)
        self.assertLess(now[0], 60 + APEX_LAUNCH_TIMEOUT_S + 10)

    def test_unreadable_process_list_cannot_prove_no_game(self):
        for failure in (Mock(returncode=1, stdout=""), subprocess.TimeoutExpired("tasklist", 10)):
            with self.subTest(failure=type(failure).__name__):
                with patch("apex_automation.ea_app_win32.subprocess.run") as run:
                    if isinstance(failure, Exception):
                        run.side_effect = failure
                    else:
                        run.return_value = failure
                    with self.assertRaises(EaAppAutomationError):
                        WindowsEaHybridDriver._process_running("r5apex.exe")


if __name__ == "__main__":
    unittest.main()

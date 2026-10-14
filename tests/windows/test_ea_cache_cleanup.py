"""App Recovery must finish before lease cleanup can hand off the desktop."""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.diagnostic_schema import EA_STEPS
from apex_automation.ea_app import EaCacheCleanupFailed, EaAppAutomationError, EaUiState
from apex_automation.ea_app_win32 import (
    APEX_EXECUTABLES,
    VK_ESCAPE,
    EaObservation,
    WindowsEaHybridDriver,
)
from apex_automation.ea_pages import EaPage
from apex_automation.ocr_obstacles import OcrToken
from apex_automation.account_provider import FakeAccountProvider
from apex_automation.account_orchestrator import AccountOrchestrator, AccountCycleOutcome
from apex_automation.orchestration_state import AtomicCheckpointStore, OrchestrationCheckpoint, WorkflowPhase
from test_account_orchestration import FakeEaDriver, FakeDrain, ScriptedLeaseKeeper


def observation(page=EaPage.SIGNED_IN, *labels):
    return EaObservation(
        rect=(100, 100, 1100, 900), frame=np.zeros((1, 1), dtype=np.uint8),
        page=page, tokens=tuple(OcrToken(text, .99, (x - 40, y - 10, x + 40, y + 10))
                                for text, x, y in labels),
        hwnd=7, process_id=42, source_verified=True,
    )


class CacheUiTest(unittest.TestCase):
    def driver(self, *, restarted_page=EaPage.SIGNED_IN):
        driver = object.__new__(WindowsEaHybridDriver)
        clock = [0.0]
        self.enterContext(patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: clock[0]))
        driver.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        driver._ea_window = Mock(side_effect=[7, 8, 8])
        self.home = observation()
        self.help = observation(EaPage.SIGNED_IN, ("Help", 150, 250))
        self.recovery = observation(EaPage.SIGNED_IN, ("Restart app", 340, 300), ("App Recovery", 340, 350))
        self.dialog = observation(EaPage.SIGNED_IN,
            ("Restart to run App Recovery?", 600, 400),
            ("CLEAR CACHE", 700, 600), ("CANCEL", 850, 600))
        self.ready = observation(restarted_page)
        driver._observe = Mock(side_effect=[
            self.home, self.home, self.help, self.recovery, self.dialog, self.ready, self.ready,
        ])
        driver._focus = Mock()
        driver._tap = Mock()
        driver._state = lambda _hwnd, obs: EaUiState.LOGIN if obs.page is EaPage.EMAIL else EaUiState.SIGNED_IN
        driver._process_running = Mock(return_value=False)
        driver._alive = Mock(return_value=False)
        driver._record = Mock()
        driver._click_point = Mock()
        driver._dismiss_expired_session = Mock()
        for name in ("_dismiss_cloud_upload_error", "_cancel_interrupted_cloud_launch",
                     "_dismiss_library_tour", "_dismiss_account_ban"):
            setattr(driver, name, lambda _hwnd, obs: obs)
        driver._restart_ea_process = Mock()
        driver.restart_app = Mock()
        driver.sign_out = Mock()
        return driver

    def test_exact_menu_and_confirmation_then_new_stable_home_or_login(self):
        for page in (EaPage.SIGNED_IN, EaPage.EMAIL):
            with self.subTest(page=page):
                driver = self.driver(restarted_page=page)
                before = Mock(side_effect=lambda: self.assertEqual(driver._click_point.call_count, 3))
                self.assertTrue(driver.clear_cache(before_clear=before))
                before.assert_called_once()
                self.assertEqual([call.args[1:] for call in driver._click_point.call_args_list],
                                 [(111, 114), (150, 250), (340, 350), (700, 600)])
                self.assertEqual(driver._ea_window.call_count, 3)
                driver.restart_app.assert_not_called()
                driver._restart_ea_process.assert_not_called()
                driver.sign_out.assert_not_called()

    def test_running_apex_or_late_launch_blocks_clear(self):
        for late in (False, True):
            driver = self.driver()
            # The second gate must also reject a late-starting game.
            calls = [0]
            def running(_name):
                calls[0] += 1
                return not late or calls[0] > len(APEX_EXECUTABLES)
            driver._process_running.side_effect = running
            before = Mock()
            with self.assertRaises(EaCacheCleanupFailed):
                driver.clear_cache(before_clear=before)
            before.assert_not_called()
            self.assertNotIn((7, 700, 600), [c.args for c in driver._click_point.call_args_list])

    def test_login_page_skips_recovery(self):
        driver = self.driver()
        driver._observe.side_effect = [observation(EaPage.EMAIL)]
        before = Mock()
        self.assertFalse(driver.clear_cache(before_clear=before))
        before.assert_not_called()
        driver._click_point.assert_not_called()

    def test_missing_or_untrusted_confirmation_never_clicks_clear(self):
        for mode in ("missing-title", "untrusted-button", "missing-cancel"):
            driver = self.driver()
            if mode == "missing-title":
                bad = replace(self.dialog, tokens=self.dialog.tokens[1:])
            elif mode == "missing-cancel":
                bad = replace(self.dialog, tokens=self.dialog.tokens[:2])
            else:
                bad = replace(self.dialog, tokens=tuple(replace(t, confidence=.4) for t in self.dialog.tokens))
            script = [self.home, self.home, self.help, self.recovery]

            def observe(_hwnd, retries=1, script=script, bad=bad):
                if driver._tap.called:
                    return bad
                if script:
                    return script.pop(0)
                return bad

            driver._observe.side_effect = observe
            before = Mock()
            with self.assertRaises(EaCacheCleanupFailed):
                driver.clear_cache(before_clear=before)
            before.assert_not_called()
            self.assertEqual(driver._tap.call_count, 1)
            self.assertNotIn((7, 700, 600), [call.args for call in driver._click_point.call_args_list])

    def test_unchanged_old_window_is_not_recovery_success(self):
        driver = self.driver()
        driver._alive.return_value = True
        with self.assertRaisesRegex(EaCacheCleanupFailed, "原窗口未退出"):
            driver.clear_cache(before_clear=Mock())
        driver._restart_ea_process.assert_not_called()

    def test_checkpoint_write_failure_prevents_clear_click(self):
        driver = self.driver()
        with self.assertRaises(OSError):
            driver.clear_cache(before_clear=Mock(side_effect=OSError("disk full")))
        self.assertEqual(driver._click_point.call_count, 3)

    def test_restart_timeout_does_not_launch_a_competing_ea_process(self):
        driver = self.driver()
        driver._ea_window.side_effect = [7] + [EaAppAutomationError("window absent")] * 130
        with self.assertRaisesRegex(EaCacheCleanupFailed, "120 秒"):
            driver.clear_cache(before_clear=Mock())
        driver._restart_ea_process.assert_not_called()
        driver.restart_app.assert_not_called()

    def test_split_labels_are_supported_but_low_confidence_is_not(self):
        obs = observation(EaPage.SIGNED_IN, ("Clear", 620, 600), ("cache", 710, 600))
        self.assertIsNotNone(WindowsEaHybridDriver._cache_action_point(obs, ("clearcache",)))
        obs = replace(obs, tokens=tuple(replace(t, confidence=.4) for t in obs.tokens))
        self.assertIsNone(WindowsEaHybridDriver._cache_action_point(obs, ("clearcache",)))

    def test_visible_app_recovery_is_clicked_without_reopening_the_menu(self):
        driver = self.driver()
        driver._observe.side_effect = [self.home, self.recovery, self.dialog, self.ready, self.ready]
        self.assertTrue(driver.clear_cache(before_clear=Mock()))
        self.assertEqual(
            [call.args[1:] for call in driver._click_point.call_args_list],
            [(340, 350), (700, 600)],
        )
        driver._tap.assert_not_called()

    def test_missing_menu_records_screenshot_and_ocr_before_raising(self):
        driver = self.driver()
        library = observation(EaPage.SIGNED_IN, ("Library", 180, 250))
        driver._observe.side_effect = [self.home, library, library]
        before = Mock()
        with self.assertRaisesRegex(EaCacheCleanupFailed, "App Recovery"):
            driver.clear_cache(before_clear=before)
        before.assert_not_called()
        self.assertEqual(driver._click_point.call_count, 1)
        driver._tap.assert_not_called()
        steps = [call.args[0] for call in driver._record.call_args_list]
        self.assertEqual(steps[:2], ["app-recovery-menu-open", "app-recovery-help-missing"])
        recorded = [
            call for call in driver._record.call_args_list
            if call.args[0] == "cache-clear-menu-missing"
        ]
        self.assertEqual(len(recorded), 1)
        shot = recorded[0].args[1]
        self.assertIsNotNone(shot.frame)
        self.assertEqual([token.text for token in shot.tokens], ["Library"])
        self.assertIn("cache-clear-menu-missing", EA_STEPS)
        for step in (
            "app-recovery-menu-open",
            "app-recovery-help-missing",
            "app-recovery-item-missing",
            "app-recovery-requested",
        ):
            self.assertIn(step, EA_STEPS)

    def _dialog_script(self, driver, second):
        bad = observation(EaPage.SIGNED_IN, ("Library", 400, 400))
        first = [self.home, self.home, self.help, self.recovery]
        cursor = {"i": 0, "j": 0}

        def observe(_hwnd, retries=1):
            if not driver._tap.called:
                if cursor["i"] < len(first):
                    item = first[cursor["i"]]
                    cursor["i"] += 1
                    return item
                return bad
            if cursor["j"] < len(second):
                item = second[cursor["j"]]
                cursor["j"] += 1
                return item
            return bad

        driver._observe.side_effect = observe
        return bad

    def test_unconfirmed_dialog_retries_the_menu_exactly_once(self):
        driver = self.driver()
        bad = self._dialog_script(driver, [self.home, self.help, self.recovery])
        before = Mock()
        with self.assertRaisesRegex(EaCacheCleanupFailed, "未确认 App Recovery 清缓存弹窗"):
            driver.clear_cache(before_clear=before)
        before.assert_not_called()
        self.assertEqual(driver._tap.call_count, 1)
        driver._tap.assert_called_once_with(VK_ESCAPE)
        help_clicks = [call for call in driver._click_point.call_args_list if call.args[1:] == (150, 250)]
        recovery_clicks = [call for call in driver._click_point.call_args_list if call.args[1:] == (340, 350)]
        self.assertEqual(len(help_clicks), 2)
        self.assertEqual(len(recovery_clicks), 2)
        self.assertNotIn((7, 700, 600), [call.args for call in driver._click_point.call_args_list])
        recorded = [
            call for call in driver._record.call_args_list
            if call.args[0] == "cache-clear-dialog-unconfirmed"
        ]
        self.assertEqual(len(recorded), 1)
        shot = recorded[0].args[1]
        self.assertIs(shot, bad)
        self.assertIsNotNone(shot.frame)
        self.assertEqual([token.text for token in shot.tokens], ["Library"])
        self.assertIn("cache-clear-dialog-unconfirmed", EA_STEPS)

    def test_dialog_found_on_the_single_retry_clears_once(self):
        driver = self.driver()
        self._dialog_script(
            driver,
            [self.home, self.help, self.recovery, self.dialog, self.ready, self.ready],
        )
        before = Mock()
        self.assertTrue(driver.clear_cache(before_clear=before))
        before.assert_called_once()
        driver._tap.assert_called_once_with(VK_ESCAPE)
        help_clicks = [call for call in driver._click_point.call_args_list if call.args[1:] == (150, 250)]
        self.assertEqual(len(help_clicks), 2)
        self.assertEqual(
            [call.args for call in driver._click_point.call_args_list].count((7, 700, 600)),
            1,
        )
        steps = [call.args[0] for call in driver._record.call_args_list]
        self.assertNotIn("cache-clear-dialog-unconfirmed", steps)
        self.assertIn("cache-clear-requested", steps)

    def test_restart_app_menu_path_is_unchanged(self):
        driver = object.__new__(WindowsEaHybridDriver)
        frames = [
            observation(EaPage.SIGNED_IN, ("Apex Legends", 900, 400)),
            observation(EaPage.SIGNED_IN, ("Help", 150, 250)),
            observation(EaPage.SIGNED_IN, ("Restart app", 340, 350)),
        ]
        driver._observe = Mock(side_effect=frames)
        driver._click_point = Mock()
        driver._record = Mock()
        driver.sleep = lambda _seconds: None
        driver._request_restart_app(7)
        self.assertEqual(
            [call.args[1:] for call in driver._click_point.call_args_list],
            [(111, 114), (150, 250), (340, 350)],
        )
        self.assertEqual(
            [call.args[0] for call in driver._record.call_args_list],
            ["restart-menu-open", "restart-requested"],
        )


class CacheLeaseTest(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory())
        self.lease = FakeAccountProvider.lease("acct_1")
        self.provider = FakeAccountProvider([self.lease, FakeAccountProvider.lease("acct_2")])
        self.provider.claim("original", "LEVEL_TO_TARGET")
        self.store = AtomicCheckpointStore(Path(directory) / "checkpoint.json")
        self.store.save(OrchestrationCheckpoint(
            device_id="test", workflow_phase=WorkflowPhase.APEX_STOPPING,
            lease_id=self.lease.lease_id, lease_fence=self.lease.lease_fence,
            account_id=self.lease.account_id, target_level=20, active_play_run_id="run_1",
            result_status="TARGET_REACHED",
            target_reading={"level": 20, "xpCurrentApprox": 100, "xpRequiredApprox": 1000, "localSeq": 8},
            report_evidence={"runId": "run_1", "lobbyProgressSeq": 10, "runFinishedSeq": 12},
        ))
        self.log = []
        self.driver = FakeEaDriver(self.log, "ea_1")

    def build(self):
        worker = AccountOrchestrator(
            provider=self.provider, ea_driver=self.driver, play_session=Mock(),
            checkpoint_store=self.store, device_id="test", capture_source=object(),
            recover_report_drain=lambda _run: FakeDrain(accepted_through=12),
            lease_keeper_factory=lambda _provider, lease, **_kwargs: ScriptedLeaseKeeper(lease),
            sleep=lambda _seconds: None, notify=lambda _message: None,
        )
        self.addCleanup(worker.stop)
        return worker

    def clear(self, *, before_clear):
        self.assertEqual(self.log, ["apex.stop"])
        before_clear()
        self.assertEqual(self.store.load().ea_cache_cleanup, "REQUESTED")
        self.log.append("ea.clear_cache")
        return True

    def test_task_stops_game_clears_then_signs_out_before_close(self):
        self.driver.clear_cache = self.clear
        self.assertEqual(self.build().run_once().outcome, AccountCycleOutcome.COMPLETED)
        self.assertEqual(self.log, ["apex.stop", "ea.clear_cache", "ea.sign_out"])
        self.assertEqual(sum(name == "close" for name, _ in self.provider.calls), 1)
        self.assertIsNone(self.store.load().ea_cache_cleanup)

    def test_completed_cache_is_not_repeated_after_failed_signout_and_restart(self):
        self.driver.clear_cache = Mock(side_effect=self.clear)
        self.driver.sign_out = Mock(side_effect=[EaAppAutomationError("signout failed"), True])
        self.assertEqual(self.build().run_once().outcome, AccountCycleOutcome.PAUSED)
        self.assertEqual(self.store.load().ea_cache_cleanup, "COMPLETE")
        self.assertEqual(self.build().run_once().outcome, AccountCycleOutcome.COMPLETED)
        self.driver.clear_cache.assert_called_once()

    def test_cache_failure_before_click_falls_back_to_sign_out(self):
        self.driver.clear_cache = Mock(side_effect=EaCacheCleanupFailed("menu missing"))
        result = self.build().run_once()
        self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
        self.assertIsNone(result.error_code)
        self.assertEqual(self.log, ["apex.stop", "ea.sign_out"])
        close = next(args for name, args in self.provider.calls if name == "close")
        self.assertEqual(close[3], "TARGET_REACHED")
        self.assertIsNone(self.store.load().ea_cache_cleanup)

    def test_uncertain_clear_preserves_result_and_blocks_close_and_next_claim(self):
        def fail(*, before_clear):
            before_clear()
            raise EaCacheCleanupFailed("restart missing")
        self.driver.clear_cache = Mock(side_effect=fail)
        for _ in range(2):
            result = self.build().run_once()
            self.assertEqual(result.error_code, "EA_CACHE_CLEANUP_FAILED")
            checkpoint = self.store.load()
            self.assertEqual(checkpoint.lease_id, self.lease.lease_id)
            self.assertEqual(checkpoint.result_status, "TARGET_REACHED")
            self.assertEqual(checkpoint.ea_cache_cleanup, "REQUESTED")
        self.driver.clear_cache.assert_called_once()
        self.assertNotIn("ea.sign_out", self.log)
        self.assertEqual(sum(name == "close" for name, _ in self.provider.calls), 0)
        self.assertEqual(sum(name == "claim" for name, _ in self.provider.calls), 1)

    def test_crash_after_click_does_not_skip_or_repeat_uncertain_clear(self):
        self.store.save(self.store.load().evolve(workflow_phase=WorkflowPhase.EA_SIGNING_OUT,
                                               ea_cache_cleanup="REQUESTED"))
        self.driver.clear_cache = Mock()
        result = self.build().run_once()
        self.assertEqual(result.error_code, "EA_CACHE_CLEANUP_FAILED")
        self.driver.clear_cache.assert_not_called()
        self.assertNotIn("ea.sign_out", self.log)

    def test_old_checkpoint_and_roundtrip_keep_cache_state_separate_from_recovery(self):
        old = {"schemaVersion": 1, "deviceId": "test"}
        self.assertIsNone(OrchestrationCheckpoint.from_payload(old).ea_cache_cleanup)
        for state in (None, "REQUESTED", "COMPLETE", "SKIPPED_NOT_SIGNED_IN"):
            checkpoint = self.store.save(self.store.load().evolve(ea_cache_cleanup=state))
            self.assertEqual(self.store.load(), checkpoint)
        with self.assertRaises(ValueError):
            OrchestrationCheckpoint.from_payload({**old, "eaCacheCleanup": "unknown"})

    def test_skipped_cache_still_requires_signout_and_survives_restart(self):
        self.driver.clear_cache = Mock(return_value=False)
        self.driver.sign_out = Mock(side_effect=[False, True])
        self.assertEqual(self.build().run_once().outcome, AccountCycleOutcome.PAUSED)
        self.assertEqual(self.store.load().ea_cache_cleanup, "SKIPPED_NOT_SIGNED_IN")
        self.assertEqual(self.build().run_once().outcome, AccountCycleOutcome.COMPLETED)
        self.driver.clear_cache.assert_called_once()
        self.assertEqual(self.driver.sign_out.call_count, 2)

    def test_only_explicit_resume_permits_new_attempt_after_uncertain_clear(self):
        self.store.save(self.store.load().evolve(workflow_phase=WorkflowPhase.EA_SIGNING_OUT,
                                               ea_cache_cleanup="REQUESTED"))
        worker = self.build()
        self.assertEqual(worker.run_once().error_code, "EA_CACHE_CLEANUP_FAILED")
        self.assertFalse(worker.resume_if_safe())
        self.assertEqual(self.store.load().ea_cache_cleanup, "REQUESTED")
        self.assertTrue(worker.resume())
        self.assertIsNone(self.store.load().ea_cache_cleanup)
        self.assertEqual(worker.run_once().outcome, AccountCycleOutcome.COMPLETED)


if __name__ == "__main__":
    unittest.main()

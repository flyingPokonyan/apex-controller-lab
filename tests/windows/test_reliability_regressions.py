"""Incident regressions: uncertain OCR, transient telemetry and safe retry."""
from dataclasses import replace
from concurrent.futures import Future
from datetime import datetime, timezone
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
import managed_launcher as launcher
from apex_automation.account_orchestrator import AccountOrchestrator, AccountCycleOutcome, AccountCycleResult
from apex_automation.account_provider import FakeAccountProvider, OtpMethod
from apex_automation.ea_app import EaIdentityFact, EaIdentityMismatch, EaIdentityUnconfirmed
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_pages import EaPage
from apex_automation.managed_runtime import ManagedRuntime, RETRY_EXIT
from apex_automation.orchestration_state import AtomicCheckpointStore, OrchestrationCheckpoint, OrchestratorRunState, WorkflowPhase


class IdentityEvidenceTest(unittest.TestCase):
    def driver(self, identities, pages=None):
        driver = object.__new__(WindowsEaHybridDriver)
        clock = [0.0]
        observation = EaObservation((0, 0, 1920, 1080), np.zeros((1, 1)), (), EaPage.SIGNED_IN)
        driver._ea_window = lambda: 1
        driver._observe = Mock(side_effect=[replace(observation, page=p) for p in pages]) if pages else Mock(return_value=observation)
        driver._dismiss_library_tour = lambda _hwnd, item: item
        driver._raise_if_account_banned = Mock()
        driver._matching_identity = Mock(return_value=None)
        driver._identity = Mock(side_effect=identities) if isinstance(identities, list) else Mock(return_value=identities)
        driver._record = Mock()
        driver.sleep = lambda seconds: clock.__setitem__(0, clock[0] + seconds)
        self.enterContext(patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: clock[0]))
        return driver

    def fact(self, value, trusted=True):
        return EaIdentityFact(value, "fixture", trusted)

    def test_three_different_ocr_guesses_do_not_prove_wrong_account(self):
        driver = self.driver([self.fact(x) for x in ("other-a", "other-b", "other-c", "wanted")])
        self.assertEqual(driver.verify_identity("wanted").ea_account_id, "wanted")

    def test_low_confidence_and_unknown_page_do_not_prove_mismatch(self):
        for trusted, page in ((False, EaPage.SIGNED_IN), (True, EaPage.UNKNOWN)):
            with self.subTest(trusted=trusted, page=page):
                driver = self.driver([self.fact("other", trusted)] * 3 + [self.fact("wanted")], [page] * 4)
                self.assertTrue(driver.verify_identity("wanted").verified)

    def test_three_consistent_trusted_wrong_badges_still_reject(self):
        driver = self.driver(self.fact("wrong-account"))
        with self.assertRaises(EaIdentityMismatch):
            driver.verify_identity("wanted")
        self.assertEqual(driver._identity.call_count, 3)

    def test_authentication_page_never_verifies_even_if_expected_text_is_visible(self):
        driver = self.driver(self.fact("wanted"), [EaPage.PASSWORD] * 20)
        driver._matching_identity.return_value = self.fact("wanted")
        with self.assertRaises(EaIdentityUnconfirmed):
            driver.verify_identity("wanted")
        driver._matching_identity.assert_not_called()
        driver._identity.assert_not_called()

    def test_unreadable_badge_has_uncertain_reason_not_wrong_account(self):
        driver = self.driver(None)
        with self.assertRaises(EaIdentityUnconfirmed) as caught:
            driver.verify_identity("wanted")
        self.assertEqual(caught.exception.reason_code, "IDENTITY_UNCONFIRMED")

    def test_login_does_not_accept_low_confidence_corner_text(self):
        driver = self.driver([self.fact("noise", False), self.fact("wanted")])
        driver._observe.return_value = replace(driver._observe.return_value, page=EaPage.SIGNED_IN)
        result = driver._await_identity(1, Mock(), otp_methods=(OtpMethod.TOTP,),
                                       initial_challenge_started_at=datetime.now(timezone.utc))
        self.assertEqual(result.ea_account_id, "wanted")
        self.assertEqual(driver._identity.call_count, 2)


class ManagedRecoveryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.runtime = ManagedRuntime(self.root, "current")
        self.app = launcher.Launcher(SimpleNamespace(root=self.root), root=self.root)
        self.app.session = "current"

    def orchestrator(self, checkpoint=None):
        store = AtomicCheckpointStore(self.root / "checkpoint.json")
        if checkpoint:
            store.save(checkpoint)
        return AccountOrchestrator(provider=FakeAccountProvider(), ea_driver=Mock(), play_session=Mock(),
            checkpoint_store=store, device_id="test", capture_source=object(), maintenance=self.runtime,
            sleep=Mock(side_effect=AssertionError("Unexpected retry")), notify=lambda _: None)

    def worker_exit(self, code):
        self.app.worker = Mock(pid=123)
        self.app.worker.poll.return_value = code
        self.app.state["running"] = "old"
        launcher.write_json(self.root / "worker.json", {
            "session": "current", "pid": 456, "reason": "EA_UI_UNKNOWN", "at": time.time()})

    def test_environment_failures_continue_after_three_with_bounded_persisted_cooldown(self):
        for _ in range(8):
            self.worker_exit(RETRY_EXIT)
            self.app.monitor("old")
        self.assertEqual(self.app.state["stage"], "WAITING_RETRY")
        self.assertEqual(self.app.state["error"], "EA_UI_UNKNOWN")
        self.assertEqual(self.app.state["failures"], {})
        self.assertLessEqual(self.app.state["retryAfter"] - time.time(), 300)
        reloaded = launcher.Launcher(SimpleNamespace(root=self.root), root=self.root)
        self.assertFalse(reloaded.can_start("old"))
        reloaded.next_start = 0
        self.assertTrue(reloaded.can_start("old"))

    def test_unknown_crashes_still_stop_after_three(self):
        for _ in range(3):
            self.worker_exit(1)
            self.app.monitor("old")
        self.app.next_start = 0
        self.assertFalse(self.app.can_start("old"))

    def test_first_fetch_after_launcher_restart_preserves_environment_cooldown(self):
        self.app.state.update(installed="old", retryAfter=time.time() + 240)
        self.app.next_start = time.monotonic() + 240
        self.app.repo = Mock(root=self.root)
        self.app.repo.head.return_value = "old"
        future = Future()
        future.set_result("old")
        executor = Mock()
        executor.submit.return_value = future
        with patch.object(launcher, "ThreadPoolExecutor", return_value=executor), \
             patch.object(launcher.time, "sleep", side_effect=KeyboardInterrupt), \
             patch.object(self.app, "start") as start:
            self.app.run()
        start.assert_not_called()

    def test_safely_closed_ea_errors_do_not_exit_worker(self):
        app = self.orchestrator()
        app.run_once = Mock(side_effect=[
            AccountCycleResult(AccountCycleOutcome.COMPLETED, error_code=reason)
            for reason in ("IDENTITY_MISMATCH", "EA_UI_UNKNOWN", "APEX_START_FAILED", "OTP_TIMEOUT")
        ] + [AccountCycleResult(AccountCycleOutcome.STOPPED)])
        app.sleep = Mock()
        self.assertEqual(app.run_forever(), 0)
        self.assertEqual([c.args[0] for c in app.sleep.call_args_list], [30, 60, 120, 240])
        self.assertEqual(app.run_once.call_count, 5)

    def test_completed_result_cannot_bypass_an_unclosed_checkpoint(self):
        app = self.orchestrator(OrchestrationCheckpoint(device_id="test", lease_id="lease-old", lease_fence=2))
        app.run_once = Mock(return_value=AccountCycleResult(AccountCycleOutcome.COMPLETED, error_code="EA_UI_UNKNOWN"))
        self.assertEqual(app.run_forever(), 1)
        self.assertEqual(app.run_once.call_count, 1)

    def test_recoverable_cleanup_pause_preserves_lease_for_next_process(self):
        app = self.orchestrator(OrchestrationCheckpoint(device_id="test", lease_id="lease-old", lease_fence=2,
            workflow_phase=WorkflowPhase.EA_SIGNING_OUT, run_state=OrchestratorRunState.PAUSED_MANUAL,
            last_error_code="EA_SIGNOUT_FAILED"))
        app.run_once = Mock(return_value=AccountCycleResult(AccountCycleOutcome.PAUSED, error_code="EA_SIGNOUT_FAILED"))
        self.assertEqual(app.run_forever(), RETRY_EXIT)
        self.assertEqual(app.checkpoint_store.load().lease_id, "lease-old")

    def test_heartbeat_write_collision_does_not_abort_game_or_disable_operator_stop(self):
        with patch("apex_automation.managed_runtime.replace_with_retry", side_effect=PermissionError):
            self.runtime.pulse("APEX_PLAYING")
            with patch.object(self.runtime, "operator_abort", return_value=True):
                with self.assertRaises(KeyboardInterrupt):
                    self.runtime.pulse(boundary=True)
        self.assertTrue(self.runtime.operator_stopped)

    def test_responsive_cleanup_is_not_killed_after_ninety_seconds(self):
        self.worker_exit(None)
        self.app.started = time.time() - 1000
        self.app.request_mode = "recover"
        self.app.requested_at = time.monotonic() - 100
        launcher.write_json(self.root / "worker.json", {"session": "current", "pid": 456,
            "at": time.time(), "phase": "EA_SIGNING_OUT"})
        with patch.object(launcher, "terminate_owned") as terminate:
            self.app.monitor("old")
            terminate.assert_not_called()
            self.app.requested_at = time.monotonic() - 601
            self.app.monitor("old")
            terminate.assert_called_once()

    def test_long_game_download_with_fresh_heartbeat_waits_for_boundary(self):
        self.assertEqual(launcher.update_mode({"phase": "APEX_STARTING", "at": 3600},
            now=3601, started=0, phase_started=0), ("boundary", None))


if __name__ == "__main__":
    unittest.main()

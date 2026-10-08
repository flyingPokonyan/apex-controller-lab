from __future__ import annotations

import json
from concurrent.futures import Future
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
import managed_launcher as launcher
from apex_automation.managed_runtime import ManagedRuntime, ManagedUpdateRequested
from apex_automation.instance_lock import SingleInstanceLock, AlreadyRunningError
from apex_automation.account_orchestrator import AccountOrchestrator, AccountCycleOutcome, AccountCycleResult
from apex_automation.account_provider import FakeAccountProvider, LeaseState, LeaseStatus
from apex_automation.orchestration_state import AtomicCheckpointStore, OrchestrationCheckpoint, OrchestratorRunState, WorkflowPhase


class RuntimeTest(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.runtime = ManagedRuntime(self.root, "session-1")

    def request(self, mode, session="session-1"):
        launcher.write_json(self.root / "request.json", {"session": session, "mode": mode})

    def test_normal_update_waits_for_boundary(self):
        self.request("boundary")
        self.runtime.pulse("APEX_PLAYING")
        self.assertFalse(self.runtime.requested)
        with self.assertRaises(ManagedUpdateRequested):
            self.runtime.boundary()

    def test_recovery_update_interrupts_without_erasing_checkpoint(self):
        checkpoint = self.root / "account-cycle-status.json"
        checkpoint.write_text('{"leaseId":"existing"}')
        self.request("recover")
        with self.assertRaises(ManagedUpdateRequested):
            self.runtime.pulse("EA_SIGNING_OUT")
        self.assertEqual(checkpoint.read_text(), '{"leaseId":"existing"}')

    def test_previous_session_request_is_ignored(self):
        self.request("recover", session="old")
        self.runtime.boundary()
        self.assertFalse(self.runtime.requested)

    def test_background_heartbeat_cannot_disguise_main_thread_hang(self):
        thread = threading.Thread(target=lambda: self.runtime.pulse("RENEWING"))
        thread.start()
        thread.join()
        self.assertFalse((self.root / "worker.json").exists())

    def test_operator_stop_takes_precedence_over_pending_update(self):
        self.request("recover")
        self.runtime.stop_by_operator()
        self.runtime.boundary()
        self.assertTrue(launcher.read_json(self.root / "worker.json")["operatorStopped"])
        self.assertFalse(self.runtime.requested)

    def test_f8_during_ea_workflow_is_operator_stop_not_a_retryable_failure(self):
        with patch.object(self.runtime, "operator_abort", return_value=True):
            with self.assertRaises(KeyboardInterrupt):
                self.runtime.pulse("EA_SIGNING_IN")
        self.assertTrue(launcher.read_json(self.root / "worker.json")["operatorStopped"])

    def orchestrator(self, checkpoint=None, provider=None):
        store = AtomicCheckpointStore(self.root / "checkpoint.json")
        if checkpoint:
            store.save(checkpoint)
        return AccountOrchestrator(provider=provider or FakeAccountProvider(), ea_driver=Mock(),
                                   play_session=Mock(), checkpoint_store=store, device_id="dev",
                                   capture_source=object(), maintenance=self.runtime, notify=lambda _: None)

    def test_update_gate_prevents_next_claim(self):
        provider = FakeAccountProvider()
        orchestrator = self.orchestrator(provider=provider)
        self.request("boundary")
        with self.assertRaises(ManagedUpdateRequested):
            orchestrator.run_once()
        self.assertFalse(any(name == "claim" for name, _ in provider.calls))

    def test_ui_pause_is_not_cleared_by_continuous_loop(self):
        orchestrator = self.orchestrator(OrchestrationCheckpoint(
            device_id="dev", run_state=OrchestratorRunState.PAUSED_MANUAL,
            last_error_code="KNOWN_STATE_STALL_UNRECOVERED"))
        self.assertEqual(orchestrator.run_forever(), 1)
        self.assertEqual(orchestrator.checkpoint_store.load().last_error_code, "KNOWN_STATE_STALL_UNRECOVERED")

    def test_new_worker_finishes_old_update_then_keeps_running(self):
        orchestrator = self.orchestrator()
        orchestrator.run_once = Mock(side_effect=[
            AccountCycleResult(AccountCycleOutcome.STOPPED, error_code="UPDATE_REQUESTED"),
            AccountCycleResult(AccountCycleOutcome.STOPPED, error_code="OPERATOR_STOPPED"),
        ])
        self.assertEqual(orchestrator.run_forever(), 0)
        self.assertEqual(orchestrator.run_once.call_count, 2)

    def test_cleanup_after_restart_does_not_consume_failure_budget(self):
        orchestrator = self.orchestrator()
        orchestrator.sleep = lambda _: None
        orchestrator.run_once = Mock(side_effect=[
            AccountCycleResult(AccountCycleOutcome.COMPLETED, error_code="RESTART_RECOVERY"),
            AccountCycleResult(AccountCycleOutcome.STOPPED),
        ])
        self.assertEqual(orchestrator.run_forever(), 0)
        self.assertEqual(orchestrator.run_once.call_count, 2)
        self.assertEqual(self.runtime.completed, 0)

    def test_expired_interrupted_lease_enters_cleanup_not_new_claim(self):
        lease = FakeAccountProvider.lease("acct")
        provider = FakeAccountProvider([lease])
        provider.claim("original", "LEVEL_TO_TARGET")
        provider._statuses[lease.lease_id] = LeaseStatus(
            lease_id=lease.lease_id, lease_fence=lease.lease_fence, account_id=lease.account_id,
            state=LeaseState.EXPIRED_UNCONFIRMED, provider_status="EXPIRED_UNCONFIRMED")
        orchestrator = self.orchestrator(OrchestrationCheckpoint(
            device_id="dev", lease_id=lease.lease_id, lease_fence=lease.lease_fence,
            account_id=lease.account_id, workflow_phase=WorkflowPhase.APEX_PLAYING), provider)
        orchestrator._cleanup_and_close_preplay_failure = Mock(return_value=AccountCycleResult(AccountCycleOutcome.COMPLETED))
        orchestrator.run_once()
        orchestrator._cleanup_and_close_preplay_failure.assert_called_once_with(lease, "RESTART_RECOVERY")
        self.assertEqual(sum(name == "claim" for name, _ in provider.calls), 1)


class LauncherTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.repo = Mock(root=self.root)
        self.app = launcher.Launcher(self.repo)

    def worker(self, code=None):
        self.app.worker = Mock(pid=123)
        self.app.worker.poll.return_value = code
        self.app.state["running"] = "old"
        self.app.state["stage"] = "WORKER_STARTING"
        self.app.session = "current"
        self.app.started = self.app.phase_started = time.time()
        return self.app.worker

    def test_failure_budget_survives_restart_and_new_commit_can_run(self):
        for _ in range(3):
            self.app.failure("bad")
        self.app.save("WAITING_FIX")
        reloaded = launcher.Launcher(self.repo)
        self.assertFalse(reloaded.can_start("bad"))
        self.assertTrue(reloaded.can_start("fixed"))

    def test_operator_exit_stays_stopped_even_when_new_commit_arrives(self):
        self.worker(code=0)
        self.app.monitor("new")
        self.assertTrue(self.app.state["operatorStopped"])
        self.assertFalse(self.app.can_start("new"))

    def test_operator_stop_survives_cleanup_error_and_fast_exit(self):
        self.worker(code=1)
        launcher.write_json(self.app.root / "worker.json", {
            "session": "current", "pid": 123, "operatorStopped": True})
        self.app.monitor("new")
        self.assertTrue(self.app.state["operatorStopped"])
        self.assertFalse(self.app.can_start("new"))
        self.assertEqual(self.app.state["failures"], {})

    def test_healthy_game_is_not_interrupted_by_phase_duration(self):
        mode, reason = launcher.update_mode({"phase": "APEX_PLAYING", "at": 9999},
                                           now=10000, started=0, phase_started=0)
        self.assertEqual((mode, reason), ("boundary", None))

    def test_confirmed_stall_allows_early_update(self):
        self.assertEqual(launcher.update_mode({"phase": "APEX_PLAYING", "at": 9, "blocked": True},
                         now=10, started=0, phase_started=0)[0], "recover")

    def test_shutdown_request_is_not_withdrawn_by_a_later_good_frame(self):
        self.app.session = "session"
        self.app.request("recover")
        deadline = self.app.requested_at
        self.app.request("boundary")
        self.assertEqual(self.app.request_mode, "recover")
        self.assertEqual(self.app.requested_at, deadline)

    def test_background_lease_activity_does_not_prevent_hang_detection(self):
        self.assertEqual(launcher.update_mode({"phase": "APEX_PLAYING", "at": 1},
                         now=400, started=0, phase_started=0), ("recover", "WORKER_UNRESPONSIVE"))

    def test_successful_start_without_completed_account_does_not_reset_budget(self):
        self.worker()
        self.app.state["failures"]["old"] = 2
        self.app.monitor("old")
        self.assertEqual(self.app.state["failures"]["old"], 2)

    def test_spawned_worker_is_not_reported_as_responding_before_heartbeat(self):
        with patch.object(launcher.subprocess, "Popen", return_value=Mock(pid=123)), patch.object(launcher, "WorkerJob"):
            self.app.start("new")
        self.assertEqual(self.app.state["stage"], "WORKER_STARTING")
        self.assertIsNone(self.app.state["workerConfirmedAt"])

    def test_current_heartbeat_confirms_version_and_persists_phase(self):
        self.worker()
        now = time.time()
        launcher.write_json(self.app.root / "worker.json", {
            "session": "current", "pid": 123, "at": now, "phase": "APEX_PLAYING"})
        self.app.monitor("old")
        saved = launcher.read_json(self.app.state_path)
        self.assertEqual(saved["stage"], "RUNNING")
        self.assertEqual(saved["workerHeartbeatAt"], now)
        self.assertEqual(saved["workerPhase"], "APEX_PLAYING")
        self.assertIsNotNone(saved["workerConfirmedAt"])

    def test_stale_heartbeat_cannot_confirm_new_worker(self):
        self.worker()
        launcher.write_json(self.app.root / "worker.json", {
            "session": "current", "pid": 123, "at": time.time() - 60, "phase": "APEX_PLAYING"})
        self.app.monitor("old")
        self.assertEqual(self.app.state["stage"], "WORKER_STARTING")
        self.assertIsNone(self.app.state.get("workerConfirmedAt"))

    def test_only_successful_account_resets_budget(self):
        self.worker()
        self.app.state["failures"]["old"] = 2
        launcher.write_json(self.app.root / "worker.json", {
            "session": "current", "pid": 123, "at": time.time(), "completed": 1})
        self.app.monitor("old")
        self.assertEqual(self.app.state["failures"]["old"], 0)

    def test_old_session_status_cannot_reset_failures_or_stop_worker(self):
        self.worker()
        self.app.state["failures"]["old"] = 2
        launcher.write_json(self.app.root / "worker.json", {
            "session": "previous", "pid": 123, "completed": 100, "operatorStopped": True})
        self.app.monitor("old")
        self.assertEqual(self.app.state["failures"]["old"], 2)
        self.assertFalse(self.app.state.get("operatorStopped"))

    def test_unresponsive_recovery_without_new_code_consumes_retry_budget(self):
        self.worker(code=75)
        self.app.request_mode = "recover"
        self.app.monitor("old")
        self.assertEqual(self.app.state["failures"]["old"], 1)

    def test_new_commit_handoff_is_not_counted_as_failure(self):
        self.worker(code=75)
        self.app.request_mode = "boundary"
        self.app.monitor("new")
        self.assertEqual(self.app.state["failures"], {})
        self.assertEqual(self.app.state["stage"], "UPDATE_READY")

    def test_existing_manual_runner_does_not_consume_failure_budget(self):
        self.worker(code=launcher.BUSY_EXIT)
        self.app.monitor("old")
        self.assertEqual(self.app.state["failures"], {})
        self.assertEqual(self.app.state["stage"], "WAITING_LOCAL_RUNNER")

    def test_same_lock_excludes_runner_and_updater(self):
        path = self.root / "windows/runs/play.lock"
        with launcher.FileLock(path):
            with self.assertRaises(AlreadyRunningError):
                SingleInstanceLock(path).acquire()
        with SingleInstanceLock(path):
            with self.assertRaises(launcher.LauncherError):
                self.app.install("new")
        self.repo.fast_forward.assert_not_called()

    def test_install_refuses_while_owned_worker_exists(self):
        self.worker()
        with self.assertRaises(launcher.LauncherError):
            self.app.install("new")
        self.repo.fast_forward.assert_not_called()

    def test_interrupted_install_is_persisted_and_retried(self):
        self.repo.install.side_effect = [launcher.LauncherError("PIP_FAILED"), "dep-hash"]
        outbox = self.root / "windows/runs/run-1/report-outbox.jsonl"
        outbox.parent.mkdir(parents=True)
        outbox.write_text('{"seq":1}')
        with self.assertRaises(launcher.LauncherError):
            self.app.install("new")
        reloaded = launcher.Launcher(self.repo)
        self.assertTrue(reloaded.state["installPending"])
        self.assertNotIn("installed", reloaded.state)
        reloaded.install("fixed")
        self.assertFalse(reloaded.state["installPending"])
        self.assertEqual(reloaded.state["installed"], "fixed")
        self.assertEqual(outbox.read_text(), '{"seq":1}')

    def test_failed_install_does_not_prevent_fetching_a_later_fix(self):
        class Finished(BaseException):
            pass

        class ImmediateExecutor:
            def __init__(self, **_):
                pass

            def submit(self, function):
                future = Future()
                try:
                    future.set_result(function())
                except Exception as error:
                    future.set_exception(error)
                return future

            def shutdown(self, **_):
                pass

        head = ["old"]
        clock = [0]
        self.repo.head.side_effect = lambda: head[0]
        self.repo.fetch.side_effect = ["bad", "fixed"]
        self.repo.fast_forward.side_effect = lambda target: head.__setitem__(0, target)
        self.repo.install.side_effect = [launcher.LauncherError("PIP_FAILED"), "dependencies"]
        self.app.start = Mock(side_effect=Finished())
        with (
            patch.object(launcher, "ThreadPoolExecutor", ImmediateExecutor),
            patch.object(launcher.time, "monotonic", side_effect=lambda: clock[0]),
            patch.object(launcher.time, "sleep", side_effect=lambda _: clock.__setitem__(0, clock[0] + 60)),
        ):
            with self.assertRaises(Finished):
                self.app.run()
        self.assertEqual(self.repo.fetch.call_count, 2)
        self.app.start.assert_called_once_with("fixed")
        self.assertEqual(self.app.state["installed"], "fixed")
        self.assertFalse(self.app.state["installPending"])

    def test_update_only_waits_for_fetch_instead_of_reporting_old_version_as_current(self):
        future = Future()
        executor = Mock()
        executor.submit.return_value = future
        self.app.state["installed"] = "old"
        self.repo.head.return_value = "old"
        self.repo.install.return_value = "deps"
        self.app.start = Mock()
        with (
            patch.object(launcher, "ThreadPoolExecutor", return_value=executor),
            patch.object(launcher.time, "sleep", side_effect=lambda _: future.set_result("new")),
        ):
            self.assertEqual(self.app.run(update_only=True), 0)
        self.repo.fast_forward.assert_called_once_with("new")
        self.app.start.assert_not_called()

    def test_update_only_reports_fetch_failure(self):
        future = Future()
        future.set_exception(launcher.LauncherError("OFFLINE"))
        executor = Mock()
        executor.submit.return_value = future
        self.app.state["installed"] = "old"
        self.repo.head.return_value = "old"
        with patch.object(launcher, "ThreadPoolExecutor", return_value=executor):
            self.assertEqual(self.app.run(update_only=True), 1)
        self.repo.install.assert_not_called()

    def test_updated_launcher_is_reloaded_without_starting_old_worker(self):
        future = Future()
        future.set_result("new")
        executor = Mock()
        executor.submit.return_value = future
        self.repo.head.return_value = "old"
        self.repo.install.return_value = "deps"
        self.app.start = Mock()
        with (
            patch.object(launcher, "ThreadPoolExecutor", return_value=executor),
            patch.object(self.app, "needs_reload", return_value=True),
        ):
            self.assertEqual(self.app.run(), launcher.RELOAD_EXIT)
        self.app.start.assert_not_called()
        self.assertEqual(self.app.state["installed"], "new")

    def test_broken_version_publisher_cannot_prevent_installing_fix(self):
        future = Future()
        future.set_result("new")
        executor = Mock()
        executor.submit.return_value = future
        self.repo.head.return_value = "old"
        self.repo.install.return_value = "deps"
        self.app.publisher = Mock()
        self.app.publisher.start.side_effect = RuntimeError("broken telemetry")
        with patch.object(launcher, "ThreadPoolExecutor", return_value=executor):
            self.assertEqual(self.app.run(update_only=True), 0)
        self.assertEqual(self.app.state["installed"], "new")


class GitUpdateTest(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = root / "source"
        self.source.mkdir()
        self.git(self.source, "init", "-b", "main")
        self.git(self.source, "config", "user.email", "tests@example.test")
        self.git(self.source, "config", "user.name", "Tests")
        (self.source / "code.txt").write_text("old")
        self.git(self.source, "add", "code.txt")
        self.git(self.source, "commit", "-m", "initial")
        self.clone = root / "clone"
        self.git(root, "clone", str(self.source), str(self.clone))
        self.repo = launcher.Repository(self.clone)
        (self.source / "code.txt").write_text("new")
        self.git(self.source, "commit", "-am", "update")

    @staticmethod
    def git(root, *args):
        return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL).decode().strip()

    def test_fetch_does_not_change_live_code_then_exact_commit_is_applied(self):
        old = self.repo.head()
        target = self.repo.fetch()
        self.assertNotEqual(old, target)
        self.assertEqual((self.clone / "code.txt").read_text(), "old")
        private = self.clone / "windows/account-cycle.private.json"
        private.parent.mkdir()
        private.write_text("private-config-placeholder")
        self.repo.fast_forward(target)
        self.assertEqual(self.repo.head(), target)
        self.assertEqual((self.clone / "code.txt").read_text(), "new")
        self.assertEqual(private.read_text(), "private-config-placeholder")

    def test_local_changes_are_not_discarded(self):
        target = self.repo.fetch()
        (self.clone / "code.txt").write_text("local modification")
        with self.assertRaisesRegex(launcher.LauncherError, "TRACKED_LOCAL_CHANGES"):
            self.repo.fast_forward(target)
        self.assertEqual((self.clone / "code.txt").read_text(), "local modification")

    def test_local_commits_are_not_reset(self):
        self.git(self.clone, "config", "user.email", "tests@example.test")
        self.git(self.clone, "config", "user.name", "Tests")
        (self.clone / "local.txt").write_text("local")
        self.git(self.clone, "add", "local.txt")
        self.git(self.clone, "commit", "-m", "local")
        head = self.repo.head()
        with self.assertRaisesRegex(launcher.LauncherError, "BRANCH_DIVERGED"):
            self.repo.fetch()
        self.assertEqual(self.repo.head(), head)

    def test_recreated_environment_installs_even_when_dependency_hash_is_unchanged(self):
        requirements = self.clone / "windows/requirements.txt"
        requirements.parent.mkdir()
        requirements.write_text("example-dependency==1.0\n")
        with patch.object(launcher, "run_command", return_value="") as command:
            self.repo.install({"dependencies": self.repo.dependency_hash()})
        self.assertEqual(command.call_args_list[0].args[0][1:3], ["-m", "venv"])
        self.assertIn("pip", command.call_args_list[1].args[0])

    def test_interrupted_recreated_environment_retries_pip_despite_old_launcher_cache(self):
        requirements = self.clone / "windows/requirements.txt"
        requirements.parent.mkdir()
        requirements.write_text("example-dependency==1.0\n")
        state = {"dependencies": self.repo.dependency_hash()}

        def interrupted(args, **_):
            if "venv" in args:
                self.repo.worker_python.parent.mkdir(parents=True)
                self.repo.worker_python.touch()
            elif "pip" in args:
                raise launcher.LauncherError("PIP_INTERRUPTED")
            return ""

        with patch.object(launcher, "run_command", side_effect=interrupted):
            with self.assertRaises(launcher.LauncherError):
                self.repo.install(state)
        with patch.object(launcher, "run_command", return_value="") as command:
            self.repo.install(state)
        self.assertIn("pip", command.call_args_list[0].args[0])

    def test_successful_dependency_marker_skips_pip_when_requirements_unchanged(self):
        requirements = self.clone / "windows/requirements.txt"
        requirements.parent.mkdir()
        requirements.write_text("example-dependency==1.0\n")
        self.repo.worker_python.parent.mkdir(parents=True)
        self.repo.worker_python.touch()
        digest = self.repo.dependency_hash()
        launcher.write_json(self.repo.worker_python.parents[1] / ".managed-dependencies.json", {"requirements": digest})
        with patch.object(launcher, "run_command", return_value="") as command:
            self.repo.install({"dependencies": digest})
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][-1], "import apex_automation.cli")


if __name__ == "__main__":
    unittest.main()

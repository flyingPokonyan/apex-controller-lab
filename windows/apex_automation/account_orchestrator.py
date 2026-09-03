from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import threading
import time
from typing import Callable, Protocol
import uuid

from .account_provider import (
    AccountLease,
    AccountProvider,
    CleanupEvidence,
    CompletionEvidence,
    DEFAULT_TASK_TYPE,
    IdempotencyConflictError,
    LeaseProviderError,
    LeaseState,
    LeaseStaleError,
    OtpCode,
)
from .ea_app import (
    EaAppAutomationError,
    EaApexDownloadRequired,
    EaAppDriver,
    EaIdentityFact,
    OtpChallenge,
)
from .lease_keeper import LeaseKeeper, LeaseKeeperSnapshot, LeaseKeeperState
from .orchestration_state import (
    AtomicCheckpointStore,
    OrchestrationCheckpoint,
    OrchestratorRunState,
    PendingOperation,
    WorkflowPhase,
)
from .play_session import (
    PlaySessionResult,
    ReportDrainHandle,
    SessionIdentity,
)
from .progression_policy import TargetLevelPolicy
from .runner_identity import IdentityVerification


LEASE_RECOVERY_GRACE_S = 60.0
LEASE_RECOVERY_POLL_S = 5.0


class AccountCycleOutcome(str, Enum):
    COMPLETED = "COMPLETED"
    NO_ACCOUNT = "NO_ACCOUNT"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class RemoteLeaseRecoveryRequired(RuntimeError):
    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


class ManagedPlaySession(Protocol):
    def run(
        self,
        identity: SessionIdentity,
        progression_policy: TargetLevelPolicy,
        capture_source: object,
        *,
        lease_is_current: Callable[[], bool],
        on_run_started: Callable[[str], None],
    ) -> PlaySessionResult: ...


@dataclass(frozen=True)
class AccountCycleResult:
    outcome: AccountCycleOutcome
    lease_id: str | None = None
    run_id: str | None = None
    error_code: str | None = None


class AccountOrchestrator:
    """Crash-aware outer workflow for one leased EA account at a time."""

    SERVER_RELEASABLE_PAUSES = frozenset(
        {
            "LEASE_STALE",
            "LEASE_NOT_ACTIVE",
            "LEASE_CLOSE_PENDING",
            "COMPLETION_RECOVERY_REQUIRED",
            "ORPHAN_RUN_RECOVERY_REQUIRED",
            "REMOTE_RUNNING_RECOVERY_REQUIRED",
            "REMOTE_COMPLETION_PENDING_RECOVERY_REQUIRED",
            "REMOTE_EXPIRED_UNCONFIRMED_RECOVERY_REQUIRED",
        }
    )

    def __init__(
        self,
        *,
        provider: AccountProvider,
        ea_driver: EaAppDriver,
        play_session: ManagedPlaySession,
        checkpoint_store: AtomicCheckpointStore,
        device_id: str,
        capture_source: object,
        task_type: str = DEFAULT_TASK_TYPE,
        lease_keeper_factory: Callable[..., LeaseKeeper] = LeaseKeeper,
        operation_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        recover_report_drain: (
            Callable[[str], ReportDrainHandle | None] | None
        ) = None,
        sleep: Callable[[float], None] = time.sleep,
        completion_poll_s: float = 2.0,
        notify: Callable[[str], None] = print,
    ) -> None:
        self.provider = provider
        self.ea_driver = ea_driver
        self.play_session = play_session
        self.checkpoint_store = checkpoint_store
        self.device_id = device_id
        self.capture_source = capture_source
        self.task_type = task_type
        self.lease_keeper_factory = lease_keeper_factory
        self.operation_id_factory = operation_id_factory
        self.recover_report_drain = recover_report_drain
        self.sleep = sleep
        self.completion_poll_s = max(0.1, completion_poll_s)
        self.notify = notify
        self._checkpoint_lock = threading.Lock()
        self._provider_operation_lock = threading.RLock()
        self._checkpoint = self._load_checkpoint()
        self._stop = threading.Event()
        self._lease_keeper: LeaseKeeper | None = None
        self._report_drain: ReportDrainHandle | None = None

    def _load_checkpoint(self) -> OrchestrationCheckpoint:
        checkpoint = self.checkpoint_store.load()
        if checkpoint is None:
            return self.checkpoint_store.save(
                OrchestrationCheckpoint(device_id=self.device_id)
            )
        if checkpoint.device_id != self.device_id:
            raise ValueError("checkpoint 的 deviceId 与当前设备不一致")
        return checkpoint

    def _update_checkpoint(self, **changes: object) -> OrchestrationCheckpoint:
        with self._checkpoint_lock:
            self._checkpoint = self.checkpoint_store.save(
                self._checkpoint.evolve(**changes)
            )
            return self._checkpoint

    def _phase(self) -> str:
        with self._checkpoint_lock:
            return self._checkpoint.workflow_phase.value

    def _active_run_id(self) -> str | None:
        with self._checkpoint_lock:
            return self._checkpoint.active_play_run_id

    def _begin_operation(self, operation: PendingOperation) -> str:
        checkpoint = self._checkpoint
        if (
            checkpoint.pending_operation is operation
            and checkpoint.operation_id is not None
        ):
            return checkpoint.operation_id
        operation_id = self.operation_id_factory()
        self._update_checkpoint(
            pending_operation=operation,
            operation_id=operation_id,
        )
        return operation_id

    def _finish_operation(self) -> None:
        self._update_checkpoint(
            pending_operation=None,
            operation_id=None,
        )

    def _pause(
        self,
        error_code: str,
        *,
        manual: bool,
    ) -> AccountCycleResult:
        checkpoint = self._update_checkpoint(
            run_state=(
                OrchestratorRunState.PAUSED_MANUAL
                if manual
                else OrchestratorRunState.PAUSED_RETRYABLE
            ),
            resume_phase=self._checkpoint.workflow_phase,
            last_error_code=error_code,
        )
        return AccountCycleResult(
            outcome=AccountCycleOutcome.PAUSED,
            lease_id=checkpoint.lease_id,
            run_id=checkpoint.active_play_run_id,
            error_code=error_code,
        )

    def _lease_checkpoint(self, lease: AccountLease) -> None:
        self._update_checkpoint(
            lease_id=lease.lease_id,
            lease_fence=lease.lease_fence,
            lease_expires_at=lease.expires_at.isoformat(),
            account_id=lease.account_id,
            target_level=lease.target_level,
            pending_operation=None,
            operation_id=None,
            run_state=OrchestratorRunState.ACTIVE,
            resume_phase=None,
            target_reading=None,
            report_evidence=None,
            result_status=None,
            result_error_code=None,
            last_error_code=None,
        )

    def _clear_lease_checkpoint(self) -> None:
        completed = self.checkpoint_store.clear_completed_lease(self._checkpoint)
        with self._checkpoint_lock:
            self._checkpoint = completed

    def _stop_lease_runtime(self) -> None:
        if self._report_drain is not None:
            self._report_drain.stop(flush_timeout_s=5.0)
            self._report_drain = None
        if self._lease_keeper is not None:
            self._lease_keeper.stop()
            self._lease_keeper = None

    def _reconcile_server_release(self) -> bool:
        checkpoint = self._checkpoint
        if (
            checkpoint.run_state is not OrchestratorRunState.PAUSED_MANUAL
            or checkpoint.last_error_code not in self.SERVER_RELEASABLE_PAUSES
            or not checkpoint.has_lease
        ):
            return False

        try:
            remote = self.provider.current()
            released = remote is None
            if remote is not None and (
                remote.lease_id != checkpoint.lease_id
                or remote.lease_fence != checkpoint.lease_fence
            ):
                status = self.provider.status(
                    checkpoint.lease_id or "",
                    checkpoint.lease_fence or 0,
                )
                released = status.terminal
            elif remote is not None:
                released = remote.terminal
        except LeaseProviderError:
            return False

        if not released:
            return False
        previous = checkpoint.last_error_code
        self._stop_lease_runtime()
        self._clear_lease_checkpoint()
        self.notify(
            f"服务端已确认旧租约释放，自动清除本地暂停（原因 {previous}）"
        )
        return True

    def _obtain_lease(self) -> AccountLease | None:
        checkpoint = self._checkpoint
        remote = self.provider.current()
        terminal_phases = {
            WorkflowPhase.APEX_STOPPING,
            WorkflowPhase.EA_SIGNING_OUT,
            WorkflowPhase.LEASE_COMPLETING,
        }

        if checkpoint.has_lease:
            if remote is None:
                try:
                    status = self.provider.status(
                        checkpoint.lease_id or "",
                        checkpoint.lease_fence or 0,
                    )
                except LeaseStaleError:
                    # Releasing a lease from the console advances its fence, so
                    # `status()` rejects the response against the fence this
                    # checkpoint still holds — before anything can read the
                    # state it just fetched. That refusal is itself the answer:
                    # the fence moved on without us, and `current()` already
                    # reported no occupancy for this device, so there is no
                    # account left to hand back. Without this the checkpoint
                    # could never be cleared and every later run paused on
                    # LEASE_STALE until the file was deleted by hand.
                    self.notify(
                        "服务端租约已被接管或释放，清除本地 checkpoint 后继续"
                    )
                    self._clear_lease_checkpoint()
                    return None
                if status.terminal:
                    self._clear_lease_checkpoint()
                    return None
                raise LeaseStaleError(
                    "本地 checkpoint 有租约，但服务端 current() 没有对应占用"
                )
            if (
                remote.lease_id != checkpoint.lease_id
                or remote.lease_fence != checkpoint.lease_fence
            ):
                raise LeaseStaleError("本地租约与服务端 current() 不一致")
            if (
                remote.state
                in {LeaseState.COMPLETION_PENDING, LeaseState.EXPIRED_UNCONFIRMED}
                and checkpoint.workflow_phase not in terminal_phases
            ):
                raise RemoteLeaseRecoveryRequired(
                    f"REMOTE_{remote.state.value}_RECOVERY_REQUIRED"
                )
            return self.provider.recover(remote.lease_id, remote.lease_fence)

        if remote is not None:
            lease = self.provider.recover(remote.lease_id, remote.lease_fence)
            self._lease_checkpoint(lease)
            if remote.state in {
                LeaseState.COMPLETION_PENDING,
                LeaseState.EXPIRED_UNCONFIRMED,
            }:
                raise RemoteLeaseRecoveryRequired(
                    f"REMOTE_{remote.state.value}_RECOVERY_REQUIRED"
                )
            if remote.provider_status == "RUNNING":
                raise RemoteLeaseRecoveryRequired(
                    "REMOTE_RUNNING_RECOVERY_REQUIRED"
                )
            return lease

        claim_request_id = (
            checkpoint.claim_request_id or self.operation_id_factory()
        )
        self._update_checkpoint(
            workflow_phase=WorkflowPhase.CLAIMING,
            claim_request_id=claim_request_id,
            pending_operation=PendingOperation.CLAIM,
            operation_id=claim_request_id,
        )
        lease = self.provider.claim(claim_request_id, self.task_type)
        if lease is None:
            self._update_checkpoint(
                claim_request_id=None,
                pending_operation=None,
                operation_id=None,
            )
            return None
        self._lease_checkpoint(lease)
        return lease

    def _on_lease_change(self, snapshot: LeaseKeeperSnapshot) -> None:
        changes: dict[str, object] = {
            "lease_expires_at": (
                None
                if snapshot.expires_at is None
                else snapshot.expires_at.isoformat()
            ),
        }
        if snapshot.pending_operation_id is not None:
            changes.update(
                {
                    "pending_operation": PendingOperation.RENEW,
                    "operation_id": snapshot.pending_operation_id,
                }
            )
        elif self._checkpoint.pending_operation is PendingOperation.RENEW:
            changes.update(
                {
                    "pending_operation": None,
                    "operation_id": None,
                }
            )
        if snapshot.state is LeaseKeeperState.UNCERTAIN:
            changes["last_error_code"] = "LEASE_RENEW_FAILED"
        elif snapshot.state is LeaseKeeperState.STALE:
            changes["last_error_code"] = snapshot.error_code or "LEASE_STALE"
            changes["run_state"] = OrchestratorRunState.PAUSED_MANUAL
            changes["resume_phase"] = self._checkpoint.workflow_phase
        elif (
            snapshot.state is LeaseKeeperState.CURRENT
            and self._checkpoint.last_error_code == "LEASE_RENEW_FAILED"
        ):
            changes["last_error_code"] = None
        self._update_checkpoint(**changes)

    def _start_lease_keeper(self, lease: AccountLease) -> LeaseKeeper:
        if self._lease_keeper is not None:
            current = self._lease_keeper
            if (
                current.lease.lease_id == lease.lease_id
                and current.lease.lease_fence == lease.lease_fence
                and current.snapshot().state is not LeaseKeeperState.STOPPED
            ):
                return current
            current.stop()
            self._lease_keeper = None
        keeper = self.lease_keeper_factory(
            self.provider,
            lease,
            phase=self._phase,
            run_id=self._active_run_id,
            on_change=self._on_lease_change,
            operation_id_factory=self.operation_id_factory,
            operation_lock=self._provider_operation_lock,
        )
        keeper.start()
        self._lease_keeper = keeper
        return keeper

    def _lease_gate(
        self,
        keeper: LeaseKeeper,
    ) -> AccountCycleResult | None:
        """Wait through transient renew failures without restarting EA/Apex."""

        if keeper.is_current():
            return None
        snapshot = keeper.snapshot()
        if snapshot.state is LeaseKeeperState.STALE:
            return self._pause(
                snapshot.error_code or "LEASE_STALE",
                manual=True,
            )
        if self._stop.is_set() or snapshot.state is LeaseKeeperState.STOPPED:
            return AccountCycleResult(AccountCycleOutcome.STOPPED)

        self.notify(
            "租约续租暂时无法确认；保持当前 EA/Apex 状态，"
            f"最多等待 {LEASE_RECOVERY_GRACE_S:g} 秒自动恢复"
        )
        remaining = LEASE_RECOVERY_GRACE_S
        while remaining > 0 and not self._stop.is_set():
            keeper.wake()
            delay = min(LEASE_RECOVERY_POLL_S, remaining)
            self.sleep(delay)
            remaining -= delay
            if keeper.is_current():
                self.notify("租约续租已恢复，继续当前账号")
                self._update_checkpoint(last_error_code=None)
                return None
            snapshot = keeper.snapshot()
            if snapshot.state is LeaseKeeperState.STALE:
                return self._pause(
                    snapshot.error_code or "LEASE_STALE",
                    manual=True,
                )
            if snapshot.state is LeaseKeeperState.STOPPED:
                return AccountCycleResult(AccountCycleOutcome.STOPPED)

        if self._stop.is_set():
            return AccountCycleResult(AccountCycleOutcome.STOPPED)
        self.notify(
            f"租约续租在 {LEASE_RECOVERY_GRACE_S:g} 秒内仍未恢复，安全暂停"
        )
        return self._pause("LEASE_UNCERTAIN", manual=False)

    def _lease_gate_after_apex_start(
        self,
        keeper: LeaseKeeper,
    ) -> AccountCycleResult | None:
        """Never leave a newly started Apex process behind after lease loss."""

        result = self._lease_gate(keeper)
        if result is None:
            return None
        try:
            exit_evidence = self.ea_driver.stop_apex()
        except EaAppAutomationError as error:
            self.notify(f"租约未恢复且停止 Apex 失败：{error}")
            return self._pause("APEX_STOP_FAILED", manual=True)
        if not exit_evidence.all_processes_exited:
            self.notify("租约未恢复且 Apex 进程未完全退出")
            return self._pause("APEX_STOP_FAILED", manual=True)
        self.notify("租约未恢复，已停止刚启动的 Apex；保留 EA 状态等待重试")
        return result

    def _otp_supplier(
        self,
        lease: AccountLease,
    ) -> Callable[[OtpChallenge], OtpCode]:
        def provide(challenge: OtpChallenge) -> OtpCode:
            with self._provider_operation_lock:
                operation_id = self.operation_id_factory()
                self._update_checkpoint(
                    pending_operation=PendingOperation.OTP,
                    operation_id=operation_id,
                )
                otp = self.provider.request_otp(
                    lease.lease_id,
                    lease.lease_fence,
                    operation_id,
                    challenge.challenge_id,
                    challenge.started_at,
                    challenge.method,
                )
                if not otp.valid_for(
                    challenge.challenge_id,
                    challenge.started_at,
                ):
                    raise EaAppAutomationError(
                        "Provider 返回了不属于当前 challenge 的 OTP"
                    )
                self._finish_operation()
                return otp

        return provide

    def _ensure_account_identity(
        self,
        lease: AccountLease,
    ) -> EaIdentityFact:
        expected = lease.expected_ea_account_id
        if not expected:
            raise EaAppAutomationError("租约缺少可验证的 EA 稳定账号 ID")
        self._update_checkpoint(workflow_phase=WorkflowPhase.EA_STARTING)
        self.ea_driver.ensure_started()
        current = self.ea_driver.current_identity()
        if current is not None and current.ea_account_id != expected:
            if not self.ea_driver.sign_out():
                raise EaAppAutomationError("EA App 无法确认已退出其他账号")
            current = None
        if current is None:
            self._update_checkpoint(workflow_phase=WorkflowPhase.EA_SIGNING_IN)
            self.notify("EA 开始自动登录")
            with self._provider_operation_lock:
                operation_id = self._begin_operation(
                    PendingOperation.CREDENTIALS
                )
                credentials = self.provider.credentials(
                    lease.lease_id,
                    lease.lease_fence,
                    operation_id,
                )
                self._finish_operation()
            self.ea_driver.sign_in(credentials, self._otp_supplier(lease))

        self._update_checkpoint(
            workflow_phase=WorkflowPhase.EA_IDENTITY_VERIFYING
        )
        self.notify("EA 登录动作完成，开始核对稳定 EA ID")
        identity = self.ea_driver.verify_identity(expected)
        if not identity.verified or identity.ea_account_id != expected:
            raise EaAppAutomationError("EA App 登录身份无法与租约账号匹配")
        return identity

    def _session_identity(
        self,
        lease: AccountLease,
        identity: EaIdentityFact,
    ) -> SessionIdentity:
        return SessionIdentity(
            account_id=lease.account_id,
            device_id=self.device_id,
            lease_id=lease.lease_id,
            lease_fence=lease.lease_fence,
            target_level=lease.target_level,
            identity_verification=IdentityVerification(
                status="VERIFIED",
                observed_platform="ea",
                observed_platform_account_id=identity.ea_account_id,
                message=f"EA identity verified via {identity.source}",
            ),
        )

    def _record_run_started(self, run_id: str) -> None:
        previous = self._checkpoint.previous_play_run_ids
        active = self._checkpoint.active_play_run_id
        if active and active != run_id and active not in previous:
            previous = (*previous, active)
        self._update_checkpoint(
            active_play_run_id=run_id,
            previous_play_run_ids=previous,
        )

    @staticmethod
    def _completion_evidence(
        result: PlaySessionResult,
    ) -> CompletionEvidence | None:
        if (
            result.status != "TARGET_REACHED"
            or result.level is None
            or result.lobby_progress_report_seq is None
            or result.run_finished_report_seq is None
        ):
            return None
        return CompletionEvidence(
            run_id=result.run_id,
            level=result.level,
            lobby_progress_seq=result.lobby_progress_report_seq,
            run_finished_seq=result.run_finished_report_seq,
        )

    def _save_result_evidence(
        self,
        result: PlaySessionResult,
        evidence: CompletionEvidence | None,
    ) -> None:
        self._report_drain = result.report_drain
        self._update_checkpoint(
            workflow_phase=WorkflowPhase.APEX_STOPPING,
            active_play_run_id=result.run_id,
            target_reading=(
                None
                if result.level is None
                else {
                    "level": result.level,
                    "xpCurrentApprox": result.xp_current_approx,
                    "xpRequiredApprox": result.xp_required_approx,
                    "localSeq": result.progress_local_seq,
                }
            ),
            report_evidence=(
                None
                if result.run_finished_report_seq is None
                else {
                    "runId": result.run_id,
                    **(
                        {}
                        if result.lobby_progress_report_seq is None
                        else {
                            "lobbyProgressSeq": result.lobby_progress_report_seq
                        }
                    ),
                    "runFinishedSeq": result.run_finished_report_seq,
                }
            ),
            result_status=result.status,
            result_error_code=result.error_code,
        )

    def _cleanup_and_close(
        self,
        lease: AccountLease,
        result: PlaySessionResult,
        evidence: CompletionEvidence | None,
    ) -> AccountCycleResult:
        exit_evidence = self.ea_driver.stop_apex()
        if not exit_evidence.all_processes_exited:
            return self._pause("APEX_EXIT_TIMEOUT", manual=True)
        self._update_checkpoint(workflow_phase=WorkflowPhase.EA_SIGNING_OUT)
        signed_out = self.ea_driver.sign_out()
        if not signed_out:
            return self._pause("EA_SIGNOUT_FAILED", manual=True)

        cleanup = CleanupEvidence(
            input_released=result.error_code != "SESSION_CLEANUP_FAILED",
            apex_exited=exit_evidence.all_processes_exited,
            ea_signed_out=signed_out,
        )
        if not cleanup.complete:
            return self._pause("CLEANUP_UNCONFIRMED", manual=True)

        return self._submit_close(lease, result, evidence, cleanup)

    def _cleanup_and_close_preplay_failure(
        self,
        lease: AccountLease,
        reason_code: str,
    ) -> AccountCycleResult:
        try:
            exit_evidence = self.ea_driver.stop_apex()
        except EaAppAutomationError:
            return self._pause("CLEANUP_UNCONFIRMED", manual=True)
        if not exit_evidence.all_processes_exited:
            return self._pause("APEX_EXIT_TIMEOUT", manual=True)
        self._update_checkpoint(workflow_phase=WorkflowPhase.EA_SIGNING_OUT)
        try:
            signed_out = self.ea_driver.sign_out()
        except EaAppAutomationError:
            return self._pause("EA_SIGNOUT_FAILED", manual=True)
        cleanup = CleanupEvidence(True, True, signed_out)
        if not cleanup.complete:
            return self._pause("EA_SIGNOUT_FAILED", manual=True)
        self._update_checkpoint(workflow_phase=WorkflowPhase.LEASE_COMPLETING)
        with self._provider_operation_lock:
            operation_id = self._begin_operation(PendingOperation.CLOSE)
            status = self.provider.close(
                lease.lease_id,
                lease.lease_fence,
                operation_id,
                "FAILED",
                None,
                None,
                reason_code,
                cleanup,
            )
            self._finish_operation()
        if not status.terminal:
            return self._pause("LEASE_CLOSE_PENDING", manual=True)
        if self._lease_keeper is not None:
            self._lease_keeper.stop()
            self._lease_keeper = None
        self._clear_lease_checkpoint()
        return AccountCycleResult(
            AccountCycleOutcome.COMPLETED,
            lease_id=lease.lease_id,
            error_code=reason_code,
        )

    def _submit_close(
        self,
        lease: AccountLease,
        result: PlaySessionResult,
        evidence: CompletionEvidence | None,
        cleanup: CleanupEvidence,
    ) -> AccountCycleResult:
        self._update_checkpoint(workflow_phase=WorkflowPhase.LEASE_COMPLETING)
        if result.status == "TARGET_REACHED":
            outcome = "TARGET_REACHED"
            reason_code = result.error_code or "TARGET_LEVEL_CONFIRMED"
        elif result.status == "FAILED":
            outcome = "FAILED"
            reason_code = result.error_code or "PLAY_SESSION_FAILED"
        else:
            outcome = "RELEASED"
            reason_code = result.error_code or "SESSION_RELEASED"
        with self._provider_operation_lock:
            operation_id = self._begin_operation(PendingOperation.CLOSE)
            status = self.provider.close(
                lease.lease_id,
                lease.lease_fence,
                operation_id,
                outcome,
                result.run_id,
                evidence,
                reason_code,
                cleanup,
            )
            self._finish_operation()
        return self._wait_for_completion(lease, result, status.state)

    def _checkpoint_result(self) -> PlaySessionResult | None:
        checkpoint = self._checkpoint
        run_id = checkpoint.active_play_run_id
        report = checkpoint.report_evidence or {}
        target = checkpoint.target_reading or {}
        if run_id is None or self.recover_report_drain is None:
            return None
        drain = self.recover_report_drain(run_id)
        if drain is None:
            return None

        def integer(source: dict[str, object], key: str) -> int | None:
            value = source.get(key)
            return value if type(value) is int else None

        level = integer(target, "level")
        status = checkpoint.result_status
        if status is None:
            # Backward compatibility is deliberately narrow: an old checkpoint
            # can only be inferred as successful when all target evidence was
            # already persisted.  Failure/stop states must never become target.
            if (
                level is not None
                and report.get("runId") == run_id
                and integer(report, "lobbyProgressSeq") is not None
                and integer(report, "runFinishedSeq") is not None
            ):
                status = "TARGET_REACHED"
            else:
                return None
        return PlaySessionResult(
            status=status,
            run_id=run_id,
            account_id=checkpoint.account_id,
            lease_id=checkpoint.lease_id,
            lease_fence=checkpoint.lease_fence,
            level=level,
            xp_current_approx=integer(target, "xpCurrentApprox"),
            xp_required_approx=integer(target, "xpRequiredApprox"),
            progress_local_seq=integer(target, "localSeq"),
            lobby_progress_report_seq=integer(report, "lobbyProgressSeq"),
            run_finished_report_seq=integer(report, "runFinishedSeq"),
            frames=0,
            actions_sent=0,
            rounds_started=0,
            rounds_returned_to_lobby=0,
            report_drain=drain,
            error_code=checkpoint.result_error_code,
        )

    def _resume_terminal_workflow(
        self,
        lease: AccountLease,
    ) -> AccountCycleResult:
        result = self._checkpoint_result()
        if result is None:
            return self._pause("REPORT_DRAIN_UNAVAILABLE", manual=True)
        self._report_drain = result.report_drain
        evidence = self._completion_evidence(result)
        phase = self._checkpoint.workflow_phase
        if result.status == "TARGET_REACHED" and evidence is None:
            return self._pause("REPORT_EVIDENCE_MISSING", manual=True)

        if phase is WorkflowPhase.APEX_STOPPING:
            return self._cleanup_and_close(lease, result, evidence)
        if phase is WorkflowPhase.EA_SIGNING_OUT:
            signed_out = self.ea_driver.sign_out()
            if not signed_out:
                return self._pause("EA_SIGNOUT_FAILED", manual=True)
            return self._submit_close(
                lease,
                result,
                evidence,
                CleanupEvidence(True, True, True),
            )

        status = self.provider.status(
            lease.lease_id,
            lease.lease_fence,
        )
        if (
            status.state is LeaseState.ACTIVE
            or self._checkpoint.pending_operation is PendingOperation.CLOSE
        ):
            return self._submit_close(
                lease,
                result,
                evidence,
                CleanupEvidence(True, True, True),
            )
        return self._wait_for_completion(lease, result, status.state)

    def _wait_for_completion(
        self,
        lease: AccountLease,
        result: PlaySessionResult,
        initial_state: LeaseState,
    ) -> AccountCycleResult:
        drain = result.report_drain
        if drain is None:
            return self._pause("REPORT_DRAIN_UNAVAILABLE", manual=True)
        required_seq = result.run_finished_report_seq
        if required_seq is None:
            return self._pause("REPORT_EVIDENCE_MISSING", manual=True)

        state = initial_state
        while not self._stop.is_set():
            drain.wake()
            accepted = drain.accepted_through >= required_seq
            if getattr(drain, "terminal_error", None) and not accepted:
                return self._pause("REPORTING_TERMINAL_ERROR", manual=True)
            if state in {LeaseState.COMPLETED, LeaseState.CLOSED} and accepted:
                drain.stop(flush_timeout_s=5.0)
                if self._lease_keeper is not None:
                    self._lease_keeper.stop()
                    self._lease_keeper = None
                self._clear_lease_checkpoint()
                if result.status == "STOPPED":
                    return AccountCycleResult(
                        outcome=AccountCycleOutcome.STOPPED,
                        lease_id=lease.lease_id,
                        run_id=result.run_id,
                        error_code=result.error_code,
                    )
                if result.status not in {"TARGET_REACHED", "FAILED"}:
                    return self._pause(
                        result.error_code or "SESSION_ENDED_WITHOUT_TARGET",
                        manual=True,
                    )
                # The cycle completing says nothing about how the game session
                # went. A crashed session closed its lease correctly and still
                # reported COMPLETED with no reason attached, which reads as a
                # clean run.
                return AccountCycleResult(
                    outcome=AccountCycleOutcome.COMPLETED,
                    lease_id=lease.lease_id,
                    run_id=result.run_id,
                    error_code=(
                        None if result.status == "TARGET_REACHED" else result.error_code
                    ),
                )
            self.sleep(self.completion_poll_s)
            state = self.provider.status(
                lease.lease_id,
                lease.lease_fence,
            ).state
        return self._pause("OPERATOR_STOPPED", manual=False)

    def run_once(self) -> AccountCycleResult:
        if self._stop.is_set():
            return AccountCycleResult(AccountCycleOutcome.STOPPED)
        self._reconcile_server_release()
        if self._checkpoint.run_state is not OrchestratorRunState.ACTIVE:
            return AccountCycleResult(
                AccountCycleOutcome.PAUSED,
                lease_id=self._checkpoint.lease_id,
                run_id=self._checkpoint.active_play_run_id,
                error_code=self._checkpoint.last_error_code,
            )
        try:
            lease = self._obtain_lease()
            if lease is None:
                if self._checkpoint.has_lease:
                    return self._pause("COMPLETION_RECOVERY_REQUIRED", manual=True)
                return AccountCycleResult(AccountCycleOutcome.NO_ACCOUNT)
            if self._checkpoint.workflow_phase in {
                WorkflowPhase.APEX_STOPPING,
                WorkflowPhase.EA_SIGNING_OUT,
                WorkflowPhase.LEASE_COMPLETING,
            }:
                status = self.provider.status(
                    lease.lease_id,
                    lease.lease_fence,
                )
                if not status.terminal:
                    self._start_lease_keeper(lease)
                return self._resume_terminal_workflow(lease)
            keeper = self._start_lease_keeper(lease)
            if (
                self._checkpoint.workflow_phase is WorkflowPhase.APEX_PLAYING
                and self._checkpoint.active_play_run_id is not None
            ):
                return self._pause("ORPHAN_RUN_RECOVERY_REQUIRED", manual=True)
            identity_fact = self._ensure_account_identity(lease)
            lease_gate = self._lease_gate(keeper)
            if lease_gate is not None:
                return lease_gate

            self._update_checkpoint(workflow_phase=WorkflowPhase.APEX_STARTING)
            try:
                self.ea_driver.start_apex()
            except EaApexDownloadRequired:
                self.notify(
                    "EA App 已安装状态未登记，自动走一次 "
                    "Download 现有文件登记流程"
                )
                lease_gate = self._lease_gate(keeper)
                if lease_gate is not None:
                    return lease_gate
                self.ea_driver.repair_apex_installation()
                lease_gate = self._lease_gate(keeper)
                if lease_gate is not None:
                    return lease_gate
                self.ea_driver.restart_app()
                lease_gate = self._lease_gate(keeper)
                if lease_gate is not None:
                    return lease_gate
                expected = lease.expected_ea_account_id
                if not expected:
                    raise EaAppAutomationError("租约缺少可验证的 EA 稳定账号 ID")
                identity_fact = self.ea_driver.verify_identity(expected)
                if not identity_fact.verified or identity_fact.ea_account_id != expected:
                    raise EaAppAutomationError(
                        "EA App 重启后登录身份无法与租约账号匹配"
                    )
                self.ea_driver.start_apex()
            lease_gate = self._lease_gate_after_apex_start(keeper)
            if lease_gate is not None:
                return lease_gate

            self._update_checkpoint(workflow_phase=WorkflowPhase.APEX_PLAYING)
            result = self.play_session.run(
                self._session_identity(lease, identity_fact),
                TargetLevelPolicy(lease.target_level),
                self.capture_source,
                lease_is_current=keeper.is_current,
                on_run_started=self._record_run_started,
            )
            evidence = self._completion_evidence(result)
            self._save_result_evidence(result, evidence)
            if result.status == "TARGET_REACHED" and evidence is None:
                return self._pause("REPORT_EVIDENCE_MISSING", manual=True)
            return self._cleanup_and_close(lease, result, evidence)
        except LeaseStaleError:
            return self._pause("LEASE_STALE", manual=True)
        except RemoteLeaseRecoveryRequired as error:
            return self._pause(error.reason_code, manual=True)
        except IdempotencyConflictError as error:
            return self._pause(error.code, manual=True)
        except LeaseProviderError as error:
            return self._pause(error.code, manual=not error.retryable)
        except EaAppAutomationError as error:
            # The reason code alone cannot tell two different EA failures
            # apart, and the message is the only place that says which gate
            # gave up. Dropping it here is what made repeated real runs
            # unreadable.
            self.notify(
                f"EA 自动化失败：{error.reason_code} @ {self._phase()}：{error}"
            )
            if "lease" in locals() and lease is not None:
                return self._cleanup_and_close_preplay_failure(
                    lease,
                    error.reason_code,
                )
            return self._pause(error.reason_code, manual=True)
        except Exception as error:
            self.notify(
                f"账号编排异常：{type(error).__name__} @ {self._phase()}：{error}"
            )
            return self._pause("ORCHESTRATOR_FAILED", manual=True)

    def resume(self) -> bool:
        """Clear a manual pause after the operator has dealt with its cause.

        A manual pause is sticky on purpose — the next cycle must not walk
        past an orphan run or a stale lease. But the pause outlives whatever
        caused it, so a server-side hold that has since been lifted keeps
        answering with the same stale reason code until this is called.
        """

        if self._checkpoint.run_state is OrchestratorRunState.ACTIVE:
            return False
        previous = self._checkpoint.last_error_code
        self._update_checkpoint(
            run_state=OrchestratorRunState.ACTIVE,
            resume_phase=None,
            last_error_code=None,
        )
        self.notify(f"已清除本地暂停状态（原因 {previous or '未记录'}）")
        return True

    def run_forever(self, *, idle_s: float = 30.0) -> int:
        while not self._stop.is_set():
            result = self.run_once()
            if result.outcome is AccountCycleOutcome.PAUSED:
                if self._checkpoint.run_state is OrchestratorRunState.PAUSED_MANUAL:
                    self.notify(f"账号编排已暂停：{result.error_code}")
                    if result.error_code not in self.SERVER_RELEASABLE_PAUSES:
                        return 1
                    self.sleep(max(1.0, idle_s))
                    continue
                delay = max(1.0, idle_s)
                retry_after = getattr(self.provider, "claim_retry_after_s", None)
                if isinstance(retry_after, (int, float)):
                    delay = max(delay, float(retry_after))
                self.notify(
                    f"账号编排遇到可重试问题：{result.error_code}；"
                    f"Runner 仍在运行，{delay:g} 秒后自动重试"
                )
                self.sleep(delay)
                if self._stop.is_set():
                    return 0
                self._update_checkpoint(
                    run_state=OrchestratorRunState.ACTIVE,
                    resume_phase=None,
                )
                self.notify(f"账号编排开始自动重试：{result.error_code}")
                continue
            if result.outcome is AccountCycleOutcome.STOPPED:
                return 0
            if result.outcome is AccountCycleOutcome.NO_ACCOUNT:
                delay = max(1.0, idle_s)
                retry_after = getattr(self.provider, "claim_retry_after_s", None)
                if isinstance(retry_after, (int, float)):
                    delay = max(delay, float(retry_after))
                self.notify(
                    "账号池暂无可领账号；Runner 仍在运行，"
                    f"{delay:g} 秒后再次尝试领号"
                )
                self.sleep(delay)
        return 0

    def stop(self) -> None:
        self._stop.set()
        self._stop_lease_runtime()

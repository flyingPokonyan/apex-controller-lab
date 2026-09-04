from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
from pathlib import Path
import time
from typing import Any, Callable

from .capabilities import CapabilityDispatcher, CapabilitySet
from .config import RunnerConfig
from .frame_normalization import (
    ReferenceCanvasFrameSource,
    capture_resolution,
    reference_resolution,
)
from .input_win32 import EmergencyStop
from .ocr_states import OcrStateDetector
from .pilot import CapabilityPilot, PilotFrameSource, PilotGuard, PilotSender
from .progression import LobbyProgressionReader, LobbyProgressionReading
from .progression_policy import ProgressionPolicy
from .recorder import RunRecorder
from .reporter import RemoteReporter
from .runner_identity import IdentityVerification, RunnerSettings


@dataclass(frozen=True)
class SessionIdentity:
    account_id: str | None
    device_id: str | None
    identity_verification: IdentityVerification
    lease_id: str | None = None
    lease_fence: int | None = None
    target_level: int | None = None

    def __post_init__(self) -> None:
        if bool(self.account_id) != bool(self.device_id):
            raise ValueError("accountId 和 deviceId 必须同时存在")
        managed_values = (self.lease_id, self.lease_fence, self.target_level)
        if any(value is not None for value in managed_values):
            if not self.account_id or not self.device_id:
                raise ValueError("托管会话必须绑定 accountId/deviceId")
            if self.lease_id is None or self.lease_fence is None:
                raise ValueError("托管会话必须同时包含 leaseId/leaseFence")
            if self.lease_fence < 1:
                raise ValueError("leaseFence 必须大于 0")
            if self.target_level is None or self.target_level < 1:
                raise ValueError("托管会话必须包含有效 targetLevel")

    @property
    def managed(self) -> bool:
        return self.lease_id is not None

    @classmethod
    def from_runner_settings(
        cls,
        settings: RunnerSettings,
        verification: IdentityVerification,
    ) -> "SessionIdentity":
        return cls(
            account_id=settings.account_id,
            device_id=settings.device_id,
            identity_verification=verification,
        )


class ReportDrainHandle:
    """Drain an immutable run after its Recorder has been finished."""

    def __init__(self, reporter: RemoteReporter) -> None:
        self._reporter = reporter
        self._stopped = False

    @property
    def accepted_through(self) -> int:
        return self._reporter.current_session.accepted_through

    @property
    def pending_count(self) -> int:
        return self._reporter.current_session.pending_count()

    @property
    def terminal_error(self) -> str | None:
        return self._reporter.current_session.terminal_error

    def report_seq(self, event_type: str) -> int | None:
        return self._reporter.current_session.report_seq(event_type)

    def wake(self) -> None:
        if not self._stopped:
            self._reporter.wake()

    def stop(self, *, flush_timeout_s: float = 5.0) -> int:
        if self._stopped:
            return self.pending_count
        pending = self._reporter.stop(flush_timeout_s=flush_timeout_s)
        self._stopped = True
        return pending


@dataclass(frozen=True)
class PlaySessionResult:
    status: str
    run_id: str
    account_id: str | None
    lease_id: str | None
    lease_fence: int | None
    level: int | None
    xp_current_approx: int | None
    xp_required_approx: int | None
    progress_local_seq: int | None
    lobby_progress_report_seq: int | None
    run_finished_report_seq: int | None
    frames: int
    actions_sent: int
    rounds_started: int
    rounds_returned_to_lobby: int
    error_code: str | None = None
    error: str | None = None
    exit_code: int = 0
    run_dir: Path | None = None
    report_drain: ReportDrainHandle | None = field(
        default=None,
        repr=False,
        compare=False,
    )


def _last_local_event_seq(path: Path, event_type: str) -> int | None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    found: int | None = None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            break
        if isinstance(record, dict) and record.get("type") == event_type:
            seq = record.get("seq")
            if type(seq) is int:
                found = seq
    return found


class PlaySessionRunner:
    """Own one verified account's Pilot, Recorder and Reporter session."""

    def __init__(
        self,
        *,
        config: RunnerConfig,
        capabilities: CapabilitySet,
        actions: dict[str, Any],
        state_detector: OcrStateDetector,
        overlay_detector: OcrStateDetector | None,
        ocr_provider: Any,
        settings: RunnerSettings,
        sender: PilotSender,
        guard: PilotGuard,
        runs_root: Path,
        app_version: str,
        config_revision: str,
        notify: Callable[[str], None] = print,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.config = config
        self.capabilities = capabilities
        self.actions = actions
        self.state_detector = state_detector
        self.overlay_detector = overlay_detector
        self.ocr_provider = ocr_provider
        self.settings = settings
        self.sender = sender
        self.guard = guard
        self.runs_root = runs_root
        self.app_version = app_version
        self.config_revision = config_revision
        self.notify = notify
        self.sleep = sleep

    def recover_report_drain(self, run_id: str) -> ReportDrainHandle | None:
        """Reopen a finished immutable run without re-enabling event writes."""

        run_dir = self.runs_root / run_id
        if not (run_dir / "result.json").is_file():
            return None
        reporter = RemoteReporter(
            self.settings,
            self.runs_root,
            run_dir,
            notify=self.notify,
        )
        session = reporter.current_session
        session.ingest()
        if not session.finished:
            return None
        reporter.start()
        return ReportDrainHandle(reporter)

    def _session_settings(self, identity: SessionIdentity) -> RunnerSettings:
        if not identity.account_id:
            return self.settings
        return replace(
            self.settings,
            account_id=identity.account_id,
            device_id=identity.device_id,
        )

    def _reporting_manifest(
        self,
        identity: SessionIdentity,
        settings: RunnerSettings,
    ) -> dict[str, object]:
        reporting = settings.safe_manifest(identity.identity_verification)
        if identity.managed:
            reporting.update(
                {
                    "leaseId": identity.lease_id,
                    "leaseFence": identity.lease_fence,
                    "targetLevel": identity.target_level,
                }
            )
        return reporting

    def run(
        self,
        identity: SessionIdentity,
        progression_policy: ProgressionPolicy,
        capture_source: PilotFrameSource,
        *,
        duration_s: float | None = None,
        countdown: int = 0,
        lease_is_current: Callable[[], bool] = lambda: True,
        on_run_started: Callable[[str], None] = lambda _: None,
    ) -> PlaySessionResult:
        session_settings = self._session_settings(identity)
        if identity.managed and not session_settings.enabled:
            raise ValueError("托管会话必须启用远程上报")

        capture_size = capture_resolution(self.config.environment)
        reference_size = reference_resolution(self.config.environment)
        run_context = {
            "mode": "account-cycle" if identity.managed else "play",
            "targetMode": "bot-royale",
            "resolution": list(capture_size),
            "referenceResolution": list(reference_size),
            "language": str(self.config.environment["language"]),
            "capabilitySet": str(self.config.capability_set),
            "targetLevel": identity.target_level,
            "targetRing": int(self.config.ranked_road_progress.get("target", 30)),
        }
        recorder = RunRecorder(
            self.runs_root,
            f"{self.config.profile}.play",
            manifest={
                "client": {
                    "appVersion": self.app_version,
                    "profile": self.config.profile,
                    "configRevision": self.config_revision,
                },
                "runContext": run_context,
                "reporting": self._reporting_manifest(identity, session_settings),
            },
            start_payload=run_context,
        )
        try:
            on_run_started(recorder.run_id)
        except Exception:
            recorder.finish("FAILED", errorCode="RUN_CHECKPOINT_FAILED")
            raise
        reporter: RemoteReporter | None = None
        pilot: CapabilityPilot | None = None
        finish_status = "PLAYED"
        finish_detail: dict[str, object] = {}
        exit_code = 0
        error_code: str | None = None
        error_message: str | None = None

        try:
            recorder.write_status(
                {
                    "runtimeState": "STARTING",
                    "elapsedMs": 0,
                    "observedState": None,
                    "foreground": None,
                    "frames": 0,
                    "actionsSent": 0,
                    "roundNumber": 0,
                    "roundsReturnedToLobby": 0,
                }
            )
            if session_settings.enabled:
                reporter = RemoteReporter(
                    session_settings,
                    self.runs_root,
                    recorder.run_dir,
                    notify=self.notify,
                )
                reporter.start()

            progression_config = self.config.lobby_progress
            progression_reader = (
                LobbyProgressionReader(
                    self.ocr_provider,
                    min_confidence=float(
                        progression_config.get("minConfidence", 0.65)
                    ),
                )
                if bool(progression_config.get("enabled", False))
                else None
            )
            for remaining in range(max(0, countdown), 0, -1):
                self.notify(f"{remaining}...")
                self.sleep(1)

            apex_source = ReferenceCanvasFrameSource(
                capture_source,
                capture_size,
                reference_size,
            )
            recorder.log(
                "CAPTURE_CANVAS_CONFIGURED",
                captureResolution=list(capture_size),
                referenceResolution=list(reference_size),
                interpolation="LANCZOS4",
            )
            pilot = CapabilityPilot(
                self.config,
                apex_source,
                self.sender,
                self.guard,
                recorder,
                state_detector=self.state_detector,
                overlay_detector=self.overlay_detector,
                dispatcher=CapabilityDispatcher(self.capabilities),
                actions=self.actions,
                progression_reader=progression_reader,
                progression_stable_samples=int(
                    progression_config.get("stableSamples", 2)
                ),
                progression_max_attempts=int(
                    progression_config.get("maxAttempts", 3)
                ),
                progression_policy=progression_policy,
                ranked_road_progress_enabled=identity.managed,
                lease_is_current=lease_is_current,
                notify=self.notify,
            )
            outcome = pilot.run(duration_s=duration_s)
            if outcome == "TARGET_REACHED":
                finish_status = "TARGET_REACHED"
            elif outcome == "STALLED":
                # Not FAILED: nothing is wrong with the account, the runner
                # just could not get the screen to move. The lease is released
                # with a reason and the cycle stops for a look rather than
                # spending the next account on the same wall.
                error_code = "STALL_UNRECOVERED"
                error_message = "画面长时间无法识别且无法恢复"
                finish_detail["reason"] = error_message
            elif outcome == "STALLED_KNOWN":
                # The page was recognised and every action was bounded, but
                # neither the primary action nor its explicitly evidenced
                # fallback moved it. Close cleanly and stop the managed loop
                # instead of spending another account on the same UI fault.
                error_code = "KNOWN_STATE_STALL_UNRECOVERED"
                error_message = "已知页面的安全动作全部耗尽且画面仍未变化"
                finish_detail["reason"] = error_message
            elif outcome == "ENVIRONMENT_INVALID":
                # Keep the established PLAYED status vocabulary. The specific
                # error makes AccountOrchestrator release this lease and enter a
                # manual pause instead of immediately spending the next account
                # on the same broken display environment.
                error_code = "RESOLUTION_MISMATCH"
                error_message = "物理捕获分辨率发生变化，已停止输入"
                finish_detail["reason"] = error_message
                finish_detail["errorCode"] = error_code
                exit_code = 1
        except (EmergencyStop, KeyboardInterrupt) as error:
            finish_status = "STOPPED"
            error_message = str(error) or "用户停止"
            error_code = "OPERATOR_STOPPED"
            finish_detail["reason"] = error_message
            self.notify(f"\n已停止：{error_message}")
        except Exception as error:
            finish_status = "FAILED"
            error_message = str(error)
            error_code = "PLAY_SESSION_FAILED"
            finish_detail["error"] = error_message
            exit_code = 1
            self.notify(f"自动游玩中断：{error_message}")
        finally:
            cleanup_errors: list[str] = []
            try:
                self.sender.release_all()
            except Exception as error:
                cleanup_errors.append(f"释放输入失败：{error}")

            if pilot is not None:
                try:
                    pilot.write_summary()
                except Exception as error:
                    recorder.log("SUMMARY_WRITE_FAILED", error=str(error))
                    cleanup_errors.append(f"统计摘要写入失败：{error}")
                finish_detail.update(
                    {
                        "frames": pilot.frames,
                        "actionsSent": pilot.actions_sent,
                        "roundsStarted": pilot.rounds_started,
                        "roundsReturnedToLobby": pilot.rounds_returned_to_lobby,
                    }
                )
            else:
                finish_detail.update(
                    {
                        "frames": 0,
                        "actionsSent": 0,
                        "roundsStarted": 0,
                        "roundsReturnedToLobby": 0,
                    }
                )

            if cleanup_errors:
                finish_status = "FAILED"
                exit_code = 1
                error_code = "SESSION_CLEANUP_FAILED"
                finish_detail["cleanupErrors"] = cleanup_errors
            try:
                recorder.write_status(
                    {
                        "runtimeState": finish_status,
                        "elapsedMs": (
                            0
                            if pilot is None
                            else max(
                                0,
                                round((pilot.monotonic() - pilot.started) * 1000),
                            )
                        ),
                        "observedState": (
                            None if pilot is None else pilot.observed_state
                        ),
                        "foreground": False,
                        "frames": finish_detail["frames"],
                        "actionsSent": finish_detail["actionsSent"],
                        "roundNumber": finish_detail["roundsStarted"],
                        "roundsReturnedToLobby": finish_detail[
                            "roundsReturnedToLobby"
                        ],
                    }
                )
            except Exception as error:
                cleanup_errors.append(f"最终状态写入失败：{error}")
                finish_status = "FAILED"
                exit_code = 1
                error_code = "SESSION_CLEANUP_FAILED"
                finish_detail["cleanupErrors"] = cleanup_errors
            try:
                recorder.finish(finish_status, **finish_detail)
            except Exception as error:
                cleanup_errors.append(f"运行结果写入失败：{error}")
                exit_code = 1
                error_code = "SESSION_CLEANUP_FAILED"
            if reporter is not None:
                reporter.wake()

        reading: LobbyProgressionReading | None = (
            None if pilot is None else pilot.target_reading
        )
        progress_local_seq = _last_local_event_seq(
            recorder.events_path,
            "LOBBY_PROGRESS",
        )
        report_drain: ReportDrainHandle | None = None
        lobby_progress_report_seq: int | None = None
        run_finished_report_seq: int | None = None
        if reporter is not None:
            reporter.current_session.ingest()
            lobby_progress_report_seq = reporter.current_session.report_seq(
                "LOBBY_PROGRESS"
            )
            run_finished_report_seq = reporter.current_session.report_seq(
                "RUN_FINISHED"
            )
            if identity.managed:
                report_drain = ReportDrainHandle(reporter)
            else:
                try:
                    pending = reporter.stop(flush_timeout_s=5.0)
                    if pending:
                        self.notify(
                            f"远程仍有 {pending} 条待上报，"
                            "已保存在本地供下次补传。"
                        )
                except Exception:
                    exit_code = 1
                    error_code = "REPORTER_STOP_FAILED"
                    error_message = "远程上报线程未能安全停止"

        return PlaySessionResult(
            status=finish_status,
            run_id=recorder.run_id,
            account_id=identity.account_id,
            lease_id=identity.lease_id,
            lease_fence=identity.lease_fence,
            level=None if reading is None else reading.level,
            xp_current_approx=(
                None if reading is None else reading.xp_current_approx
            ),
            xp_required_approx=(
                None if reading is None else reading.xp_required_approx
            ),
            progress_local_seq=progress_local_seq,
            lobby_progress_report_seq=lobby_progress_report_seq,
            run_finished_report_seq=run_finished_report_seq,
            frames=int(finish_detail["frames"]),
            actions_sent=int(finish_detail["actionsSent"]),
            rounds_started=int(finish_detail["roundsStarted"]),
            rounds_returned_to_lobby=int(
                finish_detail["roundsReturnedToLobby"]
            ),
            error_code=error_code,
            error=error_message,
            exit_code=exit_code,
            run_dir=recorder.run_dir,
            report_drain=report_drain,
        )

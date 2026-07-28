from __future__ import annotations

from dataclasses import dataclass
import time
from typing import Callable, Protocol

import numpy as np

from .config import RunnerConfig
from .control import TaskControl, TaskStopRequested
from .input_win32 import EmergencyStop
from .ocr_obstacles import ObstacleDecision, OcrObstacleDetector
from .ocr_states import OcrStateDecision, OcrStateDetector
from .runner import FrameSource, InputSender
from .vision import NearBlackFrameError, StateScore, VisionDetector


class RecoverableSafetyGuard(Protocol):
    def ensure_not_aborted(self) -> None: ...
    def ensure_target_foreground(self) -> None: ...
    def target_is_foreground(self) -> bool: ...


class SupervisorRecorder(Protocol):
    run_dir: object
    def log(self, event: str, **payload: object) -> None: ...
    def screenshot(self, stage: str, frame: np.ndarray) -> object: ...
    def write_status(self, payload: dict[str, object]) -> None: ...


@dataclass(frozen=True)
class ActionSpec:
    name: str
    kind: str
    confirm_ms: int
    max_attempts: int
    allowed_next_states: tuple[str, ...] = ()


@dataclass
class PendingAction:
    spec: ActionSpec
    origin_state: str
    origin_observation_version: int
    attempt: int
    retry_at: float


class TaskSupervisor:
    """Long-running, fail-closed observer for a configured task flow.

    A foreground loss, unknown frame, capture failure, or unconfirmed input is
    represented as a recoverable paused state. Only F8, Ctrl+C, or a truly
    unrecoverable outer-process failure ends the session.
    """

    MATCH_STATE_STAGES = {
        "CONTINUE": (0, "ENTER_GAME"),
        "LOBBY_READY": (1, "QUEUE_MATCH"),
        "LEGEND_SELECT": (2, "WAIT_LEGEND_SELECT"),
        "DROPSHIP_JUMPMASTER_WAIT": (3, "WAIT_LAUNCH_READY"),
        # Reserved for a future non-jumpmaster screenshot calibration.
        "DROPSHIP_FOLLOWING": (3, "DETACH_FROM_SQUAD"),
        "LAUNCH_READY": (4, "LAUNCH"),
        "FREEFALL": (5, "WAIT_LANDING"),
        "LANDED": (6, "IN_MATCH"),
    }

    def __init__(
        self,
        config: RunnerConfig,
        detector: VisionDetector,
        source: FrameSource,
        sender: InputSender,
        guard: RecoverableSafetyGuard,
        recorder: SupervisorRecorder,
        *,
        mode: str = "match",
        ai_fallback: bool = False,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        notify: Callable[[str], None] = lambda _: None,
        unknown_grace_ms: int | None = None,
        resume_stabilize_ms: int | None = None,
        obstacle_detector: OcrObstacleDetector | None = None,
        guided_detector: OcrStateDetector | None = None,
        safe_obstacles_armed: bool = False,
        control: TaskControl | None = None,
        control_api_url: str | None = None,
    ):
        if mode not in {"match", "tutorial"}:
            raise ValueError(f"不支持的任务模式：{mode}")
        if ai_fallback:
            raise ValueError("阶段 A 尚未实现 AI 兜底，aiFallback 必须为 false")

        self.config = config
        self.detector = detector
        self.source = source
        self.sender = sender
        self.guard = guard
        self.recorder = recorder
        self.mode = mode
        self.ai_fallback = ai_fallback
        self.sleep = sleep
        self.monotonic = monotonic
        self.notify = notify
        self.obstacle_detector = obstacle_detector
        self.guided_detector = guided_detector
        self.safe_obstacles_armed = safe_obstacles_armed
        self.control = control
        self.control_api_url = control_api_url
        self.unknown_grace_ms = (
            int(config.timing.get("unknownGraceMs", 5000))
            if unknown_grace_ms is None
            else unknown_grace_ms
        )
        self.resume_stabilize_ms = (
            int(config.timing.get("resumeStabilizeMs", 700))
            if resume_stabilize_ms is None
            else resume_stabilize_ms
        )

        self.runtime_state = "PENDING_FOREGROUND"
        self.pause_reason: str | None = "等待 Apex 位于前台"
        self.foreground: bool | None = None
        self.resume_not_before = 0.0
        self.observed_state: str | None = None
        self.observed_since = self.monotonic()
        self.observation_version = 0
        self.goal_progress_version = 0
        self.goal_stage = "NOT_STARTED"
        self.max_goal_stage_index = -1
        self.last_score: float | None = None
        self.last_event = "TASK_STARTED"
        self.last_screenshot: str | None = None

        self.candidate_state: str | None = None
        self.candidate_count = 0
        self.unknown_since: float | None = None
        self.capture_error: str | None = None
        self.pending_action: PendingAction | None = None
        self.action_attempts: dict[str, int] = {}
        self.handled_observation_versions: set[int] = set()
        self.blocked_observation_version: int | None = None
        self.stalled_observation_version: int | None = None
        self.last_ocr: dict[str, object] | None = None
        self.last_ocr_error: str | None = None
        self.current_obstacle_rule: str | None = None
        self.ocr_candidate_rule: str | None = None
        self.ocr_candidate_count = 0
        self.next_ocr_at = 0.0
        self.guided_candidate_rule: str | None = None
        self.guided_candidate_count = 0
        self.next_guided_ocr_at = 0.0
        self.last_guided_ocr: dict[str, object] | None = None
        self.last_guided_ocr_error: str | None = None
        self.manual_paused = False
        self.goal_reached = False

        self.state_stages = self._load_state_stages()
        self.manual_states = {
            str(value) for value in config.task.get("manualStates", [])
        }
        self.terminal_states = {
            str(value) for value in config.task.get("terminalStates", [])
        }
        self.stall_timeouts = {
            str(key): int(value)
            for key, value in config.task.get("stallTimeoutMs", {}).items()
        }

        self.action_specs = {
            "CONTINUE": ActionSpec(
                "continueClick",
                "click",
                int(config.timing.get("readyClickConfirmDelayMs", 1500)),
                int(config.timing.get("readyClickAttempts", 3)),
            ),
            "LOBBY_READY": ActionSpec(
                "readyClick",
                "click",
                int(config.timing.get("readyClickConfirmDelayMs", 1500)),
                int(config.timing.get("readyClickAttempts", 3)),
            ),
            # LCTRL toggles detach/rejoin. Retrying it without positive
            # evidence could undo the first press, so it is intentionally
            # limited to one attempt.
            "DROPSHIP_FOLLOWING": ActionSpec(
                "detachScanCode",
                "key",
                int(config.timing.get("launchReadyTimeoutMs", 10000)),
                1,
            ),
            "LAUNCH_READY": ActionSpec(
                "launchScanCode",
                "key",
                int(config.timing.get("freefallTimeoutMs", 15000)),
                3,
            ),
        }
        if mode == "tutorial":
            self.action_specs = self._load_task_actions()

        unknown_task_states = (
            set(self.action_specs) | self.manual_states | self.terminal_states
        ) - set(self.state_stages)
        if unknown_task_states:
            raise ValueError(
                "任务状态未配置进度阶段：" + ", ".join(sorted(unknown_task_states))
            )

        unknown_postconditions = {
            state
            for spec in self.action_specs.values()
            for state in spec.allowed_next_states
            if state not in self.state_stages
        }
        if unknown_postconditions:
            raise ValueError(
                "动作后置状态未配置进度阶段："
                + ", ".join(sorted(unknown_postconditions))
            )

        supported_action_kinds = {"click", "key", "sequence"}
        for spec in self.action_specs.values():
            if spec.name not in config.actions:
                raise ValueError(f"任务动作未配置：{spec.name}")
            if spec.kind not in supported_action_kinds:
                raise ValueError(f"不支持的任务动作类型：{spec.kind}")
            if not 1 <= spec.max_attempts <= 8:
                raise ValueError(
                    f"任务动作 maxAttempts 非法：{spec.name}={spec.max_attempts}"
                )
            if not 500 <= spec.confirm_ms <= 30_000:
                raise ValueError(
                    f"任务动作 confirmMs 非法：{spec.name}={spec.confirm_ms}"
                )

        if mode == "tutorial" and guided_detector is None:
            raise ValueError("tutorial 模式必须配置 guidedOcr 状态字典")

        self.recorder.log(
            "TASK_STARTED",
            mode=mode,
            aiFallback=ai_fallback,
            safeObstaclesArmed=safe_obstacles_armed,
            controlApi=control_api_url,
        )
        self._write_status()

    def _load_state_stages(self) -> dict[str, tuple[int, str]]:
        payload = self.config.task.get("stateStages")
        if not payload:
            if self.mode == "match":
                return dict(self.MATCH_STATE_STAGES)
            raise ValueError("tutorial 配置缺少 task.stateStages")
        stages: dict[str, tuple[int, str]] = {}
        for state, item in payload.items():
            stages[str(state)] = (int(item["index"]), str(item["goal"]))
        return stages

    def _load_task_actions(self) -> dict[str, ActionSpec]:
        result: dict[str, ActionSpec] = {}
        for state, item in self.config.task.get("actions", {}).items():
            result[str(state)] = ActionSpec(
                name=str(item["name"]),
                kind=str(item["kind"]),
                confirm_ms=int(item.get("confirmMs", 1500)),
                max_attempts=int(item.get("maxAttempts", 1)),
                allowed_next_states=tuple(str(value) for value in item.get("allowedNextStates", [])),
            )
        return result

    def _status_payload(self) -> dict[str, object]:
        pending: dict[str, object] | None = None
        if self.pending_action is not None:
            pending = {
                "name": self.pending_action.spec.name,
                "originState": self.pending_action.origin_state,
                "originObservationVersion": self.pending_action.origin_observation_version,
                "attempt": self.pending_action.attempt,
            }
        control_state = None if self.control is None else self.control.snapshot()
        return {
            "task": {
                "mode": self.mode,
                "aiFallback": self.ai_fallback,
                "safeObstaclesArmed": self.safe_obstacles_armed,
            },
            "runtimeState": self.runtime_state,
            "foreground": bool(self.foreground),
            "observedState": self.observed_state,
            "observationVersion": self.observation_version,
            "goalProgressVersion": self.goal_progress_version,
            "goalStage": self.goal_stage,
            "goalReached": self.goal_reached,
            "score": None if self.last_score is None else round(self.last_score, 4),
            "pendingAction": pending,
            "pauseReason": self.pause_reason,
            "lastEvent": self.last_event,
            "lastScreenshot": self.last_screenshot,
            "ocr": {
                "matchedRule": self.current_obstacle_rule,
                "lastProbe": self.last_ocr,
            },
            "guidedOcr": self.last_guided_ocr,
            "control": {
                "url": self.control_api_url,
                "manualPaused": False if control_state is None else control_state.paused,
            },
        }

    def _write_status(self) -> None:
        self.recorder.write_status(self._status_payload())

    def _emit(self, event: str, message: str | None = None, **payload: object) -> None:
        self.last_event = event
        self.recorder.log(event, **payload)
        self._write_status()
        if message:
            self.notify(message)

    def _safe_release(self, reason: str) -> None:
        try:
            self.sender.release_all()
            self.recorder.log("INPUT_RELEASE_ALL", reason=reason)
        except Exception as error:
            self.recorder.log("INPUT_RELEASE_FAILED", reason=reason, error=str(error))

    def _reset_candidate(self) -> None:
        self.candidate_state = None
        self.candidate_count = 0

    def _reset_ocr_candidate(self) -> None:
        self.ocr_candidate_rule = None
        self.ocr_candidate_count = 0

    def _reset_guided_candidate(self) -> None:
        self.guided_candidate_rule = None
        self.guided_candidate_count = 0

    def _invalidate_pending(self, reason: str) -> None:
        if self.pending_action is None:
            return
        pending = self.pending_action
        self.pending_action = None
        self.recorder.log(
            "ACTION_INVALIDATED",
            action=pending.spec.name,
            originState=pending.origin_state,
            originObservationVersion=pending.origin_observation_version,
            reason=reason,
        )

    def _handle_foreground(self, now: float) -> bool:
        is_foreground = self.guard.target_is_foreground()
        if not is_foreground:
            if self.foreground is not False:
                event = "FOREGROUND_WAITING" if self.foreground is None else "FOREGROUND_PAUSED"
                self._invalidate_pending("FOREGROUND_LOST")
                self._safe_release("FOREGROUND_LOST")
                self.foreground = False
                self.runtime_state = "PENDING_FOREGROUND" if event == "FOREGROUND_WAITING" else "FOREGROUND_PAUSED"
                self.pause_reason = "等待 Apex 位于前台"
                self.observed_state = None
                self.last_score = None
                self.current_obstacle_rule = None
                self._reset_candidate()
                self._reset_ocr_candidate()
                self._reset_guided_candidate()
                self.unknown_since = None
                self._emit(event, "[暂停] Apex 不在前台；输入已释放，切回后会自动重识别")
            return False

        if self.foreground is not True:
            first_acquire = self.foreground is None
            self.foreground = True
            self.runtime_state = "OBSERVING"
            self.pause_reason = None
            self.observed_state = None
            self.last_score = None
            self.current_obstacle_rule = None
            self._reset_candidate()
            self._reset_ocr_candidate()
            self._reset_guided_candidate()
            self.unknown_since = None
            self.resume_not_before = now + self.resume_stabilize_ms / 1000
            event = "FOREGROUND_ACQUIRED" if first_acquire else "FOREGROUND_RESUMED"
            self._emit(event, "[恢复] 已检测到 Apex 前台，等待画面稳定后重新识别")
            return False

        return now >= self.resume_not_before

    def _handle_manual_control(self, now: float) -> bool:
        if self.control is None:
            return True
        state = self.control.snapshot()
        if state.stop_requested:
            raise TaskStopRequested("用户从本地控制台停止任务")
        if self.control.consume_release():
            self._safe_release("CONTROL_API")
            self._emit("CONTROL_RELEASE_ALL", "[控制台] 已释放全部输入")
        if state.paused:
            if not self.manual_paused:
                self.manual_paused = True
                self._invalidate_pending("MANUAL_PAUSE")
                self._safe_release("MANUAL_PAUSE")
                self.runtime_state = "MANUAL_PAUSED"
                self.pause_reason = "用户从本地控制台暂停任务"
                self._reset_candidate()
                self._reset_ocr_candidate()
                self._reset_guided_candidate()
                self._emit("MANUAL_PAUSED", "[暂停] 已从本地控制台暂停")
            return False
        if self.manual_paused:
            self.manual_paused = False
            self.foreground = None
            self.observed_state = None
            self.current_obstacle_rule = None
            self.unknown_since = None
            self.runtime_state = "PENDING_FOREGROUND"
            self.pause_reason = "等待重新确认 Apex 前台"
            self.resume_not_before = now + self.resume_stabilize_ms / 1000
            self._reset_candidate()
            self._reset_ocr_candidate()
            self._reset_guided_candidate()
            self._emit("MANUAL_RESUMED", "[恢复] 控制台已继续，重新确认前台和画面")
            return False
        return True

    def _confirm_pending(self, evidence_state: str) -> bool:
        if self.pending_action is None:
            return True
        pending = self.pending_action
        allowed = pending.spec.allowed_next_states
        if allowed and evidence_state not in allowed:
            self.pending_action = None
            self.recorder.log(
                "ACTION_POSTCONDITION_REJECTED",
                action=pending.spec.name,
                originState=pending.origin_state,
                evidenceState=evidence_state,
                allowedNextStates=list(allowed),
                attempt=pending.attempt,
            )
            return False
        self.pending_action = None
        self.recorder.log(
            "ACTION_CONFIRMED",
            action=pending.spec.name,
            originState=pending.origin_state,
            evidenceState=evidence_state,
            attempt=pending.attempt,
        )
        return True

    def _transition_special(self, state: str, runtime_state: str, reason: str) -> None:
        if self.observed_state == state and self.runtime_state == runtime_state:
            return
        if state == "LOADING":
            if self.pending_action is not None and not self.pending_action.spec.allowed_next_states:
                self._confirm_pending(state)
        else:
            self._invalidate_pending(state)
        self.observed_state = state
        self.current_obstacle_rule = None
        self.observed_since = self.monotonic()
        self.observation_version += 1
        self.last_score = None
        self.runtime_state = runtime_state
        self.pause_reason = reason
        self.blocked_observation_version = None
        self.stalled_observation_version = None
        self._reset_candidate()
        self._reset_ocr_candidate()
        self._reset_guided_candidate()
        self._emit(
            f"STATE_{state}",
            "[识别] 加载画面，继续等待" if state == "LOADING" else f"[暂停] {reason}",
            state=state,
            observationVersion=self.observation_version,
        )

    def _handle_capture_error(self, error: Exception) -> None:
        message = str(error)
        if self.runtime_state == "CAPTURE_PAUSED" and self.capture_error == message:
            return
        self.capture_error = message
        self._safe_release("CAPTURE_ERROR")
        self._transition_special("CAPTURE_ERROR", "CAPTURE_PAUSED", message)

    def _handle_unknown(self, frame: np.ndarray, ranking: list[StateScore], now: float) -> None:
        self._reset_candidate()
        if self.unknown_since is None:
            self.unknown_since = now
            # A short unrecognized interval is normal while a click opens a
            # page or a task prompt changes. Keep the pending postcondition
            # alive through the grace window; UNKNOWN will invalidate it only
            # after the window expires.
            self._safe_release("UNSTABLE_OBSERVATION")
            self.runtime_state = "OBSERVING"
            self.pause_reason = "画面暂未稳定识别"
            self._write_status()
            return
        if (now - self.unknown_since) * 1000 < self.unknown_grace_ms:
            return
        if self.observed_state == "UNKNOWN" and self.runtime_state == "UNKNOWN_PAUSED":
            return

        leader = ranking[0] if ranking else None
        self._safe_release("UNKNOWN")
        self._transition_special("UNKNOWN", "UNKNOWN_PAUSED", "未知画面；未发送输入")
        screenshot = self.recorder.screenshot("UNKNOWN", frame)
        self.last_screenshot = str(screenshot)
        self._emit(
            "UNKNOWN_EVIDENCE_SAVED",
            "[暂停] 未识别画面已留证；程序仍在运行，会继续低频观察",
            leader=None if leader is None else leader.state,
            leaderScore=None if leader is None else round(leader.score, 4),
            path=str(screenshot),
        )

    def _maybe_execute_spec(self, spec: ActionSpec, now: float) -> None:
        if self.blocked_observation_version == self.observation_version:
            return
        if self.pending_action is not None:
            if now < self.pending_action.retry_at:
                return
            retry_spec = self.pending_action.spec
            self.pending_action = None
            self._execute_action(retry_spec, now)
            return
        if self.observation_version in self.handled_observation_versions:
            return
        self._execute_action(spec, now)

    def _accept_obstacle(
        self,
        decision: ObstacleDecision,
        frame: np.ndarray,
        now: float,
    ) -> None:
        state = decision.state
        if self.observed_state != state:
            previous = self.observed_state
            if self.pending_action is not None:
                self._confirm_pending(state)
            self.observed_state = state
            self.observed_since = now
            self.observation_version += 1
            self.last_score = decision.confidence
            self.current_obstacle_rule = decision.rule_id
            self.runtime_state = "RUNNING"
            self.pause_reason = None
            self.blocked_observation_version = None
            self.stalled_observation_version = None
            screenshot = self.recorder.screenshot(state, frame)
            self.last_screenshot = str(screenshot)
            self._emit(
                "OCR_STATE_DETECTED",
                f"[OCR] {state}，规则 {decision.rule_id}，置信度 {decision.confidence:.3f}",
                state=state,
                previousState=previous,
                ruleId=decision.rule_id,
                score=round(decision.confidence, 4),
                observationVersion=self.observation_version,
            )
        else:
            self.last_score = decision.confidence
            self.current_obstacle_rule = decision.rule_id

        spec = ActionSpec(
            decision.action.name,
            decision.action.kind,
            decision.action.confirm_ms,
            decision.action.max_attempts,
        )
        if not self.safe_obstacles_armed:
            if self.observation_version not in self.handled_observation_versions:
                self.handled_observation_versions.add(self.observation_version)
                self.runtime_state = "SAFE_ACTION_PAUSED"
                self.pause_reason = "OCR 已识别安全障碍，但清障动作尚未武装"
                self._emit(
                    "SAFE_ACTION_DISARMED",
                    "[OCR] 已识别障碍；当前仅观察，不发送清障输入",
                    state=state,
                    ruleId=decision.rule_id,
                    proposedAction=spec.name,
                    observationVersion=self.observation_version,
                )
            return
        self._maybe_execute_spec(spec, now)

    def _try_ocr_obstacle(self, frame: np.ndarray, now: float) -> bool:
        if self.obstacle_detector is None or self.unknown_since is None:
            return False
        already_obstacle = self.observed_state in self.obstacle_detector.states
        if not already_obstacle and (now - self.unknown_since) * 1000 < self.unknown_grace_ms:
            return False
        if now < self.next_ocr_at:
            return self.ocr_candidate_rule is not None or already_obstacle

        self.next_ocr_at = now + self.config.timing.get("ocrScanIntervalMs", 2000) / 1000
        analysis = self.obstacle_detector.analyze(frame)
        self.last_ocr = analysis.as_event_payload()
        self._emit("OCR_ANALYZED", None, **self.last_ocr)
        if analysis.error:
            if analysis.error != self.last_ocr_error:
                self.last_ocr_error = analysis.error
                self._emit(
                    "OCR_UNAVAILABLE",
                    f"[OCR] 不可用，保持未知暂停：{analysis.error}",
                    error=analysis.error,
                )
            self._reset_ocr_candidate()
            return False
        self.last_ocr_error = None
        decision = analysis.decision
        if decision is None:
            self._reset_ocr_candidate()
            return False
        if self.ocr_candidate_rule == decision.rule_id:
            self.ocr_candidate_count += 1
        else:
            self.ocr_candidate_rule = decision.rule_id
            self.ocr_candidate_count = 1

        required = int(self.config.ocr.get("stableObservations", 2))
        if self.ocr_candidate_count < required:
            self.runtime_state = "OBSERVING"
            self.pause_reason = "等待 OCR 候选稳定"
            self._emit(
                "OCR_CANDIDATE",
                f"[OCR] 候选 {decision.rule_id}，等待再次确认",
                ruleId=decision.rule_id,
                state=decision.state,
                count=self.ocr_candidate_count,
                required=required,
            )
            return True
        self._accept_obstacle(decision, frame, now)
        return True

    def _stall_timeout_ms(self, state: str) -> int | None:
        if state in self.stall_timeouts:
            value = self.stall_timeouts[state]
            return None if value <= 0 else value
        return {
            "CONTINUE": self.config.timing.get("continueTimeoutMs", 300000),
            "LOBBY_READY": self.config.timing.get("lobbyTimeoutMs", 120000),
            "LEGEND_SELECT": self.config.timing.get("dropshipTimeoutMs", 900000),
            "DROPSHIP_JUMPMASTER_WAIT": self.config.timing.get("dropshipTimeoutMs", 900000),
            "DROPSHIP_FOLLOWING": self.config.timing.get("dropshipTimeoutMs", 900000),
            "LAUNCH_READY": self.config.timing.get("freefallTimeoutMs", 15000) * 3,
            "FREEFALL": self.config.timing.get("landingTimeoutMs", 120000),
            "LANDED": None,
        }.get(state)

    def _check_stall(self, now: float) -> bool:
        if self.observed_state not in self.state_stages:
            return False
        timeout_ms = self._stall_timeout_ms(self.observed_state)
        if timeout_ms is None or (now - self.observed_since) * 1000 < timeout_ms:
            return False
        if self.stalled_observation_version == self.observation_version:
            return True
        self.stalled_observation_version = self.observation_version
        self.blocked_observation_version = self.observation_version
        self._invalidate_pending("STALL_WATCHDOG")
        self._safe_release("STALL_WATCHDOG")
        self.runtime_state = "STALLED_PAUSED"
        self.pause_reason = f"子目标 {self.goal_stage} 长时间没有进展"
        self._emit(
            "PROGRESS_STALLED",
            f"[暂停] {self.pause_reason}；继续观察，画面变化后会恢复",
            state=self.observed_state,
            goalStage=self.goal_stage,
            observationVersion=self.observation_version,
            timeoutMs=timeout_ms,
        )
        return True

    def _execute_sequence(self, action_name: str) -> list[dict[str, object]]:
        raw_steps = self.config.actions[action_name]
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError(f"动作序列 {action_name} 为空")
        executed: list[dict[str, object]] = []
        for index, raw_step in enumerate(raw_steps, start=1):
            if not isinstance(raw_step, dict):
                raise ValueError(f"动作序列 {action_name} 第 {index} 步不是对象")
            self.guard.ensure_target_foreground()
            step_type = str(raw_step["type"])
            duration_ms = int(raw_step.get("durationMs", self.config.timing["keyTapMs"]))
            if not 0 <= duration_ms <= 10000:
                raise ValueError(f"动作序列 {action_name} 第 {index} 步时长无效")
            if step_type == "tapKey":
                scan_code = int(raw_step["scanCode"])
                self.sender.tap_scan_code(scan_code, duration_ms)
                detail = {"type": step_type, "scanCode": scan_code, "durationMs": duration_ms}
            elif step_type == "holdKeys":
                scan_codes = tuple(int(value) for value in raw_step["scanCodes"])
                if not scan_codes:
                    raise ValueError(f"动作序列 {action_name} 第 {index} 步没有按键")
                self.sender.hold_scan_codes(scan_codes, duration_ms)
                detail = {"type": step_type, "scanCodes": list(scan_codes), "durationMs": duration_ms}
            elif step_type == "moveMouse":
                dx = int(raw_step.get("dx", 0))
                dy = int(raw_step.get("dy", 0))
                if abs(dx) > 1200 or abs(dy) > 900:
                    raise ValueError(f"动作序列 {action_name} 第 {index} 步鼠标位移过大")
                self.sender.move_mouse(dx, dy)
                detail = {"type": step_type, "dx": dx, "dy": dy}
            elif step_type == "holdMouse":
                button = str(raw_step["button"])
                self.sender.hold_mouse_button(button, duration_ms)
                detail = {"type": step_type, "button": button, "durationMs": duration_ms}
            elif step_type == "wait":
                self.sleep(duration_ms / 1000)
                detail = {"type": step_type, "durationMs": duration_ms}
            else:
                raise ValueError(f"动作序列 {action_name} 第 {index} 步类型未知：{step_type}")
            executed.append(detail)
        return executed

    def _execute_action(self, spec: ActionSpec, now: float) -> None:
        state = self.observed_state
        if state is None:
            return
        attempts = self.action_attempts.get(state, 0)
        if attempts >= spec.max_attempts:
            self.pending_action = None
            self.blocked_observation_version = self.observation_version
            self.runtime_state = "ACTION_PAUSED"
            self.pause_reason = f"动作 {spec.name} 未得到后置确认"
            self._safe_release("ACTION_UNCONFIRMED")
            self._emit(
                "ACTION_PAUSED",
                f"[暂停] {self.pause_reason}；不会继续盲目重试",
                action=spec.name,
                state=state,
                attempts=attempts,
                observationVersion=self.observation_version,
            )
            return

        try:
            self.guard.ensure_target_foreground()
            if spec.kind == "click":
                x, y = (int(value) for value in self.config.actions[spec.name])
                self.sender.click(x, y)
                detail: dict[str, object] = {"x": x, "y": y}
            elif spec.kind == "key":
                scan_code = int(self.config.actions[spec.name])
                self.sender.tap_scan_code(scan_code, self.config.timing["keyTapMs"])
                detail = {"scanCode": scan_code}
            elif spec.kind == "sequence":
                detail = {"steps": self._execute_sequence(spec.name)}
            else:
                raise ValueError(f"未知动作类型：{spec.kind}")
        except EmergencyStop:
            raise
        except Exception as error:
            self.pending_action = None
            self.blocked_observation_version = self.observation_version
            self.runtime_state = "ACTION_PAUSED"
            self.pause_reason = f"动作 {spec.name} 发送失败：{error}"
            self._safe_release("ACTION_ERROR")
            self._emit(
                "ACTION_ERROR",
                f"[暂停] {self.pause_reason}",
                action=spec.name,
                state=state,
                error=str(error),
                observationVersion=self.observation_version,
            )
            return

        attempt = attempts + 1
        self.action_attempts[state] = attempt
        self.handled_observation_versions.add(self.observation_version)
        self.pending_action = PendingAction(
            spec=spec,
            origin_state=state,
            origin_observation_version=self.observation_version,
            attempt=attempt,
            retry_at=now + spec.confirm_ms / 1000,
        )
        self.runtime_state = "RUNNING"
        self.pause_reason = None
        self._emit(
            "ACTION_SENT",
            f"[输入] {spec.name}，等待后置画面确认（第 {attempt} 次）",
            action=spec.name,
            kind=spec.kind,
            state=state,
            attempt=attempt,
            observationVersion=self.observation_version,
            **detail,
        )

    def _maybe_act(self, now: float) -> None:
        state = self.observed_state
        if state not in self.state_stages:
            return
        if self.blocked_observation_version == self.observation_version:
            return

        stage_index, _ = self.state_stages[state]
        if stage_index < self.max_goal_stage_index:
            if self.observation_version not in self.handled_observation_versions:
                self.handled_observation_versions.add(self.observation_version)
                self._emit(
                    "STATE_BEHIND_GOAL_IGNORED",
                    "[守护] 画面落后于已确认进度，不发送输入",
                    state=state,
                    maxGoalStageIndex=self.max_goal_stage_index,
                    observationVersion=self.observation_version,
                )
            return

        spec = self.action_specs.get(state)
        if spec is not None:
            self._maybe_execute_spec(spec, now)
            return

        if self.observation_version in self.handled_observation_versions:
            return

        self.handled_observation_versions.add(self.observation_version)
        if state == "LEGEND_SELECT":
            self._emit(
                "NO_INPUT",
                "[分支] 英雄选择不操作，等待自动选择",
                state=state,
                reason="AUTO_SELECT",
            )
        elif state == "DROPSHIP_JUMPMASTER_WAIT":
            self._emit(
                "NO_INPUT",
                "[分支] 当前是跳伞指挥官，不按 LCTRL，等待 E 发射",
                state=state,
                reason="JUMPMASTER_WAIT",
            )
        elif state in {"FREEFALL", "LANDED"}:
            self._safe_release(f"{state}_CONFIRMED")
            self._emit(
                "NO_INPUT",
                "[局内] 已落地，阶段 A 保持观察且不发送局内行为" if state == "LANDED" else "[输入] 已确认自由落体，释放全部输入",
                state=state,
                reason="OBSERVE_ONLY" if state == "LANDED" else "RELEASE_ALL",
            )

    def _accept_state(
        self,
        state: str,
        score: float,
        frame: np.ndarray,
        now: float,
        *,
        source: str,
        anchor_scores: tuple[float, ...] = (),
        rule_id: str | None = None,
    ) -> None:
        if state not in self.state_stages:
            return
        self.unknown_since = None
        self.capture_error = None
        self.current_obstacle_rule = None
        self._reset_ocr_candidate()
        for attempt_state in tuple(self.action_attempts):
            if attempt_state not in self.state_stages:
                self.action_attempts.pop(attempt_state, None)
        if self.observed_state != state:
            previous = self.observed_state
            if self.pending_action is not None:
                self._confirm_pending(state)
            self.observed_state = state
            self.observed_since = now
            self.observation_version += 1
            self.last_score = score
            self.runtime_state = "RUNNING"
            self.pause_reason = None
            self.blocked_observation_version = None
            self.stalled_observation_version = None

            stage_index, goal_stage = self.state_stages[state]
            if stage_index > self.max_goal_stage_index:
                self.max_goal_stage_index = stage_index
                self.goal_stage = goal_stage
                self.goal_progress_version += 1
                for old_state, (old_index, _) in self.state_stages.items():
                    if old_index < stage_index:
                        self.action_attempts.pop(old_state, None)

            screenshot = self.recorder.screenshot(state, frame)
            self.last_screenshot = str(screenshot)
            self._emit(
                "STATE_DETECTED" if source == "template" else "OCR_TASK_STATE_DETECTED",
                f"[识别] {state}，置信度 {score:.3f}",
                state=state,
                previousState=previous,
                source=source,
                ruleId=rule_id,
                score=round(score, 4),
                anchorScores=[round(value, 4) for value in anchor_scores],
                observationVersion=self.observation_version,
                goalProgressVersion=self.goal_progress_version,
                goalStage=self.goal_stage,
            )
        else:
            self.last_score = score

        if state in self.manual_states:
            if self.runtime_state != "MANUAL_REQUIRED":
                self._invalidate_pending("MANUAL_REQUIRED")
                self._safe_release("MANUAL_REQUIRED")
                self.runtime_state = "MANUAL_REQUIRED"
                self.pause_reason = f"{state} 需要人工完成；程序不会输入账号或验证码"
                self._emit(
                    "MANUAL_REQUIRED",
                    f"[暂停] {self.pause_reason}",
                    state=state,
                    observationVersion=self.observation_version,
                )
            return

        if state in self.terminal_states:
            if not self.goal_reached:
                self.goal_reached = True
                self._invalidate_pending("GOAL_REACHED")
                self._safe_release("GOAL_REACHED")
                self.runtime_state = "GOAL_REACHED"
                self.pause_reason = None
                self.handled_observation_versions.add(self.observation_version)
                self._emit(
                    "TASK_GOAL_REACHED",
                    f"[完成] 任务目标已到达：{self.goal_stage}",
                    state=state,
                    goalStage=self.goal_stage,
                    observationVersion=self.observation_version,
                )
            return

        if self._check_stall(now):
            return
        self._maybe_act(now)

    def _accept_known(self, score: StateScore, frame: np.ndarray, now: float) -> None:
        self._reset_guided_candidate()
        self._accept_state(
            score.state,
            score.score,
            frame,
            now,
            source="template",
            anchor_scores=score.anchor_scores,
        )

    def _try_guided_state(self, frame: np.ndarray, now: float) -> bool:
        if self.guided_detector is None:
            return False
        if now < self.next_guided_ocr_at:
            return self.guided_candidate_rule is not None

        interval_ms = int(self.config.guided_ocr.get("scanIntervalMs", 900))
        self.next_guided_ocr_at = now + interval_ms / 1000
        analysis = self.guided_detector.analyze(frame)
        self.last_guided_ocr = analysis.as_event_payload()
        self._emit("OCR_TASK_ANALYZED", None, **self.last_guided_ocr)
        if analysis.error:
            if analysis.error != self.last_guided_ocr_error:
                self.last_guided_ocr_error = analysis.error
                self._emit(
                    "OCR_TASK_UNAVAILABLE",
                    f"[OCR] 教程状态识别不可用，保持暂停：{analysis.error}",
                    error=analysis.error,
                )
            self._reset_guided_candidate()
            return False
        self.last_guided_ocr_error = None
        decision: OcrStateDecision | None = analysis.decision
        if decision is None or decision.state not in self.state_stages:
            self._reset_guided_candidate()
            return False
        if self.guided_candidate_rule == decision.rule_id:
            self.guided_candidate_count += 1
        else:
            self.guided_candidate_rule = decision.rule_id
            self.guided_candidate_count = 1

        required = int(self.config.guided_ocr.get("stableObservations", 2))
        if self.guided_candidate_count < required:
            self.runtime_state = "OBSERVING"
            self.pause_reason = "等待教程 OCR 候选稳定"
            self._emit(
                "OCR_TASK_CANDIDATE",
                f"[OCR] 教程候选 {decision.rule_id}，等待再次确认",
                ruleId=decision.rule_id,
                state=decision.state,
                count=self.guided_candidate_count,
                required=required,
            )
            return True
        self._accept_state(
            decision.state,
            decision.confidence,
            frame,
            now,
            source="guidedOcr",
            rule_id=decision.rule_id,
        )
        return True

    def step(self) -> None:
        """Process one observation iteration; exposed for deterministic tests."""
        now = self.monotonic()
        self.guard.ensure_not_aborted()
        if not self._handle_manual_control(now):
            return
        if not self._handle_foreground(now):
            return

        try:
            frame = self.source.grab()
            self.detector.validate_frame(frame)
            ranking = self.detector.rank_states(frame)
        except NearBlackFrameError:
            self.unknown_since = None
            self.capture_error = None
            self._transition_special("LOADING", "LOADING", "加载画面")
            return
        except Exception as error:
            self._handle_capture_error(error)
            return

        matched = next((item for item in ranking if item.matched), None)
        if matched is None:
            if self._try_guided_state(frame, now):
                return
            if self._try_ocr_obstacle(frame, now):
                return
            self._handle_unknown(frame, ranking, now)
            return

        if self.candidate_state == matched.state:
            self.candidate_count += 1
        else:
            self.candidate_state = matched.state
            self.candidate_count = 1

        if self.candidate_count < self.config.states[matched.state].stable_frames:
            return
        self._accept_known(matched, frame, now)

    def run(self, *, max_iterations: int | None = None) -> None:
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            self.step()
            iterations += 1
            if self.foreground is not True:
                delay_ms = self.config.timing.get("foregroundPollMs", 500)
            elif self.runtime_state in {"UNKNOWN_PAUSED", "SAFE_ACTION_PAUSED"}:
                delay_ms = self.config.timing.get("unknownPollMs", 2000)
            elif self.obstacle_detector is not None and self.observed_state in self.obstacle_detector.states:
                delay_ms = self.config.timing.get("unknownPollMs", 2000)
            elif self.guided_detector is not None and self.observed_state in self.guided_detector.states:
                delay_ms = self.config.guided_ocr.get("scanIntervalMs", 900)
            elif self.observed_state == "LANDED":
                delay_ms = self.config.timing.get("groundPollMs", 1500)
            else:
                delay_ms = self.config.timing["pollMs"]
            self.sleep(delay_ms / 1000)

    def stop(self, status: str, reason: str) -> None:
        self._invalidate_pending(status)
        self._safe_release(status)
        self.runtime_state = status
        self.pause_reason = reason
        self._emit(
            "TASK_STOPPED",
            f"[停止] {reason}",
            status=status,
            reason=reason,
            observationVersion=self.observation_version,
            goalProgressVersion=self.goal_progress_version,
        )

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import time
from typing import Any, Callable, Protocol

import numpy as np

from .capabilities import Capability, CapabilityDispatcher, Decision
from .config import RunnerConfig
from .ocr_states import OcrStateDetector


# A capability set that reached this module has already been validated for
# these; the check in `_validate_actions` is what makes that true rather than
# assumed, because the alternative is discovering a bad action name halfway
# into a match with an input already sent.
SUPPORTED_KINDS = frozenset({"click", "key", "sequence"})


# Declared here rather than imported from `runner`: that module pulls in the
# template matcher and therefore OpenCV, and nothing in the capability loop
# needs either. Keeping it out means this whole path stays testable without a
# vision stack installed.
class PilotFrameSource(Protocol):
    def grab(self) -> np.ndarray: ...


class PilotGuard(Protocol):
    def ensure_not_aborted(self) -> None: ...
    def ensure_target_foreground(self) -> None: ...
    def target_is_foreground(self) -> bool: ...


class PilotSender(Protocol):
    def click(self, x: int, y: int) -> None: ...
    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None: ...
    def release_all(self) -> None: ...


class PilotRecorder(Protocol):
    run_dir: Path
    def log(self, event: str, **payload: object) -> None: ...
    def screenshot(self, stage: str, frame: np.ndarray) -> object: ...


@dataclass(frozen=True)
class StateObservation:
    state: str | None
    source: str
    rule_id: str | None = None
    confidence: float = 0.0


class CapabilityPilot:
    """The capability set with the input path attached.

    `ObservationSession` runs the same detector and the same dispatcher and
    then throws the decisions away. This runs them and sends what they ask
    for. The split is deliberate and narrow: *what to do* lives entirely in
    the capability set, and *whether it is safe to do it right now* lives
    here. Nothing in this class knows the order of the screens, because there
    is no order — it handles whatever is on screen this frame.
    """

    def __init__(
        self,
        config: RunnerConfig,
        source: PilotFrameSource,
        sender: PilotSender,
        guard: PilotGuard,
        recorder: PilotRecorder,
        *,
        state_detector: OcrStateDetector,
        overlay_detector: OcrStateDetector | None = None,
        dispatcher: CapabilityDispatcher,
        actions: dict[str, Any],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        notify: Callable[[str], None] = lambda _: None,
        poll_ms: int | None = None,
        key_tap_ms: int | None = None,
        max_screenshots: int = 400,
        unknown_grace_ms: int = 8000,
    ) -> None:
        self.config = config
        self.source = source
        self.sender = sender
        self.guard = guard
        self.recorder = recorder
        self.state_detector = state_detector
        self.overlay_detector = overlay_detector
        self.dispatcher = dispatcher
        self.actions = actions
        self.sleep = sleep
        self.monotonic = monotonic
        self.notify = notify
        self.poll_ms = int(config.timing.get("pollMs", 300) if poll_ms is None else poll_ms)
        self.key_tap_ms = int(config.timing.get("keyTapMs", 80) if key_tap_ms is None else key_tap_ms)
        self.max_screenshots = max_screenshots
        # Every screen with no rule looks the same from the log: NO_STATE, over
        # and over. Whether that is a two-second loading wipe or the run being
        # stuck forever is only visible in a frame, and the useful ones are
        # exactly the screens nobody knew to collect. Loading screens get
        # captured too; one shot per episode is cheap and they are the reason
        # the grace is this long.
        self.unknown_grace_s = unknown_grace_ms / 1000

        # Full-frame and multi-region obstacle OCR is intentionally not part of
        # the 300ms fast loop. It runs once before a menu action, or after an
        # unknown screen has persisted. A positive result must repeat before it
        # can authorize input; a negative result clears that screen visit.
        overlay = config.overlay_ocr
        self.overlay_guard_states = frozenset(
            str(value) for value in overlay.get("guardStates", [])
        )
        # Rules broad enough to describe a whole family of pages — "anything
        # with ESC 返回 in the corner" — are only safe where nothing more
        # specific applies. The mode panel carries that same hint and is a
        # screen the runner must stay on, so these states are ignored whenever
        # the fast detector already named the frame.
        self.overlay_unknown_only_states = frozenset(
            str(value) for value in overlay.get("unknownOnlyStates", [])
        )
        self.overlay_scan_interval_s = int(overlay.get("scanIntervalMs", 900)) / 1000
        self.overlay_unknown_grace_s = int(overlay.get("unknownGraceMs", 1500)) / 1000
        self.overlay_unknown_retry_s = int(overlay.get("unknownRetryMs", 15000)) / 1000
        self.overlay_active_retry_s = int(overlay.get("activeRetryMs", 3000)) / 1000
        self.overlay_stable_observations = int(overlay.get("stableObservations", 2))
        if self.overlay_stable_observations < 1:
            raise ValueError("覆盖层 OCR 的 stableObservations 必须至少为 1")
        if self.overlay_scan_interval_s <= 0:
            raise ValueError("覆盖层 OCR 的 scanIntervalMs 必须大于 0")
        if self.overlay_unknown_grace_s < 0:
            raise ValueError("覆盖层 OCR 的 unknownGraceMs 不能小于 0")
        if self.overlay_unknown_retry_s <= 0 or self.overlay_active_retry_s <= 0:
            raise ValueError("覆盖层 OCR 的重试间隔必须大于 0")

        self._validate_actions()

        self.started = self.monotonic()
        self.observed_state: str | None = None
        self.state_version = 0
        self.actions_sent = 0
        self.screenshot_count = 0
        self.frames = 0
        self.counters: dict[str, int] = defaultdict(int)
        self._foreground = True
        self._released = False
        self._resolution_warned = False
        self._unknown_since: float | None = None
        self._unknown_captured = False
        self._base_state: str | None = None
        self._base_state_version = 0
        self._base_unknown_since: float | None = None
        self._overlay_checked_base_version: int | None = None
        self._overlay_next_scan_at = 0.0
        self._overlay_candidate: tuple[str, str, str] | None = None
        self._overlay_candidate_count = 0
        self._active_overlay: StateObservation | None = None

    def _validate_actions(self) -> None:
        """Fail before the first frame rather than mid-action.

        A capability naming an action the profile does not define, or a kind
        this runner cannot send, is a configuration error. Finding it at
        startup costs nothing; finding it at runtime means the dispatcher has
        already recorded an attempt for an input that never left.
        """
        for capability in self.dispatcher.capabilities.capabilities:
            if capability.kind not in SUPPORTED_KINDS:
                raise ValueError(
                    f"能力 {capability.id} 的动作类型 {capability.kind} 不被自动游玩支持"
                )
            if capability.action not in self.actions:
                raise ValueError(f"能力 {capability.id} 引用了未定义的动作 {capability.action}")
            spec = self.actions[capability.action]
            if capability.kind == "click":
                if not (isinstance(spec, (list, tuple)) and len(spec) == 2):
                    raise ValueError(f"动作 {capability.action} 不是一对点击坐标")
            elif capability.kind == "key" and not isinstance(spec, int):
                raise ValueError(f"动作 {capability.action} 不是一个扫描码")
            elif capability.kind == "sequence":
                self._validate_sequence(capability.action, spec)

    def _validate_sequence(self, action_name: str, spec: object) -> None:
        if not isinstance(spec, list) or not spec:
            raise ValueError(f"动作序列 {action_name} 为空")
        for index, raw_step in enumerate(spec, start=1):
            if not isinstance(raw_step, dict):
                raise ValueError(f"动作序列 {action_name} 第 {index} 步不是对象")
            step_type = str(raw_step.get("type", ""))
            if step_type not in {"tapKey", "wait"}:
                raise ValueError(
                    f"动作序列 {action_name} 第 {index} 步类型未知：{step_type}"
                )
            duration_ms = int(raw_step.get("durationMs", self.key_tap_ms))
            if not 0 <= duration_ms <= 10_000:
                raise ValueError(f"动作序列 {action_name} 第 {index} 步时长无效")
            if step_type == "tapKey" and not isinstance(raw_step.get("scanCode"), int):
                raise ValueError(f"动作序列 {action_name} 第 {index} 步没有扫描码")

    # ------------------------------------------------------------------ input

    def _release_all(self, reason: str) -> None:
        if self._released:
            return
        self._released = True
        self.sender.release_all()
        self.recorder.log("INPUT_RELEASE_ALL", reason=reason)

    def _reset_overlay_for_pause(self) -> None:
        self._overlay_checked_base_version = None
        self._overlay_next_scan_at = 0.0
        self._overlay_candidate = None
        self._overlay_candidate_count = 0
        self._active_overlay = None

    def _execute(self, capability: Capability) -> dict[str, object]:
        spec = self.actions[capability.action]
        if capability.kind == "click":
            x, y = (int(value) for value in spec)
            self.sender.click(x, y)
            return {"x": x, "y": y}
        if capability.kind == "key":
            scan_code = int(spec)
            duration_ms = capability.hold_ms or self.key_tap_ms
            self.sender.tap_scan_code(scan_code, duration_ms)
            return {"scanCode": scan_code, "durationMs": duration_ms}

        executed: list[dict[str, object]] = []
        for raw_step in spec:
            # A sequence can span several seconds. Re-check focus and abort
            # immediately before every input, not merely before the sequence.
            self.guard.ensure_target_foreground()
            self.guard.ensure_not_aborted()
            step_type = str(raw_step["type"])
            duration_ms = int(raw_step.get("durationMs", self.key_tap_ms))
            if step_type == "wait":
                self.sleep(duration_ms / 1000)
                executed.append({"type": step_type, "durationMs": duration_ms})
                continue
            scan_code = int(raw_step["scanCode"])
            self.sender.tap_scan_code(scan_code, duration_ms)
            executed.append(
                {
                    "type": step_type,
                    "scanCode": scan_code,
                    "durationMs": duration_ms,
                }
            )
        return {"steps": executed}

    # ------------------------------------------------------------- pending

    def _settle_pending(self, state: str | None) -> None:
        """Judge an outstanding action against the screen that replaced it.

        Only a *changed* screen is evidence. Right after a click the game has
        usually not repainted yet, and treating that unchanged frame as a
        failed postcondition would reject every action that actually worked.
        Nothing changing is handled elsewhere, by the confirm window expiring.
        """
        pending = self.dispatcher.pending
        if pending is None or state is None or state == pending.origin_state:
            return
        confirmed, settled = self.dispatcher.confirm_pending(state)
        if settled is None:
            return
        payload = {
            "capability": settled.capability.id,
            "action": settled.capability.action,
            "originState": settled.origin_state,
            "evidenceState": state,
            "attempt": settled.attempt,
        }
        if confirmed:
            self.counters["confirmed"] += 1
            self.recorder.log("ACTION_CONFIRMED", **payload)
        else:
            self.counters["rejected"] += 1
            self.recorder.log(
                "ACTION_POSTCONDITION_REJECTED",
                allowedNextStates=list(settled.capability.allowed_next_states),
                **payload,
            )
            self.notify(f"  ↳ 后置画面不对：{settled.capability.id} → {state}")

    # ---------------------------------------------------------------- frame

    def _base_observation(self, frame: np.ndarray, now: float) -> StateObservation:
        analysis = self.state_detector.analyze(frame)
        if analysis.error:
            self.counters["ocrError"] += 1
            self.recorder.log("OCR_ERROR", source="gameStates", error=analysis.error)
            observation = StateObservation(None, "gameStates")
        elif analysis.decision is None:
            observation = StateObservation(None, "gameStates")
        else:
            observation = StateObservation(
                analysis.decision.state,
                "gameStates",
                analysis.decision.rule_id,
                analysis.decision.confidence,
            )

        if self._base_state_version == 0 or observation.state != self._base_state:
            self._base_state = observation.state
            self._base_state_version += 1
            self._overlay_checked_base_version = None
            self._base_unknown_since = now if observation.state is None else None
            if self._active_overlay is not None:
                # The page under an active overlay changed, which is the best
                # cheap signal that a manual action or our dismissal worked.
                self._overlay_next_scan_at = now
            elif observation.state in self.overlay_guard_states:
                self._overlay_next_scan_at = now
            elif observation.state is None:
                self._overlay_next_scan_at = now + self.overlay_unknown_grace_s
            else:
                self._overlay_candidate = None
                self._overlay_candidate_count = 0
        return observation

    def _scan_overlay_detector(
        self,
        frame: np.ndarray,
    ) -> tuple[StateObservation | None, bool]:
        if self.overlay_detector is None:
            return None, False
        analysis = self.overlay_detector.analyze(frame)
        decision = analysis.decision
        self.recorder.log(
            "OVERLAY_OCR_ANALYZED",
            source="overlayOcr",
            matchedRule=None if decision is None else decision.rule_id,
            state=None if decision is None else decision.state,
            error=analysis.error,
        )
        if analysis.error:
            self.counters["overlayOcrError"] += 1
            return None, True
        if decision is None:
            return None, False
        return (
            StateObservation(
                decision.state,
                "overlayOcr",
                decision.rule_id,
                decision.confidence,
            ),
            False,
        )

    def _resolve_overlay(
        self,
        frame: np.ndarray,
        base: StateObservation,
        now: float,
    ) -> StateObservation:
        if self.overlay_detector is None:
            return base

        guarded_visit = (
            base.state in self.overlay_guard_states
            and self._overlay_checked_base_version != self._base_state_version
        )
        unknown_due = (
            base.state is None
            and self._base_unknown_since is not None
            and now - self._base_unknown_since >= self.overlay_unknown_grace_s
        )
        should_scan = self._active_overlay is not None or guarded_visit or unknown_due
        if not should_scan:
            return base
        if now < self._overlay_next_scan_at:
            # An active overlay remains authoritative between slow scans. A
            # guarded menu visit that has not yet been checked stays inert.
            return self._active_overlay or StateObservation(None, "overlayGate")

        match, failed = self._scan_overlay_detector(frame)
        if (
            match is not None
            and base.state is not None
            and match.state in self.overlay_unknown_only_states
        ):
            self.recorder.log(
                "OVERLAY_RULE_OUTRANKED",
                state=match.state,
                ruleId=match.rule_id,
                baseState=base.state,
            )
            match = None
        finished = self.monotonic()
        if failed:
            self._overlay_next_scan_at = finished + self.overlay_scan_interval_s
            return self._active_overlay or StateObservation(None, "overlayError")

        if match is None:
            self._overlay_candidate = None
            self._overlay_candidate_count = 0
            self._active_overlay = None
            if base.state in self.overlay_guard_states:
                self._overlay_checked_base_version = self._base_state_version
            self._overlay_next_scan_at = finished + self.overlay_unknown_retry_s
            return base

        candidate = (match.source, match.state or "", match.rule_id or "")
        if candidate == self._overlay_candidate:
            self._overlay_candidate_count += 1
        else:
            self._overlay_candidate = candidate
            self._overlay_candidate_count = 1
        self.recorder.log(
            "OVERLAY_CANDIDATE",
            source=match.source,
            state=match.state,
            ruleId=match.rule_id,
            observations=self._overlay_candidate_count,
            required=self.overlay_stable_observations,
        )
        if self._overlay_candidate_count < self.overlay_stable_observations:
            self._active_overlay = None
            self._overlay_next_scan_at = finished + self.overlay_scan_interval_s
            return StateObservation(None, "overlayCandidate")

        self._active_overlay = match
        self.counters[f"overlay:{match.state}"] += 1
        actionable = bool(self.dispatcher.capabilities.for_state(match.state or ""))
        retry_s = self.overlay_scan_interval_s if actionable else self.overlay_active_retry_s
        self._overlay_next_scan_at = finished + retry_s
        return match

    def _record_observation(
        self,
        observation: StateObservation,
        frame: np.ndarray,
    ) -> str | None:
        state = observation.state
        if state == self.observed_state:
            return state

        previous = self.observed_state
        self.observed_state = state
        self.state_version += 1
        if state is None:
            self.recorder.log(
                "STATE_UNKNOWN", previousState=previous, observationVersion=self.state_version
            )
        else:
            self.recorder.log(
                "STATE_DETECTED",
                state=state,
                previousState=previous,
                source=observation.source,
                ruleId=observation.rule_id,
                confidence=round(observation.confidence, 4),
                observationVersion=self.state_version,
            )
            self.notify(f"[状态] {state}")
            self._snapshot(state, frame)
        return state

    def _observe(self, frame: np.ndarray, now: float) -> tuple[str | None, str | None]:
        base = self._base_observation(frame, now)
        effective = self._resolve_overlay(frame, base, now)
        return self._record_observation(effective, frame), base.state

    def _note_unknown(self, state: str | None, frame: np.ndarray, now: float) -> None:
        """Keep one frame of every screen that has no rule.

        This is how the next missing rule gets found without asking anyone to
        go and reproduce it: whatever the runner stalls on ends up in the run
        directory by itself.
        """
        if state is not None:
            self._unknown_since = None
            self._unknown_captured = False
            return
        if self._unknown_since is None:
            self._unknown_since = now
            return
        if self._unknown_captured or now - self._unknown_since < self.unknown_grace_s:
            return
        self._unknown_captured = True
        self.counters["unknownScreens"] += 1
        self._snapshot("unknown", frame)
        self.notify(f"  ↳ 这个画面没有规则，已存证（停了 {now - self._unknown_since:.0f} 秒）")

    def _snapshot(self, stage: str, frame: np.ndarray) -> None:
        if self.screenshot_count >= self.max_screenshots:
            return
        self.screenshot_count += 1
        path = self.recorder.screenshot(stage.lower(), frame)
        self.recorder.log("SCREENSHOT_SAVED", stage=stage, path=str(path))

    def _act(self, decision: Decision, state: str, frame: np.ndarray) -> None:
        capability = decision.capability
        assert capability is not None  # a fire decision always carries one
        # Re-checked immediately before sending rather than once per frame:
        # the window can lose focus between the capture and the input, and
        # this is the last point where refusing still costs nothing.
        self.guard.ensure_target_foreground()
        self.guard.ensure_not_aborted()
        detail = self._execute(capability)
        self.actions_sent += 1
        self.counters[f"sent:{capability.id}"] += 1
        self._released = False
        self.recorder.log(
            "ACTION_SENT",
            capability=capability.id,
            action=capability.action,
            kind=capability.kind,
            actionClass=capability.action_class,
            state=state,
            attempt=decision.attempt,
            reason=decision.reason,
            observationVersion=self.state_version,
            **detail,
        )
        self.notify(f"  ↳ 已执行：{capability.id}（{capability.action}）")

    def step(self) -> dict[str, Any]:
        now = self.monotonic()
        self.guard.ensure_not_aborted()
        record: dict[str, Any] = {"elapsedMs": round((now - self.started) * 1000)}

        foreground = self.guard.target_is_foreground()
        if not foreground:
            if self._foreground:
                self._foreground = False
                # A pause invalidates the pending action: the operator may have
                # done anything to the game while the window was not ours.
                self._release_all("FOREGROUND_LOST")
                self.dispatcher.reset_for_pause()
                self._reset_overlay_for_pause()
                self.recorder.log("FOREGROUND_PAUSED")
                self.notify("暂停：Apex 不在前台。")
            record["skipped"] = "NOT_FOREGROUND"
            return record
        if not self._foreground:
            self._foreground = True
            self.recorder.log("FOREGROUND_RESUMED")
            self.notify("继续：Apex 回到前台。")

        try:
            frame = self.source.grab()
        except Exception as error:
            self.counters["captureError"] += 1
            self.recorder.log("CAPTURE_ERROR", error=str(error))
            record["captureError"] = str(error)
            return record

        self.frames += 1
        height, width = frame.shape[:2]
        expected = (
            int(self.config.environment["width"]),
            int(self.config.environment["height"]),
        )
        if (width, height) != expected and not self._resolution_warned:
            # Every ROI in the dictionary is absolute, so a different
            # resolution does not degrade recognition, it invalidates it.
            self._resolution_warned = True
            self.recorder.log("RESOLUTION_MISMATCH", got=[width, height], expected=list(expected))
            self.notify(f"警告：分辨率是 {width}x{height}，标定用的是 {expected[0]}x{expected[1]}。")

        state, base_state = self._observe(frame, now)
        # Overlay OCR can take seconds. Retry windows and periodic actions must
        # use the time after that work, not the stale timestamp from frame grab.
        now = self.monotonic()
        record["state"] = state
        record["baseState"] = base_state
        self._note_unknown(state, frame, now)

        self._settle_pending(state)
        self.dispatcher.note_state(state, now)
        decision = self.dispatcher.decide(state, self.state_version, now)
        record["decision"] = {"kind": decision.kind, "reason": decision.reason}
        self.counters[f"{decision.kind}:{decision.reason}"] += 1

        if decision.kind == "fire" and state is not None:
            record["decision"]["capability"] = decision.capability.id
            self._act(decision, state, frame)
        elif decision.kind == "pause":
            # The dispatcher decides when a pause lifts (a cycle ages out of
            # its window, an attempt budget resets on the next visit). All
            # this has to do is stop sending and say so once.
            self._release_all(decision.reason)
            self.recorder.log("DECISION_PAUSED", reason=decision.reason, detail=decision.detail)
            self.notify(f"暂停：{decision.reason}")
            if state is not None:
                self._snapshot(f"paused-{decision.reason.lower()}", frame)
        return record

    def run(self, duration_s: float | None = None) -> None:
        deadline = None if duration_s is None else self.monotonic() + duration_s
        try:
            while deadline is None or self.monotonic() < deadline:
                self.step()
                self.sleep(self.poll_ms / 1000)
        finally:
            # Whatever ends the run — the deadline, F8, Ctrl+C, an exception —
            # must not leave a key held down in a live match.
            self._release_all("RUN_ENDED")

    def write_summary(self) -> Path:
        path = Path(self.recorder.run_dir) / "pilot-summary.json"
        payload = {
            "schemaVersion": 1,
            "profile": self.config.profile,
            "durationMs": round((self.monotonic() - self.started) * 1000),
            "frames": self.frames,
            "actionsSent": self.actions_sent,
            "screenshots": self.screenshot_count,
            "counters": dict(sorted(self.counters.items())),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

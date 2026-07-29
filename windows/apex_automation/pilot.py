from __future__ import annotations

from collections import defaultdict
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
SUPPORTED_KINDS = frozenset({"click", "key"})


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
        dispatcher: CapabilityDispatcher,
        actions: dict[str, Any],
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        notify: Callable[[str], None] = lambda _: None,
        poll_ms: int | None = None,
        key_tap_ms: int | None = None,
        max_screenshots: int = 400,
    ) -> None:
        self.config = config
        self.source = source
        self.sender = sender
        self.guard = guard
        self.recorder = recorder
        self.state_detector = state_detector
        self.dispatcher = dispatcher
        self.actions = actions
        self.sleep = sleep
        self.monotonic = monotonic
        self.notify = notify
        self.poll_ms = int(config.timing.get("pollMs", 300) if poll_ms is None else poll_ms)
        self.key_tap_ms = int(config.timing.get("keyTapMs", 80) if key_tap_ms is None else key_tap_ms)
        self.max_screenshots = max_screenshots

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
            elif not isinstance(spec, int):
                raise ValueError(f"动作 {capability.action} 不是一个扫描码")

    # ------------------------------------------------------------------ input

    def _release_all(self, reason: str) -> None:
        if self._released:
            return
        self._released = True
        self.sender.release_all()
        self.recorder.log("INPUT_RELEASE_ALL", reason=reason)

    def _execute(self, capability: Capability) -> dict[str, object]:
        spec = self.actions[capability.action]
        if capability.kind == "click":
            x, y = (int(value) for value in spec)
            self.sender.click(x, y)
            return {"x": x, "y": y}
        scan_code = int(spec)
        duration_ms = capability.hold_ms or self.key_tap_ms
        self.sender.tap_scan_code(scan_code, duration_ms)
        return {"scanCode": scan_code, "durationMs": duration_ms}

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

    def _observe(self, frame: np.ndarray) -> str | None:
        analysis = self.state_detector.analyze(frame)
        if analysis.error:
            self.counters["ocrError"] += 1
            self.recorder.log("OCR_ERROR", error=analysis.error)
            return None
        state = None if analysis.decision is None else analysis.decision.state
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
                source="gameStates",
                ruleId=analysis.decision.rule_id,
                confidence=round(analysis.decision.confidence, 4),
                observationVersion=self.state_version,
            )
            self.notify(f"[状态] {state}")
            self._snapshot(state, frame)
        return state

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

        state = self._observe(frame)
        record["state"] = state

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

from __future__ import annotations

import time
from typing import Callable, Iterable, Protocol

import numpy as np

from .config import RunnerConfig
from .input_win32 import EmergencyStop
from .vision import NearBlackFrameError, StateScore, VisionDetector


class FrameSource(Protocol):
    def grab(self) -> np.ndarray: ...


class InputSender(Protocol):
    def click(self, x: int, y: int) -> None: ...
    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None: ...
    def hold_scan_codes(self, scan_codes: Iterable[int], duration_ms: int) -> None: ...
    def move_mouse(self, dx: int, dy: int) -> None: ...
    def hold_mouse_button(self, button: str, duration_ms: int) -> None: ...
    def release_all(self) -> None: ...


class SafetyGuard(Protocol):
    def ensure_not_aborted(self) -> None: ...
    def ensure_target_foreground(self) -> None: ...


class Recorder(Protocol):
    run_dir: object
    def log(self, event: str, **payload: object) -> None: ...
    def screenshot(self, stage: str, frame: np.ndarray) -> object: ...
    def finish(self, status: str, **payload: object) -> None: ...


class ScenarioRunner:
    def __init__(
        self,
        config: RunnerConfig,
        detector: VisionDetector,
        source: FrameSource,
        sender: InputSender,
        guard: SafetyGuard,
        recorder: Recorder,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        notify: Callable[[str], None] = lambda _: None,
    ):
        self.config = config
        self.detector = detector
        self.source = source
        self.sender = sender
        self.guard = guard
        self.recorder = recorder
        self.sleep = sleep
        self.monotonic = monotonic
        self.notify = notify

    def _poll_any(self, state_names: Iterable[str], timeout_ms: int) -> tuple[StateScore, np.ndarray]:
        names = tuple(state_names)
        counts = {name: 0 for name in names}
        started = self.monotonic()
        deadline = self.monotonic() + timeout_ms / 1000
        last_report_second = -1
        last_black_report_second = -1
        self.notify(f"[识别] 等待 {' / '.join(names)}")

        while self.monotonic() < deadline:
            self.guard.ensure_not_aborted()
            frame = self.source.grab()
            try:
                self.detector.validate_frame(frame)
            except NearBlackFrameError:
                # Apex legitimately renders near-black frames while moving
                # between the title screen, lobby, legend select, and match.
                # They carry no matching evidence, so retry without treating a
                # single transition frame as a broken capture backend.
                counts = {name: 0 for name in names}
                elapsed_second = int(self.monotonic() - started)
                if (
                    elapsed_second != last_black_report_second
                    and elapsed_second % 5 == 0
                ):
                    last_black_report_second = elapsed_second
                    self.recorder.log(
                        "CAPTURE_FRAME_SKIPPED",
                        reason="NEAR_BLACK",
                        expected=list(names),
                    )
                    self.notify("[识别] 跳过加载期黑帧")
                self.sleep(self.config.timing["pollMs"] / 1000)
                continue
            scores = {score.state: score for score in self.detector.rank_states(frame, names)}

            for name in names:
                result = scores[name]
                counts[name] = counts[name] + 1 if result.matched else 0
                if counts[name] >= self.config.states[name].stable_frames:
                    self.recorder.log(
                        "STATE_DETECTED",
                        state=name,
                        score=round(result.score, 4),
                        anchorScores=[round(value, 4) for value in result.anchor_scores],
                    )
                    self.recorder.screenshot(name, frame)
                    self.notify(f"[识别] {name}，置信度 {result.score:.3f}")
                    return result, frame

            elapsed_second = int(self.monotonic() - started)
            if elapsed_second != last_report_second and elapsed_second % 5 == 0:
                last_report_second = elapsed_second
                leader = max(scores.values(), key=lambda value: value.score)
                self.recorder.log("DETECTING", expected=list(names), leader=leader.state, score=round(leader.score, 4))
            self.sleep(self.config.timing["pollMs"] / 1000)

        raise TimeoutError(f"等待状态超时：{', '.join(names)}")

    def _input_click(self, action_name: str) -> None:
        x, y = (int(value) for value in self.config.actions[action_name])
        self.guard.ensure_target_foreground()
        self.sender.click(x, y)
        self.recorder.log("INPUT_CLICK", action=action_name, x=x, y=y)
        self.notify(f"[输入] 点击 {action_name} @ ({x}, {y})")

    def _input_key(self, action_name: str) -> None:
        scan_code = int(self.config.actions[action_name])
        self.guard.ensure_target_foreground()
        self.sender.tap_scan_code(scan_code, self.config.timing["keyTapMs"])
        self.recorder.log("INPUT_KEY", action=action_name, scanCode=scan_code)
        self.notify(f"[输入] 点按 {action_name}，扫描码 {scan_code}")

    def _state_still_present(self, state_name: str) -> bool:
        state = self.config.states[state_name]
        for _ in range(state.stable_frames):
            self.guard.ensure_not_aborted()
            frame = self.source.grab()
            try:
                self.detector.validate_frame(frame)
            except NearBlackFrameError:
                return False
            score = self.detector.rank_states(frame, [state_name])[0]
            if not score.matched:
                return False
            self.sleep(self.config.timing["pollMs"] / 1000)
        return True

    def _click_until_state_changes(self, action_name: str, state_name: str) -> None:
        attempts = self.config.timing.get("readyClickAttempts", 3)
        confirm_delay_ms = self.config.timing.get("readyClickConfirmDelayMs", 1500)
        for attempt in range(1, attempts + 1):
            self._input_click(action_name)
            self.sleep(confirm_delay_ms / 1000)
            if not self._state_still_present(state_name):
                self.recorder.log(
                    "INPUT_CONFIRMED",
                    action=action_name,
                    evidence=f"{state_name}_DEPARTED",
                    attempt=attempt,
                )
                self.notify(f"[输入] {action_name} 已生效（第 {attempt} 次）")
                return
            self.recorder.log(
                "INPUT_UNCONFIRMED",
                action=action_name,
                state=state_name,
                attempt=attempt,
            )
            if attempt < attempts:
                self.notify(f"[输入] {action_name} 未生效，重试 {attempts - attempt} 次")
        raise RuntimeError(f"{action_name} 点击 {attempts} 次后界面仍为 {state_name}，已停止")

    def run(self) -> None:
        try:
            first_frame = self.source.grab()
            self.detector.validate_frame(first_frame)
            self.guard.ensure_target_foreground()

            self._poll_any(["CONTINUE"], self.config.timing["continueTimeoutMs"])
            self._input_click("continueClick")

            self._poll_any(["LOBBY_READY"], self.config.timing["lobbyTimeoutMs"])
            self._click_until_state_changes("readyClick", "LOBBY_READY")

            dropship_state, _ = self._poll_any(
                ["LEGEND_SELECT", "DROPSHIP_JUMPMASTER_WAIT", "LAUNCH_READY"],
                self.config.timing["dropshipTimeoutMs"],
            )
            if dropship_state.state == "LEGEND_SELECT":
                self.recorder.log("NO_INPUT", state="LEGEND_SELECT", reason="等待游戏自动选择英雄")
                self.notify("[分支] 英雄选择不操作，等待自动选择")
                dropship_state, _ = self._poll_any(
                    ["DROPSHIP_JUMPMASTER_WAIT", "LAUNCH_READY"],
                    self.config.timing["dropshipTimeoutMs"],
                )

            if dropship_state.state == "DROPSHIP_JUMPMASTER_WAIT":
                self.recorder.log("NO_INPUT", state=dropship_state.state, reason="JUMPMASTER_WAIT")
                self.notify("[分支] 当前是跳伞指挥官，不按 LCTRL，等待 E 发射")
                self._poll_any(["LAUNCH_READY"], self.config.timing["launchReadyTimeoutMs"])
            else:
                self.recorder.log("BRANCH", branch="ALREADY_LAUNCH_READY", action="SKIP_LCTRL")
                self.notify("[分支] 已可发射，跳过 LCTRL")

            self._input_key("launchScanCode")
            self._poll_any(["FREEFALL"], self.config.timing["freefallTimeoutMs"])
            self.sender.release_all()
            self.recorder.log("INPUT_RELEASE_ALL", reason="FREEFALL_CONFIRMED")
            self.notify("[输入] 已确认自由落体，释放本程序持有的全部输入")

            self._poll_any(["LANDED"], self.config.timing["landingTimeoutMs"])
            self.sleep(self.config.timing["finalStabilizeMs"] / 1000)
            final_frame = self.source.grab()
            final_path = self.recorder.screenshot("FINAL_USER_SHOT", final_frame)
            self.recorder.finish("PASS", finalScreenshot=str(final_path))
            self.notify(f"[完成] 已落地，最终截图：{final_path}")
        except EmergencyStop as error:
            self.sender.release_all()
            self.recorder.finish("ABORTED", error=str(error))
            raise
        except Exception as error:
            self.sender.release_all()
            self.recorder.finish("FAILED", error=str(error))
            raise

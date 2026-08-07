from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
from typing import Any, Callable, Protocol
import unicodedata

import numpy as np

from .capabilities import Capability, CapabilityDispatcher, Decision
from .config import RunnerConfig
from .ocr_obstacles import normalize_ocr_text
from .ocr_states import OcrStateDetector
from .progression import (
    LobbyProgressionReader,
    LobbyProgressionReading,
    LobbyProgressionStabilizer,
)
from .progression_policy import (
    ContinuePlayPolicy,
    ProgressionContext,
    ProgressionDecision,
    ProgressionOutcome,
    ProgressionPolicy,
    ProgressionStatus,
)
from .safety import ForegroundLost


# A capability set that reached this module has already been validated for
# these; the check in `_validate_actions` is what makes that true rather than
# assumed, because the alternative is discovering a bad action name halfway
# into a match with an input already sent.
SUPPORTED_KINDS = frozenset({"click", "key", "sequence", "clickText"})
# The screen the watchdog names when nothing else could. It is not an OCR
# state and never appears in a rule file: it means "no rule has matched this
# frame for minutes", which is a fact about the runner rather than about a
# page. Capabilities may attach to it exactly as they do to a real screen.
STALLED_UNKNOWN = "STALLED_UNKNOWN"
RING_PROGRESS_PATTERN = re.compile(
    r"(?:经历)?缩圈[^0-9]{0,8}([0-9]{1,3})/([0-9]{1,3})"
)


def producible_states(
    state_detector: OcrStateDetector,
    overlay_detector: OcrStateDetector | None = None,
) -> set[str]:
    """Every screen name a capability may legally be written against.

    Startup rejects a capability that names a screen nothing can produce, and
    that check has to know about the one name that comes from the runner
    rather than from a dictionary. Both the launcher and the test that guards
    the shipped capability set ask this function, so neither can drift into
    believing in a screen the other does not.
    """
    states = set(state_detector.states)
    if overlay_detector is not None:
        states.update(overlay_detector.states)
    states.add(STALLED_UNKNOWN)
    return states
LOBBY_CONTEXT_STATES = frozenset(
    {
        "LOBBY_QUEUEING",
        "LOBBY_SELECT_REQUIRED",
        "LOBBY_READY_TARGET_FILL_ON",
        "LOBBY_READY_TRAINING",
        "LOBBY_READY_TARGET",
        "LOBBY_READY_OTHER",
        "MODE_PANEL_TARGET_VISIBLE",
        "MODE_PANEL_TARGET_HOVERED",
    }
)
LOBBY_PROGRESS_STATES = frozenset(
    state for state in LOBBY_CONTEXT_STATES if not state.startswith("MODE_PANEL_")
)
SAFE_LOBBY_STATES = frozenset(
    {
        "LOBBY_SELECT_REQUIRED",
        "LOBBY_READY_TARGET_FILL_ON",
        "LOBBY_READY_TRAINING",
        "LOBBY_READY_TARGET",
        "LOBBY_READY_OTHER",
    }
)
ROUND_CONTEXT_STATES = frozenset(
    {
        "DROPSHIP_FOLLOWING",
        "DROPSHIP_SOLO_JUMPMASTER",
        "LAUNCH_READY",
        "FREEFALL",
        "IN_MATCH_ALIVE",
        "SPECTATING",
        "POST_MATCH_SUMMARY",
    }
)


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
    def write_status(self, payload: dict[str, object]) -> None: ...


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
        unknown_sample_ms: int = 20_000,
        unknown_static_epsilon: float = 0.02,
        progression_reader: LobbyProgressionReader | None = None,
        progression_stable_samples: int = 2,
        progression_max_attempts: int = 3,
        progression_policy: ProgressionPolicy | None = None,
        progression_retry_ms: int = 5000,
        lease_is_current: Callable[[], bool] = lambda: True,
        status_interval_ms: int = 1000,
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
        # One shot per episode turned out to be far too coarse. In
        # `20260730-232551` a single unknown stretch ran 142 seconds and
        # covered the pre-match lobby, legend select, loading and the entire
        # dropship flight; only the first frame survived, and the dropship —
        # the one screen that needed a rule — was lost. So keep sampling while
        # the stretch lasts.
        self.unknown_sample_s = unknown_sample_ms / 1000
        # Deliberately not a "did the screen change" test. Measured on that
        # run's frames, the same lobby differs by up to 0.203 between shots
        # while genuinely different screens start at 0.097 — the two
        # distributions overlap, so no threshold can classify them. This one
        # is far below both and only suppresses a frame that is essentially
        # identical to the last kept one, which is what a loading wipe is.
        self.unknown_static_epsilon = unknown_static_epsilon
        self.progression_reader = progression_reader
        self.progression_stabilizer = LobbyProgressionStabilizer(
            progression_stable_samples
        )
        if progression_max_attempts < progression_stable_samples:
            raise ValueError("大厅等级经验最大读取次数不能小于稳定确认次数")
        self.progression_max_attempts = progression_max_attempts
        self.progression_policy = progression_policy or ContinuePlayPolicy()
        if progression_retry_ms <= 0:
            raise ValueError("大厅等级经验失败后的复核间隔必须大于 0")
        self.progression_retry_s = progression_retry_ms / 1000
        self.lease_is_current = lease_is_current
        self.status_interval_s = max(0.1, status_interval_ms / 1000)

        # Standing still is also a failure mode, and until now the only one
        # with no signal at all. `20260803-083835` returned to the lobby at
        # 04:20:23, hit a screen no rule named nine seconds later, and then
        # heartbeated `observedState: null` for **75 minutes** — foreground,
        # capturing, 2403 actions sent and not one more. Nothing was wrong
        # enough to stop, so nothing stopped.
        stall = config.stall
        self.stall_enabled = bool(stall.get("enabled", True))
        self.stall_grace_s = int(stall.get("graceMs", 120_000)) / 1000
        # After 准备 the screen is *supposed* to go dark for a while: queueing,
        # legend select and loading name nothing. Measured at 72, 104 and 116
        # seconds on 20260803, which leaves no room under a two minute grace if
        # matchmaking is ever slower than it was that day — and a stray ESC in
        # there could cancel the queue. Both stalls actually seen began after
        # returning to the lobby, where the ordinary grace still applies.
        self.stall_queue_grace_s = int(stall.get("queueGraceMs", 420_000)) / 1000
        self.stall_window_s = int(stall.get("recoverWindowMs", 20_000)) / 1000
        self.stall_retry_s = int(stall.get("retryMs", 180_000)) / 1000
        self.stall_give_up_s = int(stall.get("giveUpMs", 1_800_000)) / 1000
        self.known_stall_give_up_s = int(
            stall.get("knownStateGiveUpMs", 120_000)
        ) / 1000
        if self.stall_grace_s <= 0 or self.stall_window_s <= 0:
            raise ValueError("停滞看门狗的宽限和处置窗口必须大于 0")
        if self.stall_queue_grace_s < self.stall_grace_s:
            raise ValueError("排队后的宽限不能短于普通宽限")
        if self.stall_retry_s < self.stall_window_s:
            raise ValueError("停滞看门狗的重试间隔不能短于一次处置窗口")
        if self.stall_give_up_s and self.stall_give_up_s <= self.stall_grace_s:
            raise ValueError("停滞看门狗的放弃时限必须长于宽限")
        if self.known_stall_give_up_s <= 0:
            raise ValueError("已知画面动作耗尽后的放弃时限必须大于 0")

        # Legend select is the one screen the runner passes through every match
        # and has never seen: it lives inside the unknown stretch between
        # `LOBBY_QUEUEING` and the dropship, where the generic evidence sampler
        # keeps at most a frame every 20s and skips near-identical ones — which
        # is exactly what a page with only a countdown ticking looks like.
        legend = config.legend_select
        self.legend_capture_enabled = bool(legend.get("captureEnabled", True))
        self.legend_sample_s = int(legend.get("sampleMs", 5000)) / 1000
        self.legend_max_frames = int(legend.get("maxFrames", 8))
        self.legend_max_rounds = int(legend.get("maxRounds", 2))
        if self.legend_sample_s <= 0:
            raise ValueError("传奇选择取证的采样间隔必须大于 0")
        # `probe` reads that stretch and writes down what it saw; it never
        # sends anything. Whether a legend can be chosen by name depends on
        # facts nobody has yet — is the name drawn on the card, does the pick
        # need confirming — and one probing run answers both without asking
        # anyone to reproduce a screen.
        self.legend_mode = str(legend.get("mode", "probe"))
        if self.legend_mode not in {"off", "probe"}:
            raise ValueError(f"传奇选择模式暂不支持：{self.legend_mode}")
        self.legend_preferred = str(legend.get("preferredLegend", "")).strip()
        self.legend_probe_s = int(legend.get("probeIntervalMs", 6000)) / 1000
        self.legend_probe_max = int(legend.get("probeMaxPerRound", 5))
        if self.legend_probe_s <= 0:
            raise ValueError("传奇选择探针的间隔必须大于 0")

        page_probe = config.page_probe
        self.page_probe_states = frozenset(
            str(value) for value in page_probe.get("states", ["FULLSCREEN_ESC_BACK"])
        )
        self.page_probe_max = int(page_probe.get("maxPerSession", 8))
        if not bool(page_probe.get("enabled", True)):
            self.page_probe_states = frozenset()
        # Same budget, different trigger: pageProbe describes a page the rules
        # *did* name, this one describes a frame nothing could name.
        self.click_text_miss_max = int(page_probe.get("maxUnmatchedPerSession", 8))

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
        self._unknown_signature: np.ndarray | None = None
        self._unknown_next_sample = 0.0
        self._last_known_state: str | None = None
        self._stall_since: float | None = None
        self._stall_rounds = 0
        self._stall_round_until = 0.0
        self._stall_next_round_at = 0.0
        self._known_stall_key: tuple[str, int, str] | None = None
        self._known_stall_since: float | None = None
        self._legend_armed = False
        self._legend_kept = 0
        self._legend_rounds_captured = 0
        self._legend_next_sample = 0.0
        self._legend_probes = 0
        self._legend_next_probe = 0.0
        self._legend_text_logged = False
        self._last_page_tokens: tuple[Any, ...] = ()
        self._page_probes = 0
        self._page_probed_state: str | None = None
        self._ring_progress: tuple[int, int] | None = None
        self._click_text_misses = 0
        self._base_state: str | None = None
        self._base_state_version = 0
        self._base_unknown_since: float | None = None
        self._overlay_checked_base_version: int | None = None
        self._overlay_next_scan_at = 0.0
        self._overlay_candidate: tuple[str, str, str] | None = None
        self._overlay_candidate_count = 0
        self._active_overlay: StateObservation | None = None
        self._lobby_visit_active = False
        self._lobby_visit_count = 0
        self._progression_done = False
        self._progression_attempts = 0
        self._progression_reason = "INITIAL"
        self._last_progression: LobbyProgressionReading | None = None
        self._last_progression_attempt: LobbyProgressionReading | None = None
        self._progression_outcome: ProgressionOutcome | None = None
        self._progression_retry_at: float | None = None
        self._progression_pause_reported = False
        self.progression_decision: ProgressionDecision | None = None
        self.session_outcome: str | None = None
        self.target_reading: LobbyProgressionReading | None = None
        self._round_active = False
        self.rounds_started = 0
        self.rounds_returned_to_lobby = 0
        self._next_status_at = 0.0

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
            elif capability.kind == "clickText":
                self._validate_click_text(capability.action, spec)
            if (
                capability.evidence is not None
                and self.overlay_detector is not None
                and capability.evidence.region not in self.overlay_detector.regions
            ):
                raise ValueError(
                    f"兜底能力 {capability.id} 引用了未知区域 "
                    f"{capability.evidence.region}"
                )

    def _validate_click_text(self, action_name: str, spec: object) -> None:
        if not isinstance(spec, dict):
            raise ValueError(f"文字点击动作 {action_name} 不是对象")
        if not isinstance(spec.get("region"), str):
            raise ValueError(f"文字点击动作 {action_name} 没有声明区域")
        words = spec.get("any")
        if not isinstance(words, list) or not words:
            raise ValueError(f"文字点击动作 {action_name} 没有声明任何目标文字")
        if not all(isinstance(word, str) and word for word in words):
            raise ValueError(f"文字点击动作 {action_name} 的目标文字必须是非空字符串")
        fallback = spec.get("fallbackScanCode")
        if fallback is not None and not isinstance(fallback, int):
            raise ValueError(f"文字点击动作 {action_name} 的兜底扫描码不是整数")
        hold_words = spec.get("holdWords")
        hold_scan_code = spec.get("holdScanCode")
        if hold_words is not None:
            if not isinstance(hold_words, list) or not hold_words:
                raise ValueError(f"文字点击动作 {action_name} 的长按词表为空")
            if not all(isinstance(word, str) and word for word in hold_words):
                raise ValueError(f"文字点击动作 {action_name} 的长按词必须是非空字符串")
            if not isinstance(hold_scan_code, int):
                raise ValueError(f"文字点击动作 {action_name} 声明了长按词却没有扫描码")
            hold_ms = spec.get("holdMs", 2000)
            if not isinstance(hold_ms, int) or not 0 < hold_ms <= 10_000:
                raise ValueError(f"文字点击动作 {action_name} 的长按时长无效")
        elif hold_scan_code is not None:
            raise ValueError(f"文字点击动作 {action_name} 声明了长按扫描码却没有长按词")
        confidence = float(spec.get("minConfidence", 0.62))
        if not 0 <= confidence <= 1:
            raise ValueError(f"文字点击动作 {action_name} 的置信度无效")
        if (
            self.overlay_detector is not None
            and spec["region"] not in self.overlay_detector.regions
        ):
            raise ValueError(f"文字点击动作 {action_name} 引用了未知区域 {spec['region']}")

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

    def _find_button(
        self,
        spec: dict[str, Any],
        frame: np.ndarray,
    ) -> tuple[int, int, str, float] | None:
        """Locate a button by the word printed on it, or refuse to guess.

        Every other action in this runner is a coordinate measured from a real
        frame. This one exists for the screens nobody has measured yet — an
        error box after a network blip, a prompt a game update introduced —
        where the alternative is standing still until someone notices. It is
        still not a blind click: nothing is pressed unless the OCR read one of
        the declared words *and* the engine handed back where it read it.
        """
        if self.overlay_detector is None:
            return None
        # The name is checked at startup against this same dictionary, so a
        # miss here means the detector was swapped mid-run: read nothing
        # rather than crash a session that may be an hour in.
        region = self.overlay_detector.regions.get(str(spec["region"]))
        if region is None:
            return None
        min_confidence = float(spec.get("minConfidence", 0.62))
        words = tuple(normalize_ocr_text(str(word)) for word in spec["any"])
        # Capture backends may reuse one ndarray and repaint it in place. Force
        # the batched provider to forget any tokens cached for that object so a
        # fallback can never act on coordinates read from the previous frame.
        begin_frame = getattr(self.overlay_detector.provider, "begin_frame", None)
        if callable(begin_frame):
            begin_frame(frame)
        try:
            tokens = self.overlay_detector.provider.read(frame, region)
        except Exception as error:
            self.counters["clickTextOcrError"] += 1
            self.recorder.log("CLICK_TEXT_OCR_ERROR", action=spec.get("region"), error=str(error))
            return None
        eligible = tuple(token for token in tokens if token.confidence >= min_confidence)
        # Words are tried in the order the profile declares them, not in the
        # order OCR happened to read them: a disconnect box carrying both
        # 重试 and 返回 should retry, not back out.
        for word in words:
            if not word:
                continue
            for token in eligible:
                if word not in token.normalized:
                    continue
                if token.roi is None:
                    # A region fallback read has no coordinates. Clicking the
                    # middle of the region because a word appeared somewhere
                    # inside it is exactly the blind click this design refuses.
                    self.counters["clickTextNoPosition"] += 1
                    continue
                x1, y1, x2, y2 = token.roi
                return (x1 + x2) // 2, (y1 + y2) // 2, token.text, token.confidence
        self._log_unmatched_page(spec, tokens)
        return None

    def _log_unmatched_page(
        self,
        spec: dict[str, Any],
        tokens: tuple[Any, ...],
    ) -> None:
        """Keep what the page said when none of the declared words were on it.

        A match already reports itself: the send event carries `matchedText`.
        A miss used to report nothing at all, and the frame this runs on is by
        definition the one nobody has a rule for — so the full-frame OCR that
        was just paid for is the only description of it that exists outside a
        PNG on the runner's disk. Capped per session because a screen that
        stays unrecognised is retried for half an hour.
        """
        if self._click_text_misses >= self.click_text_miss_max:
            return
        self._click_text_misses += 1
        self.recorder.log(
            "CLICK_TEXT_NO_MATCH",
            region=spec.get("region"),
            wanted=list(spec.get("any", [])),
            tokens=[
                {
                    "text": token.text,
                    "confidence": round(token.confidence, 3),
                    "roi": list(token.roi) if token.roi else None,
                }
                for token in tokens[:80]
            ],
        )

    def _hold_for_text(
        self,
        spec: dict[str, Any],
        text: str,
    ) -> tuple[int, int] | None:
        """Decide whether the matched text names a key to hold, not a button.

        Apex writes a fair number of its confirmations as `SPACE 按住以确认`.
        The word the profile is hunting for (确认) sits inside that sentence,
        so the match is perfect and the coordinates are exact — and the click
        does nothing, because there is no button there, only a printed
        instruction. The sentence says which it is; this reads that.
        """
        hold_words = spec.get("holdWords")
        scan_code = spec.get("holdScanCode")
        if not hold_words or scan_code is None:
            return None
        normalized = normalize_ocr_text(text)
        if not any(normalize_ocr_text(str(word)) in normalized for word in hold_words):
            return None
        return int(scan_code), int(spec.get("holdMs", 2000))

    def _execute(self, capability: Capability, frame: np.ndarray) -> dict[str, object]:
        spec = self.actions[capability.action]
        if capability.kind == "click":
            x, y = (int(value) for value in spec)
            self.sender.click(x, y)
            return {"x": x, "y": y}
        if capability.kind == "key":
            evidence = capability.evidence
            if evidence is not None:
                matched, detail = self._action_evidence(capability, frame)
                if not matched:
                    return {"skipped": "ACTION_EVIDENCE_MISSING", **detail}
            scan_code = int(spec)
            duration_ms = capability.hold_ms or self.key_tap_ms
            self.sender.tap_scan_code(scan_code, duration_ms)
            return {
                "scanCode": scan_code,
                "durationMs": duration_ms,
                **({} if evidence is None else detail),
            }
        if capability.kind == "clickText":
            found = self._find_button(spec, frame)
            if found is not None:
                x, y, text, confidence = found
                hold = self._hold_for_text(spec, text)
                if hold is not None:
                    hold_scan_code, hold_ms = hold
                    self.sender.tap_scan_code(hold_scan_code, hold_ms)
                    return {
                        "matchedText": text,
                        "confidence": round(confidence, 4),
                        "scanCode": hold_scan_code,
                        "durationMs": hold_ms,
                        "hold": "HOLD_PROMPT",
                    }
                self.sender.click(x, y)
                return {
                    "x": x,
                    "y": y,
                    "matchedText": text,
                    "confidence": round(confidence, 4),
                }
            fallback = spec.get("fallbackScanCode")
            if fallback is None:
                return {"skipped": "NO_BUTTON_TEXT"}
            duration_ms = capability.hold_ms or self.key_tap_ms
            self.sender.tap_scan_code(int(fallback), duration_ms)
            # Not `reason`: the send event already carries the dispatcher's
            # reason for firing, and a detail key may not collide with it.
            return {
                "scanCode": int(fallback),
                "durationMs": duration_ms,
                "fallback": "NO_BUTTON_TEXT",
            }

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

    def _action_evidence(
        self,
        capability: Capability,
        frame: np.ndarray,
    ) -> tuple[bool, dict[str, object]]:
        """Re-read a fallback's printed key hint before sending the key."""

        evidence = capability.evidence
        assert evidence is not None
        if self.overlay_detector is None:
            return False, {"evidenceRegion": evidence.region, "evidenceError": "NO_DETECTOR"}
        region = self.overlay_detector.regions.get(evidence.region)
        if region is None:
            return False, {"evidenceRegion": evidence.region, "evidenceError": "NO_REGION"}
        provider = self.overlay_detector.provider
        begin_frame = getattr(provider, "begin_frame", None)
        if callable(begin_frame):
            begin_frame(frame)
        try:
            tokens = provider.read(frame, region)
        except Exception as error:
            self.recorder.log(
                "ACTION_EVIDENCE_ERROR",
                capability=capability.id,
                region=evidence.region,
                error=str(error),
            )
            return False, {
                "evidenceRegion": evidence.region,
                "evidenceError": type(error).__name__,
            }
        eligible = tuple(
            token for token in tokens if token.confidence >= evidence.min_confidence
        )
        joined = "".join(token.normalized for token in eligible)
        any_terms = tuple(normalize_ocr_text(value) for value in evidence.any_terms)
        all_terms = tuple(normalize_ocr_text(value) for value in evidence.all_terms)
        matched = (
            (not any_terms or any(term and term in joined for term in any_terms))
            and all(term and term in joined for term in all_terms)
        )
        return matched, {
            "evidenceRegion": evidence.region,
            "evidenceText": [token.text for token in eligible],
            "evidenceConfidence": round(
                min((token.confidence for token in eligible), default=0.0), 4
            ),
        }

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
        self._last_page_tokens = analysis.regions.get("fullFrame", ())
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
            self._page_probed_state = None
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
        self._probe_page_text(match)
        self.counters[f"overlay:{match.state}"] += 1
        actionable = bool(self.dispatcher.capabilities.for_state(match.state or ""))
        retry_s = self.overlay_scan_interval_s if actionable else self.overlay_active_retry_s
        self._overlay_next_scan_at = finished + retry_s
        return match

    def _probe_page_text(self, match: StateObservation) -> None:
        """Write down what a page said before dismissing it.

        `fullscreen-esc-back` closes a whole family of pages by the hint in
        their corner, without ever knowing which page it closed. One of them
        is 排位之路, which carries the task counters — including 经历缩圈
        30 次, the number that decides whether ring survival needs any work at
        all. The OCR for that frame has already been paid for; only the result
        was being thrown away.
        """
        if match.state not in self.page_probe_states:
            return
        if self._page_probes >= self.page_probe_max:
            return
        if not self._last_page_tokens:
            return
        if match.state == self._page_probed_state:
            # One record per visit, not one per rescan of the same page.
            return
        self._page_probed_state = match.state
        self._page_probes += 1
        self.recorder.log(
            "PAGE_TEXT",
            state=match.state,
            ruleId=match.rule_id,
            tokens=[
                {
                    "text": token.text,
                    "confidence": round(token.confidence, 3),
                    "roi": list(token.roi) if token.roi else None,
                }
                for token in self._last_page_tokens[:80]
            ],
        )
        self._record_ring_progress()

    def _record_ring_progress(self) -> None:
        tokens = self._last_page_tokens[:80]
        for width in range(1, min(5, len(tokens)) + 1):
            for start in range(0, len(tokens) - width + 1):
                group = tokens[start : start + width]
                raw_text = " ".join(str(token.text) for token in group)
                compact = "".join(
                    unicodedata.normalize("NFKC", raw_text).split()
                )
                match = RING_PROGRESS_PATTERN.search(compact)
                if match is None:
                    continue
                completed, required = (int(value) for value in match.groups())
                if required < 1 or required > 100 or completed > required:
                    continue
                current = self._ring_progress
                if current is not None and required == current[1] and completed <= current[0]:
                    return
                self._ring_progress = (completed, required)
                self.recorder.log(
                    "RING_PROGRESS",
                    completed=completed,
                    required=required,
                    rawText=raw_text,
                    confidence=round(
                        min(float(token.confidence) for token in group), 3
                    ),
                )
                return

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

    def _update_visit_tracking(self, base_state: str | None) -> None:
        if base_state in LOBBY_CONTEXT_STATES:
            if not self._lobby_visit_active:
                self._lobby_visit_active = True
                self._lobby_visit_count += 1
                self._progression_reason = (
                    "INITIAL"
                    if self._lobby_visit_count == 1
                    else (
                        "RETURNED_AFTER_MATCH"
                        if self._round_active
                        else "STATE_REENTRY"
                    )
                )
                if self._round_active:
                    self.rounds_returned_to_lobby += 1
                    self._round_active = False
                self.progression_stabilizer.reset()
                self._progression_done = self.progression_reader is None
                self._progression_attempts = 0
                self._last_progression_attempt = None
                self._progression_outcome = None
                self._progression_retry_at = None
                self._progression_pause_reported = False
                self.progression_decision = None
            return

        if base_state in ROUND_CONTEXT_STATES:
            self._lobby_visit_active = False
            if not self._round_active:
                self._round_active = True
                self.rounds_started += 1

    @staticmethod
    def _failed_progression_payload(
        reason: str,
        reading: LobbyProgressionReading | None,
    ) -> dict[str, object]:
        return {
            "reason": reason,
            "level": None,
            "xpCurrentApprox": None,
            "xpRequiredApprox": None,
            "rawText": "" if reading is None else reading.raw_text,
            "levelRawText": (
                "" if reading is None else reading.level_evidence.raw_text
            ),
            "xpRawText": "" if reading is None else reading.xp_evidence.raw_text,
            "confidence": 0.0,
            "levelConfidence": (
                0.0 if reading is None else round(reading.level_evidence.confidence, 4)
            ),
            "xpConfidence": (
                0.0 if reading is None else round(reading.xp_evidence.confidence, 4)
            ),
            "changed": False,
            "deltaApprox": None,
            "readStatus": "FAILED",
            "error": "大厅等级经验未能在限定帧数内得到一致读数"
            if reading is None
            else reading.error
            or "大厅等级经验连续读数不一致",
        }

    def _read_lobby_progress(
        self,
        state: str | None,
        frame: np.ndarray,
    ) -> bool:
        """Return True while a lobby action must wait for this short read."""

        if (
            self.progression_reader is None
            or self._progression_done
            or not self._lobby_visit_active
            or state not in LOBBY_PROGRESS_STATES
        ):
            return False

        self._progression_attempts += 1
        reading = self.progression_reader.read(frame)
        self._last_progression_attempt = reading
        stable = self.progression_stabilizer.observe(reading)
        if stable is not None:
            previous = self._last_progression
            changed = (
                previous is not None
                and (
                    stable.level,
                    stable.xp_current_approx,
                    stable.xp_required_approx,
                )
                != (
                    previous.level,
                    previous.xp_current_approx,
                    previous.xp_required_approx,
                )
            )
            delta_approx: int | None = None
            if (
                previous is not None
                and previous.level == stable.level
                and previous.xp_current_approx is not None
                and stable.xp_current_approx is not None
            ):
                candidate_delta = (
                    stable.xp_current_approx - previous.xp_current_approx
                )
                if candidate_delta >= 0:
                    delta_approx = candidate_delta
            self.recorder.log(
                "LOBBY_PROGRESS",
                **stable.as_event_payload(
                    self._progression_reason,
                    changed=changed,
                    delta_approx=delta_approx,
                ),
            )
            self._last_progression = stable
            self._progression_outcome = ProgressionOutcome.confirmed(
                stable,
                attempts=self._progression_attempts,
            )
            self._progression_done = True
            self.notify(
                f"[账号进度] 等级 {stable.level}，"
                f"{stable.raw_text or '经验值已读取'}"
            )
            return False

        if self._progression_attempts >= self.progression_max_attempts:
            payload = self._failed_progression_payload(
                self._progression_reason,
                self._last_progression_attempt,
            )
            self.recorder.log("LOBBY_PROGRESS", **payload)
            self._progression_outcome = ProgressionOutcome.failed(
                self._last_progression_attempt,
                attempts=self._progression_attempts,
                error=str(payload["error"]),
            )
            self._progression_done = True
            self.counters["progressionFailed"] += 1
            return False
        return True

    def _prepare_progression_retry(self, now: float) -> None:
        outcome = self._progression_outcome
        if (
            outcome is None
            or outcome.status is not ProgressionStatus.FAILED
            or self.progression_decision is not ProgressionDecision.PAUSE_UNCERTAIN
            or self._progression_retry_at is None
            or now < self._progression_retry_at
        ):
            return
        self.progression_stabilizer.reset()
        self._progression_done = False
        self._progression_attempts = 0
        self._last_progression_attempt = None
        self._progression_outcome = None
        self._progression_retry_at = None
        self._progression_pause_reported = False
        self.progression_decision = None

    def _progression_context(
        self,
        state: str | None,
        base_state: str | None,
    ) -> ProgressionContext:
        return ProgressionContext(
            observed_state=state,
            safe_lobby=base_state in SAFE_LOBBY_STATES,
            queueing=base_state == "LOBBY_QUEUEING",
            overlay_clear=state == base_state and self._active_overlay is None,
            pending_action=self.dispatcher.pending is not None,
            foreground=self._foreground,
            lease_current=bool(self.lease_is_current()),
        )

    def _apply_progression_policy(
        self,
        state: str | None,
        base_state: str | None,
        frame: np.ndarray,
        now: float,
    ) -> ProgressionDecision | None:
        outcome = self._progression_outcome
        if outcome is None:
            return None
        decision = self.progression_policy.decide(
            outcome,
            self._progression_context(state, base_state),
        )
        self.progression_decision = decision
        self.counters[f"progression:{decision.value}"] += 1

        if decision is ProgressionDecision.CONTINUE_PLAY:
            self._progression_retry_at = None
            self._progression_pause_reported = False
            return decision
        if decision is ProgressionDecision.DEFER_UNTIL_SAFE_LOBBY:
            return decision
        if decision is ProgressionDecision.PAUSE_UNCERTAIN:
            self._release_all("PROGRESSION_UNCERTAIN")
            if (
                outcome.status is ProgressionStatus.FAILED
                and self._progression_retry_at is None
            ):
                self._progression_retry_at = now + self.progression_retry_s
            if not self._progression_pause_reported:
                self._progression_pause_reported = True
                self.recorder.log(
                    "PROGRESSION_PAUSED",
                    decision=decision.value,
                    attempts=outcome.attempts,
                    error=outcome.error,
                    state=state,
                )
                self.notify("暂停：大厅等级经验尚未可靠确认。")
                self._snapshot("paused-progression-uncertain", frame)
            return decision

        self._release_all("TARGET_REACHED")
        reading = outcome.reading
        assert reading is not None and reading.level is not None
        self.target_reading = reading
        self.recorder.log(
            "ACCOUNT_TARGET_REACHED",
            level=reading.level,
            xpCurrentApprox=reading.xp_current_approx,
            xpRequiredApprox=reading.xp_required_approx,
            state=state,
        )
        self._snapshot("target-reached", frame)
        self.session_outcome = "TARGET_REACHED"
        self.notify(f"目标等级已达到：{reading.level}")
        self._write_status(now, force=True)
        return decision

    def _write_status(self, now: float, *, force: bool = False) -> None:
        if not force and now < self._next_status_at:
            return
        writer = getattr(self.recorder, "write_status", None)
        if not callable(writer):
            return
        writer(
            {
                "runtimeState": "RUNNING" if self._foreground else "PAUSED",
                "elapsedMs": max(0, round((now - self.started) * 1000)),
                "observedState": self.observed_state,
                "foreground": self._foreground,
                "observationVersion": self.state_version,
                "frames": self.frames,
                "actionsSent": self.actions_sent,
                "roundNumber": self.rounds_started,
                "roundsReturnedToLobby": self.rounds_returned_to_lobby,
            }
        )
        self._next_status_at = now + self.status_interval_s

    def _observe(self, frame: np.ndarray, now: float) -> tuple[str | None, str | None]:
        base = self._base_observation(frame, now)
        effective = self._resolve_overlay(frame, base, now)
        # Evidence first, and always against the screen as it actually read:
        # the watchdog's name for a stuck frame must not look to the sampler
        # like the screen was recognised.
        self._note_unknown(effective.state, frame, now)
        self._note_legend_select(effective.state, frame, now)
        effective = self._promote_stall(effective, base.state, now)
        return self._record_observation(effective, frame), base.state

    @staticmethod
    def _frame_signature(frame: np.ndarray) -> np.ndarray | None:
        """A 64x36 mean-pooled luminance thumbnail, cheap enough per frame."""
        rows, cols = 36, 64
        height, width = frame.shape[:2]
        if height < rows or width < cols:
            return None
        trimmed = frame[: height - height % rows, : width - width % cols]
        values = trimmed.mean(axis=2) if trimmed.ndim == 3 else trimmed.astype(float)
        return (
            values.reshape(rows, values.shape[0] // rows, cols, values.shape[1] // cols)
            .mean(axis=(1, 3))
            / 255.0
        )

    def _capture_unknown(self, frame: np.ndarray, now: float, reason: str) -> None:
        self._snapshot("unknown", frame)
        self._unknown_signature = self._frame_signature(frame)
        self._unknown_next_sample = now + self.unknown_sample_s
        self.notify(f"  ↳ 这个画面没有规则，已存证（{reason}）")

    def _note_unknown(self, state: str | None, frame: np.ndarray, now: float) -> None:
        """Keep frames of the screens that have no rule.

        This is how the next missing rule gets found without asking anyone to
        go and reproduce it: whatever the runner stalls on ends up in the run
        directory by itself. One frame per stretch is not enough, because a
        stretch is defined by "no rule matched" and can span several unrelated
        screens in a row.
        """
        if state is not None:
            self._unknown_since = None
            self._unknown_captured = False
            self._unknown_signature = None
            return
        if self._unknown_since is None:
            self._unknown_since = now
            return
        if not self._unknown_captured:
            if now - self._unknown_since < self.unknown_grace_s:
                return
            self._unknown_captured = True
            self.counters["unknownScreens"] += 1
            self._capture_unknown(frame, now, f"停了 {now - self._unknown_since:.0f} 秒")
            return
        if now < self._unknown_next_sample:
            return
        signature = self._frame_signature(frame)
        if (
            signature is not None
            and self._unknown_signature is not None
            and float(np.abs(signature - self._unknown_signature).mean())
            < self.unknown_static_epsilon
        ):
            self._unknown_next_sample = now + self.unknown_sample_s
            return
        self.counters["unknownSamples"] += 1
        self._capture_unknown(frame, now, "同一段里画面又变了")

    def _note_legend_select(self, state: str | None, frame: np.ndarray, now: float) -> None:
        """Keep the screens between pressing 准备 and the dropship.

        Picking a legend is the one thing in the match loop that is still left
        to the game's own timeout, and it stays that way until somebody can
        measure the roster on a real frame. Nobody should have to reproduce it
        by hand: it happens once per match, and the runner is already there.
        """
        if not self.legend_capture_enabled:
            return
        if state == "LOBBY_QUEUEING":
            if not self._legend_armed and self._legend_rounds_captured < self.legend_max_rounds:
                self._legend_armed = True
                self._legend_kept = 0
                self._legend_probes = 0
                self._legend_text_logged = False
                self._legend_next_sample = now
                self._legend_next_probe = now
            return
        if not self._legend_armed:
            return
        if state is not None:
            # A rule named the screen again — the blackout is over, whether it
            # ended at the dropship or back in the lobby.
            self._legend_armed = False
            if self._legend_kept:
                self._legend_rounds_captured += 1
            return
        self._probe_legend_screen(frame, now)
        if self._legend_kept >= self.legend_max_frames or now < self._legend_next_sample:
            return
        self._legend_kept += 1
        self._legend_next_sample = now + self.legend_sample_s
        self.counters["legendSelectFrames"] += 1
        self._snapshot("legend-select", frame)

    def _probe_legend_screen(self, frame: np.ndarray, now: float) -> None:
        """Read the blackout and write down what is on it. Sends nothing.

        Two facts decide whether a legend can be picked by name, and neither
        is knowable from here: whether the roster draws the name on the card,
        and whether picking one needs a second click to confirm. A frame plus
        the text on it answers both — so collect them, and leave the choosing
        to a later change that can be written against evidence.
        """
        if self.legend_mode != "probe" or self.overlay_detector is None:
            return
        if self._legend_probes >= self.legend_probe_max or now < self._legend_next_probe:
            return
        region = self.overlay_detector.regions.get("fullFrame")
        if region is None:
            return
        self._legend_probes += 1
        self._legend_next_probe = now + self.legend_probe_s
        try:
            tokens = self.overlay_detector.provider.read(frame, region)
        except Exception as error:
            self.recorder.log("LEGEND_PROBE_OCR_ERROR", error=str(error))
            return
        if not tokens:
            return
        if not self._legend_text_logged:
            # The whole page once, not once per probe: this is the artifact
            # that says which words a rule could key on at all.
            self._legend_text_logged = True
            self.recorder.log(
                "LEGEND_SCREEN_TEXT",
                probe=self._legend_probes,
                tokens=[
                    {
                        "text": token.text,
                        "confidence": round(token.confidence, 3),
                        "roi": list(token.roi) if token.roi else None,
                    }
                    for token in tokens[:60]
                ],
            )
        if not self.legend_preferred:
            return
        wanted = normalize_ocr_text(self.legend_preferred)
        for token in tokens:
            if wanted in token.normalized:
                self.counters["legendNameSeen"] += 1
                self.recorder.log(
                    "LEGEND_NAME_FOUND",
                    legend=self.legend_preferred,
                    text=token.text,
                    confidence=round(token.confidence, 3),
                    roi=list(token.roi) if token.roi else None,
                    probe=self._legend_probes,
                )
                self.notify(f"  ↳ 传奇选择页上读到了「{self.legend_preferred}」")
                return

    def _promote_stall(
        self,
        observation: StateObservation,
        base_state: str | None,
        now: float,
    ) -> StateObservation:
        """Name a screen that has gone unrecognised for minutes, so it can be acted on.

        The promotion is deliberately narrow. It requires the *fast* detector
        to be blank too, so a lobby waiting on its overlay scan is never
        touched, and it only holds for a short window at a time: outside that
        window the frame goes back to being an ordinary unknown that nothing
        will click. What acts on `STALLED_UNKNOWN` is a capability like any
        other, and it reads the button before pressing anything.
        """
        if observation.state is not None or base_state is not None:
            self._last_known_state = observation.state or base_state
            if self._stall_since is not None:
                self.recorder.log(
                    "STALL_RECOVERED",
                    stalledForMs=round((now - self._stall_since) * 1000),
                    rounds=self._stall_rounds,
                    state=observation.state,
                    reason=(
                        f"停滞 {now - self._stall_since:.0f} 秒后恢复，"
                        f"共 {self._stall_rounds} 轮处置"
                    ),
                )
                self.notify(f"停滞解除：画面回到 {observation.state or base_state}。")
                self._stall_since = None
                self._stall_rounds = 0
            return observation
        if not self.stall_enabled or self._unknown_since is None:
            return observation
        grace = (
            self.stall_queue_grace_s
            if self._last_known_state == "LOBBY_QUEUEING"
            else self.stall_grace_s
        )
        if now - self._unknown_since < grace:
            return observation

        if self._stall_since is None:
            self._stall_since = now
            self._stall_rounds = 0
            self._stall_round_until = 0.0
            self._stall_next_round_at = now
            self.counters["stalls"] += 1
            self.recorder.log(
                "STALL_DETECTED",
                unknownForMs=round((now - self._unknown_since) * 1000),
                reason=f"画面连续 {now - self._unknown_since:.0f} 秒无法识别",
            )
            self.notify("停滞：画面已经连续无法识别，开始尝试恢复。")

        if (
            self.stall_give_up_s
            and now - self._stall_since >= self.stall_give_up_s
            and self.session_outcome is None
        ):
            # Ending loudly beats heartbeating `RUNNING` at a screen that has
            # not changed in half an hour. The lease closes with a reason and
            # the evidence stays in the run directory.
            self.session_outcome = "STALLED"
            self.recorder.log(
                "STALL_UNRECOVERED",
                stalledForMs=round((now - self._stall_since) * 1000),
                rounds=self._stall_rounds,
                reason=(
                    f"停滞 {now - self._stall_since:.0f} 秒仍未恢复，"
                    f"{self._stall_rounds} 轮处置全部无效"
                ),
            )
            self.notify("停滞无法恢复：结束本次会话，画面已留证。")
            return observation

        if now >= self._stall_next_round_at:
            self._stall_rounds += 1
            self._stall_round_until = now + self.stall_window_s
            self._stall_next_round_at = now + self.stall_retry_s
            self.recorder.log("STALL_RECOVERY_ROUND", round=self._stall_rounds)
        if now >= self._stall_round_until:
            return observation
        return StateObservation(STALLED_UNKNOWN, "stallWatchdog", "stall-watchdog", 1.0)

    def _track_known_stall(
        self,
        decision: Decision,
        state: str | None,
        now: float,
        frame: np.ndarray,
    ) -> None:
        """End a run that exhausted every declared action on a known page."""

        current_key = None if state is None else (state, self.state_version)
        if self._known_stall_key is not None:
            stalled_state, stalled_version, capability_id = self._known_stall_key
            if current_key != (stalled_state, stalled_version):
                self.recorder.log(
                    "STALL_RECOVERED",
                    stallKind="KNOWN_STATE",
                    state=state,
                    previousState=stalled_state,
                    capability=capability_id,
                    stalledForMs=(
                        0
                        if self._known_stall_since is None
                        else round((now - self._known_stall_since) * 1000)
                    ),
                )
                self._known_stall_key = None
                self._known_stall_since = None

        if (
            decision.kind == "pause"
            and decision.reason == "ATTEMPTS_EXHAUSTED"
            and decision.capability is not None
            and state is not None
            and self._known_stall_key is None
        ):
            self._known_stall_key = (state, self.state_version, decision.capability.id)
            self._known_stall_since = now
            self.recorder.log(
                "STALL_DETECTED",
                stallKind="KNOWN_STATE",
                state=state,
                capability=decision.capability.id,
                action=decision.capability.action,
                attempts=decision.attempt,
                reason="已知页面的可用动作均未让画面变化",
                tokens=[
                    {
                        "text": token.text,
                        "confidence": round(token.confidence, 3),
                        "roi": list(token.roi) if token.roi else None,
                    }
                    for token in self._last_page_tokens[:80]
                ],
            )
            self.notify(
                f"停滞：{state} 的可用动作已经用尽，停止输入并等待画面变化。"
            )

        if self._known_stall_key is None or self._known_stall_since is None:
            return
        if now - self._known_stall_since < self.known_stall_give_up_s:
            return
        stalled_state, _, capability_id = self._known_stall_key
        self.recorder.log(
            "STALL_UNRECOVERED",
            stallKind="KNOWN_STATE",
            state=stalled_state,
            capability=capability_id,
            stalledForMs=round((now - self._known_stall_since) * 1000),
            reason="已知页面动作耗尽后仍未变化",
        )
        self._snapshot("known-stall-unrecovered", frame)
        self.session_outcome = "STALLED_KNOWN"
        self.notify("已知页面持续无法处理：结束本次会话并安全收口。")

    def _snapshot(self, stage: str, frame: np.ndarray) -> None:
        if self.screenshot_count >= self.max_screenshots:
            return
        self.screenshot_count += 1
        self.recorder.screenshot(stage.lower(), frame)

    def _act(self, decision: Decision, state: str, frame: np.ndarray) -> None:
        capability = decision.capability
        assert capability is not None  # a fire decision always carries one
        # Re-checked immediately before sending rather than once per frame:
        # the window can lose focus between the capture and the input, and
        # this is the last point where refusing still costs nothing.
        self.guard.ensure_target_foreground()
        self.guard.ensure_not_aborted()
        detail = self._execute(capability, frame)
        if detail.get("skipped"):
            # A text-driven action that found no text sent nothing, and the
            # counters exist to answer "did this runner press anything".
            self.counters[f"skipped:{capability.id}"] += 1
            self.recorder.log(
                "ACTION_SKIPPED",
                capability=capability.id,
                action=capability.action,
                state=state,
                attempt=decision.attempt,
                detail=detail,
            )
            return
        self.actions_sent += 1
        self.counters[f"sent:{capability.id}"] += 1
        self._released = False
        self.recorder.log(
            "ACTION_SENT",
            capability=capability.id,
            action=capability.action,
            kind=capability.kind,
            actionClass=capability.action_class,
            # The reporter drops periodic sends so a match does not fill the
            # remote event stream with one row per melee. It reads this field,
            # which until now only melee's own id supplied.
            trigger=capability.trigger,
            state=state,
            attempt=decision.attempt,
            reason=decision.reason,
            observationVersion=self.state_version,
            **detail,
        )
        self.notify(f"  ↳ 已执行：{capability.id}（{capability.action}）")

    def _pause_for_foreground(self) -> None:
        if not self._foreground:
            return
        self._foreground = False
        # A pause invalidates the pending action: the operator may have done
        # anything to the game while the window was not ours.
        self._release_all("FOREGROUND_LOST")
        self.dispatcher.reset_for_pause()
        self._reset_overlay_for_pause()
        # Time spent behind another window is not time the runner was stuck,
        # and whatever is on screen when it comes back deserves a fresh look.
        self._unknown_since = None
        self._stall_since = None
        self._stall_rounds = 0
        self._known_stall_key = None
        self._known_stall_since = None
        self.recorder.log("FOREGROUND_PAUSED")
        self.notify("暂停：Apex 不在前台。")

    def step(self) -> dict[str, Any]:
        now = self.monotonic()
        self.guard.ensure_not_aborted()
        record: dict[str, Any] = {"elapsedMs": round((now - self.started) * 1000)}

        foreground = self.guard.target_is_foreground()
        if not foreground:
            self._pause_for_foreground()
            record["skipped"] = "NOT_FOREGROUND"
            self._write_status(now, force=True)
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
            self._write_status(now)
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
        self._update_visit_tracking(base_state)

        self._settle_pending(state)
        self.dispatcher.note_state(state, now)
        self._prepare_progression_retry(now)
        if self._read_lobby_progress(state, frame):
            record["decision"] = {"kind": "wait", "reason": "LOBBY_PROGRESS"}
            self.counters["wait:LOBBY_PROGRESS"] += 1
            self._write_status(self.monotonic())
            return record
        progression_decision = self._apply_progression_policy(
            state,
            base_state,
            frame,
            self.monotonic(),
        )
        if (
            progression_decision is not None
            and progression_decision is not ProgressionDecision.CONTINUE_PLAY
        ):
            record["decision"] = {
                "kind": (
                    "stop"
                    if progression_decision is ProgressionDecision.TARGET_REACHED
                    else "wait"
                ),
                "reason": progression_decision.value,
            }
            self._write_status(self.monotonic())
            return record
        decision = self.dispatcher.decide(state, self.state_version, now)
        record["decision"] = {"kind": decision.kind, "reason": decision.reason}
        self.counters[f"{decision.kind}:{decision.reason}"] += 1
        self._track_known_stall(decision, state, now, frame)

        if decision.kind == "fire" and state is not None:
            record["decision"]["capability"] = decision.capability.id
            try:
                self._act(decision, state, frame)
            except ForegroundLost:
                # The window went away between the check at the top of this
                # frame and the send. That is the same recoverable pause as
                # any other alt-tab, not a reason to end a session that may
                # have been running unattended for twenty minutes.
                self._pause_for_foreground()
                record["skipped"] = "NOT_FOREGROUND"
        elif decision.kind == "pause":
            # The dispatcher decides when a pause lifts (a cycle ages out of
            # its window, an attempt budget resets on the next visit). All
            # this has to do is stop sending and say so once.
            self._release_all(decision.reason)
            self.recorder.log("DECISION_PAUSED", reason=decision.reason, detail=decision.detail)
            self.notify(f"暂停：{decision.reason}")
            if state is not None:
                self._snapshot(f"paused-{decision.reason.lower()}", frame)
        self._write_status(self.monotonic())
        return record

    def run(self, duration_s: float | None = None) -> str:
        deadline = None if duration_s is None else self.monotonic() + duration_s
        try:
            while (
                self.session_outcome is None
                and (deadline is None or self.monotonic() < deadline)
            ):
                self.step()
                self.sleep(self.poll_ms / 1000)
        finally:
            # Whatever ends the run — the deadline, F8, Ctrl+C, an exception —
            # must not leave a key held down in a live match.
            self._release_all("RUN_ENDED")
        return self.session_outcome or "COMPLETED"

    def write_summary(self) -> Path:
        path = Path(self.recorder.run_dir) / "pilot-summary.json"
        payload = {
            "schemaVersion": 1,
            "profile": self.config.profile,
            "durationMs": round((self.monotonic() - self.started) * 1000),
            "frames": self.frames,
            "actionsSent": self.actions_sent,
            "roundsStarted": self.rounds_started,
            "roundsReturnedToLobby": self.rounds_returned_to_lobby,
            "screenshots": self.screenshot_count,
            "counters": dict(sorted(self.counters.items())),
        }
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

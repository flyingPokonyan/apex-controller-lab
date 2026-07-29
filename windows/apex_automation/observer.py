from __future__ import annotations

from collections import defaultdict
import json
from pathlib import Path
import statistics
import time
from typing import Any, Callable, Protocol

import numpy as np

from .config import RunnerConfig
from .ocr_obstacles import OcrObstacleDetector
from .ocr_states import OcrStateDetector
from .runner import FrameSource
from .vision import VisionDetector, frame_gray_std, frame_motion


class ObservationGuard(Protocol):
    def ensure_not_aborted(self) -> None: ...
    def target_is_foreground(self) -> bool: ...


class ObservationRecorder(Protocol):
    run_dir: Path
    def log(self, event: str, **payload: object) -> None: ...
    def screenshot(self, stage: str, frame: np.ndarray) -> object: ...


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return ordered[index]


def _stats(values: list[float]) -> dict[str, object]:
    if not values:
        return {"n": 0}
    return {
        "n": len(values),
        "min": round(min(values), 4),
        "p05": round(_percentile(values, 0.05), 4),
        "mean": round(statistics.fmean(values), 4),
        "p95": round(_percentile(values, 0.95), 4),
        "max": round(max(values), 4),
    }


class ObservationSession:
    """Live capture with every detector running and no input path at all.

    This exists to replace "the templates score 1.000 against the frames they
    were cut from" with real numbers from the target machine. It never builds
    an input sender, so no code path here can send a key or a click. The
    operator drives the game by hand; the session only watches and measures.
    """

    def __init__(
        self,
        config: RunnerConfig,
        detector: VisionDetector,
        source: FrameSource,
        recorder: ObservationRecorder,
        *,
        guard: ObservationGuard | None = None,
        guided_detector: OcrStateDetector | None = None,
        obstacle_detector: OcrObstacleDetector | None = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        notify: Callable[[str], None] = lambda _: None,
        poll_ms: int | None = None,
        ocr_interval_ms: int = 1500,
        snapshot_interval_ms: int = 15000,
        max_screenshots: int = 400,
        snapshot_min_change: float = 0.01,
    ) -> None:
        self.config = config
        self.detector = detector
        self.source = source
        self.recorder = recorder
        self.guard = guard
        self.guided_detector = guided_detector
        self.obstacle_detector = obstacle_detector
        self.sleep = sleep
        self.monotonic = monotonic
        self.notify = notify
        self.poll_ms = int(config.timing.get("pollMs", 300) if poll_ms is None else poll_ms)
        self.ocr_interval_ms = ocr_interval_ms
        self.snapshot_interval_ms = snapshot_interval_ms
        self.max_screenshots = max_screenshots
        self.snapshot_min_change = snapshot_min_change

        self.observations_path = Path(recorder.run_dir) / "observations.jsonl"
        self.summary_path = Path(recorder.run_dir) / "observations-summary.json"

        self.started = self.monotonic()
        self.observation_count = 0
        self.screenshot_count = 0
        self._previous_frame: np.ndarray | None = None
        self._last_saved_frame: np.ndarray | None = None
        self._classification: str | None = None
        self._next_ocr_at = 0.0
        self._next_snapshot_at = 0.0
        self._task_actions = dict(config.task.get("actions", {}))

        self._motion: list[float] = []
        self._ocr_latency: dict[str, list[float]] = defaultdict(list)
        self._scores_as_classified: dict[str, list[float]] = defaultdict(list)
        self._scores_as_other: dict[str, list[float]] = defaultdict(list)
        self._classification_frames: dict[str, int] = defaultdict(int)
        self._ocr_rule_frames: dict[str, int] = defaultdict(int)
        self._ocr_errors: dict[str, int] = defaultdict(int)

    def _append(self, record: dict[str, Any]) -> None:
        with self.observations_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _screen_hint(self, motion: float, gray_std: float) -> str | None:
        """Name the three non-menu frames anchors and OCR both fail on.

        Deliberately descriptive, not actionable: nothing in this project may
        act on a hint. It exists so the operator can later choose thresholds
        that tell a cutscene apart from a loading screen or a frozen capture.
        """
        if gray_std < 3.0:
            return "NEAR_BLACK"
        if motion >= 0.02:
            return "HIGH_MOTION"
        if motion <= 0.0005:
            return "STATIC"
        return None

    def _would_fire(self, state: str | None, obstacle_rule: dict[str, Any] | None) -> dict[str, Any] | None:
        """What the current configuration would have done on this frame.

        Reported so false positives can be found before anything is allowed to
        send input. `match` profiles keep their actions in code rather than in
        `task.actions`, so this stays empty for them; that is honest rather
        than a duplicated table that can drift.
        """
        if state is not None and state in self._task_actions:
            item = self._task_actions[state]
            return {
                "source": "task.actions",
                "state": state,
                "action": item.get("name"),
                "kind": item.get("kind"),
            }
        if obstacle_rule is not None:
            return {
                "source": "obstacleRules",
                "state": obstacle_rule.get("state"),
                "action": obstacle_rule.get("action"),
                "kind": "key",
            }
        return None

    def _run_ocr(self, frame: np.ndarray, record: dict[str, Any]) -> dict[str, Any] | None:
        obstacle_rule: dict[str, Any] | None = None
        if self.guided_detector is not None:
            # Timed because whether region OCR can carry the menu screens at a
            # ~1s cadence is the one premise of the OCR-first plan that cannot
            # be measured anywhere except the target machine.
            started = self.monotonic()
            analysis = self.guided_detector.analyze(frame)
            elapsed_ms = (self.monotonic() - started) * 1000
            self._ocr_latency["guided"].append(elapsed_ms)
            record["guidedOcrMs"] = round(elapsed_ms, 1)
            payload = analysis.as_event_payload()
            record["guidedOcr"] = payload
            if analysis.error:
                self._ocr_errors["guided"] += 1
            elif analysis.decision is not None:
                self._ocr_rule_frames[f"guided:{analysis.decision.rule_id}"] += 1
        if self.obstacle_detector is not None:
            started = self.monotonic()
            analysis = self.obstacle_detector.analyze(frame)
            elapsed_ms = (self.monotonic() - started) * 1000
            self._ocr_latency["obstacle"].append(elapsed_ms)
            record["obstacleOcrMs"] = round(elapsed_ms, 1)
            record["obstacleOcr"] = analysis.as_event_payload()
            if analysis.error:
                self._ocr_errors["obstacle"] += 1
            elif analysis.decision is not None:
                self._ocr_rule_frames[f"obstacle:{analysis.decision.rule_id}"] += 1
                obstacle_rule = {
                    "state": analysis.decision.state,
                    "action": analysis.decision.action.name,
                }
        return obstacle_rule

    def step(self) -> dict[str, Any]:
        now = self.monotonic()
        if self.guard is not None:
            self.guard.ensure_not_aborted()

        record: dict[str, Any] = {"elapsedMs": round((now - self.started) * 1000)}

        if self.guard is not None:
            foreground = self.guard.target_is_foreground()
            record["foreground"] = foreground
            if not foreground:
                record["skipped"] = "NOT_FOREGROUND"
                self._append(record)
                return record

        try:
            frame = self.source.grab()
        except Exception as error:
            record["captureError"] = str(error)
            self._append(record)
            return record

        height, width = frame.shape[:2]
        expected = (
            int(self.config.environment["width"]),
            int(self.config.environment["height"]),
        )
        record["resolution"] = [width, height]
        record["resolutionOk"] = (width, height) == expected

        gray_std = frame_gray_std(frame)
        motion = frame_motion(self._previous_frame, frame)
        self._previous_frame = frame
        self._motion.append(motion)
        record["grayStd"] = round(gray_std, 3)
        record["motion"] = round(motion, 5)
        record["hint"] = self._screen_hint(motion, gray_std)

        ranking = self.detector.rank_states(frame)
        record["templates"] = {
            score.state: {
                "score": round(score.score, 4),
                "matched": score.matched,
                "anchors": [round(value, 4) for value in score.anchor_scores],
            }
            for score in ranking
        }
        matched = next((item for item in ranking if item.matched), None)
        classification = None if matched is None else matched.state
        record["classification"] = classification

        for score in ranking:
            if score.state == classification:
                self._scores_as_classified[score.state].append(score.score)
            else:
                self._scores_as_other[score.state].append(score.score)
        self._classification_frames["UNMATCHED" if classification is None else classification] += 1

        obstacle_rule = None
        if now >= self._next_ocr_at:
            self._next_ocr_at = now + self.ocr_interval_ms / 1000
            obstacle_rule = self._run_ocr(frame, record)

        record["wouldFire"] = self._would_fire(classification, obstacle_rule)

        changed = classification != self._classification
        # An unrecognised screen never triggers a change, so a whole match
        # would otherwise be captured on the timer alone: a few hundred
        # near-identical gameplay frames that are large to transfer and carry
        # nothing. Timed snapshots therefore require visible change since the
        # last saved one; a new classification is always kept.
        change_since_saved = (
            1.0
            if self._last_saved_frame is None
            else frame_motion(self._last_saved_frame, frame)
        )
        due = now >= self._next_snapshot_at and change_since_saved >= self.snapshot_min_change
        if (changed or due) and self.screenshot_count < self.max_screenshots:
            self.screenshot_count += 1
            self._next_snapshot_at = now + self.snapshot_interval_ms / 1000
            self._last_saved_frame = frame
            stage = classification or (record["hint"] or "unmatched")
            record["screenshot"] = str(self.recorder.screenshot(stage, frame))
        if changed:
            self._classification = classification
            self.notify(
                f"[观察] {classification or '未匹配'}"
                f"（motion {motion:.4f} / std {gray_std:.1f}"
                + (f" / {record['hint']}" if record["hint"] else "")
                + "）"
            )

        self.observation_count += 1
        self._append(record)
        return record

    def run(self, *, max_iterations: int | None = None, duration_s: float | None = None) -> None:
        deadline = None if duration_s is None else self.monotonic() + duration_s
        iterations = 0
        while max_iterations is None or iterations < max_iterations:
            self.step()
            iterations += 1
            if deadline is not None and self.monotonic() >= deadline:
                return
            self.sleep(self.poll_ms / 1000)

    def summary(self) -> dict[str, Any]:
        states: dict[str, Any] = {}
        for state in self.config.states:
            positive = self._scores_as_classified.get(state, [])
            negative = self._scores_as_other.get(state, [])
            configured = min(
                (anchor.threshold for anchor in self.config.states[state].anchors),
                default=0.0,
            )
            entry: dict[str, Any] = {
                "configuredThreshold": configured,
                "whenClassified": _stats(positive),
                "whenNotClassified": _stats(negative),
            }
            if positive and negative:
                # The number that actually decides whether a threshold is
                # safe: how far the worst true positive sits above the best
                # look-alike. Offline validation cannot produce it because the
                # templates are exact crops of their own source frames.
                entry["separation"] = round(min(positive) - max(negative), 4)
            states[state] = entry

        return {
            "schemaVersion": 1,
            "profile": self.config.profile,
            "observations": self.observation_count,
            "screenshots": self.screenshot_count,
            "durationMs": round((self.monotonic() - self.started) * 1000),
            "motion": _stats(self._motion),
            "ocrLatencyMs": {name: _stats(values) for name, values in self._ocr_latency.items()},
            "classificationFrames": dict(self._classification_frames),
            "ocrRuleFrames": dict(self._ocr_rule_frames),
            "ocrErrors": dict(self._ocr_errors),
            "states": states,
        }

    def write_summary(self) -> Path:
        self.summary_path.write_text(
            json.dumps(self.summary(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return self.summary_path

from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.config import load_config
from apex_automation.observer import ObservationSession
from apex_automation.recorder import RunRecorder
from apex_automation.vision import VisionDetector, frame_motion, load_frame


TUTORIAL_CONFIG = REPOSITORY_ROOT / "windows" / "config" / "first-tutorial-2560x1440.zh-CN.json"


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeSource:
    def __init__(self, frames: list[np.ndarray]):
        self.frames = deque(frames)
        self.last = frames[-1]
        self.error: Exception | None = None

    def grab(self) -> np.ndarray:
        if self.error is not None:
            raise self.error
        if self.frames:
            self.last = self.frames.popleft()
        return self.last.copy()


class FakeGuard:
    def __init__(self, foreground: bool = True) -> None:
        self.foreground = foreground

    def ensure_not_aborted(self) -> None:
        return None

    def target_is_foreground(self) -> bool:
        return self.foreground


class RecordingSender:
    """Fails the test loudly if the observer ever reaches an input path."""

    def __getattr__(self, name: str):
        raise AssertionError(f"观察模式不允许发送输入：{name}")


def _blank(value: int = 0) -> np.ndarray:
    return np.full((1440, 2560, 3), value, dtype=np.uint8)


class ObserverTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_config(TUTORIAL_CONFIG)
        self.detector = VisionDetector(self.config)
        self.clock = FakeClock()
        self.temporary = tempfile.TemporaryDirectory()
        self.recorder = RunRecorder(Path(self.temporary.name), "test.observe")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _session(self, frames: list[np.ndarray], **kwargs) -> ObservationSession:
        return ObservationSession(
            self.config,
            self.detector,
            FakeSource(frames),
            self.recorder,
            guard=kwargs.pop("guard", FakeGuard()),
            sleep=self.clock.sleep,
            monotonic=self.clock.monotonic,
            **kwargs,
        )

    def test_recognises_known_lobby_frame_and_reports_would_fire(self) -> None:
        frame = load_frame(REPOSITORY_ROOT / "calibration" / "tutorial" / "raw" / "01-lobby-training-ready.png")
        session = self._session([frame])
        record = session.step()

        self.assertEqual(record["classification"], "LOBBY_TRAINING_READY")
        self.assertTrue(record["resolutionOk"])
        self.assertEqual(record["wouldFire"]["action"], "trainingReadyClick")
        self.assertEqual(record["wouldFire"]["source"], "task.actions")

    def test_writes_one_json_line_per_observation(self) -> None:
        session = self._session([_blank(90), _blank(95)])
        session.run(max_iterations=2)

        lines = session.observations_path.read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["elapsedMs"], 0)

    def test_pauses_recording_when_apex_is_not_foreground(self) -> None:
        guard = FakeGuard(foreground=False)
        session = self._session([_blank(90)], guard=guard)
        record = session.step()

        self.assertEqual(record["skipped"], "NOT_FOREGROUND")
        self.assertNotIn("templates", record)
        self.assertEqual(session.observation_count, 0)

    def test_capture_failure_is_recorded_without_stopping(self) -> None:
        session = self._session([_blank(90)])
        session.source.error = RuntimeError("DXcam 没有返回画面帧")
        record = session.step()

        self.assertEqual(record["captureError"], "DXcam 没有返回画面帧")
        session.source.error = None
        self.assertIn("templates", session.step())

    def test_hints_separate_cutscene_loading_and_frozen_frames(self) -> None:
        session = self._session([_blank(90)])
        self.assertEqual(session._screen_hint(0.0, 1.0), "NEAR_BLACK")
        self.assertEqual(session._screen_hint(0.08, 60.0), "HIGH_MOTION")
        self.assertEqual(session._screen_hint(0.0, 60.0), "STATIC")
        self.assertIsNone(session._screen_hint(0.005, 60.0))

    def test_motion_metric_reacts_to_change_and_ignores_first_frame(self) -> None:
        first, second = _blank(0), _blank(255)
        self.assertEqual(frame_motion(None, first), 0.0)
        self.assertAlmostEqual(frame_motion(first, second), 1.0, places=3)
        self.assertEqual(frame_motion(first, first), 0.0)

    def test_summary_reports_separation_between_true_and_false_matches(self) -> None:
        lobby = load_frame(REPOSITORY_ROOT / "calibration" / "tutorial" / "raw" / "01-lobby-training-ready.png")
        session = self._session([lobby, _blank(90)])
        session.run(max_iterations=2)
        summary = session.summary()

        entry = summary["states"]["LOBBY_TRAINING_READY"]
        self.assertEqual(entry["whenClassified"]["n"], 1)
        self.assertEqual(entry["whenNotClassified"]["n"], 1)
        self.assertGreater(entry["separation"], 0.0)
        self.assertEqual(summary["classificationFrames"]["LOBBY_TRAINING_READY"], 1)
        self.assertEqual(summary["classificationFrames"]["UNMATCHED"], 1)

    def test_screenshots_are_capped_and_taken_on_classification_change(self) -> None:
        lobby = load_frame(REPOSITORY_ROOT / "calibration" / "tutorial" / "raw" / "01-lobby-training-ready.png")
        session = self._session(
            [lobby, lobby, _blank(90), _blank(90)],
            snapshot_interval_ms=10_000_000,
            max_screenshots=1,
        )
        session.run(max_iterations=4)

        self.assertEqual(session.screenshot_count, 1)

    def test_timed_snapshots_skip_frames_that_have_not_changed(self) -> None:
        still = _blank(90)
        session = self._session(
            [still, still, still, _blank(200)],
            snapshot_interval_ms=0,
            max_screenshots=50,
        )
        session.run(max_iterations=4)

        # One for the first unmatched frame, one for the frame that changed;
        # the two identical frames in between are not worth transferring.
        self.assertEqual(session.screenshot_count, 2)

    def test_observer_never_exposes_an_input_sender(self) -> None:
        session = self._session([_blank(90)])
        for name in ("sender", "input", "send"):
            self.assertFalse(hasattr(session, name), f"观察会话不应持有 {name}")
        session.step()


if __name__ == "__main__":
    unittest.main()

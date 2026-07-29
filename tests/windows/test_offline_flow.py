from __future__ import annotations

from collections import deque
from pathlib import Path
import sys
import unittest

import numpy as np
import cv2


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.config import load_config
from apex_automation.capture import DxcamFrameSource, _camera_keepalive
from apex_automation.runner import ScenarioRunner
from apex_automation.vision import VisionDetector, load_frame


class FakeSource:
    def __init__(self, frames: list[np.ndarray]):
        self.frames = deque(frames)
        self.last = frames[-1]

    def grab(self) -> np.ndarray:
        if self.frames:
            self.last = self.frames.popleft()
        return self.last.copy()


class FakeGuard:
    def ensure_not_aborted(self) -> None:
        return None

    def ensure_target_foreground(self) -> None:
        return None


class FakeInput:
    def __init__(self):
        self.actions: list[tuple[object, ...]] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))

    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None:
        self.actions.append(("key", scan_code, duration_ms))

    def release_all(self) -> None:
        self.actions.append(("release",))


class FakeRecorder:
    run_dir = Path("fake-run")

    def __init__(self):
        self.events: list[tuple[str, dict[str, object]]] = []

    def log(self, event: str, **payload: object) -> None:
        self.events.append((event, payload))

    def screenshot(self, stage: str, frame: np.ndarray) -> Path:
        self.events.append(("SCREENSHOT", {"stage": stage}))
        return Path(f"{stage}.png")

    def finish(self, status: str, **payload: object) -> None:
        self.events.append(("FINISH", {"status": status, **payload}))


def repeated(frame: np.ndarray, count: int = 3) -> list[np.ndarray]:
    return [frame.copy() for _ in range(count)]


class OfflineFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config()
        cls.detector = VisionDetector(cls.config)
        cls.frames = {
            state: load_frame(path)
            for path, state in cls.config.offline_cases
        }

    def test_all_reference_screens_classify(self) -> None:
        for expected, frame in self.frames.items():
            with self.subTest(expected=expected):
                best = self.detector.rank_states(frame)[0]
                self.assertEqual(best.state, expected)
                self.assertTrue(best.matched)

    def test_profile_accepts_dx11_and_dx12_apex_processes(self) -> None:
        self.assertEqual(
            set(self.config.environment["foregroundExecutables"]),
            {"r5apex.exe", "r5apex_dx12.exe"},
        )

    def test_capture_exit_avoids_broken_dxcam_release(self) -> None:
        class FakeCamera:
            released = False

            def release(self) -> None:
                self.released = True

        source = DxcamFrameSource()
        camera = FakeCamera()
        source._camera = camera
        source.__exit__(None, None, None)
        self.assertFalse(camera.released)
        self.assertIs(_camera_keepalive[-1], camera)
        _camera_keepalive.pop()

    def test_reference_screens_tolerate_small_capture_variation(self) -> None:
        transform = np.float32([[1, 0, 2], [0, 1, -2]])
        for expected, frame in self.frames.items():
            adjusted = cv2.convertScaleAbs(frame, alpha=0.94, beta=10)
            adjusted = cv2.warpAffine(
                adjusted,
                transform,
                (frame.shape[1], frame.shape[0]),
                borderMode=cv2.BORDER_REFLECT,
            )
            with self.subTest(expected=expected):
                best = self.detector.rank_states(adjusted)[0]
                self.assertEqual(best.state, expected)
                self.assertTrue(best.matched)

    def _run(self, states: list[str]) -> FakeInput:
        frames: list[np.ndarray] = []
        for state in states:
            frames.extend(repeated(self.frames[state]))
        sender = FakeInput()
        recorder = FakeRecorder()
        ScenarioRunner(
            self.config,
            self.detector,
            FakeSource(frames),
            sender,
            FakeGuard(),
            recorder,
            sleep=lambda _: None,
        ).run()
        self.assertEqual(recorder.events[-1][1]["status"], "PASS")
        return sender

    def test_following_path_detaches_then_launches(self) -> None:
        sender = self._run([
            "CONTINUE",
            "LOBBY_READY",
            "LEGEND_SELECT",
            "DROPSHIP_FOLLOWING",
            "LAUNCH_READY",
            "FREEFALL",
            "LANDED",
        ])
        self.assertEqual(
            sender.actions,
            [
                ("click", 1280, 865),
                ("click", 1280, 1295),
                ("key", 29, 80),
                ("key", 18, 80),
                ("release",),
            ],
        )

    def test_already_launch_ready_path_skips_detach(self) -> None:
        sender = self._run([
            "CONTINUE",
            "LOBBY_READY",
            "LEGEND_SELECT",
            "LAUNCH_READY",
            "FREEFALL",
            "LANDED",
        ])
        self.assertEqual(
            sender.actions,
            [
                ("click", 1280, 865),
                ("click", 1280, 1295),
                ("key", 18, 80),
                ("release",),
            ],
        )

    def test_loading_black_frames_are_retried(self) -> None:
        frames = repeated(self.frames["CONTINUE"])
        frames.extend([np.zeros_like(self.frames["CONTINUE"]) for _ in range(5)])
        for state in [
            "LOBBY_READY",
            "LEGEND_SELECT",
            "LAUNCH_READY",
            "FREEFALL",
            "LANDED",
        ]:
            frames.extend(repeated(self.frames[state]))

        sender = FakeInput()
        recorder = FakeRecorder()
        ScenarioRunner(
            self.config,
            self.detector,
            FakeSource(frames),
            sender,
            FakeGuard(),
            recorder,
            sleep=lambda _: None,
        ).run()

        self.assertEqual(recorder.events[-1][1]["status"], "PASS")
        self.assertTrue(
            any(event == "CAPTURE_FRAME_SKIPPED" for event, _ in recorder.events)
        )

    def test_ready_click_retries_until_lobby_changes(self) -> None:
        frames = repeated(self.frames["CONTINUE"])
        # Two frames detect the lobby. Two more keep it present through the
        # first confirmation; the second attempt then observes the transition.
        frames.extend(repeated(self.frames["LOBBY_READY"], 5))
        for state in ["LEGEND_SELECT", "LAUNCH_READY", "FREEFALL", "LANDED"]:
            frames.extend(repeated(self.frames[state]))

        sender = FakeInput()
        recorder = FakeRecorder()
        ScenarioRunner(
            self.config,
            self.detector,
            FakeSource(frames),
            sender,
            FakeGuard(),
            recorder,
            sleep=lambda _: None,
        ).run()

        self.assertEqual(
            [action for action in sender.actions if action[:1] == ("click",)],
            [
                ("click", 1280, 865),
                ("click", 1280, 1295),
                ("click", 1280, 1295),
            ],
        )


if __name__ == "__main__":
    unittest.main()

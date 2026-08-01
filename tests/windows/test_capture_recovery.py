from __future__ import annotations

from pathlib import Path
import sys
import unittest


import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capture import DxcamFrameSource


class FakeCamera:
    """Stands in for a DXcam camera with a scripted sequence of results."""

    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.grabs = 0

    def grab(self, new_frame_only: bool = True) -> object:
        self.grabs += 1
        if not self.results:
            return np.zeros((2, 2, 4), dtype=np.uint8)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class CaptureRecoveryTest(unittest.TestCase):
    def source(self, *cameras: FakeCamera) -> tuple[DxcamFrameSource, list[str]]:
        built = list(cameras)
        messages: list[str] = []

        def factory(**_: object) -> FakeCamera:
            return built.pop(0)

        return (
            DxcamFrameSource(
                camera_factory=factory,
                sleep=lambda _: None,
                notify=messages.append,
            ),
            messages,
        )

    def test_a_frame_is_returned_without_rebuilding(self) -> None:
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        camera = FakeCamera([frame])
        source, messages = self.source(camera)
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))
        self.assertEqual(messages, [])

    def test_access_loss_rebuilds_the_camera_and_keeps_going(self) -> None:
        # DXGI hands the duplicator back as an OSError on access loss, which
        # is routine on a machine left running overnight.
        broken = FakeCamera([OSError("access lost")])
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        replacement = FakeCamera([frame])
        source, messages = self.source(broken, replacement)

        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))

        self.assertEqual(replacement.grabs, 1)
        self.assertTrue(any("重建" in message for message in messages))

    def test_an_empty_frame_also_triggers_one_rebuild(self) -> None:
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        source, _ = self.source(FakeCamera([None]), FakeCamera([frame]))
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))

    def test_a_replacement_that_also_fails_is_reported(self) -> None:
        source, _ = self.source(FakeCamera([None]), FakeCamera([None]))
        with source as capture:
            with self.assertRaisesRegex(RuntimeError, "没有返回画面帧"):
                capture.grab()

    def test_grabbing_before_start_is_refused(self) -> None:
        source, _ = self.source(FakeCamera([]))
        with self.assertRaisesRegex(RuntimeError, "尚未启动"):
            source.grab()

    def test_the_old_camera_is_never_released(self) -> None:
        # Releasing an invalidated duplicator is what corrupts the process,
        # so the broken one has to be abandoned rather than closed.
        broken = FakeCamera([OSError("access lost")])
        broken.release = lambda: self.fail("release must not be called")
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        source, _ = self.source(broken, FakeCamera([frame]))
        with source as capture:
            capture.grab()


if __name__ == "__main__":
    unittest.main()

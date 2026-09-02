from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.frame_normalization import (
    CaptureResolutionMismatch,
    ReferenceCanvasFrameSource,
    capture_resolution,
)


class SequenceSource:
    def __init__(self, *frames: np.ndarray) -> None:
        self.frames = list(frames)

    def grab(self) -> np.ndarray:
        return self.frames.pop(0)


def fake_resize(frame: np.ndarray, size: tuple[int, int]) -> np.ndarray:
    width, height = size
    return np.full((height, width, frame.shape[2]), int(frame[0, 0, 0]), dtype=frame.dtype)


class ReferenceCanvasFrameSourceTest(unittest.TestCase):
    def test_native_1080p_is_exposed_as_the_existing_2k_reference_canvas(self) -> None:
        raw = np.full((1080, 1920, 4), 37, dtype=np.uint8)
        source = ReferenceCanvasFrameSource(
            SequenceSource(raw),
            (1920, 1080),
            (2560, 1440),
            resize=fake_resize,
        )

        normalized = source.grab()

        self.assertEqual(normalized.shape, (1440, 2560, 4))
        self.assertEqual(int(normalized[0, 0, 0]), 37)
        self.assertEqual(source.last_source_resolution, (1920, 1080))

    def test_a_runtime_mode_flip_is_rejected_with_the_raw_fault_frame(self) -> None:
        first = np.zeros((1080, 1920, 4), dtype=np.uint8)
        changed = np.zeros((1440, 2560, 4), dtype=np.uint8)
        source = ReferenceCanvasFrameSource(
            SequenceSource(first, changed),
            (1920, 1080),
            (2560, 1440),
            resize=fake_resize,
        )
        source.grab()

        with self.assertRaises(CaptureResolutionMismatch) as caught:
            source.grab()

        error = caught.exception
        self.assertEqual(error.expected, (1920, 1080))
        self.assertEqual(error.got, (2560, 1440))
        self.assertEqual(error.previous, (1920, 1080))
        self.assertIs(error.frame, changed)

    def test_a_wrong_initial_resolution_is_rejected_instead_of_rescaled(self) -> None:
        cropped_or_wrong = np.zeros((720, 1280, 4), dtype=np.uint8)
        source = ReferenceCanvasFrameSource(
            SequenceSource(cropped_or_wrong),
            (1920, 1080),
            (2560, 1440),
            resize=fake_resize,
        )

        with self.assertRaises(CaptureResolutionMismatch) as caught:
            source.grab()

        self.assertIsNone(caught.exception.previous)
        self.assertEqual(caught.exception.got, (1280, 720))

    def test_capture_resolution_defaults_to_reference_for_old_profiles(self) -> None:
        environment = {"width": 2560, "height": 1440}
        self.assertEqual(capture_resolution(environment), (2560, 1440))

    def test_different_aspect_ratios_are_rejected_at_configuration_time(self) -> None:
        environment = {
            "width": 2560,
            "height": 1440,
            "captureResolution": [1920, 1200],
        }
        with self.assertRaisesRegex(ValueError, "宽高比不同"):
            capture_resolution(environment)


if __name__ == "__main__":
    unittest.main()

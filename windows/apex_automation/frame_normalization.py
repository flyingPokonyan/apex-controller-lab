from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol

import numpy as np


Resolution = tuple[int, int]
ResizeFrame = Callable[[np.ndarray, Resolution], np.ndarray]


class RawFrameSource(Protocol):
    def grab(self) -> np.ndarray: ...


def reference_resolution(environment: Mapping[str, Any]) -> Resolution:
    resolution = (int(environment["width"]), int(environment["height"]))
    if resolution[0] <= 0 or resolution[1] <= 0:
        raise ValueError(f"参考分辨率无效：{resolution[0]}x{resolution[1]}")
    return resolution


def capture_resolution(environment: Mapping[str, Any]) -> Resolution:
    reference = reference_resolution(environment)
    raw = environment.get("captureResolution", reference)
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise ValueError("captureResolution 必须是 [width, height]")
    resolution = (int(raw[0]), int(raw[1]))
    if resolution[0] <= 0 or resolution[1] <= 0:
        raise ValueError(f"物理捕获分辨率无效：{resolution[0]}x{resolution[1]}")
    if resolution[0] * reference[1] != reference[0] * resolution[1]:
        raise ValueError(
            "物理捕获与参考画布宽高比不同："
            f"{resolution[0]}x{resolution[1]} -> {reference[0]}x{reference[1]}"
        )
    return resolution


@dataclass(eq=False)
class CaptureResolutionMismatch(RuntimeError):
    expected: Resolution
    got: Resolution
    reference: Resolution
    frame: np.ndarray
    previous: Resolution | None = None

    def __str__(self) -> str:
        transition = (
            ""
            if self.previous is None
            else f"，上一帧是 {self.previous[0]}x{self.previous[1]}"
        )
        return (
            f"物理捕获必须保持 {self.expected[0]}x{self.expected[1]}，"
            f"实际为 {self.got[0]}x{self.got[1]}{transition}"
        )

    def as_event_payload(self) -> dict[str, object]:
        return {
            "got": list(self.got),
            "expected": list(self.expected),
            "reference": list(self.reference),
            "previous": None if self.previous is None else list(self.previous),
        }


def _resize_lanczos(frame: np.ndarray, size: Resolution) -> np.ndarray:
    # OpenCV is a Windows runtime dependency, but keeping the import lazy lets
    # configuration and orchestration tests run on machines without the vision
    # stack installed.
    import cv2

    return cv2.resize(frame, size, interpolation=cv2.INTER_LANCZOS4)


class ReferenceCanvasFrameSource:
    """Expose a fixed reference canvas without changing the physical desktop.

    EA automation must keep using the raw 1920x1080 source because its HWND
    rectangles are physical desktop coordinates. Only Apex recognition receives
    this wrapper. Any source-size change is rejected before OCR or input: a
    1920x1080 frame after a 2K mode collapse may be a crop, not a scaled image.
    """

    def __init__(
        self,
        source: RawFrameSource,
        capture_size: Resolution,
        reference_size: Resolution,
        *,
        resize: ResizeFrame = _resize_lanczos,
    ) -> None:
        if capture_size[0] * reference_size[1] != reference_size[0] * capture_size[1]:
            raise ValueError("物理捕获与参考画布必须使用相同宽高比")
        self.source = source
        self.capture_size = capture_size
        self.reference_size = reference_size
        self.resize = resize
        self.last_source_resolution: Resolution | None = None

    @classmethod
    def from_environment(
        cls,
        source: RawFrameSource,
        environment: Mapping[str, Any],
        *,
        resize: ResizeFrame = _resize_lanczos,
    ) -> "ReferenceCanvasFrameSource":
        return cls(
            source,
            capture_resolution(environment),
            reference_resolution(environment),
            resize=resize,
        )

    def grab(self) -> np.ndarray:
        frame = self.source.grab()
        if not isinstance(frame, np.ndarray) or frame.ndim < 2:
            raise RuntimeError("截图源返回了无效画面")
        height, width = frame.shape[:2]
        got = (int(width), int(height))
        previous = self.last_source_resolution
        self.last_source_resolution = got
        if got != self.capture_size:
            raise CaptureResolutionMismatch(
                expected=self.capture_size,
                got=got,
                reference=self.reference_size,
                previous=previous,
                frame=frame,
            )
        if got == self.reference_size:
            return frame
        normalized = self.resize(frame, self.reference_size)
        normalized_height, normalized_width = normalized.shape[:2]
        if (normalized_width, normalized_height) != self.reference_size:
            raise RuntimeError(
                "画布归一化返回了错误尺寸："
                f"{normalized_width}x{normalized_height}"
            )
        return normalized

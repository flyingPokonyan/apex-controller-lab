from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np

from .config import AnchorConfig, RunnerConfig


@dataclass(frozen=True)
class StateScore:
    state: str
    matched: bool
    score: float
    anchor_scores: tuple[float, ...]


class NearBlackFrameError(ValueError):
    """A transient frame with too little visual information for matching."""


def load_frame(path: Path | str) -> np.ndarray:
    frame = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if frame is None:
        raise FileNotFoundError(f"无法读取图片：{path}")
    return frame


def save_frame(path: Path | str, frame: np.ndarray) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(target), frame):
        raise OSError(f"无法保存图片：{target}")


def _gray(frame: np.ndarray) -> np.ndarray:
    if frame.ndim == 2:
        return frame
    if frame.shape[2] == 4:
        return cv2.cvtColor(frame, cv2.COLOR_BGRA2GRAY)
    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)


def _edges(frame: np.ndarray) -> np.ndarray:
    return cv2.Canny(_gray(frame), 70, 160)


def frame_gray_std(frame: np.ndarray) -> float:
    return float(_gray(frame).std())


def frame_motion(previous: np.ndarray | None, current: np.ndarray) -> float:
    """Mean absolute luma change between two frames, normalised to 0..1.

    Downscaled so the metric stays cheap at the observation cadence and
    tolerates per-pixel capture noise. It separates three cases the anchor
    scores alone cannot tell apart: a cutscene (high and sustained), a loading
    screen (low and dark) and a frozen frame (near zero).
    """
    if previous is None or previous.shape != current.shape:
        return 0.0
    size = (320, 180)
    before = cv2.resize(_gray(previous), size, interpolation=cv2.INTER_AREA)
    after = cv2.resize(_gray(current), size, interpolation=cv2.INTER_AREA)
    return float(np.mean(cv2.absdiff(before, after))) / 255.0


def _edge_f1(template_edges: np.ndarray, current_edges: np.ndarray) -> float:
    template = template_edges > 0
    current = current_edges > 0
    kernel = np.ones((3, 3), dtype=np.uint8)
    dilated_template = cv2.dilate(template.astype(np.uint8), kernel) > 0
    dilated_current = cv2.dilate(current.astype(np.uint8), kernel) > 0

    template_count = int(template.sum())
    current_count = int(current.sum())
    if template_count == 0 or current_count == 0:
        return 0.0

    recall = float((template & dilated_current).sum()) / template_count
    precision = float((current & dilated_template).sum()) / current_count
    return 2.0 * precision * recall / max(precision + recall, 1e-9)


class VisionDetector:
    def __init__(self, config: RunnerConfig):
        self.config = config
        self._template_edges: dict[Path, np.ndarray] = {}
        for state in config.states.values():
            for anchor in state.anchors:
                template = load_frame(anchor.template)
                expected_width = anchor.roi[2] - anchor.roi[0]
                expected_height = anchor.roi[3] - anchor.roi[1]
                if template.shape[:2] != (expected_height, expected_width):
                    raise ValueError(
                        f"模板尺寸与 ROI 不一致：{anchor.template} "
                        f"{template.shape[1]}x{template.shape[0]} != {expected_width}x{expected_height}"
                    )
                self._template_edges[anchor.template] = _edges(template)

    def validate_frame(self, frame: np.ndarray) -> None:
        height, width = frame.shape[:2]
        expected = (int(self.config.environment["width"]), int(self.config.environment["height"]))
        if (width, height) != expected:
            raise ValueError(f"捕获分辨率必须为 {expected[0]}x{expected[1]}，实际为 {width}x{height}")
        if frame_gray_std(frame) < 3.0:
            raise NearBlackFrameError("捕获画面接近纯黑")

    def _score_anchor(self, frame: np.ndarray, anchor: AnchorConfig) -> float:
        x1, y1, x2, y2 = anchor.roi
        margin = max(0, anchor.search_px)
        best = 0.0
        template_edges = self._template_edges[anchor.template]

        for offset_y in range(-margin, margin + 1, 2 if margin else 1):
            for offset_x in range(-margin, margin + 1, 2 if margin else 1):
                left, top = x1 + offset_x, y1 + offset_y
                right, bottom = x2 + offset_x, y2 + offset_y
                if left < 0 or top < 0 or right > frame.shape[1] or bottom > frame.shape[0]:
                    continue
                crop = frame[top:bottom, left:right]
                best = max(best, _edge_f1(template_edges, _edges(crop)))
        return best

    def score_state(self, frame: np.ndarray, state_name: str) -> StateScore:
        state = self.config.states[state_name]
        anchor_scores = tuple(self._score_anchor(frame, anchor) for anchor in state.anchors)
        matched = all(
            score >= anchor.threshold
            for score, anchor in zip(anchor_scores, state.anchors, strict=True)
        )
        score = min(anchor_scores) if anchor_scores else 0.0
        return StateScore(state_name, matched, score, anchor_scores)

    def rank_states(self, frame: np.ndarray, states: Iterable[str] | None = None) -> list[StateScore]:
        candidates = list(states) if states is not None else list(self.config.states)
        return sorted(
            (self.score_state(frame, state_name) for state_name in candidates),
            key=lambda item: item.score,
            reverse=True,
        )

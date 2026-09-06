"""Locate the close control on EA's observed Library introduction card.

The reference is a remote-desktop screenshot, not a calibrated game frame.
All geometry therefore comes from current OCR boxes and the button itself.
"""

from __future__ import annotations

from typing import Sequence

import cv2
import numpy as np

from .ocr_obstacles import OcrToken


def library_tour_visible(tokens: Sequence[OcrToken]) -> bool:
    compact = "".join(token.normalized for token in tokens if token.confidence >= 0.65)
    return (
        "allyourcontentinoneplace" in compact
        and "manageownedgamesandaddons" in compact
        and "previous" in compact
    )


def library_tour_close_point(
    frame: np.ndarray,
    tokens: Sequence[OcrToken],
    rect: tuple[int, int, int, int],
) -> tuple[int, int] | None:
    """Require the known copy, a square blue outline, and an inner X glyph.

    No screen-coordinate fallback: a missing/ambiguous control is not a
    license to click an illustration or the EA application's own close button.
    """

    if not library_tour_visible(tokens) or frame.ndim != 3:
        return None
    titles = [
        token for token in tokens
        if token.roi is not None and token.confidence >= 0.65
        and "allyourcontent" in token.normalized
    ]
    next_buttons = [
        token for token in tokens
        if token.roi is not None and token.confidence >= 0.65
        and token.normalized in ("next", "nxt")
    ]
    candidates: set[tuple[int, int]] = set()
    for title in titles:
        tx1, ty1, tx2, ty2 = title.roi
        text_height = max(1, ty2 - ty1)
        for next_button in next_buttons:
            nx1, ny1, nx2, _ = next_button.roi
            if nx1 <= tx1 or ny1 <= ty2:
                continue
            # The close control is above the title and near the card's right
            # edge, indicated by NEXT. The title bar is outside this search.
            left = max(0, rect[0], (tx1 + tx2) // 2)
            top = max(0, rect[1] + 2 * text_height, ty1 - 20 * text_height)
            right = min(frame.shape[1], rect[2], nx2 + 2 * text_height)
            bottom = min(frame.shape[0], rect[3], ty1 - text_height)
            if left >= right or top >= bottom:
                continue
            crop = frame[top:bottom, left:right, :3]
            hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
            blue = cv2.inRange(hsv, (95, 100, 100), (125, 255, 255))
            contours, _ = cv2.findContours(blue, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            for contour in contours:
                x, y, width, height = cv2.boundingRect(contour)
                if not (
                    0.8 <= width / height <= 1.2
                    and 0.8 * text_height <= min(width, height)
                    and max(width, height) <= 2.5 * text_height
                ):
                    continue
                # Blue squares also occur in the tour illustration. Only
                # accept a gray/white X inside the candidate's border.
                inner = hsv[
                    y + height // 4 : y + 3 * height // 4,
                    x + width // 4 : x + 3 * width // 4,
                ]
                white = cv2.inRange(inner, (0, 0, 110), (179, 85, 255))
                ys, xs = np.nonzero(white)
                if len(xs) < 6 or np.ptp(xs) < width * 0.2 or np.ptp(ys) < height * 0.2:
                    continue
                u = (xs - xs.min()) / np.ptp(xs)
                v = (ys - ys.min()) / np.ptp(ys)
                diagonal_a, diagonal_b = np.abs(u - v), np.abs(u + v - 1)
                if (
                    np.mean(np.minimum(diagonal_a, diagonal_b)) > 0.16
                    or np.mean(diagonal_a < 0.20) < 0.30
                    or np.mean(diagonal_b < 0.20) < 0.30
                ):
                    continue
                candidates.add((left + x + width // 2, top + y + height // 2))
    return next(iter(candidates)) if len(candidates) == 1 else None

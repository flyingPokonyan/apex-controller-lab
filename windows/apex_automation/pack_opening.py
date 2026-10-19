"""Open the packs already on an account before that session exits.

The lobby entry is the whole pack card. Counts that cannot be read are not
treated as zero, and a red cell is reported even when a later step fails.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable

import numpy as np

from .apex_packs import ApexPackReader, pack_region
from .safety import EmergencyStop


ESC_SCAN = 1
SPACE_SCAN = 57


@dataclass(frozen=True)
class PackLimits:
    page_s: float = 20
    hold_s: float = 8
    session_s: float = 600
    max_batches: int = 5
    minimum_hold_s: float = 0.4


@dataclass(frozen=True)
class PackOpeningResult:
    status: str
    heirloom: bool
    batches: int
    packs_before: int | None
    reason: str | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status,
            "heirloom": self.heirloom,
            "batches": self.batches,
            "packsBefore": self.packs_before,
        }
        if self.reason:
            payload["reason"] = self.reason
        return payload


def _red(frame: np.ndarray) -> np.ndarray:
    # Capture frames are BGR. A red fill is brighter in red than in green or
    # blue, which keeps an orange selection border and the sparks on the right
    # out of the match when the sample stays inside the cell.
    blue = frame[:, :, 0].astype(np.int16)
    green = frame[:, :, 1].astype(np.int16)
    red = frame[:, :, 2].astype(np.int16)
    return (red > 150) & (red > green + 50) & (red > blue + 50)


def heirloom_visible(frame: np.ndarray) -> bool:
    """Loose check: any visible cell fill, or the red shard icon in the total."""
    if not isinstance(frame, np.ndarray) or frame.ndim < 3 or frame.shape[2] < 3:
        return False
    red = _red(frame)
    height, width = red.shape[:2]
    x0, x1, y0, y1 = 0.04, 0.33, 0.18, 0.81
    columns, rows = 5, 6
    cell_w, cell_h = (x1 - x0) / columns, (y1 - y0) / rows
    for row in range(rows):
        for column in range(columns):
            left, right = x0 + column * cell_w, x0 + (column + 1) * cell_w
            top, bottom = y0 + row * cell_h, y0 + (row + 1) * cell_h
            margin_x, margin_y = (right - left) * 0.2, (bottom - top) * 0.2
            crop = red[
                int(height * (top + margin_y)):int(height * (bottom - margin_y)),
                int(width * (left + margin_x)):int(width * (right - margin_x)),
            ]
            if crop.size and float(crop.mean()) >= 0.08:
                return True
    currency = red[int(height * 0.835):int(height * 0.90), int(width * 0.04):int(width * 0.33)]
    return bool(currency.size and int(currency.sum()) >= 25)


def grid_settled(previous: np.ndarray, current: np.ndarray) -> bool:
    """Two reward-grid samples match apart from under 2% of points."""
    if previous.shape != current.shape:
        return False
    changed = np.abs(previous - current).max(axis=2) > 40
    return float(changed.mean()) < 0.02


def classify(tokens) -> str:
    names = [token.normalized for token in tokens]
    if any("组合包奖励" in name for name in names):
        return "result"
    if any("已获得奖励" in name for name in names):
        return "reward"
    if any("按住打开" in name for name in names):
        return "hold"
    if any("火花奖励计数器" in name for name in names):
        return "intro"
    if any("开启组合包" in name for name in names) and any(name == "最高" for name in names):
        return "select"
    return "unknown"


def _center(token) -> tuple[int, int]:
    left, top, right, bottom = token.roi
    return (left + right) // 2, (top + bottom) // 2


def select_count(tokens, width: int, height: int) -> int | None:
    """The large count on the left of the open page, not the spark 0/10."""
    digits = []
    for token in tokens:
        if token.roi is None or token.confidence < 0.7:
            continue
        if not re.fullmatch(r"[0-9]{1,4}", token.normalized):
            continue
        left, top, right, bottom = token.roi
        center_x, center_y = (left + right) / 2, (top + bottom) / 2
        if center_x > width * 0.40 or not height * 0.10 <= center_y <= height * 0.55:
            continue
        digits.append(token)
    if not digits:
        return None
    chosen = max(digits, key=lambda token: token.roi[3] - token.roi[1])
    return int(chosen.normalized)


def card_point(frame, tokens) -> tuple[int, int] | None:
    left, top, right, bottom = pack_region(frame)
    boxes = [
        token.roi for token in tokens
        if token.roi and token.normalized in {"组合包", "apex"}
        and left <= token.roi[0] <= right and top <= token.roi[1] <= bottom
    ]
    if not boxes:
        return None
    return (
        (min(box[0] for box in boxes) + max(box[2] for box in boxes)) // 2,
        (min(box[1] for box in boxes) + max(box[3] for box in boxes)) // 2,
    )


class PackOpener:
    def __init__(self, source, ocr, sender, guard, *, save_frame, log, is_current=lambda: True,
                 clock=time.monotonic, sleep=time.sleep, limits: PackLimits | None = None):
        self.source, self.ocr, self.sender, self.guard = source, ocr, sender, guard
        self.save_frame, self.log = save_frame, log
        self.is_current, self.clock, self.sleep = is_current, clock, sleep
        self.limits = limits or PackLimits()
        self.frame = None
        self.tokens = ()
        self.heirloom = False
        self.batches = 0
        self.packs_before = None

    def run(self) -> PackOpeningResult:
        try:
            return self._run()
        except EmergencyStop:
            self._release()
            raise
        except Exception as error:
            self.log("PACK_OPEN_ERROR", errorType=type(error).__name__)
            return self._failed("PAGE_UNKNOWN")
        finally:
            self._release()

    def _release(self) -> None:
        release = getattr(self.sender, "release_all", None)
        if callable(release):
            release()

    def _failed(self, reason: str) -> PackOpeningResult:
        return PackOpeningResult("FAILED", self.heirloom, self.batches, self.packs_before, reason)

    def _done(self) -> PackOpeningResult:
        status = "NO_PACKS" if self.batches == 0 else "DONE"
        return PackOpeningResult(status, self.heirloom, self.batches, self.packs_before)

    def _look(self):
        self.guard.ensure_not_aborted()
        if not self.guard.target_is_foreground() or not self.is_current():
            return "unknown"
        frame = self.source.grab()
        if isinstance(frame, np.ndarray) and frame.ndim == 3 and frame.shape[2] == 4:
            # RapidOCR treats a fourth channel as transparency. A capture whose
            # alpha is 0 becomes a blank white image and reads no text at all.
            frame = np.ascontiguousarray(frame[:, :, :3])
        self.frame = frame
        self.tokens = tuple(self.ocr.read_with_boxes(frame))
        return classify(self.tokens)

    def _lobby_card(self):
        """The pack card, from the whole frame or else from the card region alone.

        The region read is what the inventory probe already relies on; small
        card text can be missed when the whole 2560x1440 frame is read at once.
        """
        point = card_point(self.frame, self.tokens)
        if point is None:
            reading = ApexPackReader(self.ocr).read(self.frame)
            point = card_point(self.frame, reading.tokens)
            if self.packs_before is None and reading.status == "OK":
                self.packs_before = reading.count
        return point

    def _wait(self, kinds: set[str], timeout: float) -> str | None:
        deadline = self.clock() + timeout
        while True:
            page = self._look()
            if page in kinds:
                return page
            if self.clock() >= deadline:
                return None
            self.sleep(0.2)

    def _click_token(self, predicate, *, lowest=False) -> bool:
        found = [token for token in self.tokens if token.roi and predicate(token)]
        if not found:
            return False
        if lowest:
            found.sort(key=lambda token: token.roi[1])
            token = found[-1]
        else:
            token = found[0]
        self.sender.click(*_center(token))
        return True

    def _hold(self) -> str:
        page = self._wait({"hold", "result"}, self.limits.page_s)
        if page == "result":
            return "opened"
        if page != "hold":
            return "HOLD_TIMEOUT"
        button = next((token for token in self.tokens if token.roi and "按住打开" in token.normalized), None)
        if button is None:
            return "PAGE_UNKNOWN"
        self.sender.move_to(*_center(button))
        self.sender.mouse_down()
        try:
            started = self.clock()
            deadline = started + self.limits.hold_s
            while self.clock() < deadline:
                self.sleep(0.2)
                if self._look() != "hold" and self.clock() - started >= self.limits.minimum_hold_s:
                    return "opened"
            return "HOLD_TIMEOUT"
        finally:
            self.sender.mouse_up()

    def _grid_sample(self) -> np.ndarray:
        height, width = self.frame.shape[:2]
        crop = self.frame[int(height * 0.18):int(height * 0.90), int(width * 0.04):int(width * 0.33), :3]
        return crop[::8, ::8].astype(np.int16)

    def _stable_result(self, deadline: float):
        # The title is visible and the reward grid has stopped changing.
        # A changing grid is not "no red". Sparks drifting through the gaps and
        # capture noise move a few samples, so "stopped" allows a small share.
        previous = None
        while self.clock() < deadline:
            if self._look() == "result" and self.frame is not None:
                current = (heirloom_visible(self.frame), self._grid_sample())
                if previous is not None and previous[0] == current[0] and grid_settled(previous[1], current[1]):
                    return current[0]
                previous = current
            else:
                previous = None
            if self.clock() >= deadline:
                break
            self.sleep(0.3)
        return None

    def _run(self) -> PackOpeningResult:
        deadline = self.clock() + self.limits.session_s
        page = self._look()
        if page == "unknown":
            point = None if self.frame is None else self._lobby_card()
            if point is None:
                page = self._wait({"intro", "select", "reward", "result"}, self.limits.page_s)
                if page is None:
                    return self._failed("PAGE_UNKNOWN")
            else:
                self.sender.click(*point)
                page = None
        while self.clock() < deadline:
            if page is None:
                remaining = deadline - self.clock()
                page = self._wait({"intro", "select", "reward", "result", "hold"}, min(self.limits.page_s, remaining))
            if page is None:
                return self._failed("RESULT_TIMEOUT" if self.batches else "PAGE_UNKNOWN")
            if page == "intro":
                if not self._click_token(lambda token: "开启组合包" in token.normalized, lowest=True):
                    return self._failed("PAGE_UNKNOWN")
                page = None
                continue
            if page == "reward":
                self.sender.tap_scan_code(SPACE_SCAN, 80)
                page = None
                continue
            if page == "hold":
                outcome = self._hold()
                if outcome != "opened":
                    return self._failed(outcome)
                page = "result"
            if page == "result":
                red = self._stable_result(min(deadline, self.clock() + self.limits.page_s))
                if red is None:
                    return self._failed("RESULT_TIMEOUT")
                self.heirloom = self.heirloom or red
                self.batches += 1
                if not self.save_frame(self.frame, self.batches, red):
                    return self._failed("SCREENSHOT_FAILED")
                self.sender.tap_scan_code(ESC_SCAN, 80)
                page = None
                continue
            count = select_count(self.tokens, self.frame.shape[1], self.frame.shape[0])
            if self.packs_before is None:
                self.packs_before = count
            if count == 0:
                return self._done()
            if count is None:
                return self._failed("PAGE_UNKNOWN")
            if self.batches >= self.limits.max_batches:
                return self._failed("BATCH_LIMIT")
            if not self._click_token(lambda token: token.normalized == "最高"):
                return self._failed("PAGE_UNKNOWN")
            outcome = self._hold()
            if outcome != "opened":
                return self._failed(outcome)
            red = self._stable_result(min(deadline, self.clock() + self.limits.page_s))
            if red is None:
                return self._failed("RESULT_TIMEOUT")
            self.heirloom = self.heirloom or red
            self.batches += 1
            if not self.save_frame(self.frame, self.batches, red):
                return self._failed("SCREENSHOT_FAILED")
            self.sender.tap_scan_code(ESC_SCAN, 80)
            page = None
        return self._failed("RESULT_TIMEOUT")


def open_account_packs(source, ocr, sender, guard, **kwargs) -> PackOpeningResult:
    return PackOpener(source, ocr, sender, guard, **kwargs).run()

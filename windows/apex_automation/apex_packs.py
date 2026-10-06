"""Read inventory from the labelled Apex Packs card, independently of account level."""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
import numpy as np

from .ocr_obstacles import OcrToken
from .safety import EmergencyStop


def pack_region(frame):
    height, width = frame.shape[:2]
    return int(width * .77), int(height * .66), int(width * .995), int(height * .975)


def notification_marker(tokens):
    for token in tokens:
        if token.confidence < .70:
            continue
        if "手心输入法" in token.normalized:
            return "hand-input", token
        if "uu远程" in token.normalized or (
            "uu" in token.normalized and "程" in token.normalized
            and any("已成功断开" in other.normalized and other.confidence >= .85 for other in tokens)
        ):
            return "uu-remote", token
    return None


@dataclass(frozen=True)
class PackReading:
    count: int | None
    confidence: float
    status: str
    tokens: tuple[OcrToken, ...] = ()


def parse_pack_tokens(tokens, *, min_confidence=.85):
    tokens = tuple(tokens)
    if notification_marker(tokens):
        return PackReading(None, 0, "OCCLUDED", tokens)
    labels = [t for t in tokens if t.normalized == "组合包" and t.roi]
    candidates = []
    for label in labels:
        lx, ly, rx, ry = label.roi
        height = ry - ly
        apex = [t for t in tokens if t.normalized == "apex" and t.roi
                and abs(t.roi[0] - lx) < height * 1.5
                and ly - height * 2 <= t.roi[1] < ly]
        if not apex:
            continue
        for digit in tokens:
            if not digit.roi or not re.fullmatch(r"[0-9]{1,4}", digit.normalized):
                continue
            dx, dy, dr, db = digit.roi
            # Inventory sits immediately left of both stacked labels. Reject
            # promo dates and the separate "开启 0/10" action underneath.
            if not (lx - height * 5 <= dx < lx and dr <= lx + height * .5
                    and db > ly and dy < ly and db <= ry + height * .5):
                continue
            confidence = min(label.confidence, digit.confidence, max(t.confidence for t in apex))
            if confidence >= min_confidence:
                candidates.append((int(digit.normalized), confidence))
    if len(candidates) == 1:
        count, confidence = candidates[0]
        return PackReading(count, confidence, "OK", tokens)
    return PackReading(None, 0, "UNREADABLE" if labels else "NOT_VISIBLE", tokens)


class ApexPackReader:
    def __init__(self, provider):
        self.provider = provider

    def read(self, frame):
        x, y, r, b = pack_region(frame)
        crop = np.ascontiguousarray(frame[y:b, x:r])
        tokens = tuple(OcrToken(t.text, t.confidence,
                               None if t.roi is None else
                               (t.roi[0] + x, t.roi[1] + y, t.roi[2] + x, t.roi[3] + y))
                       for t in self.provider.read_with_boxes(crop))
        return parse_pack_tokens(tokens)


class PackInventoryProbe:
    """Two fresh captures confirm a count. Failure never holds the play loop."""
    def __init__(self, reader, source, guard, log, *, close_notification=None,
                 metrics=None, clock=time.monotonic, sleep=time.sleep, is_current=lambda: True):
        self.reader, self.source, self.guard, self.log = reader, source, guard, log
        self.close_notification = close_notification
        self.metrics = metrics
        self.clock, self.sleep = clock, sleep
        self.is_current = is_current
        self.reset()

    def reset(self):
        self.attempts = 0
        self.last_at = -float("inf")
        self.candidate = None
        self.samples = 0
        self.done = False
        self.last_status = "UNCONFIRMED"
        self.reported_failure = None

    def _report_failure(self):
        status = "UNCONFIRMED" if self.last_status == "OK" else self.last_status
        key = status, self.samples
        if self.reported_failure != key:
            self.log("APEX_PACKS", count=None, confidence=0, readStatus=status, samples=self.samples)
            self.reported_failure = key

    def observe(self, *, force=False):
        now = self.clock()
        if self.done or (not force and (self.attempts >= 3 or now - self.last_at < 1)):
            return
        self.guard.ensure_not_aborted()
        if not self.guard.target_is_foreground():
            return
        self.last_at = now
        try:
            if not self.is_current():
                return
            fresh = getattr(self.source, "grab_fresh", None)
            frame = fresh() if callable(fresh) else None
            # A cached DXGI frame is not an independent observation. Static
            # desktops legitimately return None, without needing recovery.
            if frame is None:
                return
            start = self.clock()
            reading = self.reader.read(frame)
            self.guard.ensure_not_aborted()
            if not self.guard.target_is_foreground() or not self.is_current():
                return
            if self.metrics:
                self.metrics.add("packOcr", (self.clock() - start) * 1000)
            self.attempts += 1
            self.last_status = reading.status
            if reading.status == "OCCLUDED" and self.close_notification:
                self.close_notification(frame, reading.tokens)
                self.candidate, self.samples = None, 0
            elif reading.status == "OK":
                if reading.count == self.candidate:
                    self.samples += 1
                else:
                    self.candidate, self.samples = reading.count, 1
                if self.samples >= 2:
                    self.log("APEX_PACKS", count=reading.count, confidence=round(reading.confidence, 4),
                             readStatus="OK", samples=self.samples)
                    self.done = True
                    return
            else:
                self.candidate, self.samples = None, 0
            if self.attempts >= 3:
                self._report_failure()
        except EmergencyStop:
            raise
        except Exception as error:
            self.attempts += 1
            self.log("PACK_READ_ERROR", errorType=type(error).__name__)

    def finish(self, *, is_current=lambda: True, budget_s=4):
        if self.done:
            return
        self.attempts = 0
        deadline = self.clock() + budget_s
        # Also bounded by iteration count for injected clocks and capture
        # sources that return cached/empty frames throughout the final read.
        for _ in range(6):
            if self.done or self.clock() >= deadline:
                break
            try:
                if not is_current():
                    break
            except Exception:
                break
            self.observe(force=True)
            if not self.done:
                self.sleep(min(.5, max(0, deadline - self.clock())))
        if not self.done:
            self._report_failure()

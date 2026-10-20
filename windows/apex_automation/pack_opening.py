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

from .apex_packs import ApexPackReader, PackReading, pack_region
from .safety import EmergencyStop, ForegroundLost


ESC_SCAN = 1
SPACE_SCAN = 57
PACK_PAGES = frozenset({"intro", "select", "reward", "result"})
# Coming back from another window, a fullscreen game is still switching
# display modes: the first frames can be the old desktop and a click can be lost.
FOCUS_SETTLE_S = 1.5
FRESH_FRAME_S = 1.0
CARD_ATTEMPTS = 3
CARD_WAIT_S = 6.0
# A hold that never leaves the open page is retried this often per batch.
PRESS_ATTEMPTS = 3
# The spark reward (已获得奖励) can pop up a moment after the last batch, and
# what it grants is one more pack, so a count of 0 is watched this long first.
REWARD_GRACE_S = 8.0
# Its pack drops in before SPACE 继续 takes a key.
REWARD_SETTLE_S = 1.0
REWARD_PRESSES = 6


class _Covered(Exception):
    """Another program's window is on top where a pack button was to be clicked."""


@dataclass(frozen=True)
class PackLimits:
    page_s: float = 20
    # Opening a whole stack plays one animation before the reward page.
    animation_s: float = 45
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


OPEN_BUTTON = re.compile(r"开启([0-9]{1,3})个")


def space_continue(names) -> bool:
    """A follow-up 'SPACE 继续' whose title the OCR missed.

    Normalization drops the space, so the live prompt is one token, space继续.
    """
    return any("继续" in name and ("space" in name or "空格" in name) for name in names)


def classify(tokens) -> str:
    names = [token.normalized for token in tokens]
    if any("组合包奖励" in name for name in names):
        return "result"
    if any("已获得奖励" in name for name in names) or space_continue(names):
        return "reward"
    # The open page, whether its centre button reads 开启N个 or, under the
    # pointer, 按住打开. Before intro: after 最高 it adds a footnote ending in
    # 火花奖励计数器 ("11个超额组合包打开时不适用于火花奖励计数器").
    if any("开启组合包" in name for name in names) and any(name == "最高" for name in names):
        return "select"
    if any(name == "火花奖励计数器" for name in names):
        return "intro"
    return "unknown"


def open_point(tokens) -> tuple[int, int] | None:
    """Centre of the open button: its own text, else midway between 重置 and 最高.

    Its bold count is the text OCR misses most (Runner 1 read none of
    开启21个 on 10-19), and the button always sits centred in that row.
    """
    for token in tokens:
        if token.roi and (OPEN_BUTTON.fullmatch(token.normalized) or "按住打开" in token.normalized):
            return _center(token)
    reset = next((token for token in tokens if token.roi and token.normalized == "重置"), None)
    highest = next((token for token in tokens if token.roi and token.normalized == "最高"), None)
    if reset is None or highest is None:
        return None
    (reset_x, reset_y), (highest_x, highest_y) = _center(reset), _center(highest)
    if reset_x >= highest_x or abs(reset_y - highest_y) > highest.roi[3] - highest.roi[1]:
        return None
    return (reset_x + highest_x) // 2, (reset_y + highest_y) // 2


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


def desktop_window_owner(reference_width: int, reference_height: int):
    """Who owns the top-level window under a reference-canvas point, on Windows.

    A remote-desktop notice in the bottom-right corner can sit over the pack
    card unseen; clicking it hands the focus away and Apex drops to the desktop.
    """
    try:
        from .notifications import Win32NotificationBackend
        backend = Win32NotificationBackend()
    except Exception:
        return None

    def owner(point):
        width, height = backend.size()
        physical = (int(point[0] * width / reference_width), int(point[1] * height / reference_height))
        window = backend.window_at(physical)
        if window is None:
            return None
        return window.executable.lower(), list(window.rect)

    return owner


class PackOpener:
    def __init__(self, source, ocr, sender, guard, *, save_frame, log, is_current=lambda: True,
                 clock=time.monotonic, sleep=time.sleep, limits: PackLimits | None = None,
                 window_owner=None):
        self.source, self.ocr, self.sender, self.guard = source, ocr, sender, guard
        self.save_frame, self.log = save_frame, log
        self.window_owner = window_owner
        self.covered_by: str | None = None
        self.maxed = False
        self.presses = 0
        self.reward_presses = 0
        # The count when the open button was last held, until its result page shows.
        self.count_at_press: int | None = None
        self.is_current, self.clock, self.sleep = is_current, clock, sleep
        self.limits = limits or PackLimits()
        self.frame = None
        self.tokens = ()
        self.heirloom = False
        self.batches = 0
        self.packs_before = None
        # Why the last look saw nothing; it names a timeout better than the page did.
        self.blocked: str | None = None
        # Space on the spark reward can play another open animation, or drop
        # back to the lobby with the granted pack on the card.
        self.after_reward = False

    def run(self) -> PackOpeningResult:
        try:
            return self._run()
        except EmergencyStop:
            self._release()
            raise
        except ForegroundLost:
            # The input guard refused a click because another window took over.
            return self._failed("FOREGROUND_LOST")
        except _Covered:
            return self._failed("CLICK_COVERED")
        except Exception as error:
            self.log("PACK_OPEN_ERROR", errorType=type(error).__name__)
            return self._failed("OPEN_ERROR")
        finally:
            self._release()

    def _release(self) -> None:
        release = getattr(self.sender, "release_all", None)
        if callable(release):
            release()

    def _failed(self, reason: str) -> PackOpeningResult:
        return PackOpeningResult("FAILED", self.heirloom, self.batches, self.packs_before, reason)

    def _reason(self, default: str) -> str:
        return self.blocked or default

    def _done(self) -> PackOpeningResult:
        status = "NO_PACKS" if self.batches == 0 else "DONE"
        return PackOpeningResult(status, self.heirloom, self.batches, self.packs_before)

    def _grab(self):
        """A frame drawn after this call, else the latest one.

        A plain grab can return a cached frame from before the last click or
        from while another window was in front.
        """
        fresh = getattr(self.source, "grab_fresh", None)
        if callable(fresh):
            deadline = self.clock() + FRESH_FRAME_S
            while True:
                frame = fresh()
                if frame is not None:
                    return frame
                if self.clock() >= deadline:
                    break
                self.sleep(0.05)
        return self.source.grab()

    def _look(self):
        self.guard.ensure_not_aborted()
        if not self.is_current():
            self.blocked = "LEASE_LOST"
            return "blocked"
        if not self.guard.target_is_foreground():
            self.blocked = "FOREGROUND_LOST"
            return "blocked"
        if self.blocked == "FOREGROUND_LOST":
            self.log("PACK_FOREGROUND_RESUMED")
            self.sleep(FOCUS_SETTLE_S)
        self.blocked = None
        frame = self._grab()
        if isinstance(frame, np.ndarray) and frame.ndim == 3 and frame.shape[2] == 4:
            # RapidOCR treats a fourth channel as transparency. A capture whose
            # alpha is 0 becomes a blank white image and reads no text at all.
            frame = np.ascontiguousarray(frame[:, :, :3])
        self.frame = frame
        self.tokens = tuple(self.ocr.read_with_boxes(frame))
        return classify(self.tokens)

    def _lobby_card(self, *, strict=False):
        """The pack card, from the whole frame or else from the card region alone.

        The region read is what the inventory probe already relies on; small
        card text can be missed when the whole 2560x1440 frame is read at once.
        Strict asks for the lobby layout itself (count beside APEX/组合包), so
        a pack page that also says 组合包 there is never clicked as the card.
        """
        point = None if strict else card_point(self.frame, self.tokens)
        if point is None:
            reading = ApexPackReader(self.ocr).read(self.frame)
            if strict and reading.status != "OK":
                return None
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

    def _lobby_reading(self) -> PackReading:
        if self.frame is None:
            return PackReading(None, 0, "NOT_VISIBLE")
        return ApexPackReader(self.ocr).read(self.frame)

    def _wait_known(self, timeout: float) -> str | None:
        """A pack page, or the lobby when its pack card reads a number.

        The card text is too small for the full-frame read that classify uses,
        so a return to the lobby otherwise looks like an unknown page.
        """
        deadline = self.clock() + timeout
        while True:
            page = self._look()
            if page in PACK_PAGES:
                return page
            if page != "blocked" and self._lobby_reading().status == "OK":
                return "lobby"
            if self.clock() >= deadline:
                return None
            self.sleep(0.2)

    def _resume_from_lobby(self) -> str | None:
        """Open packs still on the lobby card. None means the card reads 0."""
        deadline = self.clock() + REWARD_GRACE_S
        while True:
            reading = self._lobby_reading()
            if reading.status == "OK" and reading.count > 0:
                return self._enter_from_lobby()
            page = self._look()
            if page in PACK_PAGES:
                return page
            reading = self._lobby_reading()
            if page != "blocked" and reading.status == "OK" and reading.count > 0:
                return self._enter_from_lobby()
            if self.clock() >= deadline:
                if reading.status == "OK" and reading.count == 0:
                    return None
                self._log_unread()
                return self._reason("RESULT_TIMEOUT" if self.batches else "PAGE_UNKNOWN")
            self.sleep(0.2)

    def _log_unread(self) -> None:
        if not self.log:
            return
        names = [token.normalized for token in self.tokens if token.normalized][:8]
        self.log("PACK_PAGE_UNREAD", names=names)

    def _covered(self, x: int, y: int, target: str) -> bool:
        """True when another program's window is on top at this point."""
        expected = getattr(self.guard, "expected", None)
        found = self.window_owner((x, y)) if self.window_owner and expected else None
        if found is None:
            return False
        owner, rect = found
        if owner in expected:
            return False
        self.covered_by = owner
        self.log("PACK_CLICK_COVERED", target=target, x=x, y=y, owner=owner, rect=rect)
        return True

    def _click(self, point, target: str, **details) -> bool:
        """Click only where Apex's own window is on top; False when another window is."""
        x, y = int(point[0]), int(point[1])
        if self._covered(x, y, target):
            return False
        self.log("PACK_INPUT", action="click", target=target, x=x, y=y, **details)
        self.sender.click(x, y)
        return True

    def _key(self, scan_code: int, target: str) -> None:
        self.log("PACK_INPUT", action="key", target=target, scanCode=scan_code)
        self.sender.tap_scan_code(scan_code, 80)

    def _click_token(self, predicate, target: str, *, lowest=False, **details) -> bool:
        found = [token for token in self.tokens if token.roi and predicate(token)]
        if not found:
            return False
        if lowest:
            found.sort(key=lambda token: token.roi[1])
            token = found[-1]
        else:
            token = found[0]
        if not self._click(_center(token), target, text=token.text, **details):
            raise _Covered()
        return True

    def _enter_from_lobby(self) -> str:
        """Click the lobby card until a pack page shows; a page name or a reason.

        One lost click must not end the run, so the card is clicked up to three
        times, but only while the lobby card itself is still on screen.
        """
        for attempt in range(1, CARD_ATTEMPTS + 1):
            point = None
            if self.frame is not None and self.blocked is None:
                point = self._lobby_card(strict=attempt > 1)
            if point is None:
                # Already on a pack page, or one still loading after a click.
                page = self._wait(PACK_PAGES, self.limits.page_s)
                return page or self._reason("CARD_NOT_FOUND" if attempt == 1 else "PAGE_UNKNOWN")
            if not self._click(point, "lobby-card", attempt=attempt):
                return "CARD_COVERED"
            page = self._wait(PACK_PAGES, CARD_WAIT_S)
            if page:
                return page
            if self.blocked:
                return self.blocked
        return "CARD_NO_EFFECT"

    def _press(self, point, target: str, **details) -> str:
        """Hold the left button on the open button until the open page goes away.

        The button is press-and-hold: under the pointer it turns from 开启N个
        into 按住打开, and a click alone opens nothing.
        """
        x, y = int(point[0]), int(point[1])
        if self._covered(x, y, target):
            return "CLICK_COVERED"
        self.sender.move_to(x, y)
        self.sleep(0.3)
        hovered = self._look() == "select" and any("按住打开" in token.normalized for token in self.tokens)
        self.log("PACK_INPUT", action="hold", target=target, x=x, y=y, hovered=hovered, **details)
        self.sender.mouse_down()
        try:
            started = self.clock()
            deadline = started + self.limits.hold_s
            while self.clock() < deadline:
                self.sleep(0.2)
                page = self._look()
                if page == "blocked":
                    return self.blocked or "FOREGROUND_LOST"
                left = select_count(self.tokens, self.frame.shape[1], self.frame.shape[0]) if page == "select" else None
                fewer = left is not None and self.count_at_press is not None and left < self.count_at_press
                if (page != "select" or fewer) and self.clock() - started >= self.limits.minimum_hold_s:
                    self.log("PACK_HOLD_RELEASED", heldMs=round((self.clock() - started) * 1000), page=page)
                    return "opened"
            return "HOLD_TIMEOUT"
        finally:
            self.sender.mouse_up()

    def _leave_result(self) -> str | None:
        """Esc off the rewards page and return the page after it.

        Frames right after Esc can still show the rewards page (Runner 1,
        10-19 21:39); read as a new batch, the run then waited for it to
        settle while the spark reward sat unclaimed. Esc is repeated only
        while the rewards page itself is still there, never on the spark
        reward, whose ESC 返回 would leave it unclaimed.
        """
        self._key(ESC_SCAN, "result-back")
        pressed_at, presses = self.clock(), 1
        deadline = self.clock() + self.limits.page_s
        while self.clock() < deadline:
            page = self._look()
            if page in PACK_PAGES and page != "result":
                return page
            # Esc can leave the pack UI entirely. The lobby card is not a pack
            # page to classify(), and the granted pack is still sitting on it.
            if page not in {"result", "blocked"} and self._lobby_reading().status == "OK":
                return "lobby"
            if page == "result" and self.clock() - pressed_at >= 3 and presses < 3:
                self._key(ESC_SCAN, "result-back")
                pressed_at, presses = self.clock(), presses + 1
            self.sleep(0.3)
        return None

    def _lobby_empty(self) -> bool:
        """Back in the lobby with the pack card reading 0, after at least one batch."""
        if not self.batches:
            return False
        reading = self._lobby_reading()
        return reading.status == "OK" and reading.count == 0

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
        if page not in PACK_PAGES:
            page = self._enter_from_lobby()
            if page not in PACK_PAGES:
                return self._failed(page)
        while self.clock() < deadline:
            if page is None:
                # Claiming the spark reward can open another pack. That animation
                # is the same length as a normal batch; the short page timeout
                # expired while it was still playing (both 10-20 runs, 26s).
                timeout = self.limits.animation_s if self.after_reward else self.limits.page_s
                self.after_reward = False
                page = self._wait_known(min(timeout, deadline - self.clock()))
            if page == "lobby":
                page = self._resume_from_lobby()
                if page is None:
                    return self._done()
            if page is None:
                if self._lobby_empty():
                    return self._done()
                self._log_unread()
                return self._failed(self._reason("RESULT_TIMEOUT" if self.batches else "PAGE_UNKNOWN"))
            if page not in PACK_PAGES:
                return self._failed(page)
            if page == "intro":
                if not self._click_token(lambda token: "开启组合包" in token.normalized, "intro-open", lowest=True):
                    return self._failed("INTRO_BUTTON_NOT_FOUND")
                page = None
                continue
            if page == "reward":
                if self.reward_presses >= REWARD_PRESSES:
                    return self._failed("REWARD_STUCK")
                self.reward_presses += 1
                self.sleep(REWARD_SETTLE_S)
                self._key(SPACE_SCAN, "reward-continue")
                self.after_reward = True
                page = None
                continue
            if page == "result":
                red = self._stable_result(min(deadline, self.clock() + self.limits.animation_s))
                if red is None:
                    return self._failed(self._reason("RESULT_TIMEOUT"))
                self.heirloom = self.heirloom or red
                self.batches += 1
                self.maxed, self.presses, self.count_at_press = False, 0, None
                if not self.save_frame(self.frame, self.batches, red):
                    return self._failed("SCREENSHOT_FAILED")
                page = self._leave_result()
                if page is None:
                    if self._lobby_empty():
                        return self._done()
                    return self._failed(self._reason("RESULT_STUCK"))
                continue
            count = select_count(self.tokens, self.frame.shape[1], self.frame.shape[0])
            if self.packs_before is None:
                self.packs_before = count
            if self.count_at_press is not None and count is not None and count < self.count_at_press:
                # Packs went but their rewards were never seen: a red cannot be ruled out.
                return self._failed("RESULT_NOT_SEEN")
            if count == 0:
                page = self._wait({"reward"}, REWARD_GRACE_S)
                if page is None:
                    return self._done()
                continue
            if count is None:
                return self._failed("COUNT_UNREADABLE")
            if self.batches >= self.limits.max_batches:
                return self._failed("BATCH_LIMIT")
            if not self.maxed:
                # Never the 升级N个 button above it, which spends Legend Tokens.
                if not self._click_token(lambda token: token.normalized == "最高", "select-max", count=count):
                    return self._failed("MAX_BUTTON_NOT_FOUND")
                self.maxed = True
                page = None
                continue
            point = open_point(self.tokens)
            if point is None:
                return self._failed("OPEN_BUTTON_NOT_FOUND")
            if self.presses >= PRESS_ATTEMPTS:
                return self._failed("HOLD_NO_EFFECT")
            self.presses += 1
            self.count_at_press = count
            outcome = self._press(point, "open-hold", count=count, attempt=self.presses)
            if outcome != "opened":
                return self._failed(outcome)
            # The opening animation, then the rewards; back on the open page
            # means the hold ended too early and is pressed again.
            page = self._wait({"result", "select", "reward"}, self.limits.animation_s)
            if page is None:
                return self._failed(self._reason("RESULT_TIMEOUT"))
        return self._failed("SESSION_TIMEOUT")


def open_account_packs(source, ocr, sender, guard, **kwargs) -> PackOpeningResult:
    return PackOpener(source, ocr, sender, guard, **kwargs).run()

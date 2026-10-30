"""All in-match input, sent from one thread at a steady rate.

The 10-29 brain released every key at the end of each 0.5–1.2 s leg and then
stood still while the next frame was read and OCR'd, so the player moved in
jerks. Here the decision side only states what it wants — which keys stay
down, how fast the view turns, which taps to send — and this thread keeps
doing that between decisions, including while the pilot spends 300–400 ms
recognising the screen.

Mouse motion is velocity driven. The view never jumps to a new speed: it
accelerates toward the wanted rate within a limit, the way a hand does, and a
planned turn follows a ramp-up, steady, ramp-down speed profile. A small
amount of tremor rides on any motion. Nothing here decides *what* to do.

Safety: every tick checks that Apex is in front and F8 is not held. Anything
unexpected, a pause, or a decision side that stops talking for longer than
``deadman_s`` releases every key and button at once.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math
import random
import threading
import time
from typing import Callable, Iterable, Protocol

from ..safety import EmergencyStop, ForegroundLost


class ControllerSender(Protocol):
    def key_down(self, scan_code: int) -> None: ...
    def key_up(self, scan_code: int) -> None: ...
    def mouse_down(self, button: str = "left") -> None: ...
    def mouse_up(self, button: str = "left") -> None: ...
    def move_mouse(self, dx: int, dy: int) -> None: ...
    def release_all(self) -> None: ...


class ControllerGuard(Protocol):
    def target_is_foreground(self) -> bool: ...


TICK_S = 0.008
# How fast the view may change speed, in mouse counts per second squared.
# About 1,300°/s² at 9 counts per degree: a quick but human wrist.
MAX_ACCEL = 12_000.0
# With no word from the decision side for this long, let go of everything.
DEADMAN_S = 3.0
# Share of a planned turn spent speeding up, and again slowing down.
RAMP_SHARE = 0.22
# Tremor: a slow random drift on top of motion, scaled by the speed.
TREMOR_SHARE = 0.04
TREMOR_FLOOR = 6.0
TREMOR_RETURN = 9.0


@dataclass
class _Turn:
    """A planned turn of ``dx``/``dy`` counts over ``duration`` seconds."""

    dx: float
    dy: float
    started: float
    duration: float
    sent: float = 0.0  # Share of the path already emitted, 0..1.

    def progress(self, now: float) -> float:
        """Share of the path the speed profile has covered by ``now``."""
        t = min(1.0, max(0.0, (now - self.started) / self.duration))
        ramp = RAMP_SHARE
        # Speed rises over the first ramp, holds, and falls over the last one,
        # each ramp a half cosine. Normalised so the area is 1.
        cruise = 1.0 - 2 * ramp
        total = cruise + ramp  # two half-cosine ramps add up to one ramp's area
        if t < ramp:
            covered = (t - ramp / math.pi * math.sin(math.pi * t / ramp)) / 2
        elif t <= 1 - ramp:
            covered = ramp / 2 + (t - ramp)
        else:
            u = t - (1 - ramp)
            covered = ramp / 2 + cruise + (u + ramp / math.pi * math.sin(math.pi * u / ramp)) / 2
        return min(1.0, covered / total)

    def done(self, now: float) -> bool:
        return now - self.started >= self.duration


@dataclass
class _Tap:
    scan_code: int | None
    button: str | None
    hold_s: float
    release_at: float | None = None


class InputController:
    def __init__(
        self,
        sender: ControllerSender,
        guard: ControllerGuard,
        *,
        tick_s: float = TICK_S,
        max_accel: float = MAX_ACCEL,
        deadman_s: float = DEADMAN_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self.sender = sender
        self.guard = guard
        self.tick_s = tick_s
        self.max_accel = max_accel
        self.deadman_s = deadman_s
        self.clock = clock
        self.sleep = sleep
        self.rng = rng or random.Random()
        # Wants are swapped under ``_lock``; everything that sends holds
        # ``_send_lock`` so a pause can never interleave with a tick's key-downs.
        self._lock = threading.Lock()
        self._send_lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # What the decision side wants.
        self._want_keys: frozenset[int] = frozenset()
        self._want_buttons: frozenset[str] = frozenset()
        self._want_rate = (0.0, 0.0)
        self._turn: _Turn | None = None
        self._taps: deque[_Tap] = deque()
        self._heartbeat = clock()
        self._active = False
        # What is actually down right now.
        self._down_keys: set[int] = set()
        self._down_buttons: set[str] = set()
        self._tap: _Tap | None = None
        self._velocity = [0.0, 0.0]
        self._tremor = [0.0, 0.0]
        self._remainder = [0.0, 0.0]
        self._last_tick: float | None = None
        self.fault: str | None = None
        self.sent_counts = [0, 0]
        self.ticks = 0

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    # ------------------------------------------------------------------ decision side

    def resume(self) -> None:
        """Start (or restart after a pause) sending. Clears an old fault."""
        with self._lock:
            self._active = True
            self.fault = None
            self._heartbeat = self.clock()
            self._last_tick = None

    def pause(self, reason: str | None = None) -> None:
        """Stop sending and let go of everything, but keep the thread."""
        with self._lock:
            self._active = False
            self._clear_wants()
            if reason:
                self.fault = reason
        with self._send_lock:
            self._release()

    @property
    def active(self) -> bool:
        with self._lock:
            return self._active

    def set_keys(self, scan_codes: Iterable[int]) -> None:
        with self._lock:
            self._want_keys = frozenset(int(code) for code in scan_codes)
            self._heartbeat = self.clock()

    def set_buttons(self, buttons: Iterable[str]) -> None:
        with self._lock:
            self._want_buttons = frozenset(buttons)
            self._heartbeat = self.clock()

    def set_rate(self, yaw: float, pitch: float = 0.0) -> None:
        """Wanted view speed in mouse counts per second (positive yaw = right)."""
        with self._lock:
            self._want_rate = (float(yaw), float(pitch))
            self._heartbeat = self.clock()

    def turn(self, dx: float, dy: float, duration_s: float) -> None:
        """Turn by ``dx``/``dy`` counts over ``duration_s``. Replaces a turn in progress."""
        with self._lock:
            self._turn = _Turn(float(dx), float(dy), self.clock(), max(0.05, float(duration_s)))
            self._heartbeat = self.clock()

    def tap(self, scan_code: int, hold_s: float) -> None:
        with self._lock:
            self._taps.append(_Tap(int(scan_code), None, max(0.02, float(hold_s))))
            self._heartbeat = self.clock()

    def click(self, button: str, hold_s: float) -> None:
        with self._lock:
            self._taps.append(_Tap(None, button, max(0.02, float(hold_s))))
            self._heartbeat = self.clock()

    def heartbeat(self) -> None:
        with self._lock:
            self._heartbeat = self.clock()

    @property
    def turning(self) -> bool:
        with self._lock:
            return self._turn is not None

    @property
    def busy_tapping(self) -> bool:
        with self._lock:
            return self._tap is not None or bool(self._taps)

    def snapshot(self) -> dict[str, object]:
        with self._lock:
            return {
                "active": self._active,
                "keys": sorted(self._down_keys),
                "buttons": sorted(self._down_buttons),
                "velocity": [round(v, 1) for v in self._velocity],
                "turning": self._turn is not None,
                "fault": self.fault,
            }

    # ------------------------------------------------------------------ thread

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="apex-input", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=1.0)
        self._thread = None
        with self._lock:
            self._active = False
            self._clear_wants()
        with self._send_lock:
            self._release()

    def _run(self) -> None:
        next_at = self.clock()
        while not self._stop.is_set():
            self.tick()
            next_at += self.tick_s
            delay = next_at - self.clock()
            if delay > 0:
                self.sleep(delay)
            else:
                # Fell behind (a long GC, a busy machine): do not try to catch up.
                next_at = self.clock()

    # ------------------------------------------------------------------ one tick

    def tick(self) -> None:
        with self._send_lock:
            self._tick()
        self.ticks += 1

    def _tick(self) -> None:
        now = self.clock()
        with self._lock:
            if not self._active:
                self._last_tick = now
                return
            if now - self._heartbeat > self.deadman_s:
                self._active = False
                self._clear_wants()
                self.fault = "DEADMAN"
                deadman = True
            else:
                deadman = False
                keys, buttons = self._want_keys, self._want_buttons
                rate = self._want_rate
                turn = self._turn
                if self._tap is None and self._taps:
                    self._tap = self._taps.popleft()
                tap = self._tap
        if deadman:
            self._release()
            return
        try:
            if not self.guard.target_is_foreground():
                raise ForegroundLost("Apex 不在前台")
            self._apply_keys(keys)
            self._apply_buttons(buttons)
            self._apply_tap(tap, now, keys, buttons)
            self._apply_motion(rate, turn, now)
        except EmergencyStop:
            # The decision side sees the fault and raises in its own thread.
            self._fail("EMERGENCY_STOP")
            self._stop.set()
        except ForegroundLost:
            self._fail("FOREGROUND_LOST")
        except Exception as error:  # noqa: BLE001 - anything else must also let go
            self._fail(f"INPUT_ERROR:{type(error).__name__}")

    def _apply_keys(self, keys: frozenset[int]) -> None:
        tapping = {self._tap.scan_code} if self._tap is not None and self._tap.scan_code is not None else set()
        for code in sorted(self._down_keys - keys - tapping):
            self.sender.key_up(code)
            self._down_keys.discard(code)
        for code in sorted(keys - self._down_keys):
            self.sender.key_down(code)
            self._down_keys.add(code)

    def _apply_buttons(self, buttons: frozenset[str]) -> None:
        tapping = {self._tap.button} if self._tap is not None and self._tap.button is not None else set()
        for button in sorted(self._down_buttons - buttons - tapping):
            self.sender.mouse_up(button)
            self._down_buttons.discard(button)
        for button in sorted(buttons - self._down_buttons):
            self.sender.mouse_down(button)
            self._down_buttons.add(button)

    def _apply_tap(self, tap: _Tap | None, now: float, keys: frozenset[int], buttons: frozenset[str]) -> None:
        if tap is None:
            return
        if tap.release_at is None:
            # A tap on a key that is already held (jump while climbing) sends nothing.
            if tap.scan_code is not None and tap.scan_code not in self._down_keys:
                self.sender.key_down(tap.scan_code)
            elif tap.button is not None and tap.button not in self._down_buttons:
                self.sender.mouse_down(tap.button)
            tap.release_at = now + tap.hold_s
            return
        if now < tap.release_at:
            return
        if tap.scan_code is not None and tap.scan_code not in keys and tap.scan_code not in self._down_keys:
            self.sender.key_up(tap.scan_code)
        if tap.button is not None and tap.button not in buttons and tap.button not in self._down_buttons:
            self.sender.mouse_up(tap.button)
        with self._lock:
            self._tap = None

    def _apply_motion(self, rate: tuple[float, float], turn: _Turn | None, now: float) -> None:
        dt = 0.0 if self._last_tick is None else min(0.05, now - self._last_tick)
        self._last_tick = now
        step = [0.0, 0.0]
        # Free rotation toward the wanted rate, within the acceleration limit.
        for axis in (0, 1):
            gap = rate[axis] - self._velocity[axis]
            limit = self.max_accel * dt
            self._velocity[axis] += max(-limit, min(limit, gap))
            step[axis] += self._velocity[axis] * dt
        # A planned turn adds its own path on top.
        if turn is not None:
            covered = turn.progress(now)
            share = covered - turn.sent
            turn.sent = covered
            step[0] += turn.dx * share
            step[1] += turn.dy * share
            if turn.done(now):
                with self._lock:
                    if self._turn is turn:
                        self._turn = None
        speed = math.hypot(step[0], step[1]) / dt if dt > 0 else 0.0
        for axis in (0, 1):
            if speed > 1.0:
                # Ornstein-Uhlenbeck drift: wanders, but keeps returning to zero.
                sigma = TREMOR_FLOOR + TREMOR_SHARE * speed
                self._tremor[axis] += -TREMOR_RETURN * self._tremor[axis] * dt + sigma * math.sqrt(dt) * self.rng.gauss(0, 1)
                step[axis] += self._tremor[axis] * dt
            else:
                self._tremor[axis] = 0.0
            self._remainder[axis] += step[axis]
        dx, dy = int(self._remainder[0]), int(self._remainder[1])
        if dx or dy:
            self._remainder[0] -= dx
            self._remainder[1] -= dy
            self.sender.move_mouse(dx, dy)
            self.sent_counts[0] += dx
            self.sent_counts[1] += dy

    # ------------------------------------------------------------------ release

    def _clear_wants(self) -> None:
        self._want_keys = frozenset()
        self._want_buttons = frozenset()
        self._want_rate = (0.0, 0.0)
        self._turn = None
        self._taps.clear()

    def _fail(self, reason: str) -> None:
        with self._lock:
            self._active = False
            self._clear_wants()
            self.fault = reason
        self._release()

    def _release(self) -> None:
        """Let go of everything. Callers hold ``_send_lock``."""
        try:
            self.sender.release_all()
        finally:
            self._down_keys.clear()
            self._down_buttons.clear()
            with self._lock:
                self._tap = None
            self._velocity = [0.0, 0.0]
            self._tremor = [0.0, 0.0]
            self._remainder = [0.0, 0.0]

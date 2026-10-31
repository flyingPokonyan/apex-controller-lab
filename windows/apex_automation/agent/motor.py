"""Game-level verbs on top of the input thread: walk, sprint, turn by degrees.

Skills speak in degrees and named keys; this turns that into scan codes and
mouse counts. Randomness lives only here, in how a thing is done (turn speed,
key hold times, small hesitations), never in what is done.
"""

from __future__ import annotations

from dataclasses import dataclass
import random

from .controller import InputController


@dataclass(frozen=True)
class Keys:
    forward: int = 17
    left: int = 30
    back: int = 31
    right: int = 32
    jump: int = 57
    crouch: int = 29
    sprint: int = 42
    interact: int = 18
    reload: int = 19
    melee: int = 47
    tactical: int = 16
    ultimate: int = 44
    heal: int = 5  # 「4」: use the selected health item
    slot1: int = 2
    slot2: int = 3

    @classmethod
    def from_config(cls, spec: dict[str, object] | None) -> "Keys":
        if not spec:
            return cls()
        known = {name: int(value) for name, value in spec.items() if name in cls.__dataclass_fields__}
        return cls(**known)


# Turning speed for a deliberate turn, degrees per second. People swing
# faster for big turns and slow down for small corrections.
TURN_DPS = (170.0, 300.0)
FINE_DPS = (60.0, 120.0)
# Pitch is clamped by the game near straight down/up.
PITCH_LIMIT = 89.0
# Apex's default mouse sensitivity (5.0) at 0.022° per count: what the view
# turns by until the compass has measured this machine.
DEFAULT_COUNTS_PER_DEGREE = 1 / (0.022 * 5.0)


class Motor:
    def __init__(self, controller: InputController, keys: Keys, rng: random.Random, *, crouch_toggle: bool = False) -> None:
        self.controller = controller
        self.keys = keys
        self.rng = rng
        self.crouch_toggle = crouch_toggle
        self.counts_per_degree: float = DEFAULT_COUNTS_PER_DEGREE
        # True once measured on this machine or given in the config.
        self.calibrated = False
        self.fire_s = 0.0
        self.aim_since: float | None = None
        self.aim_s = 0.0
        # Degrees below (negative) or above the horizon, valid after level().
        self.pitch: float | None = None
        # When the view was last brought to the horizon against the pitch clamp.
        self.levelled_at: float | None = None
        self._crouched = False

    # ------------------------------------------------------------------ movement

    def move(
        self,
        *,
        forward: bool = False,
        back: bool = False,
        left: bool = False,
        right: bool = False,
        sprint: bool = False,
        crouch: bool = False,
        jump: bool = False,
    ) -> None:
        held: list[int] = []
        if forward:
            held.append(self.keys.forward)
        if back:
            held.append(self.keys.back)
        if left:
            held.append(self.keys.left)
        if right:
            held.append(self.keys.right)
        if sprint and forward:
            held.append(self.keys.sprint)
        if jump:
            held.append(self.keys.jump)
        if crouch and not self.crouch_toggle:
            held.append(self.keys.crouch)
        if self.crouch_toggle and crouch != self._crouched:
            self.tap("crouch")
            self._crouched = crouch
        self.controller.set_keys(held)

    def stop(self, now: float | None = None) -> None:
        self.controller.set_keys([])
        self.controller.set_rate(0.0, 0.0)
        self.aim(False, now)
        if self.crouch_toggle and self._crouched:
            self.tap("crouch")
            self._crouched = False

    # ------------------------------------------------------------------ view

    @property
    def can_turn(self) -> bool:
        return bool(self.counts_per_degree)

    def counts(self, degrees: float) -> float:
        return degrees * float(self.counts_per_degree or 0.0)

    def turn(self, yaw: float, pitch: float = 0.0, *, fine: bool = False) -> float:
        """Turn by degrees (positive yaw = right, positive pitch = up). Returns seconds."""
        if not self.can_turn:
            return 0.0
        pitch = self._clamp_pitch(pitch)
        size = max(abs(yaw), abs(pitch))
        low, high = FINE_DPS if fine or size < 15 else TURN_DPS
        duration = max(0.12, size / self.rng.uniform(low, high))
        # Mouse pitch: moving the mouse up (negative dy) looks up.
        self.controller.turn(self.counts(yaw), -self.counts(pitch), duration)
        return duration

    def look(self, yaw_dps: float, pitch_dps: float = 0.0) -> None:
        """Keep turning at a steady rate until told otherwise."""
        if not self.can_turn:
            return
        self.controller.set_rate(self.counts(yaw_dps), -self.counts(pitch_dps))

    def steer(self, error: float, *, gain: float = 2.5, max_dps: float = 160.0) -> None:
        """Turn toward a heading error (degrees) while moving: proportional, capped."""
        rate = max(-max_dps, min(max_dps, error * gain))
        if abs(error) < 3:
            rate = 0.0
        self.look(rate, 0.0)

    def _clamp_pitch(self, pitch: float) -> float:
        if self.pitch is None:
            return pitch
        target = max(-PITCH_LIMIT + 2, min(PITCH_LIMIT - 2, self.pitch + pitch))
        change = target - self.pitch
        self.pitch = target
        return change

    def level(self) -> float:
        """Down to the pitch clamp, then back up to the horizon. Returns seconds."""
        if not self.can_turn:
            return 0.0
        self.pitch = None
        down = self.rng.uniform(0.28, 0.4)
        self.controller.turn(0.0, self.counts(PITCH_LIMIT * 1.15), down)
        # The second half is queued by the caller after ``down`` seconds.
        return down

    def level_up(self) -> float:
        duration = self.rng.uniform(0.3, 0.45)
        self.controller.turn(0.0, -self.counts(PITCH_LIMIT), duration)
        self.pitch = 0.0
        return duration

    def pitch_to(self, target: float) -> float:
        if self.pitch is None:
            return 0.0
        return self.turn(0.0, target - self.pitch, fine=True)

    # ------------------------------------------------------------------ actions

    def tap(self, name: str, hold: tuple[float, float] = (0.05, 0.11)) -> None:
        self.controller.tap(getattr(self.keys, name), self.rng.uniform(*hold))

    def fire(self, hold_s: float) -> None:
        self.controller.click("left", hold_s)
        self.fire_s += hold_s

    def aim(self, on: bool, now: float | None = None) -> None:
        self.controller.set_buttons(["right"] if on else [])
        if now is None:
            return
        if on and self.aim_since is None:
            self.aim_since = now
        elif not on and self.aim_since is not None:
            self.aim_s += now - self.aim_since
            self.aim_since = None

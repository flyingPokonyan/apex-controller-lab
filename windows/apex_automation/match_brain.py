"""Purposeful behavior while alive on the ground: the ``inMatchActive`` profile.

The blind profile in ``match_behavior`` rolls a weighted gesture and holds it
for 1–5 seconds without looking at the screen. Here no single hold lasts more
than 1.2 seconds, and each leg starts from a fresh reading of the HUD. With the
state OCR every loop pays (≈0.35 s) a hit is answered 1–2 seconds after it
lands: a slow player, not an instant one. Priority order:

1. answer a hit for a few seconds: turn toward the red damage arrow, or sweep
   a circle in steps when there is none; short bursts with a weapon, a run and
   punches without one; a hit confirmation stops the turning. Only the own
   bars count as a hit. Red decals in the world can look like an arrow, so an
   arrow alone never starts a reaction; it only gives the direction of one.
   Low on health and not landing hits, it breaks off and runs instead;
   no arrow: turn 90–180° one way, then back past the start the other way;
2. a movement leg that left the scene unchanged twice running means the
   player is against a wall: turn away and step back;
3. level the view after landing and every minute or so;
4. press E while a pickup prompt shows;
5. otherwise search: sprint, walk, crouch-walk, slide and look around a little
   below the horizon, so ground loot can raise its pickup prompt.

Nothing here finds other players on screen or aims at them. The only feedback
is what the game shows the player about themself: the damage arc, their own
bars, the hit marker.

Crouch can be bound as hold (Apex's default for LCTRL) or toggle. With
``crouchMode: "toggle"`` every crouch is a tap in and a tap out, so a leg never
leaves the player stuck crouching for the rest of the match.

Shift (sprint) and Ctrl (crouch) are ground-only. On the dropship LCTRL leaves
the squad, so neither is sent until the player has been alive on the ground for
GROUND_KEYS_AFTER_MS, which a misread dropship frame never is.

Mouse amounts depend on the machine's sensitivity setting. When the config
does not give counts per degree, the brain measures it: a plain turn of known
counts while searching, with the compass heading read before and after,
three times, median. The pitch reset is then derived on the assumption that
Apex scales pitch like yaw and clamps it near 89° (verify on the first watched
match). Until then the brain neither turns by angle nor levels the view.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math
import random
from typing import Protocol

import numpy as np

from .match_behavior import REQUIRED_SCAN_CODES


ACTIVE_SCAN_CODES = (*REQUIRED_SCAN_CODES, "sprint", "crouch")
OPTIONAL_SCAN_CODES = ("ultimate",)
# ESC, TAB and ENTER open menus. They never belong in a ground leg.
MENU_SCAN_CODES = frozenset({1, 15, 28})
GROUND_KEYS_AFTER_MS = 10_000

# One held chord, in milliseconds. Short so the next frame can interrupt it.
LEG_HOLD_MS = (500, 1200)
REACT_MS = (3000, 8000)
# Below this health, with no hit landing, break off: turn away and sprint.
LOW_HEALTH = 0.4
RETREAT_MS = (1000, 1600)
# Mean absolute change of a 64x36 grey thumbnail between two reads. Standing
# still measured 0.3–2.5, moving 5–60 (2026-10-29 recording).
STUCK_CHANGE = 3.0
STUCK_STRIKES = 2
MOVEMENT_LEGS = frozenset({"sprint", "walk", "crouch", "slide", "sprintJump"})
# The own bars shrinking by this share of their length count as a hit.
VITALITY_DROP = 0.03
SHIELD_DROP = 0.05
# A damage arrow seen this recently still gives the direction of a hit.
ARROW_MEMORY_MS = 1000
# A damage arc lingers after the turn it caused. Ignore it this long.
TURN_SETTLE_MS = 600
LEVEL_FIRST_AFTER_MS = 1500
LEVEL_EVERY_MS = (45_000, 90_000)
MAX_MOUSE_DX = 1200
MAX_MOUSE_DY = 900

# Self-calibration: a turn this many counts, re-sized until it moves the
# compass by a measurable amount, sampled this many times.
CALIBRATION_COUNTS = 600
CALIBRATION_COUNTS_RANGE = (150, 4800)
CALIBRATION_DEGREES = (8, 150)
CALIBRATION_SAMPLES = 3
# Pitch reset derived from a measured yaw scale.
PITCH_LIMIT_DEGREES = 89
PITCH_FLOOR_MARGIN = 1.15

# Weights are out of 100 before the per-life style scales them.
LEGS: tuple[tuple[str, int], ...] = (
    ("sprint", 30),
    ("walk", 14),
    ("crouch", 10),
    ("slide", 6),
    ("sprintJump", 5),
    ("scan", 25),
    ("pause", 6),
    ("ability", 4),
)
STYLES = ("runner", "careful", "mixed")
STYLE_SCALE: dict[str, dict[str, float]] = {
    "runner": {"sprint": 1.4, "slide": 1.5, "sprintJump": 1.4, "crouch": 0.6, "scan": 0.8},
    "careful": {"sprint": 0.7, "walk": 1.4, "crouch": 1.6, "scan": 1.3, "slide": 0.6},
    "mixed": {},
}
# Legs that need Shift or Ctrl become a walk until the ground gate opens.
GROUND_LEGS = frozenset({"sprint", "crouch", "slide", "sprintJump"})
# The same leg at most this many times in a row.
MAX_REPEAT = 3

Step = dict[str, object]


@dataclass(frozen=True)
class HudReading:
    """What the HUD shows about the player. ``None`` means it was not read."""

    has_weapon: bool | None = None
    # Health, as a share of the full bar.
    vitality: float | None = None
    # Lit shield cells, as a share of two full evo cells (more cells read above 1).
    shield: float | None = None
    # Where the damage arrow points: 0 straight ahead, positive to the right.
    hit_bearing: float | None = None
    # The own bar shows the red trail a fresh hit leaves.
    hurt: bool = False
    pickup_prompt: bool = False
    hit_confirmed: bool = False
    # Compass heading in degrees, when read.
    heading: int | None = None
    # How much the middle of the view changed since the previous read.
    scene_change: float | None = None


class HudReader(Protocol):
    def read(self, frame: np.ndarray) -> HudReading: ...


class BlindHud:
    """Reads nothing. Stands in until the HUD regions are calibrated."""

    def read(self, frame: np.ndarray) -> HudReading:
        return HudReading()


@dataclass(frozen=True)
class MouseCalibration:
    counts_per_degree: float | None = None
    # Mouse-down counts that surely reach the pitch clamp from any view.
    pitch_floor_counts: int | None = None
    # Counts back up from the clamp to the horizon.
    level_from_floor_counts: int | None = None
    # How far below the horizon the search looks once the view is level.
    search_pitch_degrees: float = 12.0

    @property
    def can_turn(self) -> bool:
        return self.counts_per_degree is not None

    @property
    def can_level(self) -> bool:
        return self.pitch_floor_counts is not None and self.level_from_floor_counts is not None

    @classmethod
    def from_spec(cls, spec: object) -> "MouseCalibration":
        if spec is None:
            return cls()
        if not isinstance(spec, dict):
            raise ValueError("鼠标标定必须是对象")

        def number(name: str, maximum: float) -> float | None:
            value = spec.get(name)
            if value is None:
                return None
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= maximum:
                raise ValueError(f"鼠标标定 {name} 无效：{value!r}")
            return float(value)

        per_degree = number("countsPerDegree", 200)
        floor = number("pitchFloorCounts", 20_000)
        level = number("levelFromFloorCounts", 20_000)
        search = number("searchPitchDegrees", 45)
        return cls(
            counts_per_degree=per_degree,
            pitch_floor_counts=None if floor is None else int(floor),
            level_from_floor_counts=None if level is None else int(level),
            search_pitch_degrees=12.0 if search is None else search,
        )


def _hold(scan_codes: list[int], duration_ms: int) -> Step:
    return {"type": "holdKeys", "scanCodes": scan_codes, "durationMs": duration_ms}


def _tap(scan_code: int, picker: random.Random) -> Step:
    return {"type": "tapKey", "scanCode": scan_code, "durationMs": picker.randint(50, 90)}


def _wait(picker: random.Random, low: int, high: int) -> Step:
    return {"type": "wait", "durationMs": picker.randint(low, high)}


def _moves(dx: int, dy: int, picker: random.Random, pieces: int = 1) -> list[Step]:
    """One relative move split into pieces the sequence validator accepts."""
    count = max(1, pieces, math.ceil(abs(dx) / MAX_MOUSE_DX), math.ceil(abs(dy) / MAX_MOUSE_DY))
    steps: list[Step] = []
    sent_x = sent_y = 0
    for index in range(1, count + 1):
        x = round(dx * index / count) - sent_x
        y = round(dy * index / count) - sent_y
        sent_x += x
        sent_y += y
        steps.append({"type": "moveMouse", "dx": x, "dy": y})
        if index < count:
            steps.append(_wait(picker, 15, 45))
    return steps


def _ease(t: float) -> float:
    """Minimum-jerk position: slow start, fast middle, slow stop."""
    return t * t * t * (10 - 15 * t + 6 * t * t)


def _path(dx: int, dy: int, ticks: int, picker: random.Random, wobble: float = 0.0) -> list[tuple[int, int]]:
    """Per-tick increments that sum exactly to (dx, dy) along an eased curve.

    ``wobble`` bends the path sideways by that share of its length, as a hand
    does; the bend returns to zero at both ends.
    """
    ticks = max(1, ticks)
    bend = wobble * picker.uniform(-1, 1) * (abs(dx) + abs(dy))
    length = math.hypot(dx, dy) or 1.0
    # Perpendicular unit vector for the bend.
    px, py = -dy / length, dx / length
    points = []
    for index in range(ticks + 1):
        t = index / ticks
        along = _ease(t)
        side = bend * math.sin(math.pi * t)
        points.append((dx * along + px * side, dy * along + py * side))
    out: list[tuple[int, int]] = []
    sent_x = sent_y = 0
    for x, y in points[1:]:
        step_x, step_y = round(x) - sent_x, round(y) - sent_y
        sent_x += step_x
        sent_y += step_y
        out.append((step_x, step_y))
    # Rounding of the bend can leave a remainder; the end point is exact.
    out[-1] = (out[-1][0] + dx - sent_x, out[-1][1] + dy - sent_y)
    return out


def _smooth(dx: int, dy: int, picker: random.Random, duration_ms: int) -> list[Step]:
    """A continuous mouse motion: many small moves about 12 ms apart."""
    ticks = max(4, min(60, duration_ms // 12))
    steps: list[Step] = []
    for index, (step_x, step_y) in enumerate(_path(dx, dy, ticks, picker, wobble=0.06)):
        if step_x or step_y:
            steps.append({"type": "moveMouse", "dx": step_x, "dy": step_y})
        if index + 1 < ticks:
            steps.append({"type": "wait", "durationMs": picker.randint(9, 15)})
    return steps


def _signed(degrees: float) -> float:
    """The same direction as the shortest turn, in (-180, 180]."""
    value = math.fmod(degrees, 360.0)
    if value > 180:
        value -= 360
    elif value <= -180:
        value += 360
    return value


@dataclass
class MatchBrain:
    scan_codes: dict[str, int]
    mouse: MouseCalibration = field(default_factory=MouseCalibration)
    crouch_toggle: bool = False
    style: str = "mixed"
    counters: Counter = field(default_factory=Counter)
    _rng: random.Random = field(default_factory=random.Random)

    def __post_init__(self) -> None:
        # A measurement survives deaths and rounds: the sensitivity is per machine.
        self._calibration_counts = CALIBRATION_COUNTS
        self._calibration_pending: tuple[int, int] | None = None
        self._calibration_samples: list[float] = []
        self._reset_life()

    def _reset_life(self) -> None:
        self._last_vitality: float | None = None
        self._last_shield: float | None = None
        self._arrow: tuple[float, int] | None = None
        self._react_until_ms: int | None = None
        self._react_fresh = False
        self._turn_plan: list[float] = []
        self._last_turn_ms: int | None = None
        self._next_level_ms: int | None = None
        self._last_alive_ms = 0
        self._last_leg: str | None = None
        self._repeat = 0
        # Counts below the search pitch. Only meaningful once leveled.
        self._pitch = 0
        self._retreated = False
        self._moved_last = False
        self._stuck_strikes = 0

    def begin_life(self, rng: random.Random | None = None) -> None:
        """New round: new style, new counters. Not called on landing flaps."""
        picker = rng or self._rng
        self.style = picker.choice(STYLES)
        self.counters = Counter()
        self._reset_life()

    def summary(self) -> dict[str, object]:
        summary: dict[str, object] = {"style": self.style, **dict(sorted(self.counters.items()))}
        if self.mouse.counts_per_degree is not None:
            summary["countsPerDegree"] = round(self.mouse.counts_per_degree, 3)
        return summary

    # ------------------------------------------------------------------ calibration

    def _calibration_step(self, reading: HudReading, picker: random.Random) -> list[Step] | None:
        """Measure counts per degree from the compass. None when not calibrating now."""
        if self.mouse.can_turn:
            return None
        if self._calibration_pending is not None:
            before, counts = self._calibration_pending
            self._calibration_pending = None
            if reading.heading is not None:
                self._note_calibration(before, reading.heading, counts)
            if self.mouse.can_turn:
                return None
        if reading.heading is None:
            return None
        counts = self._calibration_counts * picker.choice((-1, 1))
        self._calibration_pending = (reading.heading, counts)
        self.counters["calibrationTurns"] += 1
        return [*_smooth(counts, 0, picker, picker.randint(220, 380)), _wait(picker, 150, 250)]

    def _note_calibration(self, before: int, after: int, counts: int) -> None:
        turned = abs(_signed(after - before))
        if turned < CALIBRATION_DEGREES[0]:
            self._calibration_counts = min(CALIBRATION_COUNTS_RANGE[1], self._calibration_counts * 2)
            return
        if turned > CALIBRATION_DEGREES[1]:
            self._calibration_counts = max(CALIBRATION_COUNTS_RANGE[0], self._calibration_counts // 2)
            return
        self._calibration_samples.append(abs(counts) / turned)
        if len(self._calibration_samples) < CALIBRATION_SAMPLES:
            return
        per_degree = sorted(self._calibration_samples)[len(self._calibration_samples) // 2]
        self.mouse = MouseCalibration(
            counts_per_degree=per_degree,
            pitch_floor_counts=self.mouse.pitch_floor_counts
            or round(PITCH_LIMIT_DEGREES * PITCH_FLOOR_MARGIN * per_degree),
            level_from_floor_counts=self.mouse.level_from_floor_counts
            or round(PITCH_LIMIT_DEGREES * per_degree),
            search_pitch_degrees=self.mouse.search_pitch_degrees,
        )

    def step(
        self,
        reading: HudReading,
        *,
        alive_ms: int,
        now_ms: int,
        rng: random.Random | None = None,
    ) -> tuple[str, list[Step]]:
        picker = rng or self._rng
        if alive_ms < self._last_alive_ms:
            # Back on the ground after a respawn or a misread: the view is unknown again.
            self._next_level_ms = None
        self._last_alive_ms = alive_ms
        ground = alive_ms >= GROUND_KEYS_AFTER_MS

        self._count_weapon(reading, alive_ms)
        if reading.hit_bearing is not None:
            self._arrow = (reading.hit_bearing, now_ms)
        moved_last, self._moved_last = self._moved_last, False
        if self._was_hit(reading):
            self.counters["hitReads"] += 1
            self._start_reaction(self._recent_bearing(now_ms), now_ms, picker)
        if self._react_until_ms is not None and now_ms >= self._react_until_ms:
            self._react_until_ms = None
            self._turn_plan = []
        if self._react_until_ms is not None:
            # A reaction turns the view; a measurement in flight would be wrong.
            self._calibration_pending = None
            return "react", self._react(reading, now_ms, picker, ground)
        if self._stuck(reading, moved_last):
            self._calibration_pending = None
            return "unstick", self._unstick(picker)
        calibration = self._calibration_step(reading, picker)
        if calibration is not None:
            return "calibrate", calibration
        if self._level_due(alive_ms, picker):
            return "level", self._level(picker)
        if reading.pickup_prompt:
            self.counters["pickupPresses"] += 1
            return "pickup", [_tap(self.scan_codes["interact"], picker)]
        name = self._choose_leg(picker)
        if name in GROUND_LEGS and not ground:
            name = "walk"
        self.counters[f"leg:{name}"] += 1
        self._moved_last = name in MOVEMENT_LEGS
        return f"search:{name}", _LEG_BUILDERS[name](self, picker)

    # ------------------------------------------------------------------ bookkeeping

    def _count_weapon(self, reading: HudReading, alive_ms: int) -> None:
        """How often the HUD showed a weapon: the go/no-go number for looting."""
        if reading.has_weapon is None:
            return
        self.counters["hudReads"] += 1
        if reading.has_weapon:
            self.counters["weaponReads"] += 1
            if "firstWeaponAliveMs" not in self.counters:
                self.counters["firstWeaponAliveMs"] = alive_ms
                self.counters["weaponSeen"] = 1

    def _stuck(self, reading: HudReading, moved_last: bool) -> bool:
        if not moved_last or reading.scene_change is None:
            return False
        self._stuck_strikes = self._stuck_strikes + 1 if reading.scene_change < STUCK_CHANGE else 0
        if self._stuck_strikes < STUCK_STRIKES:
            return False
        self._stuck_strikes = 0
        return True

    def _unstick(self, picker: random.Random) -> list[Step]:
        self.counters["unsticks"] += 1
        sign = picker.choice((-1, 1))
        if self.mouse.can_turn:
            steps = self._turn(sign * picker.uniform(90, 180), picker)
        else:
            steps = _smooth(sign * picker.randint(400, 900), 0, picker, picker.randint(250, 400))
        steps.append(_hold([self.scan_codes["back"]], picker.randint(400, 700)))
        return steps

    # ------------------------------------------------------------------ hits

    def _was_hit(self, reading: HudReading) -> bool:
        hit = reading.hurt
        if reading.vitality is not None:
            if self._last_vitality is not None and self._last_vitality - reading.vitality >= VITALITY_DROP:
                hit = True
            self._last_vitality = reading.vitality
        if reading.shield is not None:
            if self._last_shield is not None and self._last_shield - reading.shield >= SHIELD_DROP:
                hit = True
            self._last_shield = reading.shield
        return hit

    def _recent_bearing(self, now_ms: int) -> float | None:
        if self._arrow is None or now_ms - self._arrow[1] > ARROW_MEMORY_MS:
            return None
        return self._arrow[0]

    def _start_reaction(self, bearing: float | None, now_ms: int, picker: random.Random) -> None:
        if self._react_until_ms is None:
            self.counters["reactions"] += 1
            self._react_fresh = True
            self._turn_plan = []
            self._retreated = False
            if bearing is None and self.mouse.can_turn:
                # Look one way, then back past the start the other way.
                sign = picker.choice((-1, 1))
                first = picker.uniform(90, 180)
                self._turn_plan = [sign * first, -sign * (first + picker.uniform(60, 120))]
        settled = self._last_turn_ms is None or now_ms - self._last_turn_ms >= TURN_SETTLE_MS
        if bearing is not None and self.mouse.can_turn and settled:
            self._turn_plan = [_signed(bearing)]
            # Used up: the next hit needs a fresh arrow.
            self._arrow = None
        self._react_until_ms = now_ms + picker.randint(*REACT_MS)

    def _react(self, reading: HudReading, now_ms: int, picker: random.Random, ground: bool) -> list[Step]:
        steps: list[Step] = []
        if self._react_fresh:
            steps.append(_wait(picker, 200, 500))
            self._react_fresh = False
        if reading.hit_confirmed:
            # The game says this direction lands. Stay on it.
            self.counters["hitConfirmed"] += 1
            self._turn_plan = []
        elif (
            not self._retreated
            and reading.vitality is not None
            and reading.vitality < LOW_HEALTH
        ):
            return steps + self._retreat(now_ms, picker, ground)
        if self._turn_plan:
            steps.extend(self._turn(self._turn_plan.pop(0), picker))
            self._last_turn_ms = now_ms
            self.counters["turns"] += 1
        # Fire whenever hit: with a weapon it shoots, with fists it punches.
        steps.extend(self._burst(picker))
        if reading.has_weapon is False and picker.random() < 0.5:
            steps.extend(self._close_in_and_punch(picker, ground))
        return steps

    def _retreat(self, now_ms: int, picker: random.Random, ground: bool) -> list[Step]:
        """Break off: face away from the fight and run, then the reaction ends."""
        self._retreated = True
        self._turn_plan = []
        self.counters["retreats"] += 1
        duration = picker.randint(*RETREAT_MS)
        if self.mouse.can_turn:
            steps = self._turn(picker.choice((-1, 1)) * picker.uniform(150, 210), picker)
            self._last_turn_ms = now_ms
            keys = [self.scan_codes["forward"]] + ([self.scan_codes["sprint"]] if ground else [])
            if ground:
                self.counters["sprintMs"] += duration
        else:
            # Apex does not sprint backwards: back off diagonally instead.
            steps = []
            keys = [self.scan_codes["back"], self.scan_codes["left"] if picker.random() < 0.5 else self.scan_codes["right"]]
        steps.append(_hold(keys, duration))
        self._react_until_ms = now_ms
        return steps

    def _turn(self, degrees: float, picker: random.Random) -> list[Step]:
        """One eased swing a little past the target, then a short settle back."""
        per_degree = float(self.mouse.counts_per_degree or 0)
        net = round(degrees * per_degree)
        main = round(degrees * (1 + picker.uniform(0.04, 0.12)) * per_degree)
        swing_ms = int(min(480, 150 + abs(degrees) * 1.4) * picker.uniform(0.85, 1.15))
        steps = _smooth(main, picker.randint(-12, 12), picker, swing_ms)
        steps.append(_wait(picker, 40, 110))
        steps.extend(_smooth(net - main, 0, picker, picker.randint(60, 120)))
        return steps

    def _burst(self, picker: random.Random) -> list[Step]:
        steps: list[Step] = []
        bursts = picker.randint(2, 4)
        for index in range(bursts):
            duration = picker.randint(150, 400)
            steps.append({"type": "holdMouse", "button": "left", "durationMs": duration})
            self.counters["bursts"] += 1
            self.counters["burstMs"] += duration
            if index + 1 < bursts:
                steps.extend(_smooth(self._small_yaw(picker, 2, 6), picker.randint(-6, 6), picker, picker.randint(60, 140)))
                steps.append(_wait(picker, 50, 140))
        return steps

    def _close_in_and_punch(self, picker: random.Random, ground: bool) -> list[Step]:
        keys = [self.scan_codes["forward"]]
        duration = picker.randint(500, 1000)
        if ground:
            keys.append(self.scan_codes["sprint"])
            self.counters["sprintMs"] += duration
        steps: list[Step] = [_hold(keys, duration)]
        punches = picker.randint(1, 3)
        for index in range(punches):
            steps.append(_tap(self.scan_codes["melee"], picker))
            if index + 1 < punches:
                steps.append(_wait(picker, 180, 360))
        self.counters["punches"] += punches
        return steps

    # ------------------------------------------------------------------ view

    def _level_due(self, alive_ms: int, picker: random.Random) -> bool:
        if not self.mouse.can_level:
            return False
        if self._next_level_ms is None:
            self._next_level_ms = LEVEL_FIRST_AFTER_MS
        if alive_ms < self._next_level_ms:
            return False
        self._next_level_ms = alive_ms + picker.randint(*LEVEL_EVERY_MS)
        return True

    def _level(self, picker: random.Random) -> list[Step]:
        """Down to the pitch clamp, then up to just below the horizon."""
        floor = int(self.mouse.pitch_floor_counts or 0)
        up = int(self.mouse.level_from_floor_counts or 0)
        below = round(self.mouse.search_pitch_degrees * float(self.mouse.counts_per_degree or 0))
        steps = _smooth(0, floor, picker, picker.randint(280, 420))
        steps.append(_wait(picker, 80, 160))
        steps.extend(_smooth(0, below - up, picker, picker.randint(320, 480)))
        self._pitch = 0
        self.counters["levelViews"] += 1
        return steps

    def _small_yaw(self, picker: random.Random, low_degrees: float, high_degrees: float) -> int:
        sign = picker.choice((-1, 1))
        if self.mouse.can_turn:
            return round(sign * picker.uniform(low_degrees, high_degrees) * float(self.mouse.counts_per_degree or 0))
        return sign * picker.randint(round(low_degrees * 6), round(high_degrees * 6))

    def _pitch_step(self, picker: random.Random) -> int:
        """Small vertical drift that stays near the search pitch."""
        limit = round(8 * float(self.mouse.counts_per_degree)) if self.mouse.can_turn else 60
        step = max(1, limit // 4)
        dy = picker.randint(-step, step)
        if abs(self._pitch + dy) > limit:
            dy = -dy
        self._pitch += dy
        return dy

    # ------------------------------------------------------------------ search

    def _choose_leg(self, picker: random.Random) -> str:
        scale = STYLE_SCALE.get(self.style, {})
        names: list[str] = []
        weights: list[float] = []
        for name, weight in LEGS:
            adjusted = weight * scale.get(name, 1.0)
            if name == self._last_leg and self._repeat >= MAX_REPEAT:
                adjusted = 0
            names.append(name)
            weights.append(adjusted)
        name = picker.choices(names, weights=weights)[0]
        self._repeat = self._repeat + 1 if name == self._last_leg else 1
        self._last_leg = name
        return name

    def _diagonal(self, keys: list[int], picker: random.Random, share: float) -> list[int]:
        if picker.random() < share:
            keys.append(self.scan_codes["left"] if picker.random() < 0.5 else self.scan_codes["right"])
        return keys

    def _drift(self, picker: random.Random, duration_ms: int, max_degrees: float) -> list[list[int]]:
        """The view while moving: an eased curve of up to ``max_degrees``, or near still."""
        degrees = picker.uniform(-max_degrees, max_degrees) if picker.random() < 0.7 else picker.uniform(-3, 3)
        yaw = round(degrees * float(self.mouse.counts_per_degree)) if self.mouse.can_turn else round(degrees * 6)
        pitch = self._pitch_step(picker)
        ticks = max(4, min(120, duration_ms // 16))
        return [[dx, dy] for dx, dy in _path(yaw, pitch, ticks, picker, wobble=0.04)]

    def _moving(self, keys: list[int], duration_ms: int, picker: random.Random, max_degrees: float) -> Step:
        step = _hold(keys, duration_ms)
        step["look"] = self._drift(picker, duration_ms, max_degrees)
        return step

    def _sprint(self, picker: random.Random) -> list[Step]:
        keys = self._diagonal([self.scan_codes["forward"], self.scan_codes["sprint"]], picker, 0.3)
        duration = picker.randint(*LEG_HOLD_MS)
        self.counters["sprintMs"] += duration
        return [self._moving(keys, duration, picker, 35)]

    def _walk(self, picker: random.Random) -> list[Step]:
        keys = self._diagonal([self.scan_codes["forward"]], picker, 0.35)
        return [self._moving(keys, picker.randint(*LEG_HOLD_MS), picker, 45)]

    def _crouch(self, picker: random.Random) -> list[Step]:
        duration = picker.randint(600, 1100)
        self.counters["crouchMs"] += duration
        forward, crouch = self.scan_codes["forward"], self.scan_codes["crouch"]
        if self.crouch_toggle:
            # Tap in, walk, tap out: never left crouching.
            return [_tap(crouch, picker), self._moving([forward], duration, picker, 40), _tap(crouch, picker)]
        return [self._moving([forward, crouch], duration, picker, 40)]

    def _slide(self, picker: random.Random) -> list[Step]:
        run = [self.scan_codes["forward"], self.scan_codes["sprint"]]
        sprint_ms = picker.randint(400, 800)
        self.counters["sprintMs"] += sprint_ms
        self.counters["slides"] += 1
        if self.crouch_toggle:
            return [
                _hold(run, sprint_ms),
                _hold([*run, self.scan_codes["crouch"]], picker.randint(60, 100)),
                _hold([self.scan_codes["forward"]], picker.randint(300, 500)),
                _tap(self.scan_codes["crouch"], picker),
            ]
        return [
            _hold(run, sprint_ms),
            _hold([*run, self.scan_codes["crouch"]], picker.randint(300, 500)),
        ]

    def _sprint_jump(self, picker: random.Random) -> list[Step]:
        run = [self.scan_codes["forward"], self.scan_codes["sprint"]]
        before = picker.randint(350, 600)
        after = picker.randint(250, 500)
        self.counters["sprintMs"] += before + after
        self.counters["jumps"] += 1
        return [
            _hold(run, before),
            _hold([*run, self.scan_codes["jump"]], picker.randint(100, 160)),
            _hold(run, after),
        ]

    def _scan(self, picker: random.Random) -> list[Step]:
        """Look around below the horizon, then maybe try E on whatever is there."""
        steps: list[Step] = []
        sign = picker.choice((-1, 1))
        sweeps = picker.randint(1, 2)
        for index in range(sweeps):
            yaw = abs(self._small_yaw(picker, 20, 70))
            steps.extend(_smooth(sign * yaw, self._pitch_step(picker), picker, picker.randint(280, 650)))
            if index + 1 < sweeps:
                steps.append(_wait(picker, 150, 450))
                if picker.random() < 0.4:
                    sign = -sign
        if picker.random() < 0.4:
            steps.append(_wait(picker, 80, 160))
            steps.append(_tap(self.scan_codes["interact"], picker))
            self.counters["blindInteract"] += 1
        return steps

    def _pause(self, picker: random.Random) -> list[Step]:
        if picker.random() < 0.15:
            return [_wait(picker, 1000, 2000)]
        return [_wait(picker, 200, 600)]

    def _ability(self, picker: random.Random) -> list[Step]:
        key = self.scan_codes["tactical"]
        if "ultimate" in self.scan_codes and picker.random() < 0.3:
            key = self.scan_codes["ultimate"]
        self.counters["abilities"] += 1
        return [_tap(key, picker)]


_LEG_BUILDERS = {
    "sprint": MatchBrain._sprint,
    "walk": MatchBrain._walk,
    "crouch": MatchBrain._crouch,
    "slide": MatchBrain._slide,
    "sprintJump": MatchBrain._sprint_jump,
    "scan": MatchBrain._scan,
    "pause": MatchBrain._pause,
    "ability": MatchBrain._ability,
}

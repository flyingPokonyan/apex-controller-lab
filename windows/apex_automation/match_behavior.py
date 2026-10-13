"""Blind behavior while the player is alive on the ground.

Movement holds last a random 1–5 seconds. There is still no sprint, and the
same gesture never runs twice in a row. Looking, pausing, and the other taps
sit between those holds.

Each life rolls a mood so two matches do not share one mixture.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
from typing import Callable


REQUIRED_SCAN_CODES = (
    "forward",
    "left",
    "back",
    "right",
    "jump",
    "melee",
    "tactical",
    "interact",
)
# ESC, TAB, ENTER, and LCTRL open menus or, on the dropship, leave the squad.
# Shift is sprint. None of those belong in a ground gesture.
FORBIDDEN_SCAN_CODES = frozenset({1, 15, 28, 29, 42})

# One movement hold, in milliseconds. Picked again every step.
HOLD_MS = (1000, 5000)

# Weights are out of 100 before the per-life mood scales them.
GESTURES: tuple[tuple[str, int], ...] = (
    ("look", 22),
    ("idle", 14),
    ("forward", 24),
    ("strafe", 18),
    ("back", 6),
    ("jump", 6),
    ("interact", 5),
    ("melee", 3),
    ("tactical", 2),
)

MOODS = ("still", "fidget", "wander")
# Scales applied to the base weights, then renormalized by the sampler.
MOOD_SCALE: dict[str, dict[str, float]] = {
    "still": {"forward": 0.65, "strafe": 0.75, "look": 1.25, "idle": 1.45},
    "fidget": {"forward": 0.85, "strafe": 1.45, "jump": 1.6, "look": 1.1},
    "wander": {"forward": 1.25, "strafe": 1.1, "back": 1.3, "idle": 0.7},
}


@dataclass
class MatchBehavior:
    """Pitch, turn direction, and the mood of the current life."""

    pitch: int = 0
    yaw_sign: int = 1
    mood: str = "fidget"
    last_gesture: str | None = None
    _rng: random.Random = field(default_factory=random.Random)

    def begin_life(self, rng: random.Random | None = None) -> None:
        """Roll a new mixture. Called once when a round starts, not on landing flaps."""
        picker = rng or self._rng
        self.mood = picker.choice(MOODS)
        self.last_gesture = None
        self.pitch = 0
        self.yaw_sign = picker.choice((-1, 1))

    def sample(
        self,
        scan_codes: dict[str, int],
        rng: random.Random | None = None,
    ) -> tuple[str, list[dict[str, object]]]:
        picker = rng or self._rng
        if self.mood not in MOODS:
            self.begin_life(picker)
        if picker.random() < 0.35:
            self.yaw_sign = -self.yaw_sign
        name = self._choose(picker)
        self.last_gesture = name
        steps = _GESTURE_BUILDERS[name](self, scan_codes, picker)
        return name, steps

    def _choose(self, picker: random.Random) -> str:
        scale = MOOD_SCALE.get(self.mood, {})
        names: list[str] = []
        weights: list[float] = []
        for name, weight in GESTURES:
            adjusted = weight * scale.get(name, 1.0)
            if name == self.last_gesture:
                adjusted = 0
            names.append(name)
            weights.append(adjusted)
        if sum(weights) <= 0:
            weights = [1 for _ in names]
        return picker.choices(names, weights=weights)[0]


def _hold(scan_codes: list[int], duration_ms: int) -> dict[str, object]:
    return {"type": "holdKeys", "scanCodes": scan_codes, "durationMs": duration_ms}


def _tap(scan_code: int, picker: random.Random, low: int = 50, high: int = 90) -> dict[str, object]:
    return {
        "type": "tapKey",
        "scanCode": scan_code,
        "durationMs": picker.randint(low, high),
    }


def _wait(picker: random.Random, low: int, high: int) -> dict[str, object]:
    return {"type": "wait", "durationMs": picker.randint(low, high)}


def _look(
    state: MatchBehavior,
    picker: random.Random,
    *,
    steps: int,
    yaw: tuple[int, int],
) -> list[dict[str, object]]:
    """Small relative moves. Positive dy looks down, and pitch returns to the horizon."""
    events: list[dict[str, object]] = []
    for index in range(steps):
        dx = state.yaw_sign * picker.randint(*yaw)
        if state.pitch > 50:
            dy = -picker.randint(8, 24)
        elif state.pitch < -50:
            dy = picker.randint(8, 24)
        else:
            dy = picker.randint(-12, 14)
        state.pitch += dy
        events.append({"type": "moveMouse", "dx": dx, "dy": dy})
        if index + 1 < steps:
            events.append(_wait(picker, 40, 110))
    return events


def _look_only(
    state: MatchBehavior,
    _scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    return _look(state, picker, steps=picker.randint(1, 3), yaw=(18, 64))


def _idle(
    _state: MatchBehavior,
    _scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    # Most pauses are short. A few last long enough to break a steady rhythm.
    # Nothing is held down, so a death during the pause does not keep walking.
    if picker.random() < 0.28:
        return [_wait(picker, 1600, 4000)]
    return [_wait(picker, 280, 700)]


def _strafe(
    _state: MatchBehavior,
    scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    side = scan_codes["left"] if picker.random() < 0.5 else scan_codes["right"]
    return [_hold([side], picker.randint(*HOLD_MS))]


def _forward(
    state: MatchBehavior,
    scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    # One hold. Sometimes a diagonal, never a second hold and never sprint.
    keys = [scan_codes["forward"]]
    if picker.random() < 0.4:
        keys.append(scan_codes["left"] if picker.random() < 0.5 else scan_codes["right"])
    return [
        _hold(keys, picker.randint(*HOLD_MS)),
        *_look(state, picker, steps=1, yaw=(12, 40)),
    ]


def _back(
    _state: MatchBehavior,
    scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    return [_hold([scan_codes["back"]], picker.randint(*HOLD_MS))]


def _jump(
    _state: MatchBehavior,
    scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    return [_tap(scan_codes["jump"], picker)]


def _interact(
    _state: MatchBehavior,
    scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    steps: list[dict[str, object]] = [_tap(scan_codes["interact"], picker)]
    if picker.random() < 0.35:
        steps.extend([_wait(picker, 160, 320), _tap(scan_codes["interact"], picker)])
    return steps


def _melee(
    _state: MatchBehavior,
    scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    punches = picker.randint(2, 3)
    steps: list[dict[str, object]] = []
    for index in range(punches):
        steps.append(_tap(scan_codes["melee"], picker))
        if index + 1 < punches:
            steps.append(_wait(picker, 220, 420))
    return steps


def _tactical(
    _state: MatchBehavior,
    scan_codes: dict[str, int],
    picker: random.Random,
) -> list[dict[str, object]]:
    return [_tap(scan_codes["tactical"], picker)]


_GESTURE_BUILDERS: dict[
    str,
    Callable[[MatchBehavior, dict[str, int], random.Random], list[dict[str, object]]],
] = {
    "look": _look_only,
    "idle": _idle,
    "strafe": _strafe,
    "forward": _forward,
    "jump": _jump,
    "interact": _interact,
    "melee": _melee,
    "tactical": _tactical,
    "back": _back,
}

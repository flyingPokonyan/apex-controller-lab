"""One match's worth of agent: perceive, believe, decide, act, in a loop.

The pilot calls ``run_slice`` while the screen reads as alive in a match. A
slice runs the decision loop at about 10 Hz for up to ``budget_s`` and then
hands control back so the pilot can recognise the screen as usual; the input
thread keeps the last intention going in between, so the player does not stop
while the pilot reads. ``suspend`` lets go of everything at once.
"""

from __future__ import annotations

from collections import Counter
import random
import time
from typing import Callable

import numpy as np

from .belief import Belief, Memory
from .controller import InputController
from .goals import GoalManager
from .motor import Keys, Motor
from .perception import Observation, Perceiver
from .skills import Context


DECISION_S = 0.1
# Training frames for the loot detector: one every few seconds on the ground,
# more while a pickup or bin prompt shows (those are the useful ones).
FRAME_EVERY_S = 4.0
FRAME_PROMPT_EVERY_S = 1.0
# The bottom HUD gone for this long without a knock means dead or spectating.
HUD_LOST_S = 1.5
# How often the optional reads run when no skill asks for them.
EVERY = {"knocked": 0.5, "ring": 1.0, "center": 2.0, "heading": 0.25, "minimap": 0.3}


class AgentSession:
    def __init__(
        self,
        controller: InputController,
        perceiver: Perceiver,
        *,
        keys: Keys | None = None,
        rng: random.Random | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        crouch_toggle: bool = False,
        frame_sink: Callable[[np.ndarray, str], object] | None = None,
    ) -> None:
        self.frame_sink = frame_sink
        self._last_frame_saved = -1e9
        self.controller = controller
        self.perceiver = perceiver
        self.rng = rng or random.Random()
        self.clock = clock
        self.sleep = sleep
        self.motor = Motor(controller, keys or Keys(), self.rng, crouch_toggle=crouch_toggle)
        self.belief = Belief()
        self.memory = Memory()
        self.goals = GoalManager()
        self.counters: Counter = Counter()
        self.timings: dict[str, list[float]] = {}
        self._last_read: dict[str, float] = {}
        self._wants: set[str] = set()
        self._sent_x = 0
        self._hud_lost_since: float | None = None
        self._life_started: float | None = None
        self._armed_time = 0.0
        self._storm_time = 0.0
        self._knocked_time = 0.0
        self._last_tick: float | None = None

    # ------------------------------------------------------------------ lifecycle

    def begin_life(self) -> None:
        """A new match. Calibration survives: it belongs to the machine."""
        self.belief = Belief()
        self.memory = Memory()
        self.goals = GoalManager()
        self.counters = Counter()
        self.timings = {}
        self._last_read = {}
        self._hud_lost_since = None
        self._life_started = None
        self._armed_time = self._storm_time = self._knocked_time = 0.0
        self._last_tick = None
        self.motor.pitch = None

    def suspend(self, reason: str) -> None:
        now = self.clock()
        ctx = self._context(now)
        self.goals.abandon(ctx, reason)
        self.controller.pause(None)
        self._last_tick = None

    # ------------------------------------------------------------------ loop

    def run_slice(self, grab: Callable[[], np.ndarray], budget_s: float = 2.0) -> str:
        """Decide and act until the budget is used up or the player is no longer on the ground."""
        if self.controller.fault == "EMERGENCY_STOP":
            return "EMERGENCY_STOP"
        if not self.controller.active:
            self.controller.resume()
        self.controller.start()
        ends = self.clock() + budget_s
        while True:
            started = self.clock()
            frame = grab()
            outcome = self.tick(frame, started)
            if outcome is not None:
                return outcome
            if self.controller.fault is not None:
                return f"INPUT_{self.controller.fault}"
            if self.clock() >= ends:
                return "BUDGET"
            spare = DECISION_S - (self.clock() - started)
            if spare > 0:
                self.sleep(spare)

    def tick(self, frame: np.ndarray, now: float) -> str | None:
        if self._life_started is None:
            self._life_started = now
        want = self._schedule(now)
        obs = self.perceiver.read(frame, now, **{f"want_{name}": name in want for name in EVERY}, want_prompt=True)
        for name in want:
            self._last_read[name] = now
        self._note_timings(obs)
        self._carry_heading()
        self.belief.update(obs)
        self._account(now)
        if obs.hud or self.belief.is_knocked():
            self._hud_lost_since = None
        else:
            if self._hud_lost_since is None:
                self._hud_lost_since = now
            elif now - self._hud_lost_since >= HUD_LOST_S:
                self.goals.abandon(self._context(now), "HUD_LOST")
                self.controller.pause(None)
                return "HUD_LOST"
        ctx = self._context(now)
        if not (self.belief.hud.fresh(now, 0.6) or self.belief.is_knocked()):
            # Landing flicker or a banner over the HUD: hold still rather than act blind.
            self.motor.stop()
            self.controller.heartbeat()
            return None
        decision_started = time.perf_counter()
        self.goals.tick(ctx)
        self._time("decide", (time.perf_counter() - decision_started) * 1000)
        self._wants = set(ctx.wants)
        self.controller.heartbeat()
        self.counters["ticks"] += 1
        self._keep_frame(frame, now)
        return None

    def _keep_frame(self, frame: np.ndarray, now: float) -> None:
        if self.frame_sink is None:
            return
        prompt = self.belief.current_prompt(now)
        every = FRAME_PROMPT_EVERY_S if prompt is not None else FRAME_EVERY_S
        if now - self._last_frame_saved < every:
            return
        self._last_frame_saved = now
        goal = self.goals.current.name if self.goals.current else "none"
        label = f"{goal}-{prompt.kind}" if prompt is not None else goal
        try:
            self.frame_sink(frame, label)
            self.counters["framesKept"] += 1
        except Exception:  # noqa: BLE001 - training material must never stop play
            self.counters["framesFailed"] += 1

    # ------------------------------------------------------------------ helpers

    def _context(self, now: float) -> Context:
        return Context(now=now, belief=self.belief, memory=self.memory, motor=self.motor, rng=self.rng,
                       counters=self.counters)

    def _schedule(self, now: float) -> set[str]:
        due = {name for name, every in EVERY.items() if now - self._last_read.get(name, -1e9) >= every}
        return due | (self._wants & set(EVERY))

    def _carry_heading(self) -> None:
        """Turn the counts the input thread sent since last time into degrees."""
        sent = self.controller.sent_counts[0]
        delta, self._sent_x = sent - self._sent_x, sent
        if delta and self.motor.counts_per_degree:
            self.belief.turned(delta / self.motor.counts_per_degree)

    def _account(self, now: float) -> None:
        if self._last_tick is not None:
            dt = min(1.0, now - self._last_tick)
            if self.belief.is_armed():
                self._armed_time += dt
            if self.belief.in_storm():
                self._storm_time += dt
            if self.belief.is_knocked():
                self._knocked_time += dt
        self._last_tick = now

    def _note_timings(self, obs: Observation) -> None:
        for name, value in obs.timings_ms.items():
            self._time(name, value)

    def _time(self, name: str, value: float) -> None:
        samples = self.timings.setdefault(name, [])
        if len(samples) < 4000:
            samples.append(value)

    def summary(self) -> dict[str, object]:
        def pct(values: list[float], share: float) -> float:
            ordered = sorted(values)
            return round(ordered[min(len(ordered) - 1, int(share * len(ordered)))], 1) if ordered else 0.0

        alive = 0.0 if self._life_started is None or self._last_tick is None else self._last_tick - self._life_started
        return {
            "agent": 1,
            "aliveS": round(alive, 1),
            "armedS": round(self._armed_time, 1),
            "stormS": round(self._storm_time, 1),
            "knockedS": round(self._knocked_time, 1),
            "countsPerDegree": None if self.motor.counts_per_degree is None else round(self.motor.counts_per_degree, 3),
            "counters": dict(sorted(self.counters.items())),
            "timingsMs": {name: {"p50": pct(values, 0.5), "p95": pct(values, 0.95)} for name, values in self.timings.items()},
            "switches": list(self.goals.switches)[-40:],
        }

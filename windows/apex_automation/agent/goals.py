"""Which goal to pursue now, and when to let it go.

Rules that keep the agent from dithering or getting stuck:

1. Only hard events preempt a running goal at once: being shot, being
   knocked, the storm, and a weapon card in reach while unarmed.
2. A softer goal takes over only when the current one ends, or when it has
   been wanted for SOFT_SETTLE_S and outranks the current one by SOFT_MARGIN.
3. Every goal has a time limit. Running out of it is a TIMEOUT, and every
   failure or timeout starts a cooldown during which that goal is not chosen.
4. "wait" is always available, so there is always something to do.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable

from . import skills as sk
from .skills import Context, Skill, Status


SOFT_SETTLE_S = 0.5
SOFT_MARGIN = 20


@dataclass(frozen=True)
class Goal:
    name: str
    priority: int
    wanted: Callable[[Context], bool]
    make: Callable[[Context], Skill]
    hard: Callable[[Context], bool] = lambda ctx: False
    timeout_s: float = 20.0
    cooldown_s: float = 10.0
    # Longer cooldowns for specific failure reasons.
    reason_cooldown: tuple[tuple[str, float], ...] = ()


def _recent_fail(ctx: Context, goal: str, within: float, *reasons: str) -> bool:
    result = ctx.memory.last_result.get(goal)
    if result is None:
        return False
    status, reason, at = result
    return status in (Status.FAILED.value, Status.TIMEOUT.value) and ctx.now - at <= within and (not reasons or reason in reasons)


def _prompt(ctx: Context):
    return ctx.belief.current_prompt(ctx.now)


def _loot_wanted(ctx: Context) -> bool:
    prompt = _prompt(ctx)
    if prompt is None:
        return False
    heading = ctx.belief.heading.value
    if prompt.word == "打开":
        return not ctx.memory.tried_near("bin", heading, ctx.now, 90.0)
    if prompt.word in ("捡起", "装备"):
        return sk.wanted_item(prompt, ctx.belief) and not ctx.memory.tried_near("item", heading, ctx.now, 4.0, spread=8.0)
    return False


def _loot_hard(ctx: Context) -> bool:
    prompt = _prompt(ctx)
    return prompt is not None and prompt.kind == "weapon" and not ctx.belief.is_armed()


def _needs_gear(ctx: Context) -> bool:
    belief = ctx.belief
    return not belief.is_armed() or belief.out_of_ammo(ctx.now)


def _mate_far(ctx: Context) -> bool:
    mate = ctx.belief.nearest_mate(ctx.now)
    if mate is None:
        return False
    # Hysteresis: start following beyond 30 px, Follow itself stops at 22.
    return mate.distance > 30


def default_goals() -> tuple[Goal, ...]:
    return (
        Goal("crawl", 100, lambda c: c.belief.is_knocked(), sk.Crawl, hard=lambda c: c.belief.is_knocked(),
             timeout_s=120, cooldown_s=0),
        Goal("return_fire", 90, lambda c: c.belief.under_fire(c.now) and not _recent_fail(c, "return_fire", 4.0),
             sk.ReturnFire, hard=lambda c: c.belief.under_fire(c.now), timeout_s=20, cooldown_s=4,
             reason_cooldown=(("NO_WEAPON", 6.0), ("LOSING", 6.0))),
        Goal("retreat", 85, lambda c: c.belief.under_fire(c.now) and _recent_fail(c, "return_fire", 3.0),
             sk.Retreat, hard=lambda c: c.belief.under_fire(c.now) and _recent_fail(c, "return_fire", 3.0),
             timeout_s=12, cooldown_s=3),
        Goal("escape_storm", 80, lambda c: c.belief.in_storm(), sk.EscapeStorm, hard=lambda c: c.belief.in_storm(),
             timeout_s=40, cooldown_s=2),
        Goal("calibrate", 72, lambda c: not c.motor.can_turn and c.belief.safe_for(c.now, 2.0), sk.Calibrate,
             timeout_s=12, cooldown_s=15),
        Goal("level", 70, lambda c: c.motor.can_turn and c.motor.pitch is None and c.belief.safe_for(c.now, 1.5),
             sk.Level, timeout_s=3, cooldown_s=5),
        Goal("heal", 60, lambda c: (c.belief.health_share() or 1.0) < 0.5 and c.belief.safe_for(c.now, 4.0), sk.Heal,
             timeout_s=10, cooldown_s=8, reason_cooldown=(("NO_HEAL_ITEMS", 90.0),)),
        Goal("reload", 55, lambda c: c.belief.mag_low(c.now) and c.belief.safe_for(c.now, 2.0), sk.Reload,
             timeout_s=4, cooldown_s=5, reason_cooldown=(("NO_AMMO", 30.0),)),
        Goal("loot", 50, _loot_wanted, sk.Loot, hard=_loot_hard, timeout_s=15, cooldown_s=2),
        Goal("follow", 30, _mate_far, sk.Follow, timeout_s=45, cooldown_s=4, reason_cooldown=(("UNREACHABLE", 30.0),)),
        Goal("search", 20, lambda c: _needs_gear(c) and c.belief.safe_for(c.now, 3.0), sk.Search, timeout_s=20,
             cooldown_s=3),
        Goal("wait", 0, lambda c: True, sk.Wait, timeout_s=10, cooldown_s=0),
    )


class GoalManager:
    def __init__(self, goals: tuple[Goal, ...] | None = None) -> None:
        self.goals = goals or default_goals()
        self.by_name = {goal.name: goal for goal in self.goals}
        self.current: Goal | None = None
        self.skill: Skill | None = None
        self._wanted_since: dict[str, float] = {}
        self.switches: deque[dict[str, object]] = deque(maxlen=200)

    def _candidates(self, ctx: Context) -> list[Goal]:
        wanted = []
        for goal in self.goals:
            if ctx.memory.cooling(goal.name, ctx.now):
                self._wanted_since.pop(goal.name, None)
                continue
            if goal.wanted(ctx):
                self._wanted_since.setdefault(goal.name, ctx.now)
                wanted.append(goal)
            else:
                self._wanted_since.pop(goal.name, None)
        return sorted(wanted, key=lambda goal: -goal.priority)

    def tick(self, ctx: Context) -> str:
        candidates = self._candidates(ctx)
        if self.current is not None and self.skill is not None:
            preempt = self._preemptor(ctx, candidates)
            if preempt is not None:
                self._end(ctx, Status.INTERRUPTED, f"by:{preempt.name}")
                self._start(ctx, preempt, "preempt")
            else:
                status = self._step(ctx)
                if status is not Status.RUNNING:
                    self._end(ctx, status, self.skill.reason)
        if self.current is None:
            candidates = self._candidates(ctx)
            goal = candidates[0] if candidates else self.by_name["wait"]
            self._start(ctx, goal, "next")
            self._step(ctx)
        return self.current.name if self.current else "none"

    def _preemptor(self, ctx: Context, candidates: list[Goal]) -> Goal | None:
        current = self.current
        for goal in candidates:
            if goal is current or goal.priority <= current.priority:
                continue
            if goal.hard(ctx):
                return goal
            since = self._wanted_since.get(goal.name, ctx.now)
            if goal.priority >= current.priority + SOFT_MARGIN and ctx.now - since >= SOFT_SETTLE_S:
                return goal
        return None

    def _step(self, ctx: Context) -> Status:
        skill = self.skill
        if ctx.now - skill.started > self.current.timeout_s:
            return Status.TIMEOUT
        try:
            return skill.step(ctx)
        except Exception as error:  # noqa: BLE001 - a broken skill must not stall the match
            skill.reason = f"ERROR:{type(error).__name__}"
            ctx.counters[f"skill_error:{skill.name}:{type(error).__name__}"] += 1
            return Status.FAILED

    def _start(self, ctx: Context, goal: Goal, why: str) -> None:
        self.current = goal
        self.skill = goal.make(ctx)
        ctx.counters[f"goal:{goal.name}"] += 1
        self.switches.append({"at": round(ctx.now, 2), "to": goal.name, "why": why})

    def _end(self, ctx: Context, status: Status, reason: str | None) -> None:
        goal, skill = self.current, self.skill
        skill.stop(ctx)
        ctx.memory.last_result[goal.name] = (status.value, reason, ctx.now)
        key = f"goal:{goal.name}:{status.value}"
        ctx.counters[key + (f":{reason}" if reason and status is Status.FAILED else "")] += 1
        if status in (Status.FAILED, Status.TIMEOUT):
            cooldown = dict(goal.reason_cooldown).get(reason or "", goal.cooldown_s)
            if cooldown > 0:
                ctx.memory.cooldown_until[goal.name] = ctx.now + cooldown
        self.switches.append({"at": round(ctx.now, 2), "from": goal.name, "status": status.value, "reason": reason})
        self.current = None
        self.skill = None

    def abandon(self, ctx: Context, reason: str) -> None:
        """Stop whatever runs, e.g. the slice ended because the player died."""
        if self.current is not None:
            self._end(ctx, Status.INTERRUPTED, reason)

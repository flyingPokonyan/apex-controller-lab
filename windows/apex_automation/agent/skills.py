"""Skills: small state machines that each pursue one thing and check it worked.

Every skill is stepped once per decision tick and answers RUNNING, SUCCESS,
FAILED, INTERRUPTED or TIMEOUT. A failure carries a reason code, so a match
summary can say *why* nothing was looted, not only that nothing was. Each
step looks at the belief again; nothing is assumed to have happened because
a key was pressed.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
import math
import random

from .belief import Belief, Memory, _signed
from .motor import Motor
from .perception import Prompt


class Status(str, Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    INTERRUPTED = "interrupted"
    TIMEOUT = "timeout"


@dataclass
class Context:
    now: float
    belief: Belief
    memory: Memory
    motor: Motor
    rng: random.Random
    counters: Counter = field(default_factory=Counter)
    # Perception extras the skills want on the next frames.
    wants: set[str] = field(default_factory=set)
    # Shooting at what the detector sees: Bot Royale only, never real players.
    allow_engage: bool = False


class Skill:
    name = "skill"
    timeout_s = 20.0

    def __init__(self, ctx: Context) -> None:
        self.started = ctx.now
        self.reason: str | None = None
        self.phase = "start"
        self.phase_at = ctx.now

    def go(self, phase: str, ctx: Context) -> None:
        self.phase = phase
        self.phase_at = ctx.now

    def in_phase(self, ctx: Context) -> float:
        return ctx.now - self.phase_at

    def fail(self, reason: str) -> Status:
        self.reason = reason
        return Status.FAILED

    def step(self, ctx: Context) -> Status:  # pragma: no cover - overridden
        raise NotImplementedError

    def stop(self, ctx: Context) -> None:
        ctx.motor.stop(ctx.now)


# ---------------------------------------------------------------------- movement


# Mean change of the 64x36 scene thumbnail between reads. Standing still read
# 0.3–2.5 on the 10-29 recordings, walking 5–60.
STILL_CHANGE = 3.0
BLOCKED_AFTER_S = 0.7


class Navigator:
    """Moves toward a heading and climbs a ladder of tricks when blocked.

    Blocked = forward held, the view not turning, and the scene not changing
    for BLOCKED_AFTER_S. The ladder: jump (Apex mantles low walls and sills),
    sidestep along the wall, climb (hold forward and jump against it), turn
    60–90° away; each rung is given a moment and checked before the next.
    """

    RUNGS = ("jump", "sidestep", "climb", "turn")

    def __init__(self) -> None:
        self._changes: deque[tuple[float, float]] = deque(maxlen=20)
        self.rung: int | None = None
        self.rung_at = 0.0
        self.side = 1
        self.ladders = 0

    def note(self, ctx: Context, moving: bool) -> None:
        change = ctx.belief.scene_change.value
        steady = not ctx.motor.controller.turning and abs(ctx.motor.controller.snapshot()["velocity"][0]) < 200
        if moving and steady and isinstance(change, float) and ctx.belief.scene_change.fresh(ctx.now, 0.4):
            self._changes.append((ctx.now, change))
        elif not moving:
            self._changes.clear()

    def blocked(self, ctx: Context) -> bool:
        recent = [change for at, change in self._changes if ctx.now - at <= BLOCKED_AFTER_S]
        span = ctx.now - self._changes[0][0] if self._changes else 0.0
        return span >= BLOCKED_AFTER_S and len(recent) >= 3 and max(recent) < STILL_CHANGE

    def drive(self, ctx: Context, heading_error: float | None, *, sprint: bool, look_pitch: float | None = None) -> str:
        """One tick of travel. Returns "moving", "unsticking" or "gave_up"."""
        motor = ctx.motor
        if self.rung is not None:
            return self._unstick(ctx)
        self.note(ctx, moving=True)
        if self.blocked(ctx):
            self._changes.clear()
            self.rung = 0
            self.rung_at = ctx.now
            self.side = ctx.rng.choice((-1, 1))
            ctx.counters["unstick:start"] += 1
            return self._unstick(ctx)
        if heading_error is not None:
            motor.steer(heading_error)
            sprint = sprint and abs(heading_error) < 35
        motor.move(forward=True, sprint=sprint)
        return "moving"

    def _unstick(self, ctx: Context) -> str:
        motor = ctx.motor
        rung = self.RUNGS[self.rung]
        elapsed = ctx.now - self.rung_at
        change = ctx.belief.scene_change.value
        moving_again = isinstance(change, float) and change >= STILL_CHANGE * 2 and elapsed > 0.35
        if elapsed > 0.35 and moving_again and rung != "turn":
            ctx.counters[f"unstick:{rung}:ok"] += 1
            self._finish()
            return "moving"
        if rung == "jump":
            if elapsed < 0.05:
                motor.move(forward=True)
                motor.tap("jump", (0.12, 0.2))
            elif elapsed > 0.7:
                self._next(ctx)
        elif rung == "sidestep":
            motor.move(forward=True, left=self.side < 0, right=self.side > 0)
            if elapsed > 0.9:
                self._next(ctx)
        elif rung == "climb":
            motor.move(forward=True, jump=True)
            if elapsed > 1.2:
                self._next(ctx)
        elif rung == "turn":
            if elapsed < 0.05:
                heading = ctx.belief.heading.value
                ctx.memory.block(heading, ctx.now)
                motor.move()
                motor.turn(self.side * ctx.rng.uniform(60, 95))
            elif elapsed > 0.6:
                motor.move(forward=True)
            if elapsed > 1.8:
                ctx.counters["unstick:turn:done"] += 1
                self._finish()
                self.ladders += 1
                return "gave_up" if self.ladders >= 3 else "moving"
        return "unsticking"

    def _next(self, ctx: Context) -> None:
        ctx.counters[f"unstick:{self.RUNGS[self.rung]}:no"] += 1
        self.rung += 1
        self.rung_at = ctx.now

    def _finish(self) -> None:
        self.rung = None
        self._changes.clear()


# ---------------------------------------------------------------------- setup


class Calibrate(Skill):
    """Measure mouse counts per degree from the compass: 3 turns, median."""

    name = "calibrate"
    timeout_s = 12.0
    COUNTS = 600

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.samples: list[float] = []
        self.counts = self.COUNTS
        self.before: float | None = None

    def step(self, ctx: Context) -> Status:
        ctx.wants.add("heading")
        motor = ctx.motor
        motor.move()
        heading = ctx.belief.heading
        if self.phase == "start":
            if heading.fresh(ctx.now, 0.25):
                self.before = float(heading.value)
                self.sign = ctx.rng.choice((-1, 1))
                motor.controller.turn(self.sign * self.counts, 0, ctx.rng.uniform(0.25, 0.4))
                self.go("turning", ctx)
            return Status.RUNNING
        if self.phase == "turning":
            if self.in_phase(ctx) < 0.6 or motor.controller.turning:
                return Status.RUNNING
            if heading.fresh(ctx.now, 0.2) and heading.at > self.phase_at + 0.45:
                turned = abs(_signed(float(heading.value) - float(self.before)))
                if turned < 8:
                    self.counts = min(4800, self.counts * 2)
                elif turned > 150:
                    self.counts = max(150, self.counts // 2)
                else:
                    self.samples.append(self.counts / turned)
                self.go("start", ctx)
                if len(self.samples) >= 3:
                    motor.counts_per_degree = sorted(self.samples)[1]
                    motor.calibrated = True
                    ctx.counters["calibrated"] += 1
                    return Status.SUCCESS
            return Status.RUNNING
        return Status.RUNNING


class Level(Skill):
    """Bring the view to the horizon: down to the clamp, then up 89°."""

    name = "level"
    timeout_s = 3.0

    def step(self, ctx: Context) -> Status:
        motor = ctx.motor
        if self.phase == "start":
            self.down = motor.level()
            self.go("down", ctx)
            return Status.RUNNING
        if self.phase == "down":
            if self.in_phase(ctx) >= self.down + 0.08 and not motor.controller.turning:
                self.up = motor.level_up()
                self.go("up", ctx)
            return Status.RUNNING
        if self.in_phase(ctx) >= self.up and not motor.controller.turning:
            ctx.counters["levelled"] += 1
            return Status.SUCCESS
        return Status.RUNNING


# ---------------------------------------------------------------------- danger


class Crawl(Skill):
    """Knocked: crawl toward the nearest teammate, no attacks."""

    name = "crawl"
    timeout_s = 120.0

    def step(self, ctx: Context) -> Status:
        ctx.wants.update({"knocked", "minimap"})
        if not ctx.belief.is_knocked():
            return Status.SUCCESS
        turn = ctx.belief.mate_turn(ctx.now)
        if turn is not None:
            ctx.motor.steer(turn, max_dps=90)
        ctx.motor.move(forward=True)
        return Status.RUNNING


class ReturnFire(Skill):
    """Turn toward where the shots came from and answer.

    Armed with rounds: aim down sights and fire short bursts with a little
    strafing. Empty magazine: reload. Unarmed: punch only when the game shows a
    target's bar near the crosshair (someone close), otherwise give up so the
    planner can retreat toward the team instead of punching air.
    """

    name = "return_fire"
    timeout_s = 20.0

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.sweep: list[float] = []
        self.bursts = 0
        self.next_burst = ctx.now
        self.strafe = ctx.rng.choice((-1, 1))

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        ctx.wants.add("center")
        quiet = belief.safe_for(ctx.now, 6.0) and not belief.last_arrow.fresh(ctx.now, 6.0)
        if quiet:
            return Status.SUCCESS
        health = belief.health_share()
        if health is not None and health < 0.3 and not belief.hit_confirmed.fresh(ctx.now, 3.0):
            return self.fail("LOSING")
        if self.phase == "start":
            arrow = belief.last_arrow.value if belief.last_arrow.fresh(ctx.now, 1.2) else None
            if arrow is not None:
                self.wait = motor.turn(float(arrow))
                ctx.counters["fire:turn_to_arrow"] += 1
            else:
                first = ctx.rng.uniform(90, 160) * ctx.rng.choice((-1, 1))
                self.sweep = [-(first + ctx.rng.uniform(60, 110))]
                self.wait = motor.turn(first)
                ctx.counters["fire:sweep"] += 1
            self.go("turning", ctx)
            return Status.RUNNING
        if self.phase == "turning":
            # A fresh arrow well off to the side wins over the planned sweep.
            arrow = belief.last_arrow.value
            if belief.last_arrow.fresh(ctx.now, 0.3) and self.in_phase(ctx) > 0.6 and abs(float(arrow)) > 30:
                self.go("start", ctx)
                return Status.RUNNING
            if self.in_phase(ctx) < self.wait:
                return Status.RUNNING
            self.go("engage", ctx)
        # engage
        if belief.is_armed():
            if belief.mag.value == 0 and belief.mag.fresh(ctx.now, 1.0):
                motor.aim(False, ctx.now)
                if not motor.controller.busy_tapping:
                    motor.tap("reload")
                    ctx.counters["fire:reload"] += 1
                if belief.out_of_ammo(ctx.now):
                    return self.fail("NO_AMMO")
                return Status.RUNNING
            motor.aim(True, ctx.now)
            motor.move(left=self.strafe < 0, right=self.strafe > 0)
            if ctx.now >= self.next_burst and not motor.controller.busy_tapping:
                motor.fire(ctx.rng.uniform(0.15, 0.4))
                self.bursts += 1
                ctx.counters["fire:burst"] += 1
                self.next_burst = ctx.now + ctx.rng.uniform(0.35, 0.8)
                if ctx.rng.random() < 0.35:
                    self.strafe = -self.strafe
            if belief.hit_confirmed.fresh(ctx.now, 0.5):
                ctx.counters["fire:hit_confirmed_tick"] += 1
            elif self.sweep and self.in_phase(ctx) > 1.5 and not belief.last_arrow.fresh(ctx.now, 1.5):
                self.wait = motor.turn(self.sweep.pop(0))
                self.go("turning", ctx)
            return Status.RUNNING
        if belief.hit_confirmed.fresh(ctx.now, 1.0):
            motor.move(forward=True)
            if not motor.controller.busy_tapping:
                motor.tap("melee", (0.06, 0.1))
                ctx.counters["fire:punch"] += 1
            return Status.RUNNING
        return self.fail("NO_WEAPON")

    def stop(self, ctx: Context) -> None:
        ctx.motor.aim(False, ctx.now)
        super().stop(ctx)


class Retreat(Skill):
    """Break off: face away from the shots (or toward the team) and run."""

    name = "retreat"
    timeout_s = 12.0

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        if self.phase == "start":
            mate = belief.mate_turn(ctx.now)
            arrow = belief.last_arrow.value if belief.last_arrow.fresh(ctx.now, 3.0) else None
            if mate is not None and (arrow is None or abs(_signed(mate - float(arrow))) > 70):
                target = mate
            elif arrow is not None:
                target = _signed(float(arrow) + 180 + ctx.rng.uniform(-30, 30))
            else:
                target = ctx.rng.uniform(140, 220) * ctx.rng.choice((-1, 1))
            self.wait = motor.turn(target)
            self.go("turning", ctx)
            return Status.RUNNING
        if self.phase == "turning":
            if self.in_phase(ctx) >= self.wait:
                self.go("running", ctx)
            return Status.RUNNING
        motor.move(forward=True, sprint=True)
        if self.in_phase(ctx) > 1.5 and ctx.rng.random() < 0.04:
            motor.tap("crouch", (0.25, 0.4))  # slide
        if belief.safe_for(ctx.now, 8.0):
            return Status.SUCCESS
        if self.in_phase(ctx) > 10.0:
            return self.fail("STILL_HIT")
        return Status.RUNNING


class EscapeStorm(Skill):
    """Run out of the ring: toward the team, else straight on; turn round if it gets no better."""

    name = "escape_storm"
    timeout_s = 40.0

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.nav = Navigator()
        self.heading_goal: float | None = None

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        ctx.wants.update({"ring", "minimap"})
        if not belief.in_storm():
            return Status.SUCCESS
        if self.heading_goal is None and belief.heading.value is not None:
            mate = belief.nearest_mate(ctx.now)
            tried = [a.heading for a in ctx.memory.attempts if a.kind == "storm" and ctx.now - a.at < 60]
            if mate is not None and not any(t is not None and abs(_signed(t - mate.bearing)) < 40 for t in tried):
                self.heading_goal = mate.bearing
            elif tried and tried[-1] is not None:
                self.heading_goal = (tried[-1] + 180) % 360
            else:
                self.heading_goal = float(belief.heading.value)
            ctx.memory.note_attempt("storm", self.heading_goal, ctx.now, "started")
        error = None
        if self.heading_goal is not None and belief.heading.value is not None:
            error = _signed(self.heading_goal - float(belief.heading.value))
        self.nav.drive(ctx, error, sprint=True)
        if ctx.now - self.started > 20.0:
            return self.fail("STILL_IN_STORM")
        return Status.RUNNING


class Heal(Skill):
    """Press the health-item key and check the bars actually rise."""

    name = "heal"
    timeout_s = 10.0

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        level = (belief.vitality.value or 0.0) + (belief.shield.value or 0.0)
        if self.phase == "start":
            self.baseline = level
            motor.move(crouch=ctx.rng.random() < 0.3)
            motor.tap("heal", (0.06, 0.12))
            ctx.counters["heal:press"] += 1
            self.go("using", ctx)
            return Status.RUNNING
        if level >= self.baseline + 0.12:
            ctx.counters["heal:ok"] += 1
            return Status.SUCCESS
        if self.in_phase(ctx) > 2.5 and level <= self.baseline + 0.01:
            return self.fail("NO_HEAL_ITEMS")
        return Status.RUNNING


class Reload(Skill):
    name = "reload"
    timeout_s = 4.0

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        ctx.wants.add("center")
        if self.phase == "start":
            motor.tap("reload")
            ctx.counters["reload:press"] += 1
            self.go("waiting", ctx)
            return Status.RUNNING
        mag = belief.mag.value
        if isinstance(mag, int) and mag > 2 and belief.mag.at > self.phase_at:
            return Status.SUCCESS
        if self.in_phase(ctx) > 3.5:
            return self.fail("NO_AMMO")
        return Status.RUNNING


# ---------------------------------------------------------------------- loot


def wanted_item(prompt: Prompt, belief: Belief) -> bool:
    kind = prompt.kind
    if kind == "weapon":
        # With both slots full, 装备 swaps out the gun in hand: two cards side
        # by side would then trade guns back and forth for ever.
        return belief.has_free_slot()
    return kind in ("ammo", "heal", "shield", "armor", "attachment", "grenade")


class Loot(Skill):
    """Take what is under the crosshair; open a bin and work through its contents.

    Item cards follow the item, so an item card on screen means the crosshair
    is on it already. A bin's 「打开」 sits under the crosshair; after the lid
    opens the agent looks down into it and sweeps slowly, taking every useful
    card that comes up.

    A press only counts once the screen agrees: within CONFIRM_S the card is
    gone or shows another item, or the weapon panel changed (a gun in hand, a
    fuller magazine). The view holds still meanwhile, so a card that left the
    crosshair because the view moved is not mistaken for one that was taken.
    An 「打开」 counts as opened when the prompt leaves or item cards appear.
    Nothing confirmed = failure, remembered so the spot is not tried again.
    """

    name = "loot"
    timeout_s = 15.0
    CONFIRM_S = 1.2

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.taken = 0
        self.presses = 0
        self.last_press = -1e9
        self.armed_before = ctx.belief.is_armed()
        self.sweep_dir = ctx.rng.choice((-1, 1))
        self.bin = False
        self.opened = False
        # Cards pressed with no visible effect: a full bag, a locked item.
        self.pressed: dict[str, int] = {}
        # The press waiting for the screen to confirm it: (kind, label, at, mag, armed).
        self.pending: tuple[str, str, float, object, bool] | None = None

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        prompt = belief.current_prompt(ctx.now)
        motor.move()
        if self.pending is not None:
            return self._confirm(ctx, prompt)
        if prompt is not None and ctx.now - self.last_press > 0.45:
            if prompt.word in ("打开",):
                if self.bin:
                    # Another 「打开」 after we opened one: a door or something else; move on.
                    return self._finish(ctx)
                self._press(ctx, "open", "打开")
                self.bin = True
                return Status.RUNNING
            if prompt.word in ("捡起", "装备") and wanted_item(prompt, belief):
                label = prompt.name or prompt.text
                if self.pressed.get(label, 0) < 2:
                    self.pressed[label] = self.pressed.get(label, 0) + 1
                    self._press(ctx, prompt.kind, label)
                    return Status.RUNNING
                ctx.counters["loot:no_effect"] += 1
                if not self.bin:
                    return self._finish(ctx)
        if self.bin and self.opened:
            if self.in_phase(ctx) < getattr(self, "look_in", 0.3) + 0.4:
                return Status.RUNNING
            # Sweep across the open bin slowly so each card comes under the crosshair.
            if not motor.controller.turning:
                motor.turn(self.sweep_dir * ctx.rng.uniform(14, 24), ctx.rng.uniform(-3, 3), fine=True)
                self.sweep_dir = -self.sweep_dir
            if self.in_phase(ctx) > 5.0:
                return self._finish(ctx)
            return Status.RUNNING
        if self.bin and not self.opened:
            return self._finish(ctx)
        if prompt is None and ctx.now - self.last_press > 1.0:
            return self._finish(ctx)
        return Status.RUNNING

    def _press(self, ctx: Context, kind: str, label: str) -> None:
        ctx.motor.tap("interact", (0.06, 0.11))
        self.presses += 1
        self.last_press = ctx.now
        ctx.counters[f"loot:press:{kind}"] += 1
        self.pending = (kind, label, ctx.now, ctx.belief.mag.value, ctx.belief.is_armed())

    def _confirm(self, ctx: Context, prompt) -> Status:
        kind, label, at, mag_before, armed_before = self.pending
        belief = ctx.belief
        waited = ctx.now - at
        if waited < 0.25:
            return Status.RUNNING
        if kind == "open":
            gone = prompt is None or prompt.word != "打开"
            if gone:
                self.pending = None
                self.opened = True
                ctx.counters["loot:opened"] += 1
                self.go("look_in", ctx)
                self.look_in = ctx.motor.turn(ctx.rng.uniform(-8, 8), -ctx.rng.uniform(12, 20), fine=True)
                return Status.RUNNING
        else:
            card_changed = prompt is None or (prompt.name or prompt.text) != label
            mag = belief.mag.value
            panel_changed = (belief.is_armed() and not armed_before) or (
                isinstance(mag, int) and isinstance(mag_before, int) and mag > mag_before
            )
            if card_changed or panel_changed:
                self.pending = None
                self.taken += 1
                ctx.counters[f"loot:taken:{kind}"] += 1
                if belief.is_armed() and not armed_before:
                    ctx.counters["loot:armed"] += 1
                return Status.RUNNING
        if waited >= self.CONFIRM_S:
            self.pending = None
            ctx.counters[f"loot:unconfirmed:{kind}"] += 1
        return Status.RUNNING

    def _finish(self, ctx: Context) -> Status:
        heading = ctx.belief.heading.value
        if self.taken:
            ctx.memory.note_attempt("bin" if self.bin else "item", heading, ctx.now, "taken")
            ctx.counters["loot:ok"] += 1
            return Status.SUCCESS
        ctx.memory.note_attempt("bin" if self.bin else "item", heading, ctx.now, "nothing")
        if self.bin:
            return self.fail("OPEN_NO_EFFECT" if not self.opened else "BIN_NOTHING_TAKEN")
        return self.fail("NO_EFFECT")


# ---------------------------------------------------------------------- seen targets

GEAR_KINDS = ("bin_closed", "item", "deathbox", "bin_open")
TOP_UP_KINDS = ("bin_closed", "deathbox")


def target_heading(ctx: Context, detection) -> float | None:
    heading = ctx.belief.heading.value
    return None if heading is None else (float(heading) + detection.yaw) % 360


def fresh_target(ctx: Context, kinds: tuple[str, ...], max_yaw: float = 75.0):
    """The nearest-looking confirmed track of these kinds not already tried."""
    candidates = []
    for track in ctx.belief.tracks(ctx.now, kinds):
        if abs(track.detection.yaw) > max_yaw or track.height_share < 0.025:
            continue
        if ctx.memory.tried_near("target", track.bearing, ctx.now, 60.0, spread=10.0):
            continue
        candidates.append(track)
    # Bigger on screen = closer: go for the closest first.
    return max(candidates, key=lambda t: t.height_share, default=None)


class Approach(Skill):
    """Walk to a bin, item or death box the detector sees, until its prompt shows.

    The target is one track: the same bin while the view sways and other bins
    come into view. If its track is lost for a moment, a track of the same kind
    at about the same compass bearing is taken as it again. Loot takes
    over once the prompt is up. Lost from view for 2 s, no closer for 6 s,
    or close with no prompt: failure, and that direction is remembered.
    """

    name = "approach"
    timeout_s = 20.0
    KINDS = GEAR_KINDS

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.nav = Navigator()
        target = fresh_target(ctx, self.KINDS)
        self.track_id = target.id if target else None
        self.kind = target.kind if target else None
        self.heading = target.bearing if target else None
        self.best_height = target.height_share if target else 0.0
        self.best_at = ctx.now
        self.seen_at = ctx.now
        self.close_since: float | None = None

    def _track(self, ctx: Context):
        track = ctx.belief.tracker.get(self.track_id)
        if track is not None and ctx.now - track.last_seen <= 0.8:
            return track.detection
        # Lost its identity (hidden for a moment): the same kind at about the same bearing.
        best, best_error = None, 18.0
        for candidate in ctx.belief.tracks(ctx.now, (self.kind,)):
            if candidate.bearing is not None and self.heading is not None:
                error = abs(_signed(candidate.bearing - self.heading))
            else:
                error = abs(candidate.detection.yaw)
            if error < best_error:
                best, best_error = candidate, error
        if best is None:
            return None
        self.track_id = best.id
        ctx.counters["approach:reacquired"] += 1
        return best.detection

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        ctx.wants.add("detect")
        if self.kind is None:
            return self.fail("NO_TARGET")
        if belief.current_prompt(ctx.now) is not None:
            ctx.counters[f"approach:arrived:{self.kind}"] += 1
            # Not again for a minute: loot decides what is worth taking from it.
            ctx.memory.note_attempt("target", self.heading, ctx.now, "ARRIVED")
            return Status.SUCCESS
        target = self._track(ctx)
        if target is not None:
            self.seen_at = ctx.now
            heading = target_heading(ctx, target)
            if heading is not None:
                self.heading = heading
            if target.height_share > self.best_height * 1.1:
                self.best_height, self.best_at = target.height_share, ctx.now
            if target.height_share > 0.4:
                self.close_since = self.close_since or ctx.now
        if ctx.now - self.seen_at > 2.0:
            return self._give_up(ctx, "LOST")
        if ctx.now - self.best_at > 6.0:
            return self._give_up(ctx, "NOT_CLOSING")
        if self.close_since is not None and ctx.now - self.close_since > 3.0:
            return self._give_up(ctx, "NO_PROMPT")
        error = None
        if self.heading is not None and belief.heading.value is not None:
            error = _signed(self.heading - float(belief.heading.value))
        elif target is not None:
            error = target.yaw
        if motor.pitch is not None and target is not None and not motor.controller.turning:
            # Keep the target a little below the centre, where its prompt appears.
            wanted = max(-35.0, min(5.0, motor.pitch + target.pitch - 3.0))
            if abs(wanted - motor.pitch) > 6:
                motor.pitch_to(wanted)
        far = target is None or target.height_share < 0.12
        if self.nav.drive(ctx, error, sprint=far) == "gave_up":
            return self._give_up(ctx, "BLOCKED")
        return Status.RUNNING

    def _give_up(self, ctx: Context, reason: str) -> Status:
        ctx.memory.note_attempt("target", self.heading, ctx.now, reason)
        return self.fail(reason)


class TopUp(Approach):
    """Armed: still worth walking to a bin or a death box for ammo and heals."""

    name = "top_up"
    KINDS = TOP_UP_KINDS


class Engage(Skill):
    """Shoot at a bot the detector sees, the way a beginner would.

    Only in Bot Royale (``ctx.allow_engage``). A short reaction delay first;
    then turn toward it with an aiming error of a few degrees, aim down
    sights, fire short bursts and re-aim every second or so, never tracking
    it frame by frame. Holds fire while a teammate is in the way. Ends when
    no bot has been seen for 1.5 s.
    """

    name = "engage"
    timeout_s = 15.0

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.react_at = ctx.now + ctx.rng.uniform(0.3, 0.8)
        self.next_aim = self.react_at
        self.next_burst = self.react_at
        self.seen_at = ctx.now
        self.strafe = ctx.rng.choice((-1, 1))
        self.target_id: int | None = None

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        ctx.wants.update({"detect", "center"})
        tracks = belief.tracks(ctx.now, ("enemy",), within=0.6)
        # Stay on one bot while it is in view rather than flicking between them.
        tracks.sort(key=lambda t: t.id != self.target_id)
        enemies = [t.detection for t in tracks]
        if tracks:
            self.target_id = tracks[0].id
            self.seen_at = ctx.now
        elif ctx.now - self.seen_at > 1.5:
            return Status.SUCCESS
        health = belief.health_share()
        if health is not None and health < 0.3 and not belief.hit_confirmed.fresh(ctx.now, 3.0):
            return self.fail("LOSING")
        if ctx.now < self.react_at:
            return Status.RUNNING
        if not belief.is_armed():
            return self.fail("NO_WEAPON")
        if belief.mag.value == 0 and belief.mag.fresh(ctx.now, 1.0):
            motor.aim(False, ctx.now)
            if not motor.controller.busy_tapping:
                motor.tap("reload")
            return self.fail("NO_AMMO") if belief.out_of_ammo(ctx.now) else Status.RUNNING
        motor.aim(True, ctx.now)
        motor.move(left=self.strafe < 0, right=self.strafe > 0)
        if enemies and ctx.now >= self.next_aim:
            target = enemies[0]
            width = abs(target.x2 - target.x1) / 2560 * 106.0
            sigma = max(1.5, min(5.0, 0.3 * width + 1.0))
            motor.turn(target.yaw + ctx.rng.gauss(0, sigma), 0.7 * target.pitch + ctx.rng.gauss(0, sigma * 0.6))
            self.next_aim = ctx.now + ctx.rng.uniform(0.6, 1.2)
            ctx.counters["engage:aim"] += 1
        blocked = any(mate.covers_crosshair(margin=40) for mate in belief.seen(ctx.now, ("teammate",), within=0.6))
        if blocked:
            ctx.counters["engage:hold_teammate"] += 1
        elif enemies and ctx.now >= self.next_burst and not motor.controller.busy_tapping:
            motor.fire(ctx.rng.uniform(0.2, 0.5))
            self.next_burst = ctx.now + ctx.rng.uniform(0.4, 0.9)
            ctx.counters["engage:burst"] += 1
            if ctx.rng.random() < 0.35:
                self.strafe = -self.strafe
        if belief.hit_confirmed.fresh(ctx.now, 0.5):
            ctx.counters["engage:hit_confirmed_tick"] += 1
        return Status.RUNNING

    def stop(self, ctx: Context) -> None:
        ctx.motor.aim(False, ctx.now)
        super().stop(ctx)


# ---------------------------------------------------------------------- travel


class Follow(Skill):
    """Go to the nearest teammate on the minimap."""

    name = "follow"
    timeout_s = 45.0
    NEAR = 22.0  # minimap pixels
    FAR = 55.0

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.nav = Navigator()
        self.best = math.inf
        self.best_at = ctx.now

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        ctx.wants.update({"minimap", "heading"})
        mate = belief.nearest_mate(ctx.now)
        if mate is None:
            return self.fail("NO_TEAMMATE")
        if mate.distance <= self.NEAR:
            return Status.SUCCESS
        if mate.distance < self.best - 3:
            self.best, self.best_at = mate.distance, ctx.now
        elif ctx.now - self.best_at > 8.0:
            self.best_at = ctx.now
            ctx.counters["follow:not_closing"] += 1
            self.nav.rung, self.nav.rung_at = 0, ctx.now
        if motor.pitch is not None and abs(motor.pitch) > 6 and not motor.controller.turning:
            motor.pitch_to(-ctx.rng.uniform(0, 4))
        result = self.nav.drive(ctx, belief.mate_turn(ctx.now), sprint=mate.distance > self.FAR)
        if result == "gave_up":
            return self.fail("UNREACHABLE")
        return Status.RUNNING


class Search(Skill):
    """Near the team with something missing: walk slowly, look down, scan for cards."""

    name = "search"
    timeout_s = 20.0
    # Degrees below the horizon while walking, and whether to break into a run.
    # Steeper than ~20° shows nothing but the ground right ahead.
    PITCH = (12, 18)
    SPRINT_SHARE = 0.0

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.nav = Navigator()
        self.next_look = ctx.now
        self.heading_goal: float | None = None

    def step(self, ctx: Context) -> Status:
        belief, motor = ctx.belief, ctx.motor
        if belief.current_prompt(ctx.now) is not None:
            return Status.SUCCESS  # the planner hands over to Loot
        if self.heading_goal is None and belief.heading.value is not None:
            base = float(belief.heading.value)
            options = [(base + offset) % 360 for offset in (0, 45, -45, 90, -90, 135, -135, 180)]
            mate = belief.nearest_mate(ctx.now)
            if mate is not None:
                # Stay around the team: head out on their side of us.
                options.sort(key=lambda h: abs(_signed(h - mate.bearing)))
            fresh = [h for h in options if not ctx.memory.is_blocked(h, ctx.now) and not ctx.memory.tried_near(self.name, h, ctx.now, 40)]
            self.heading_goal = fresh[0] if fresh else options[ctx.rng.randrange(len(options))]
            ctx.memory.note_attempt(self.name, self.heading_goal, ctx.now, "started")
            self.sprinting = ctx.rng.random() < self.SPRINT_SHARE
        low, high = self.PITCH
        if motor.pitch is not None and abs(motor.pitch + (low + high) / 2) > 8 and not motor.controller.turning:
            motor.pitch_to(-ctx.rng.uniform(low, high))
        error = None
        if self.heading_goal is not None and belief.heading.value is not None:
            error = _signed(self.heading_goal - float(belief.heading.value))
            # Look side to side while walking; the goal heading stays the same.
            error += 25 * math.sin((ctx.now - self.started) * 1.3)
        if self.nav.drive(ctx, error, sprint=getattr(self, "sprinting", False)) == "gave_up":
            return self.fail("BLOCKED")
        return Status.RUNNING


class Roam(Search):
    """Armed and nothing urgent: move around near the team, eyes up, picking up what shows.

    A player with a gun keeps moving with the squad, tops up ammo and heals on
    the way and looks around; standing in one spot is what a passenger does.
    """

    name = "roam"
    timeout_s = 25.0
    PITCH = (2, 10)
    SPRINT_SHARE = 0.35


class Wait(Skill):
    """Nothing worth doing: stand, look around a little, then let the planner choose again."""

    name = "wait"

    def __init__(self, ctx: Context) -> None:
        super().__init__(ctx)
        self.timeout_s = ctx.rng.uniform(2.0, 4.0)
        self.next_look = ctx.now

    def step(self, ctx: Context) -> Status:
        motor = ctx.motor
        motor.move()
        if ctx.now >= self.next_look and not motor.controller.turning:
            motor.turn(ctx.rng.uniform(-60, 60), ctx.rng.uniform(-5, 5), fine=True)
            self.next_look = ctx.now + ctx.rng.uniform(1.0, 2.5)
        if ctx.now - self.started >= self.timeout_s - 0.05:
            return Status.SUCCESS
        return Status.RUNNING

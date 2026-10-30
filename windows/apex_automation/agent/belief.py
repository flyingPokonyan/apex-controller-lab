"""What the agent believes about itself, built from many frames.

One frame can be wrong: the HUD hides during a banner, snow lights the
empty ammo digits, a red decal looks like an arrow, an orange pipe tints the
border like the storm. So nothing here flips on a single reading. Each fact
keeps the time it was last confirmed and is treated as unknown once stale.

The Memory half records what the agent tried and how it went, so a target
that failed is not tried again at once and a wall that blocked once is not
walked into again.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import math

from .perception import Mate, Observation, Prompt
from .space import SpaceView
from .tracking import Tracker


# A drop of this share of the bar between two reads is damage taken.
VITALITY_DROP = 0.03
SHIELD_DROP = 0.05
# Damage counts as "from a player" when an arrow showed within this window.
ARROW_WINDOW_S = 1.2
# Being shot stays true this long after the last hit.
UNDER_FIRE_HOLD_S = 4.0
STORM_ENTER = 0.35
STORM_EXIT = 0.2
STORM_EXIT_S = 3.0
BANNER_HOLD_S = 3.0
PROMPT_HOLD_S = 0.5
MATE_STALE_S = 10.0
# Each teammate keeps its colour all match; track them separately so one
# arrow missed for a frame does not make "nearest" jump to the other.
MATE_COLOURS = ((15, 24), (84, 94), (34, 41))
NO_AMMO_HOLD_S = 6.0
# A free-space reading older than this no longer describes where we are.
SPACE_STALE_S = 1.5
# Frames needed to change the weapon belief either way.
WEAPON_FRAMES = 3


@dataclass
class Fact:
    value: object = None
    at: float = -1e9

    def fresh(self, now: float, within: float) -> bool:
        return now - self.at <= within

    def set(self, value: object, now: float) -> None:
        self.value = value
        self.at = now


class Belief:
    def __init__(self) -> None:
        self.hud = Fact(False)
        self.armed = Fact(None)
        self._weapon_votes: deque[bool] = deque(maxlen=WEAPON_FRAMES)
        self.mag = Fact(None)
        self.reserve_left = Fact(None)
        self.guns = Fact(0)
        self.no_ammo_message = Fact(False)
        self.vitality = Fact(None)
        self.shield = Fact(None)
        self.last_damage = Fact(None)  # value: "player", "storm" or "other"
        self.under_fire_since: float | None = None
        self.last_arrow = Fact(None)  # value: bearing relative to the view, degrees
        self.hit_confirmed = Fact(False)
        self.knocked = Fact(False)
        self._knock_votes: deque[bool] = deque(maxlen=2)
        self.storm = Fact(False)
        self._storm_votes: deque[bool] = deque(maxlen=3)
        self._storm_clear_since: float | None = None
        self.ring_banner = Fact(False)
        self.prompt = Fact(None)
        self.mates = Fact(())
        self._tracked: dict[int, tuple[Mate, float]] = {}
        self.heading = Fact(None)  # compass degrees, from OCR plus turns since
        self.scene_change = Fact(None)
        self.detections = Fact(())
        self.tracker = Tracker()
        self.space = Fact(None)  # SpaceView
        self.frames = 0

    # ------------------------------------------------------------------ update

    def update(self, obs: Observation) -> None:
        now = obs.at
        self.frames += 1
        if obs.scene_change is not None:
            self.scene_change.set(obs.scene_change, now)
        if obs.knocked is not None:
            self._knock_votes.append(bool(obs.knocked))
            if len(self._knock_votes) == self._knock_votes.maxlen and len(set(self._knock_votes)) == 1:
                self.knocked.set(self._knock_votes[-1], now)
            elif self.knocked.value is None:
                self.knocked.set(False, now)
        if obs.ring_banner:
            self.ring_banner.set(True, now)
        if obs.storm_edge is not None:
            self._update_storm(obs.storm_edge, now)
        if obs.center_text is not None and ("没有弹药" in obs.center_text or "弹药不足" in obs.center_text):
            self.no_ammo_message.set(True, now)
        if obs.heading is not None:
            self.heading.set(float(obs.heading), now)
        if obs.detections is not None:
            self.detections.set(tuple(obs.detections), now)
            heading = self.heading.value if self.heading.fresh(now, 6.0) else None
            self.tracker.update(tuple(obs.detections), None if heading is None else float(heading), now)
        if obs.space is not None:
            heading = self.heading.value if self.heading.fresh(now, 6.0) else None
            self.space.set(SpaceView(None if heading is None else float(heading), tuple(obs.space)), now)
        if obs.mates is not None and (obs.mates or obs.minimap_self):
            # An empty list only counts when the minimap itself read fine.
            for mate in obs.mates:
                for index, (low, high) in enumerate(MATE_COLOURS):
                    if low <= mate.hue <= high:
                        previous = self._tracked.get(index)
                        if previous is None or previous[1] < now or mate.distance < previous[0].distance:
                            self._tracked[index] = (mate, now)
            live = [mate for mate, at in self._tracked.values() if now - at <= MATE_STALE_S]
            self.mates.set(tuple(sorted(live, key=lambda mate: mate.distance)), now)
        if obs.prompt_checked:
            if obs.prompt is not None:
                self.prompt.set(obs.prompt, now)
            elif not self.prompt.fresh(now, PROMPT_HOLD_S):
                self.prompt.set(None, now)
        if not obs.hud:
            return
        self.hud.set(True, now)
        if obs.weapon is not None:
            self._weapon_votes.append(bool(obs.weapon))
            if len(self._weapon_votes) == WEAPON_FRAMES and len(set(self._weapon_votes)) == 1:
                self.armed.set(self._weapon_votes[-1], now)
        if obs.weapon and obs.mag is not None:
            self.mag.set(obs.mag, now)
            if obs.mag > 0:
                self.no_ammo_message.set(False, now)
        if obs.guns is not None:
            self.guns.set(obs.guns, now)
        elif self.armed.value is False:
            self.guns.set(0, now)
        if obs.weapon and obs.reserve_left is not None:
            self.reserve_left.set(obs.reserve_left, now)
        if obs.arrow_bearing is not None:
            self.last_arrow.set(obs.arrow_bearing, now)
        if obs.hit_confirmed:
            self.hit_confirmed.set(True, now)
        self._update_damage(obs, now)

    def _update_storm(self, edge: float, now: float) -> None:
        self._storm_votes.append(edge >= STORM_ENTER)
        inside = bool(self.storm.value)
        if not inside:
            if sum(self._storm_votes) >= 2 or self.ring_banner.fresh(now, 0.5):
                self.storm.set(True, now)
                self._storm_clear_since = None
            return
        if edge < STORM_EXIT and not self.ring_banner.fresh(now, BANNER_HOLD_S):
            if self._storm_clear_since is None:
                self._storm_clear_since = now
            elif now - self._storm_clear_since >= STORM_EXIT_S:
                self.storm.set(False, now)
                self._storm_votes.clear()
        else:
            self._storm_clear_since = None
            self.storm.at = now

    def _update_damage(self, obs: Observation, now: float) -> None:
        dropped = obs.hurt_trail
        if obs.vitality is not None:
            previous = self.vitality.value
            if previous is not None and previous - obs.vitality >= VITALITY_DROP:
                dropped = True
            self.vitality.set(obs.vitality, now)
        if obs.shield is not None:
            previous = self.shield.value
            if previous is not None and previous - obs.shield >= SHIELD_DROP:
                dropped = True
            self.shield.set(obs.shield, now)
        if not dropped:
            if self.under_fire_since is not None and not self.last_damage.fresh(now, UNDER_FIRE_HOLD_S):
                self.under_fire_since = None
            return
        if self.last_arrow.fresh(now, ARROW_WINDOW_S):
            source = "player"
        elif self.storm.value:
            source = "storm"
        else:
            source = "other"
        self.last_damage.set(source, now)
        if source == "player" and self.under_fire_since is None:
            self.under_fire_since = now

    # ------------------------------------------------------------------ turns

    def turned(self, degrees: float) -> None:
        """Carry the heading through a turn the controller made since the last OCR."""
        if self.heading.value is not None:
            self.heading.value = (float(self.heading.value) + degrees) % 360

    # ------------------------------------------------------------------ queries

    def is_armed(self) -> bool:
        return bool(self.armed.value)

    def has_free_slot(self) -> bool:
        return not self.is_armed() or int(self.guns.value or 0) < 2

    def out_of_ammo(self, now: float) -> bool:
        if not self.is_armed():
            return False
        empty = self.mag.value == 0 and self.mag.fresh(now, 2.0)
        return empty and (self.no_ammo_message.fresh(now, NO_AMMO_HOLD_S) and bool(self.no_ammo_message.value)
                          or self.reserve_left.value is False)

    def mag_low(self, now: float) -> bool:
        value = self.mag.value
        return self.is_armed() and self.mag.fresh(now, 2.0) and isinstance(value, int) and value <= 2

    def under_fire(self, now: float) -> bool:
        return self.last_damage.value == "player" and self.last_damage.fresh(now, UNDER_FIRE_HOLD_S)

    def in_storm(self) -> bool:
        return bool(self.storm.value)

    def is_knocked(self) -> bool:
        return bool(self.knocked.value)

    def health_share(self) -> float | None:
        return None if self.vitality.value is None else float(self.vitality.value)

    def safe_for(self, now: float, seconds: float) -> bool:
        """No damage of any kind for ``seconds``."""
        return not self.last_damage.fresh(now, seconds)

    def current_prompt(self, now: float) -> Prompt | None:
        value = self.prompt.value
        return value if isinstance(value, Prompt) and self.prompt.fresh(now, PROMPT_HOLD_S) else None

    def _space_now(self, now: float) -> list[tuple[float, float]]:
        """Measured sectors as (degrees to turn from the current view, metres), known ones only."""
        view = self.space.value
        if view is None or not self.space.fresh(now, SPACE_STALE_S):
            return []
        shift = 0.0
        if view.heading is not None and self.heading.value is not None:
            shift = _signed(view.heading - float(self.heading.value))
        return [(yaw + shift, metres) for yaw, metres in view.sectors if metres is not None]

    def free_towards(self, now: float, turn: float, spread: float = 6.0) -> float | None:
        """Metres free in the direction ``turn`` degrees from the view, if measured."""
        near = [metres for yaw, metres in self._space_now(now) if abs(yaw - turn) <= spread]
        return min(near) if near else None

    def open_direction(self, now: float, prefer: float, need_m: float, max_dev: float = 70.0) -> float | None:
        """The measured direction closest to ``prefer`` with at least ``need_m`` metres free."""
        options = [yaw for yaw, metres in self._space_now(now) if metres >= need_m and abs(yaw - prefer) <= max_dev]
        return min(options, key=lambda yaw: abs(yaw - prefer), default=None)

    def more_open_side(self, now: float) -> int | None:
        """-1 left / 1 right: the half of the view with more room, when it clearly has more."""
        sectors = self._space_now(now)
        left = [min(metres, 10.0) for yaw, metres in sectors if yaw < -5]
        right = [min(metres, 10.0) for yaw, metres in sectors if yaw > 5]
        if not left or not right:
            return None
        difference = sum(right) / len(right) - sum(left) / len(left)
        return None if abs(difference) < 1.0 else (1 if difference > 0 else -1)

    def tracks(self, now: float, kinds: tuple[str, ...], within: float = 0.8) -> list:
        """Confirmed tracks (seen on two reads) of these kinds, nearest the crosshair first."""
        found = self.tracker.visible(now, kinds, within)
        return sorted(found, key=lambda t: abs(t.detection.yaw) + abs(t.detection.pitch))

    def seen(self, now: float, kinds: tuple[str, ...], within: float = 0.8) -> list:
        """Fresh raw detections of these kinds (one read is enough), nearest the crosshair first."""
        if not self.detections.fresh(now, within):
            return []
        found = [d for d in self.detections.value or () if d.kind in kinds]
        return sorted(found, key=lambda d: abs(d.yaw) + abs(d.pitch))

    def nearest_mate(self, now: float) -> Mate | None:
        if not self.mates.fresh(now, MATE_STALE_S):
            return None
        mates = self.mates.value or ()
        return mates[0] if mates else None

    def mate_turn(self, now: float) -> float | None:
        """Degrees to turn (positive right) to face the nearest teammate."""
        mate = self.nearest_mate(now)
        if mate is None or self.heading.value is None or not self.heading.fresh(now, 6.0):
            return None
        return _signed(mate.bearing - float(self.heading.value))


def _signed(degrees: float) -> float:
    value = math.fmod(degrees, 360.0)
    if value > 180:
        value -= 360
    elif value <= -180:
        value += 360
    return value


# ---------------------------------------------------------------------- memory


@dataclass
class Attempt:
    kind: str
    heading: float | None
    at: float
    result: str


@dataclass
class Memory:
    attempts: deque[Attempt] = field(default_factory=lambda: deque(maxlen=64))
    blocked: deque[tuple[float, float]] = field(default_factory=lambda: deque(maxlen=16))  # (heading, until)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    last_result: dict[str, tuple[str, str | None, float]] = field(default_factory=dict)

    def note_attempt(self, kind: str, heading: float | None, now: float, result: str) -> None:
        self.attempts.append(Attempt(kind, heading, now, result))

    def tried_near(self, kind: str, heading: float | None, now: float, within_s: float, spread: float = 25.0) -> bool:
        """Whether a target of this kind roughly in this direction was already tried."""
        if heading is None:
            return False
        for attempt in self.attempts:
            if attempt.kind != kind or now - attempt.at > within_s or attempt.heading is None:
                continue
            if abs(_signed(attempt.heading - heading)) <= spread:
                return True
        return False

    def block(self, heading: float | None, now: float, seconds: float = 20.0) -> None:
        if heading is not None:
            self.blocked.append((heading % 360, now + seconds))

    def is_blocked(self, heading: float | None, now: float, spread: float = 30.0) -> bool:
        if heading is None:
            return False
        return any(until > now and abs(_signed(blocked - heading)) <= spread for blocked, until in self.blocked)

    def cooling(self, goal: str, now: float) -> bool:
        return self.cooldown_until.get(goal, -1e9) > now

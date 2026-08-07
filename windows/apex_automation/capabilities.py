from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
from pathlib import Path
import random
from typing import Callable, Iterable, Literal


ActionClass = Literal["idempotent", "toggle", "commit"]
Trigger = Literal["onState", "periodic"]

ACTION_CLASSES: frozenset[str] = frozenset({"idempotent", "toggle", "commit"})
TRIGGERS: frozenset[str] = frozenset({"onState", "periodic"})
ACTION_KINDS: frozenset[str] = frozenset({"click", "key", "sequence", "clickText"})


@dataclass(frozen=True)
class CapabilityEvidence:
    """Text that must still be visible immediately before an action is sent."""

    region: str
    any_terms: tuple[str, ...] = ()
    all_terms: tuple[str, ...] = ()
    min_confidence: float = 0.65


@dataclass(frozen=True)
class Capability:
    """One thing the runner knows how to do, and the rules for doing it safely.

    A capability never decides *when* it runs. It only declares the screens it
    handles, what it sends, and what counts as done. Ordering between screens
    is not expressed anywhere: priority resolves a tie inside a single frame,
    never a sequence across frames.
    """

    id: str
    priority: int
    states: tuple[str, ...]
    action: str
    kind: str
    action_class: ActionClass
    trigger: Trigger = "onState"
    confirm_ms: int = 1500
    max_attempts: int = 1
    allowed_next_states: tuple[str, ...] = ()
    min_interval_ms: int = 0
    max_interval_ms: int = 0
    # How long this screen must have been on show before the capability may
    # act on it. Not a safety margin — recognition is already stable by the
    # time a state is reported — but a way to say "later is a different
    # outcome": the dropship is the same screen for its whole flight, and when
    # you leave it decides where you land.
    delay_ms: int = 0
    delay_jitter_ms: int = 0
    # How long a key stays down. Left at 0 the runner uses the profile's
    # default tap. Some prompts do not accept a tap at all, so this has to be
    # per-capability rather than one number for the whole session.
    hold_ms: int = 0
    # A fallback is never an alternative interpretation of a frame. It may run
    # only after the named capability for the current state has spent its
    # budget on the same visit, and only while current-frame evidence remains.
    fallback_for: tuple[str, ...] = ()
    evidence: CapabilityEvidence | None = None

    def __post_init__(self) -> None:
        if not self.states:
            raise ValueError(f"能力 {self.id} 没有声明任何可处理画面")
        if self.action_class not in ACTION_CLASSES:
            raise ValueError(f"能力 {self.id} 的动作分级非法：{self.action_class}")
        if self.trigger not in TRIGGERS:
            raise ValueError(f"能力 {self.id} 的触发方式非法：{self.trigger}")
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"能力 {self.id} 的动作类型非法：{self.kind}")
        if self.hold_ms and self.kind != "key":
            raise ValueError(f"只有按键动作可以设置按住时长：{self.id}")
        if not 0 <= self.hold_ms <= 2000:
            raise ValueError(f"能力 {self.id} 的按住时长必须为 0 到 2000ms")

        if self.fallback_for:
            if self.trigger != "onState" or self.action_class != "idempotent":
                raise ValueError(f"兜底能力必须是 onState idempotent：{self.id}")
            if self.max_attempts != 1:
                raise ValueError(f"兜底能力只允许一次尝试：{self.id}")
            if self.kind == "key" and self.evidence is None:
                raise ValueError(f"键盘兜底能力必须带文字证据：{self.id}")
            if self.kind not in {"key", "clickText"}:
                raise ValueError(f"兜底能力只允许 key 或 clickText：{self.id}")
            if self.kind == "clickText" and self.evidence is not None:
                raise ValueError(f"clickText 兜底使用动作自身的 OCR 证据：{self.id}")
        elif self.evidence is not None:
            raise ValueError(f"只有显式兜底能力可以声明动作证据：{self.id}")

        if self.evidence is not None:
            if not self.evidence.region:
                raise ValueError(f"兜底能力没有声明证据区域：{self.id}")
            if not self.evidence.any_terms and not self.evidence.all_terms:
                raise ValueError(f"兜底能力没有声明任何文字证据：{self.id}")
            if not 0 <= self.evidence.min_confidence <= 1:
                raise ValueError(f"兜底能力的证据置信度无效：{self.id}")

        # A toggle undoes itself when repeated, so a retry without positive
        # evidence is never safe. LCTRL on the dropship is the example that
        # motivates this: pressing it twice rejoins the squad.
        if self.action_class == "toggle" and self.max_attempts != 1:
            raise ValueError(f"toggle 动作只允许一次尝试：{self.id}")
        if self.action_class == "toggle" and not self.allowed_next_states:
            raise ValueError(f"toggle 动作必须声明正向后置画面：{self.id}")

        # A commit costs something or starts something (queueing a match,
        # picking a mode). It must be verifiable and must not be retried
        # indefinitely.
        if self.action_class == "commit":
            if not self.allowed_next_states:
                raise ValueError(f"commit 动作必须声明正向后置画面：{self.id}")
            if not 1 <= self.max_attempts <= 2:
                raise ValueError(f"commit 动作的尝试上限必须为 1 或 2：{self.id}")

        if not 0 <= self.delay_ms <= 120_000:
            raise ValueError(f"能力 {self.id} 的延迟必须为 0 到 120000ms")
        if not 0 <= self.delay_jitter_ms <= 60_000:
            raise ValueError(f"能力 {self.id} 的延迟抖动必须为 0 到 60000ms")
        if self.delay_jitter_ms and not self.delay_ms:
            raise ValueError(f"能力 {self.id} 声明了抖动却没有延迟")

        if self.trigger == "periodic":
            # Periodic actions fire while a screen merely persists, so there is
            # no postcondition to check and nothing stops them repeating.
            # Restricting them to idempotent keeps that safe by construction.
            if self.action_class != "idempotent":
                raise ValueError(f"周期触发只允许 idempotent 动作：{self.id}")
            if self.delay_ms:
                raise ValueError(f"周期触发用间隔表达节奏，不该再声明延迟：{self.id}")
            if self.allowed_next_states:
                raise ValueError(f"周期触发不应声明后置画面：{self.id}")
            if not 0 < self.min_interval_ms <= self.max_interval_ms:
                raise ValueError(f"周期触发的间隔区间非法：{self.id}")
        else:
            if self.min_interval_ms or self.max_interval_ms:
                raise ValueError(f"状态触发不应声明周期间隔：{self.id}")
            if not 1 <= self.max_attempts <= 8:
                raise ValueError(f"能力 {self.id} 的尝试上限必须为 1 到 8")
            if not 500 <= self.confirm_ms <= 30_000:
                raise ValueError(f"能力 {self.id} 的确认时间必须为 500 到 30000ms")


@dataclass
class PendingAction:
    capability: Capability
    origin_state: str
    origin_observation_version: int
    attempt: int
    retry_at: float


@dataclass(frozen=True)
class Decision:
    """What the dispatcher wants done with the current frame."""

    kind: Literal["fire", "wait", "pause"]
    capability: Capability | None = None
    attempt: int = 0
    reason: str = ""
    detail: dict[str, object] = field(default_factory=dict)


class CapabilitySet:
    def __init__(self, capabilities: Iterable[Capability]) -> None:
        items = list(capabilities)
        seen: set[str] = set()
        for capability in items:
            if capability.id in seen:
                raise ValueError(f"能力 id 重复：{capability.id}")
            seen.add(capability.id)
        by_id = {capability.id: capability for capability in items}
        fallback_parents: set[str] = set()
        for capability in items:
            if not capability.fallback_for:
                continue
            parents: list[Capability] = []
            for parent_id in capability.fallback_for:
                parent = by_id.get(parent_id)
                if parent is None:
                    raise ValueError(
                        f"兜底能力 {capability.id} 引用了不存在的能力 {parent_id}"
                    )
                if parent.fallback_for:
                    raise ValueError(f"兜底能力不能继续串联兜底：{capability.id}")
                if parent.trigger != "onState" or parent.action_class != "idempotent":
                    raise ValueError(
                        f"兜底能力的主能力必须是 onState idempotent：{capability.id}"
                    )
                if capability.priority >= parent.priority:
                    raise ValueError(f"兜底能力优先级必须低于主能力：{capability.id}")
                if parent.id in fallback_parents:
                    raise ValueError(f"一个主能力只能声明一个兜底：{parent.id}")
                fallback_parents.add(parent.id)
                parents.append(parent)
            for state in capability.states:
                matches = [parent for parent in parents if state in parent.states]
                if len(matches) != 1:
                    raise ValueError(
                        f"兜底能力 {capability.id} 的画面 {state} "
                        "必须恰好对应一个主能力"
                    )
        # Highest priority first; ties broken by id so arbitration is stable
        # across runs and reorderings of the config file.
        self.capabilities = tuple(sorted(items, key=lambda item: (-item.priority, item.id)))
        self.by_id = by_id

    def for_state(self, state: str) -> tuple[Capability, ...]:
        return tuple(item for item in self.capabilities if state in item.states)

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> "CapabilitySet":
        if int(payload.get("schemaVersion", 0)) != 1:
            raise ValueError("能力字典 schemaVersion 必须为 1")
        return cls(
            Capability(
                id=str(item["id"]),
                priority=int(item["priority"]),
                states=tuple(str(value) for value in item["states"]),
                action=str(item["action"]),
                kind=str(item["kind"]),
                action_class=str(item["actionClass"]),  # type: ignore[arg-type]
                trigger=str(item.get("trigger", "onState")),  # type: ignore[arg-type]
                confirm_ms=int(item.get("confirmMs", 1500)),
                max_attempts=int(item.get("maxAttempts", 1)),
                allowed_next_states=tuple(str(value) for value in item.get("allowedNextStates", [])),
                min_interval_ms=int(item.get("minIntervalMs", 0)),
                max_interval_ms=int(item.get("maxIntervalMs", 0)),
                delay_ms=int(item.get("delayMs", 0)),
                delay_jitter_ms=int(item.get("delayJitterMs", 0)),
                hold_ms=int(item.get("holdMs", 0)),
                fallback_for=(
                    ()
                    if item.get("fallbackFor") is None
                    else (
                        (str(item["fallbackFor"]),)
                        if isinstance(item["fallbackFor"], str)
                        else tuple(str(value) for value in item["fallbackFor"])
                    )
                ),
                evidence=(
                    None
                    if item.get("evidence") is None
                    else CapabilityEvidence(
                        region=str(item["evidence"]["region"]),
                        any_terms=tuple(
                            str(value) for value in item["evidence"].get("any", [])
                        ),
                        all_terms=tuple(
                            str(value) for value in item["evidence"].get("all", [])
                        ),
                        min_confidence=float(
                            item["evidence"].get("minConfidence", 0.65)
                        ),
                    )
                ),
            )
            for item in payload.get("capabilities", [])
        )

    @classmethod
    def from_path(cls, path: Path | str) -> "CapabilitySet":
        return cls.from_payload(json.loads(Path(path).read_text(encoding="utf-8")))


class CapabilityDispatcher:
    """Chooses one capability per frame, and refuses to keep going in circles.

    There is deliberately no notion of "which step are we on". The dispatcher
    holds only what a capability cannot observe from a single frame: whether an
    action is still awaiting its postcondition, how many times it has been
    tried on this visit, and whether the session is looping.
    """

    def __init__(
        self,
        capabilities: CapabilitySet,
        *,
        cycle_window_s: float = 180.0,
        cycle_threshold: int = 4,
        jitter: Callable[[int, int], int] = random.randint,
    ) -> None:
        self.capabilities = capabilities
        self.cycle_window_s = cycle_window_s
        self.cycle_threshold = cycle_threshold
        self.jitter = jitter

        self.pending: PendingAction | None = None
        self.attempts: dict[str, int] = {}
        self.blocked_observation_version: int | None = None
        self.handled_observation_versions: set[int] = set()
        self.next_periodic_at: dict[str, float] = {}
        self.delay_deadlines: dict[str, float] = {}
        self._state_entries: deque[tuple[float, str]] = deque()
        self._current_state: str | None = None
        self._current_state_since = 0.0
        self._cycle_paused = False

    def reset_for_pause(self) -> None:
        """Drop cross-frame memory that a pause invalidates."""
        self.pending = None
        self.blocked_observation_version = None
        # The operator may have flown the dropship somewhere else entirely.
        self.delay_deadlines.clear()

    def counts_toward_cycle(self, state: str) -> bool:
        """Whether revisiting this screen is evidence of a loop.

        A cycle means *the runner's own actions* are not making progress, so
        only screens it tries to advance past can form one. Screens it never
        acts on are ordinary gameplay rhythm however often they recur, and
        periodic screens are excluded for the same reason: repeating melee
        while a match runs is the intended behaviour, not a loop.

        `20260730-215512` is why this distinction exists. Dying while the
        squad lived entered `SPECTATING` five times in 145s; that latched the
        session-wide pause, and the match summary that arrived 56s later went
        unhandled for another 78 seconds — `TAB` was on screen the whole time.
        """
        return any(
            capability.trigger == "onState"
            for capability in self.capabilities.for_state(state)
        )

    def _age_entries(self, now: float) -> None:
        """Drop entries the cycle window has moved past.

        Called on every decision rather than only on a screen change. Ageing
        used to happen inside `note_state`, which returns early while the
        screen is unchanged — so a latched pause could outlive its own
        evidence indefinitely. `20260730-225411` did exactly that: the latch
        closed at 226.8s and the screen then went unrecognised until the run
        ended, leaving the runner inert for **704 seconds** with entries that
        should have expired at 406s.
        """
        cutoff = now - self.cycle_window_s
        while self._state_entries and self._state_entries[0][0] < cutoff:
            self._state_entries.popleft()

    def note_state(self, state: str | None, now: float) -> None:
        """Record a screen change so attempt budgets and cycles stay honest."""
        if state == self._current_state:
            return
        previous = self._current_state
        self._current_state = state
        self._current_state_since = now
        # A delay is measured per visit: leaving the dropship and boarding the
        # next one starts the wait again from the top. A frame nobody could
        # name is not evidence of having left, though — over a 35 second wait
        # a single missed OCR read would otherwise restart the clock, and
        # enough of them would ride the flight to its forced drop.
        if state is not None:
            for capability in self.capabilities.capabilities:
                if state not in capability.states:
                    self.delay_deadlines.pop(capability.id, None)
        if previous is not None:
            # Leaving a screen ends that visit: a later return gets a fresh
            # budget, which is what makes "handle whatever shows up" workable.
            for capability in self.capabilities.for_state(previous):
                self.attempts.pop(capability.id, None)
        self._age_entries(now)
        if state is None or not self.counts_toward_cycle(state):
            return
        self._state_entries.append((now, state))

    def cycle_count(self, state: str) -> int:
        return sum(1 for _, entry in self._state_entries if entry == state)

    def confirm_pending(self, evidence_state: str) -> tuple[bool, PendingAction | None]:
        """Settle an outstanding action against the screen that followed it."""
        pending = self.pending
        if pending is None:
            return True, None
        self.pending = None
        allowed = pending.capability.allowed_next_states
        if allowed and evidence_state not in allowed:
            return False, pending
        return True, pending

    def delay_remaining(self, capability: Capability, now: float) -> float:
        """Seconds this capability still has to wait on the current screen."""
        if not capability.delay_ms:
            return 0.0
        deadline = self.delay_deadlines.get(capability.id)
        if deadline is None:
            # Rolled once per visit, not per frame: the wait has to be a fixed
            # point in time, and re-rolling it every 300ms would average the
            # jitter away into always leaving at the same moment — which is
            # the thing the jitter exists to stop.
            extra = (
                self.jitter(0, capability.delay_jitter_ms)
                if capability.delay_jitter_ms
                else 0
            )
            deadline = self._current_state_since + (capability.delay_ms + extra) / 1000
            self.delay_deadlines[capability.id] = deadline
        return max(0.0, deadline - now)

    def _periodic_decision(self, capability: Capability, now: float) -> Decision:
        due_at = self.next_periodic_at.get(capability.id)
        if due_at is not None and now < due_at:
            return Decision("wait", reason="PERIODIC_COOLDOWN", capability=capability)
        interval = self.jitter(capability.min_interval_ms, capability.max_interval_ms)
        self.next_periodic_at[capability.id] = now + interval / 1000
        return Decision(
            "fire",
            capability=capability,
            attempt=1,
            reason="PERIODIC",
            detail={"intervalMs": interval},
        )

    def decide(self, state: str | None, observation_version: int, now: float) -> Decision:
        self._age_entries(now)
        if state is None:
            return Decision("wait", reason="NO_STATE")

        # A loop alternates between screens, so latch on the session rather
        # than on one state; otherwise an A-B-A-B cycle re-reports itself on
        # every alternation. The latch clears once the window has aged out,
        # which is also the only honest signal that the loop actually stopped.
        looping = any(
            self.cycle_count(entry) > self.cycle_threshold
            for _, entry in self._state_entries
        )
        if looping:
            if not self._cycle_paused:
                self._cycle_paused = True
                self.pending = None
                return Decision(
                    "pause",
                    reason="CYCLE_DETECTED",
                    detail={
                        "state": state,
                        "entries": self.cycle_count(state),
                        "windowS": self.cycle_window_s,
                    },
                )
            return Decision("wait", reason="CYCLE_PAUSED")
        self._cycle_paused = False

        if self.pending is not None:
            if now < self.pending.retry_at:
                return Decision("wait", reason="AWAITING_POSTCONDITION")
            retry = self.pending
            self.pending = None
            # A retry is only a retry on a screen the capability still owns.
            # Pressing 准备 is followed by a minute of legend select and
            # loading that no rule names, so an unconditional retry would come
            # back long after the fact and click the lobby button at a point
            # where those coordinates mean something else entirely.
            if (
                state in retry.capability.states
                and self.delay_remaining(retry.capability, now) <= 0
            ):
                attempts = self.attempts.get(retry.capability.id, 0)
                if attempts < retry.capability.max_attempts:
                    return self._start(retry.capability, state, observation_version, now)

        candidates = self.capabilities.for_state(state)
        if not candidates:
            return Decision("wait", reason="NO_CAPABILITY")

        periodic = [item for item in candidates if item.trigger == "periodic"]
        on_state = [item for item in candidates if item.trigger == "onState"]

        def fallback_parent(item: Capability) -> Capability | None:
            if not item.fallback_for:
                return None
            return next(
                (
                    self.capabilities.by_id[parent_id]
                    for parent_id in item.fallback_for
                    if state in self.capabilities.by_id[parent_id].states
                ),
                None,
            )

        def eligible(item: Capability) -> bool:
            attempts = self.attempts.get(item.id, 0)
            if attempts >= item.max_attempts:
                return False
            parent = fallback_parent(item)
            if parent is None:
                return True
            return self.attempts.get(parent.id, 0) >= parent.max_attempts

        eligible_on_state = [item for item in on_state if eligible(item)]
        waiting = [
            item for item in eligible_on_state if self.delay_remaining(item, now) > 0
        ]
        ready = [item for item in eligible_on_state if self.delay_remaining(item, now) <= 0]

        # A screen that has already been acted on still lets periodic
        # behaviours run: they are what "keep doing this while in a match"
        # means, and they carry no postcondition to wait for.
        if ready:
            capability = ready[0]
            if (
                observation_version not in self.handled_observation_versions
                or bool(capability.fallback_for)
            ):
                return self._start(
                    capability,
                    state,
                    observation_version,
                    now,
                    reason=(
                        "ON_STATE"
                        if not capability.fallback_for
                        else "FALLBACK_AFTER_EXHAUSTED"
                    ),
                )
        if waiting and not ready and not periodic:
            return Decision(
                "wait",
                capability=waiting[0],
                reason="ACTION_DELAYED",
                detail={
                    "state": state,
                    "remainingMs": round(self.delay_remaining(waiting[0], now) * 1000),
                },
            )
        for capability in periodic:
            # Each periodic behaviour keeps its own interval, so the highest
            # priority one being mid-cooldown must not silence the others.
            # Melee repeats every few seconds and a tactical ability every ten;
            # ranking them would mean only the faster one ever fired.
            decision = self._periodic_decision(capability, now)
            if decision.kind == "fire":
                return decision
        if periodic:
            return Decision("wait", reason="PERIODIC_COOLDOWN", capability=periodic[0])

        if self.blocked_observation_version == observation_version:
            return Decision("wait", reason="OBSERVATION_BLOCKED")
        exhausted = [
            item
            for item in on_state
            if self.attempts.get(item.id, 0) >= item.max_attempts
        ]
        if exhausted:
            terminal = exhausted[-1]
            self.blocked_observation_version = observation_version
            return Decision(
                "pause",
                capability=terminal,
                attempt=self.attempts.get(terminal.id, 0),
                reason="ATTEMPTS_EXHAUSTED",
                detail={
                    "state": state,
                    "action": terminal.action,
                    "fallbackFor": list(terminal.fallback_for),
                },
            )
        return Decision("wait", reason="ALREADY_HANDLED")

    def _start(
        self,
        capability: Capability,
        state: str,
        observation_version: int,
        now: float,
        *,
        reason: str = "ON_STATE",
    ) -> Decision:
        attempts = self.attempts.get(capability.id, 0)
        if attempts >= capability.max_attempts:
            self.blocked_observation_version = observation_version
            return Decision(
                "pause",
                capability=capability,
                attempt=attempts,
                reason="ATTEMPTS_EXHAUSTED",
                detail={"state": state, "action": capability.action},
            )
        attempt = attempts + 1
        self.attempts[capability.id] = attempt
        self.handled_observation_versions.add(observation_version)
        self.pending = PendingAction(
            capability=capability,
            origin_state=state,
            origin_observation_version=observation_version,
            attempt=attempt,
            retry_at=now + capability.confirm_ms / 1000,
        )
        return Decision(
            "fire",
            capability=capability,
            attempt=attempt,
            reason=reason,
            detail=(
                {}
                if not capability.fallback_for
                else {
                    "fallbackFor": next(
                        parent_id
                        for parent_id in capability.fallback_for
                        if state in self.capabilities.by_id[parent_id].states
                    )
                }
            ),
        )

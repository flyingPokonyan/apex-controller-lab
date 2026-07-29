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
ACTION_KINDS: frozenset[str] = frozenset({"click", "key", "sequence"})


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

    def __post_init__(self) -> None:
        if not self.states:
            raise ValueError(f"能力 {self.id} 没有声明任何可处理画面")
        if self.action_class not in ACTION_CLASSES:
            raise ValueError(f"能力 {self.id} 的动作分级非法：{self.action_class}")
        if self.trigger not in TRIGGERS:
            raise ValueError(f"能力 {self.id} 的触发方式非法：{self.trigger}")
        if self.kind not in ACTION_KINDS:
            raise ValueError(f"能力 {self.id} 的动作类型非法：{self.kind}")

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

        if self.trigger == "periodic":
            # Periodic actions fire while a screen merely persists, so there is
            # no postcondition to check and nothing stops them repeating.
            # Restricting them to idempotent keeps that safe by construction.
            if self.action_class != "idempotent":
                raise ValueError(f"周期触发只允许 idempotent 动作：{self.id}")
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
        # Highest priority first; ties broken by id so arbitration is stable
        # across runs and reorderings of the config file.
        self.capabilities = tuple(sorted(items, key=lambda item: (-item.priority, item.id)))

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
        self._state_entries: deque[tuple[float, str]] = deque()
        self._current_state: str | None = None
        self._cycle_paused = False

    def reset_for_pause(self) -> None:
        """Drop cross-frame memory that a pause invalidates."""
        self.pending = None
        self.blocked_observation_version = None

    def note_state(self, state: str | None, now: float) -> None:
        """Record a screen change so attempt budgets and cycles stay honest."""
        if state == self._current_state:
            return
        previous = self._current_state
        self._current_state = state
        if previous is not None:
            # Leaving a screen ends that visit: a later return gets a fresh
            # budget, which is what makes "handle whatever shows up" workable.
            for capability in self.capabilities.for_state(previous):
                self.attempts.pop(capability.id, None)
        if state is None:
            return
        self._state_entries.append((now, state))
        cutoff = now - self.cycle_window_s
        while self._state_entries and self._state_entries[0][0] < cutoff:
            self._state_entries.popleft()

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

        if self.blocked_observation_version == observation_version:
            return Decision("wait", reason="OBSERVATION_BLOCKED")

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
            if state in retry.capability.states:
                return self._start(retry.capability, state, observation_version, now)

        candidates = self.capabilities.for_state(state)
        if not candidates:
            return Decision("wait", reason="NO_CAPABILITY")

        periodic = [item for item in candidates if item.trigger == "periodic"]
        on_state = [item for item in candidates if item.trigger == "onState"]

        # A screen that has already been acted on still lets periodic
        # behaviours run: they are what "keep doing this while in a match"
        # means, and they carry no postcondition to wait for.
        if on_state and observation_version not in self.handled_observation_versions:
            return self._start(on_state[0], state, observation_version, now)
        if periodic:
            return self._periodic_decision(periodic[0], now)
        return Decision("wait", reason="ALREADY_HANDLED")

    def _start(
        self,
        capability: Capability,
        state: str,
        observation_version: int,
        now: float,
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
        return Decision("fire", capability=capability, attempt=attempt, reason="ON_STATE")

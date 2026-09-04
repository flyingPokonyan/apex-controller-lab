from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from .progression import LobbyProgressionReading


class ProgressionStatus(str, Enum):
    CONFIRMED = "CONFIRMED"
    FAILED = "FAILED"


class ProgressionDecision(str, Enum):
    CONTINUE_PLAY = "CONTINUE_PLAY"
    TARGET_REACHED = "TARGET_REACHED"
    PAUSE_UNCERTAIN = "PAUSE_UNCERTAIN"
    DEFER_UNTIL_SAFE_LOBBY = "DEFER_UNTIL_SAFE_LOBBY"


@dataclass(frozen=True)
class ProgressionOutcome:
    status: ProgressionStatus
    reading: LobbyProgressionReading | None
    attempts: int
    error: str | None = None

    @classmethod
    def confirmed(
        cls,
        reading: LobbyProgressionReading,
        *,
        attempts: int,
    ) -> "ProgressionOutcome":
        if not reading.is_complete:
            raise ValueError("确认的大厅进度必须包含完整等级和经验值")
        return cls(
            status=ProgressionStatus.CONFIRMED,
            reading=reading,
            attempts=attempts,
        )

    @classmethod
    def failed(
        cls,
        reading: LobbyProgressionReading | None,
        *,
        attempts: int,
        error: str,
    ) -> "ProgressionOutcome":
        return cls(
            status=ProgressionStatus.FAILED,
            reading=reading,
            attempts=attempts,
            error=error,
        )


@dataclass(frozen=True)
class ProgressionContext:
    observed_state: str | None
    safe_lobby: bool
    queueing: bool
    overlay_clear: bool
    pending_action: bool
    foreground: bool
    lease_current: bool = True
    ring_progress: int | None = None
    ring_target: int | None = None


class ProgressionPolicy(Protocol):
    def decide(
        self,
        outcome: ProgressionOutcome,
        context: ProgressionContext,
    ) -> ProgressionDecision: ...


class ContinuePlayPolicy:
    """Manual play keeps the existing best-effort progression behavior."""

    def decide(
        self,
        outcome: ProgressionOutcome,
        context: ProgressionContext,
    ) -> ProgressionDecision:
        return ProgressionDecision.CONTINUE_PLAY


class TargetLevelPolicy:
    """Fail closed and stop only after the target is proven in a safe lobby."""

    def __init__(self, target_level: int) -> None:
        if target_level < 1:
            raise ValueError("目标等级必须大于 0")
        self.target_level = target_level

    def decide(
        self,
        outcome: ProgressionOutcome,
        context: ProgressionContext,
    ) -> ProgressionDecision:
        if not context.lease_current:
            return ProgressionDecision.PAUSE_UNCERTAIN
        if outcome.status is ProgressionStatus.FAILED:
            return ProgressionDecision.PAUSE_UNCERTAIN

        reading = outcome.reading
        if reading is None or not reading.is_complete or reading.level is None:
            return ProgressionDecision.PAUSE_UNCERTAIN
        if reading.level < self.target_level:
            return ProgressionDecision.CONTINUE_PLAY
        if (
            not context.safe_lobby
            or context.queueing
            or not context.overlay_clear
            or context.pending_action
            or not context.foreground
        ):
            return ProgressionDecision.DEFER_UNTIL_SAFE_LOBBY
        return ProgressionDecision.TARGET_REACHED


class TargetLevelAndRingPolicy(TargetLevelPolicy):
    """Require the ring target only after that optional objective was observed."""

    def __init__(
        self,
        target_level: int,
        target_ring: int = 30,
        *,
        observed_ring_progress: int | None = None,
        observed_ring_target: int | None = None,
    ) -> None:
        super().__init__(target_level)
        if target_ring < 1:
            raise ValueError("目标缩圈次数必须大于 0")
        if (observed_ring_progress is None) != (observed_ring_target is None):
            raise ValueError("历史缩圈进度必须同时包含当前值和目标值")
        if observed_ring_progress is not None and (
            observed_ring_progress < 0
            or observed_ring_target is None
            or observed_ring_target < 1
            or observed_ring_progress > observed_ring_target
        ):
            raise ValueError("历史缩圈进度无效")
        self.target_ring = target_ring
        self.observed_ring_progress = observed_ring_progress
        self.observed_ring_target = observed_ring_target

    def decide(
        self,
        outcome: ProgressionOutcome,
        context: ProgressionContext,
    ) -> ProgressionDecision:
        if not context.lease_current:
            return ProgressionDecision.PAUSE_UNCERTAIN
        if outcome.status is ProgressionStatus.FAILED:
            return ProgressionDecision.PAUSE_UNCERTAIN

        reading = outcome.reading
        if reading is None or not reading.is_complete or reading.level is None:
            return ProgressionDecision.PAUSE_UNCERTAIN
        if reading.level < self.target_level:
            return ProgressionDecision.CONTINUE_PLAY
        ring_progress = (
            context.ring_progress
            if context.ring_progress is not None
            else self.observed_ring_progress
        )
        ring_target = (
            context.ring_target
            if context.ring_progress is not None
            else self.observed_ring_target
        )
        if ring_progress is not None and (
            ring_target != self.target_ring
            or ring_progress < self.target_ring
        ):
            return ProgressionDecision.CONTINUE_PLAY
        if (
            not context.safe_lobby
            or context.queueing
            or not context.overlay_clear
            or context.pending_action
            or not context.foreground
        ):
            return ProgressionDecision.DEFER_UNTIL_SAFE_LOBBY
        return ProgressionDecision.TARGET_REACHED

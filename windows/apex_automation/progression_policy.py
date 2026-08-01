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

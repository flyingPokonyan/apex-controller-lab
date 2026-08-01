from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
import unicodedata

import numpy as np

from .ocr_obstacles import OcrProvider, OcrToken, Region


DEFAULT_LEVEL_REGION = Region(
    name="lobbyLevel",
    roi=(36, 27, 76, 76),
    single_line=True,
)
DEFAULT_XP_REGION = Region(
    name="lobbyXp",
    roi=(200, 54, 356, 91),
    single_line=True,
)

_LEVEL_PATTERN = re.compile(
    r"(?:(?:LV|LEVEL|等级)[.:：]?)?([0-9]{1,3})",
    re.IGNORECASE,
)
_XP_PAIR_PATTERN = re.compile(r"([^/]+)/([^/]+)")
_K_AMOUNT_PATTERN = re.compile(r"(?:0|[1-9][0-9]{0,2})(?:\.[0-9]{1,3})?K")
_INTEGER_AMOUNT_PATTERN = re.compile(r"(?:0|[1-9][0-9]*|[1-9][0-9]{0,2}(?:,[0-9]{3})+)")


def _normalized_text(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).split())


def parse_level(raw_text: str) -> int | None:
    """Parse an explicit lobby level without correcting OCR lookalikes."""

    match = _LEVEL_PATTERN.fullmatch(_normalized_text(raw_text))
    if match is None:
        return None
    level = int(match.group(1))
    return level if 1 <= level <= 999 else None


def _parse_xp_amount(raw_text: str) -> int | None:
    value = raw_text.upper()
    if _K_AMOUNT_PATTERN.fullmatch(value):
        try:
            scaled = Decimal(value[:-1]) * 1000
        except InvalidOperation:
            return None
        if scaled != scaled.to_integral_value():
            return None
        return int(scaled)
    if _INTEGER_AMOUNT_PATTERN.fullmatch(value):
        return int(value.replace(",", ""))
    return None


def parse_xp_pair(raw_text: str) -> tuple[int, int] | None:
    """Parse explicit current/required XP values into approximate integers."""

    match = _XP_PAIR_PATTERN.fullmatch(_normalized_text(raw_text))
    if match is None:
        return None
    current = _parse_xp_amount(match.group(1))
    required = _parse_xp_amount(match.group(2))
    if current is None or required is None:
        return None
    if required <= 0 or current < 0 or current > required:
        return None
    return current, required


@dataclass(frozen=True)
class ProgressionEvidence:
    raw_text: str
    confidence: float
    tokens: tuple[OcrToken, ...]


@dataclass(frozen=True)
class LobbyProgressionReading:
    level: int | None
    xp_current_approx: int | None
    xp_required_approx: int | None
    level_evidence: ProgressionEvidence
    xp_evidence: ProgressionEvidence
    error: str | None = None

    @property
    def is_complete(self) -> bool:
        return (
            self.error is None
            and self.level is not None
            and self.xp_current_approx is not None
            and self.xp_required_approx is not None
        )

    @property
    def raw_text(self) -> str:
        return self.xp_evidence.raw_text

    @property
    def confidence(self) -> float:
        if not self.is_complete:
            return 0.0
        return min(self.level_evidence.confidence, self.xp_evidence.confidence)

    @property
    def read_status(self) -> str:
        return "OK" if self.is_complete else "FAILED"

    def as_event_payload(
        self,
        reason: str,
        *,
        changed: bool = False,
        delta_approx: int | None = None,
    ) -> dict[str, object]:
        if self.is_complete:
            level = self.level
            xp_current = self.xp_current_approx
            xp_required = self.xp_required_approx
        else:
            # A partially parsed frame is evidence, not an account snapshot.
            level = None
            xp_current = None
            xp_required = None
            changed = False
            delta_approx = None
        return {
            "reason": reason,
            "level": level,
            "xpCurrentApprox": xp_current,
            "xpRequiredApprox": xp_required,
            "rawText": self.raw_text,
            "levelRawText": self.level_evidence.raw_text,
            "xpRawText": self.xp_evidence.raw_text,
            "confidence": round(self.confidence, 4),
            "levelConfidence": round(self.level_evidence.confidence, 4),
            "xpConfidence": round(self.xp_evidence.confidence, 4),
            "changed": changed,
            "deltaApprox": delta_approx,
            "readStatus": self.read_status,
        }


def _evidence(tokens: tuple[OcrToken, ...]) -> ProgressionEvidence:
    raw_text = " ".join(token.text.strip() for token in tokens if token.text.strip())
    confidence = min((token.confidence for token in tokens), default=0.0)
    return ProgressionEvidence(
        raw_text=raw_text,
        confidence=float(confidence),
        tokens=tokens,
    )


def _evidence_problem(
    label: str,
    evidence: ProgressionEvidence,
    provider_error: str | None,
    min_confidence: float,
) -> str | None:
    if provider_error is not None:
        return f"{label} OCR 失败: {provider_error}"
    if not evidence.raw_text:
        return f"{label} OCR 没有文字"
    if not 0 <= evidence.confidence <= 1:
        return f"{label} OCR 置信度无效"
    if evidence.confidence < min_confidence:
        return f"{label} OCR 置信度不足"
    return None


class LobbyProgressionReader:
    """Read the fixed 2560x1440 lobby level and XP regions once."""

    def __init__(
        self,
        provider: OcrProvider,
        *,
        level_region: Region = DEFAULT_LEVEL_REGION,
        xp_region: Region = DEFAULT_XP_REGION,
        min_confidence: float = 0.65,
    ) -> None:
        if not 0 <= min_confidence <= 1:
            raise ValueError("等级经验 OCR 置信度必须在 0 到 1 之间")
        self.provider = provider
        self.level_region = level_region
        self.xp_region = xp_region
        self.min_confidence = min_confidence

    def _read_region(
        self,
        frame: np.ndarray,
        region: Region,
    ) -> tuple[tuple[OcrToken, ...], str | None]:
        try:
            return self.provider.read(frame, region), None
        except Exception as error:
            return (), str(error)

    def read(self, frame: np.ndarray) -> LobbyProgressionReading:
        begin_frame = getattr(self.provider, "begin_frame", None)
        if callable(begin_frame):
            try:
                begin_frame(frame)
            except Exception as error:
                empty = _evidence(())
                return LobbyProgressionReading(
                    level=None,
                    xp_current_approx=None,
                    xp_required_approx=None,
                    level_evidence=empty,
                    xp_evidence=empty,
                    error=f"OCR 初始化失败: {error}",
                )

        level_tokens, level_error = self._read_region(frame, self.level_region)
        xp_tokens, xp_error = self._read_region(frame, self.xp_region)
        level_evidence = _evidence(level_tokens)
        xp_evidence = _evidence(xp_tokens)

        level_problem = _evidence_problem(
            "等级",
            level_evidence,
            level_error,
            self.min_confidence,
        )
        xp_problem = _evidence_problem(
            "经验",
            xp_evidence,
            xp_error,
            self.min_confidence,
        )
        level = (
            None if level_problem is not None else parse_level(level_evidence.raw_text)
        )
        xp_pair = (
            None if xp_problem is not None else parse_xp_pair(xp_evidence.raw_text)
        )

        problems = [
            problem for problem in (level_problem, xp_problem) if problem is not None
        ]
        if level is None and level_problem is None:
            problems.append("等级文字格式无法确认")
        if xp_pair is None and xp_problem is None:
            problems.append("经验文字格式无法确认")

        return LobbyProgressionReading(
            level=level,
            xp_current_approx=None if xp_pair is None else xp_pair[0],
            xp_required_approx=None if xp_pair is None else xp_pair[1],
            level_evidence=level_evidence,
            xp_evidence=xp_evidence,
            error="; ".join(problems) or None,
        )


class LobbyProgressionStabilizer:
    """Emit one reading only after the complete numeric tuple repeats."""

    def __init__(self, required_samples: int = 2) -> None:
        if required_samples < 1:
            raise ValueError("稳定确认次数必须至少为 1")
        self.required_samples = required_samples
        self._candidate: tuple[int, int, int] | None = None
        self._candidate_count = 0
        self._last_confirmed: tuple[int, int, int] | None = None

    @property
    def candidate_count(self) -> int:
        return self._candidate_count

    def reset(self) -> None:
        """Start a new lobby visit, allowing the same values to emit again."""

        self._candidate = None
        self._candidate_count = 0
        self._last_confirmed = None

    def observe(
        self,
        reading: LobbyProgressionReading,
    ) -> LobbyProgressionReading | None:
        if not reading.is_complete:
            self._candidate = None
            self._candidate_count = 0
            return None

        assert reading.level is not None
        assert reading.xp_current_approx is not None
        assert reading.xp_required_approx is not None
        candidate = (
            reading.level,
            reading.xp_current_approx,
            reading.xp_required_approx,
        )
        if candidate == self._last_confirmed:
            self._candidate = None
            self._candidate_count = 0
            return None
        if candidate == self._candidate:
            self._candidate_count += 1
        else:
            self._candidate = candidate
            self._candidate_count = 1

        if self._candidate_count < self.required_samples:
            return None
        self._last_confirmed = candidate
        self._candidate = None
        self._candidate_count = 0
        return reading

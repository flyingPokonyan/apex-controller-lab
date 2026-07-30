from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np

from .ocr_obstacles import (
    OcrProvider,
    OcrToken,
    Region,
    RegionRequirement,
    normalize_ocr_text,
    parse_regions,
)


Roi = tuple[int, int, int, int]


@dataclass(frozen=True)
class PixelRequirement:
    """A checkbox is a picture, not a word.

    OCR reads "补满" identically whether or not the box beside it is ticked,
    so the only evidence that separates the two is the tick itself. This
    measures one tight crop: what fraction of it is brighter than
    `bright_above`. The tick is a fixed white sprite, so on the target
    machine the ratio is 0.284 when ticked and 0.000 when not — the widest
    margin anything in this dictionary has.
    """

    region: str
    bright_above: float
    min_ratio: float
    max_ratio: float


@dataclass(frozen=True)
class OcrStateRule:
    rule_id: str
    enabled: bool
    state: str
    requirements: tuple[RegionRequirement, ...]
    pixel_requirements: tuple[PixelRequirement, ...] = ()


@dataclass(frozen=True)
class OcrStateDecision:
    rule_id: str
    state: str
    confidence: float
    evidence: dict[str, tuple[OcrToken, ...]]


@dataclass(frozen=True)
class OcrStateAnalysis:
    regions: dict[str, tuple[OcrToken, ...]]
    decision: OcrStateDecision | None
    error: str | None = None

    def as_event_payload(self) -> dict[str, object]:
        return {
            "regions": {
                name: [
                    {"text": token.text, "confidence": round(token.confidence, 4)}
                    for token in tokens
                ]
                for name, tokens in self.regions.items()
            },
            "matchedRule": None if self.decision is None else self.decision.rule_id,
            "error": self.error,
        }


class OcrStateDetector:
    """Positive OCR router for a known task flow.

    This detector only names a screen state. It never authorizes an input by
    itself; the task profile must separately define an action and its allowed
    postconditions. This keeps recognition evidence and input policy apart.
    """

    def __init__(
        self,
        provider: OcrProvider,
        regions: dict[str, Region],
        rules: tuple[OcrStateRule, ...],
    ) -> None:
        self.provider = provider
        self.regions = regions
        self.rules = rules
        self.states = {rule.state for rule in rules if rule.enabled}

    @classmethod
    def from_path(cls, provider: OcrProvider, path: Path | str) -> "OcrStateDetector":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(payload.get("schemaVersion", 0)) != 1:
            raise ValueError("OCR 状态字典 schemaVersion 必须为 1")
        default_confidence = float(payload.get("defaultMinConfidence", 0.62))
        if not 0 <= default_confidence <= 1:
            raise ValueError("OCR 状态默认置信度必须在 0 到 1 之间")

        regions = parse_regions(payload["regions"])

        rules: list[OcrStateRule] = []
        states: set[str] = set()
        for item in payload.get("rules", []):
            requirements = tuple(
                RegionRequirement(
                    region=str(requirement["region"]),
                    any_terms=tuple(str(value) for value in requirement.get("any", [])),
                    all_terms=tuple(str(value) for value in requirement.get("all", [])),
                    min_confidence=float(requirement.get("minConfidence", default_confidence)),
                )
                for requirement in item.get("requirements", [])
            )
            for requirement in requirements:
                if requirement.region not in regions:
                    raise ValueError(
                        f"OCR 状态规则 {item.get('id')} 引用了未知区域 {requirement.region}"
                    )
                if not requirement.any_terms and not requirement.all_terms:
                    raise ValueError(f"OCR 状态规则 {item.get('id')} 存在空匹配条件")
                if not 0 <= requirement.min_confidence <= 1:
                    raise ValueError(f"OCR 状态规则 {item.get('id')} 的置信度无效")
            pixel_requirements = tuple(
                PixelRequirement(
                    region=str(requirement["region"]),
                    bright_above=float(requirement.get("brightAbove", 0.75)),
                    min_ratio=float(requirement.get("minRatio", 0.0)),
                    max_ratio=float(requirement.get("maxRatio", 1.0)),
                )
                for requirement in item.get("pixelRequirements", [])
            )
            for pixel in pixel_requirements:
                if pixel.region not in regions:
                    raise ValueError(
                        f"OCR 状态规则 {item.get('id')} 引用了未知像素区域 {pixel.region}"
                    )
                if not 0 <= pixel.bright_above <= 1:
                    raise ValueError(f"OCR 状态规则 {item.get('id')} 的亮度阈值无效")
                if not 0 <= pixel.min_ratio <= pixel.max_ratio <= 1:
                    raise ValueError(f"OCR 状态规则 {item.get('id')} 的亮点比例区间无效")
            rule = OcrStateRule(
                rule_id=str(item["id"]),
                enabled=bool(item.get("enabled", True)),
                state=str(item["state"]),
                requirements=requirements,
                pixel_requirements=pixel_requirements,
            )
            # A pixel measurement is a modifier on a page the text already
            # identified, never the whole case for acting on one.
            if rule.enabled and rule.pixel_requirements and not rule.requirements:
                raise ValueError(f"OCR 状态规则 {rule.rule_id} 只有像素证据，没有文字证据")
            if rule.enabled and not rule.requirements:
                raise ValueError(f"OCR 状态规则 {rule.rule_id} 没有正向页面证据")
            if rule.enabled and rule.state in states:
                raise ValueError(f"OCR 状态 {rule.state} 被重复定义")
            if rule.enabled:
                states.add(rule.state)
            rules.append(rule)
        return cls(provider, regions, tuple(rules))

    @staticmethod
    def _requirement_matches(
        requirement: RegionRequirement,
        tokens: tuple[OcrToken, ...],
    ) -> tuple[bool, float]:
        eligible = tuple(
            token for token in tokens if token.confidence >= requirement.min_confidence
        )
        joined = "".join(token.normalized for token in eligible)
        any_terms = tuple(normalize_ocr_text(value) for value in requirement.any_terms)
        all_terms = tuple(normalize_ocr_text(value) for value in requirement.all_terms)
        any_matches = not any_terms or any(term and term in joined for term in any_terms)
        all_matches = all(term and term in joined for term in all_terms)
        if not any_matches or not all_matches:
            return False, 0.0
        matched_confidences = [
            token.confidence
            for token in eligible
            if any(
                term and term in token.normalized
                for term in (*any_terms, *all_terms)
            )
        ]
        return True, min(matched_confidences or [token.confidence for token in eligible])

    def _pixel_matches(self, frame: np.ndarray, requirement: PixelRequirement) -> bool:
        x1, y1, x2, y2 = self.regions[requirement.region].roi
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return False
        # Averaging the channels keeps this independent of whether the capture
        # backend hands over BGR or RGB.
        values = crop.mean(axis=2) if crop.ndim == 3 else crop
        ratio = float((values / 255.0 > requirement.bright_above).mean())
        return requirement.min_ratio <= ratio <= requirement.max_ratio

    def analyze(self, frame: np.ndarray) -> OcrStateAnalysis:
        begin_frame = getattr(self.provider, "begin_frame", None)
        if callable(begin_frame):
            # Batch providers may reuse one expensive full-frame detection for
            # several logical regions, but the cache must end with this
            # analysis even if the capture backend reuses the same ndarray.
            begin_frame(frame)
        required_regions = {
            requirement.region
            for rule in self.rules
            if rule.enabled
            for requirement in rule.requirements
        }
        region_results: dict[str, tuple[OcrToken, ...]] = {}
        try:
            for name in sorted(required_regions):
                region_results[name] = self.provider.read(frame, self.regions[name])
        except Exception as error:
            return OcrStateAnalysis(region_results, None, str(error))

        for rule in self.rules:
            if not rule.enabled:
                continue
            confidences: list[float] = []
            evidence: dict[str, tuple[OcrToken, ...]] = {}
            for requirement in rule.requirements:
                tokens = region_results.get(requirement.region, ())
                matched, confidence = self._requirement_matches(requirement, tokens)
                if not matched:
                    break
                confidences.append(confidence)
                evidence[requirement.region] = tokens
            else:
                if not all(
                    self._pixel_matches(frame, pixel)
                    for pixel in rule.pixel_requirements
                ):
                    continue
                return OcrStateAnalysis(
                    region_results,
                    OcrStateDecision(
                        rule_id=rule.rule_id,
                        state=rule.state,
                        confidence=min(confidences, default=0.0),
                        evidence=evidence,
                    ),
                )
        return OcrStateAnalysis(region_results, None)

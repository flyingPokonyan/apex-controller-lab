from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path

import numpy as np

from .ocr_obstacles import (
    OcrProvider,
    OcrToken,
    RegionRequirement,
    normalize_ocr_text,
)


Roi = tuple[int, int, int, int]


@dataclass(frozen=True)
class OcrStateRule:
    rule_id: str
    enabled: bool
    state: str
    requirements: tuple[RegionRequirement, ...]


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
        regions: dict[str, Roi],
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

        regions = {
            name: tuple(int(value) for value in roi)
            for name, roi in payload["regions"].items()
        }
        for name, roi in regions.items():
            if len(roi) != 4 or roi[0] >= roi[2] or roi[1] >= roi[3]:
                raise ValueError(f"OCR 状态区域无效：{name}={roi}")

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
            rule = OcrStateRule(
                rule_id=str(item["id"]),
                enabled=bool(item.get("enabled", True)),
                state=str(item["state"]),
                requirements=requirements,
            )
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

    def analyze(self, frame: np.ndarray) -> OcrStateAnalysis:
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

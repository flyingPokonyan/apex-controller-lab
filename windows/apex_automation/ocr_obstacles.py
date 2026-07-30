from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
from typing import Any, Protocol
import unicodedata

import numpy as np


Roi = tuple[int, int, int, int]
SAFE_KEY_ACTIONS = {"escapeScanCode", "enterScanCode", "spaceScanCode"}


def normalize_ocr_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    return "".join(
        character
        for character in normalized
        if not unicodedata.category(character).startswith(("P", "S", "Z", "C"))
    )


@dataclass(frozen=True)
class OcrToken:
    text: str
    confidence: float
    # Absolute coordinates when the provider read a complete frame. Most
    # callers only need text, so this stays optional and preserves the small
    # fake providers used by the state-machine tests.
    roi: Roi | None = None

    @property
    def normalized(self) -> str:
        return normalize_ocr_text(self.text)


@dataclass(frozen=True)
class Region:
    """A named crop, and whether it is tight enough to skip text detection.

    `single_line` is a promise about the crop, not a hint: recognition runs
    directly on the whole region, so a region that actually holds several
    lines returns garbage rather than an error.
    """

    name: str
    roi: Roi
    single_line: bool = False


def parse_regions(payload: dict[str, Any]) -> dict[str, Region]:
    regions: dict[str, Region] = {}
    for name, item in payload.items():
        if isinstance(item, dict):
            roi = tuple(int(value) for value in item["roi"])
            single_line = bool(item.get("singleLine", False))
        else:
            roi = tuple(int(value) for value in item)
            single_line = False
        if len(roi) != 4 or roi[0] >= roi[2] or roi[1] >= roi[3]:
            raise ValueError(f"OCR 区域无效：{name}={roi}")
        regions[str(name)] = Region(name=str(name), roi=roi, single_line=single_line)
    return regions


class OcrProvider(Protocol):
    def read(self, frame: np.ndarray, region: Region) -> tuple[OcrToken, ...]: ...


class PositionedOcrProvider(Protocol):
    def read_with_boxes(self, frame: np.ndarray) -> tuple[OcrToken, ...]: ...


class RapidOcrProvider:
    """Lazy RapidOCR adapter so template-only startup stays inexpensive."""

    def __init__(self) -> None:
        self._engine: Any | None = None
        self._lock = threading.Lock()

    def _get_engine(self) -> Any:
        with self._lock:
            if self._engine is None:
                from rapidocr import RapidOCR

                self._engine = RapidOCR()
            return self._engine

    def read(self, frame: np.ndarray, region: Region) -> tuple[OcrToken, ...]:
        x1, y1, x2, y2 = region.roi
        crop = frame[y1:y2, x1:x2]
        if crop.size == 0:
            return ()
        if region.single_line:
            # Detection dominates a RapidOCR pass and its cost barely depends
            # on the input size, so cropping alone saves little. Reading a
            # tight single-line region directly measured 4.7ms against 234ms
            # for the same crop with detection enabled.
            result = self._get_engine()(crop, use_det=False, use_cls=False, use_rec=True)
        else:
            result = self._get_engine()(crop)
        texts = tuple(getattr(result, "txts", ()) or ())
        scores = tuple(getattr(result, "scores", ()) or ())
        return tuple(
            OcrToken(str(text), float(score))
            for text, score in zip(texts, scores, strict=False)
        )

    def read_with_boxes(self, frame: np.ndarray) -> tuple[OcrToken, ...]:
        """Read a complete frame and retain each text block's coordinates."""
        result = self._get_engine()(frame)
        texts = tuple(getattr(result, "txts", ()) or ())
        scores = tuple(getattr(result, "scores", ()) or ())
        raw_boxes = getattr(result, "boxes", None)
        boxes = () if raw_boxes is None else tuple(raw_boxes)
        if len(texts) != len(scores) or len(texts) != len(boxes):
            raise RuntimeError("RapidOCR 没有为每个文字块返回位置，不能安全复用全屏结果")

        tokens: list[OcrToken] = []
        for text, score, box in zip(texts, scores, boxes, strict=True):
            points = np.asarray(box, dtype=float).reshape(-1, 2)
            if not len(points):
                raise RuntimeError("RapidOCR 返回了空文字位置")
            x1 = int(np.floor(points[:, 0].min()))
            y1 = int(np.floor(points[:, 1].min()))
            x2 = int(np.ceil(points[:, 0].max()))
            y2 = int(np.ceil(points[:, 1].max()))
            if x1 >= x2 or y1 >= y2:
                raise RuntimeError("RapidOCR 返回了无效文字位置")
            tokens.append(OcrToken(str(text), float(score), (x1, y1, x2, y2)))
        return tuple(tokens)


class FullFrameOcrProvider:
    """Run detection once, then preserve region semantics by token position.

    The play overlay dictionary mixes full-screen rules with the older safety
    rules' title/button regions. Running RapidOCR once per named crop costs
    several seconds each. This adapter detects the whole frame once and then
    serves every region from the positioned token list, so performance does
    not require loosening the safety dictionary's spatial evidence.
    """

    def __init__(self, provider: PositionedOcrProvider) -> None:
        self.provider = provider
        self._frame: np.ndarray | None = None
        self._tokens: tuple[OcrToken, ...] | None = None

    def begin_frame(self, frame: np.ndarray) -> None:
        self._frame = frame
        self._tokens = None

    def read(self, frame: np.ndarray, region: Region) -> tuple[OcrToken, ...]:
        if self._frame is not frame or self._tokens is None:
            self._tokens = self.provider.read_with_boxes(frame)
            self._frame = frame

        x1, y1, x2, y2 = region.roi
        selected: list[OcrToken] = []
        for token in self._tokens:
            if token.roi is None:
                raise RuntimeError("全屏 OCR 文字块缺少位置，拒绝放宽区域证据")
            tx1, ty1, tx2, ty2 = token.roi
            center_x = (tx1 + tx2) / 2
            center_y = (ty1 + ty2) / 2
            if x1 <= center_x < x2 and y1 <= center_y < y2:
                selected.append(token)
        return tuple(selected)


@dataclass(frozen=True)
class RegionRequirement:
    region: str
    any_terms: tuple[str, ...]
    all_terms: tuple[str, ...]
    min_confidence: float


@dataclass(frozen=True)
class ObstacleAction:
    name: str
    kind: str
    max_attempts: int
    confirm_ms: int


@dataclass(frozen=True)
class ObstacleRule:
    rule_id: str
    enabled: bool
    state: str
    safe_page: bool
    requirements: tuple[RegionRequirement, ...]
    action: ObstacleAction


@dataclass(frozen=True)
class ObstacleDecision:
    rule_id: str
    state: str
    confidence: float
    action: ObstacleAction
    evidence: dict[str, tuple[OcrToken, ...]]


@dataclass(frozen=True)
class OcrAnalysis:
    regions: dict[str, tuple[OcrToken, ...]]
    decision: ObstacleDecision | None
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


class OcrObstacleDetector:
    def __init__(
        self,
        provider: OcrProvider,
        regions: dict[str, Region],
        probe_regions: tuple[str, ...],
        rules: tuple[ObstacleRule, ...],
    ) -> None:
        self.provider = provider
        self.regions = regions
        self.probe_regions = probe_regions
        self.rules = rules
        self.states = {rule.state for rule in rules if rule.enabled}

    @classmethod
    def from_path(cls, provider: OcrProvider, path: Path | str) -> "OcrObstacleDetector":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(payload.get("schemaVersion", 0)) != 1:
            raise ValueError("OCR 障碍字典 schemaVersion 必须为 1")
        default_confidence = float(payload.get("defaultMinConfidence", 0.65))
        if not 0 <= default_confidence <= 1:
            raise ValueError("OCR 默认置信度必须在 0 到 1 之间")
        regions = parse_regions(payload["regions"])

        rules: list[ObstacleRule] = []
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
                        f"OCR 规则 {item.get('id')} 引用了未知区域 {requirement.region}"
                    )
                if not requirement.any_terms and not requirement.all_terms:
                    raise ValueError(f"OCR 规则 {item.get('id')} 存在空匹配条件")
                if not 0 <= requirement.min_confidence <= 1:
                    raise ValueError(f"OCR 规则 {item.get('id')} 的置信度无效")
            action_payload = item["action"]
            rule = ObstacleRule(
                rule_id=str(item["id"]),
                enabled=bool(item.get("enabled", False)),
                state=str(item["state"]),
                safe_page=bool(item.get("safePage", False)),
                requirements=requirements,
                action=ObstacleAction(
                    name=str(action_payload["name"]),
                    kind=str(action_payload["kind"]),
                    max_attempts=int(action_payload.get("maxAttempts", 1)),
                    confirm_ms=int(action_payload.get("confirmMs", 2000)),
                ),
            )
            if rule.enabled and not rule.safe_page:
                raise ValueError(f"OCR 规则 {rule.rule_id} 未声明 safePage，不能启用动作")
            if rule.enabled and not rule.requirements:
                raise ValueError(f"OCR 规则 {rule.rule_id} 没有正向页面证据")
            if rule.action.kind != "key":
                raise ValueError(f"OCR 规则 {rule.rule_id} 只允许键盘消除动作")
            if rule.action.name not in SAFE_KEY_ACTIONS:
                raise ValueError(
                    f"OCR 规则 {rule.rule_id} 的动作不在安全键白名单：{rule.action.name}"
                )
            if not 1 <= rule.action.max_attempts <= 3:
                raise ValueError(f"OCR 规则 {rule.rule_id} 的最大尝试次数必须为 1 到 3")
            if not 500 <= rule.action.confirm_ms <= 10000:
                raise ValueError(f"OCR 规则 {rule.rule_id} 的确认时间必须为 500 到 10000ms")
            rules.append(rule)

        probe_regions = tuple(str(value) for value in payload.get("probeRegions", []))
        if any(name not in regions for name in probe_regions):
            raise ValueError("probeRegions 引用了未知 OCR 区域")
        return cls(provider, regions, probe_regions, tuple(rules))

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
        confidence = min((token.confidence for token in eligible), default=0.0)
        return True, confidence

    def analyze(self, frame: np.ndarray) -> OcrAnalysis:
        required_regions = set(self.probe_regions)
        for rule in self.rules:
            if rule.enabled:
                required_regions.update(requirement.region for requirement in rule.requirements)
        region_results: dict[str, tuple[OcrToken, ...]] = {}
        try:
            for name in sorted(required_regions):
                region_results[name] = self.provider.read(frame, self.regions[name])
        except Exception as error:
            return OcrAnalysis(region_results, None, str(error))

        for rule in self.rules:
            if not rule.enabled:
                continue
            confidences: list[float] = []
            evidence: dict[str, tuple[OcrToken, ...]] = {}
            matched = True
            for requirement in rule.requirements:
                tokens = region_results.get(requirement.region, ())
                requirement_matched, confidence = self._requirement_matches(
                    requirement,
                    tokens,
                )
                if not requirement_matched:
                    matched = False
                    break
                confidences.append(confidence)
                evidence[requirement.region] = tokens
            if matched:
                decision = ObstacleDecision(
                    rule_id=rule.rule_id,
                    state=rule.state,
                    confidence=min(confidences, default=0.0),
                    action=rule.action,
                    evidence=evidence,
                )
                return OcrAnalysis(region_results, decision)
        return OcrAnalysis(region_results, None)

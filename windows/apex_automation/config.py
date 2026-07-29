from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = REPOSITORY_ROOT / "windows" / "config" / "first-match-2560x1440.zh-CN.json"
TUTORIAL_CONFIG_PATH = REPOSITORY_ROOT / "windows" / "config" / "first-tutorial-2560x1440.zh-CN.json"
PLAY_CONFIG_PATH = REPOSITORY_ROOT / "windows" / "config" / "play-2560x1440.zh-CN.json"


@dataclass(frozen=True)
class AnchorConfig:
    template: Path
    roi: tuple[int, int, int, int]
    threshold: float
    search_px: int


@dataclass(frozen=True)
class StateConfig:
    stable_frames: int
    anchors: tuple[AnchorConfig, ...]


@dataclass(frozen=True)
class RunnerConfig:
    path: Path
    profile: str
    environment: dict[str, Any]
    timing: dict[str, int]
    actions: dict[str, Any]
    states: dict[str, StateConfig]
    offline_cases: tuple[tuple[Path, str], ...]
    guided_offline_cases: tuple[tuple[Path, str], ...]
    ocr: dict[str, Any]
    guided_ocr: dict[str, Any]
    task: dict[str, Any]
    control_api: dict[str, Any]
    game_states: dict[str, Any]
    capability_set: str | None


def _repository_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> RunnerConfig:
    config_path = Path(path).expanduser().resolve()
    payload = json.loads(config_path.read_text(encoding="utf-8"))

    states: dict[str, StateConfig] = {}
    for state_name, state_payload in payload["states"].items():
        anchors = tuple(
            AnchorConfig(
                template=_repository_path(anchor["template"]),
                roi=tuple(int(value) for value in anchor["roi"]),
                threshold=float(anchor["threshold"]),
                search_px=int(anchor.get("searchPx", 0)),
            )
            for anchor in state_payload["anchors"]
        )
        states[state_name] = StateConfig(
            stable_frames=int(state_payload.get("stableFrames", 2)),
            anchors=anchors,
        )

    offline_cases = tuple(
        (_repository_path(image_path), expected_state)
        for image_path, expected_state in payload.get("offlineCases", [])
    )
    guided_offline_cases = tuple(
        (_repository_path(image_path), expected_state)
        for image_path, expected_state in payload.get("guidedOfflineCases", [])
    )

    return RunnerConfig(
        path=config_path,
        profile=str(payload["profile"]),
        environment=dict(payload["environment"]),
        timing={key: int(value) for key, value in payload["timing"].items()},
        actions=dict(payload["actions"]),
        states=states,
        offline_cases=offline_cases,
        guided_offline_cases=guided_offline_cases,
        ocr=dict(payload.get("ocr", {})),
        guided_ocr=dict(payload.get("guidedOcr", {})),
        task=dict(payload.get("task", {})),
        control_api=dict(payload.get("controlApi", {})),
        game_states=dict(payload.get("gameStates", {})),
        capability_set=payload.get("capabilitySet"),
    )

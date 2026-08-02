from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import secrets
import threading
import time
from typing import TYPE_CHECKING, Any

from .atomic_files import replace_with_retry

if TYPE_CHECKING:
    import numpy as np


EVENT_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
_SINGLETON_EVENTS = frozenset({"RUN_STARTED", "RUN_FINISHED"})


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _save_frame(path: Path, frame: np.ndarray) -> None:
    # Importing vision also imports OpenCV. Recorder-only tooling and tests do
    # not need that dependency until they actually save a screenshot.
    from .vision import save_frame

    save_frame(path, frame)


class RunRecorder:
    def __init__(
        self,
        root: Path,
        profile: str,
        *,
        manifest: dict[str, Any] | None = None,
        start_payload: dict[str, Any] | None = None,
    ):
        self._reject_sensitive_keys(manifest or {})
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.run_id, self.run_dir = self._create_run_dir(root)
        self.screenshot_dir = self.run_dir / "screenshots"
        self.screenshot_dir.mkdir()
        self.events_path = self.run_dir / "events.jsonl"
        # Compatibility for callers that only use this attribute as the event
        # stream path. No separate actions.jsonl is created or written.
        self.actions_path = self.events_path
        self.manifest_path = self.run_dir / "manifest.json"
        self.result_path = self.run_dir / "result.json"
        self.status_path = self.run_dir / "status.json"
        self.current_status_path = root / "status.json"
        self.started = time.monotonic()
        self.started_at = _now()
        self.profile = profile
        self._lock = threading.RLock()
        self._seq = 0
        self._screenshot_index = 0
        self._singleton_events: set[str] = set()
        self._screenshot_event_paths: set[str] = set()
        self._finished = False
        manifest_payload = {
            **(manifest or {}),
            "schemaVersion": MANIFEST_SCHEMA_VERSION,
            "eventSchemaVersion": EVENT_SCHEMA_VERSION,
            "runId": self.run_id,
            "profile": self.profile,
            "startedAt": self.started_at,
        }
        self._atomic_json(self.manifest_path, manifest_payload)
        initial_event = {"profile": profile, **(start_payload or {})}
        self.log("RUN_STARTED", **initial_event)

    @staticmethod
    def _create_run_dir(root: Path) -> tuple[str, Path]:
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for _ in range(100):
            run_id = f"{timestamp}-{secrets.token_hex(4)}"
            run_dir = root / run_id
            try:
                run_dir.mkdir()
            except FileExistsError:
                continue
            return run_id, run_dir
        raise FileExistsError("连续生成了重复的运行 ID，无法创建会话目录")

    def _elapsed_ms(self) -> int:
        return max(0, round((time.monotonic() - self.started) * 1000))

    def _screenshot_event_key(self, raw_path: object) -> str | None:
        if raw_path is None:
            return None
        path = Path(str(raw_path))
        if not path.is_absolute():
            path = self.run_dir / path
        return os.path.normcase(str(path.resolve(strict=False)))

    def log(self, event: str, **payload: Any) -> None:
        event = str(event)
        with self._lock:
            if self._finished:
                return
            if event in _SINGLETON_EVENTS and event in self._singleton_events:
                return

            screenshot_key = None
            if event == "SCREENSHOT_SAVED":
                screenshot_key = self._screenshot_event_key(payload.get("path"))
                if (
                    screenshot_key is not None
                    and screenshot_key in self._screenshot_event_paths
                ):
                    return

            next_seq = self._seq + 1
            record = {
                "schemaVersion": EVENT_SCHEMA_VERSION,
                "runId": self.run_id,
                "seq": next_seq,
                "occurredAt": _now(),
                "elapsedMs": self._elapsed_ms(),
                "type": event,
                "payload": dict(payload),
            }
            line = json.dumps(record, ensure_ascii=False) + "\n"
            with self.events_path.open("a", encoding="utf-8") as handle:
                handle.write(line)

            self._seq = next_seq
            if event in _SINGLETON_EVENTS:
                self._singleton_events.add(event)
            if screenshot_key is not None:
                self._screenshot_event_paths.add(screenshot_key)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp"
        )
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            replace_with_retry(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def write_status(self, payload: dict[str, Any]) -> None:
        with self._lock:
            snapshot = {
                **payload,
                "schemaVersion": 1,
                "runId": self.run_id,
                "profile": self.profile,
                "runDir": str(self.run_dir.resolve()),
                "updatedAt": _now(),
            }
            self._atomic_json(self.status_path, snapshot)
            self._atomic_json(self.current_status_path, snapshot)

    @staticmethod
    def _reject_sensitive_keys(value: object, trail: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key)
                lowered = name.lower()
                if any(marker in lowered for marker in ("token", "password", "secret")):
                    location = f"{trail}.{name}" if trail else name
                    raise ValueError(f"manifest 不允许敏感字段：{location}")
                RunRecorder._reject_sensitive_keys(
                    child,
                    f"{trail}.{name}" if trail else name,
                )
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                RunRecorder._reject_sensitive_keys(child, f"{trail}[{index}]")

    def update_manifest(self, payload: dict[str, Any]) -> None:
        """Merge token-free run metadata before background reporting starts."""

        immutable = {
            "schemaVersion",
            "eventSchemaVersion",
            "runId",
            "profile",
            "startedAt",
        }
        if immutable.intersection(payload):
            raise ValueError("manifest 的运行身份字段不可修改")
        self._reject_sensitive_keys(payload)
        with self._lock:
            current = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            current.update(payload)
            self._atomic_json(self.manifest_path, current)

    def screenshot(self, stage: str, frame: np.ndarray) -> Path:
        with self._lock:
            self._screenshot_index += 1
            path = (
                self.screenshot_dir
                / f"{self._screenshot_index:03d}-{stage.lower()}.png"
            )
        _save_frame(path, frame)
        self.log(
            "SCREENSHOT_SAVED",
            stage=stage,
            path=path.relative_to(self.run_dir).as_posix(),
        )
        return path

    def finish(self, status: str, **payload: Any) -> None:
        with self._lock:
            if self._finished:
                return
            result = {
                **payload,
                "schemaVersion": 1,
                "runId": self.run_id,
                "status": status,
                "profile": self.profile,
                "finishedAt": _now(),
                "durationMs": self._elapsed_ms(),
            }
            self._atomic_json(self.result_path, result)
            event_payload = dict(payload)
            event_payload.update(
                {
                    "status": status,
                    "profile": self.profile,
                    "durationMs": result["durationMs"],
                }
            )
            self.log("RUN_FINISHED", **event_payload)
            self._finished = True

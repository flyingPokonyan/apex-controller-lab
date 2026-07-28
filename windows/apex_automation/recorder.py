from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import time
from typing import Any

import numpy as np

from .vision import save_frame


class RunRecorder:
    def __init__(self, root: Path, profile: str):
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_dir = root / run_id
        self.screenshot_dir = self.run_dir / "screenshots"
        self.screenshot_dir.mkdir(parents=True, exist_ok=False)
        self.actions_path = self.run_dir / "actions.jsonl"
        self.events_path = self.run_dir / "events.jsonl"
        self.status_path = self.run_dir / "status.json"
        self.current_status_path = root / "status.json"
        self.started = time.monotonic()
        self.profile = profile
        self._screenshot_index = 0
        self._finished = False
        self.log("RUN_STARTED", profile=profile)

    def log(self, event: str, **payload: Any) -> None:
        record = {
            "elapsedMs": round((time.monotonic() - self.started) * 1000),
            "event": event,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False) + "\n"
        for path in (self.actions_path, self.events_path):
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)

    @staticmethod
    def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def write_status(self, payload: dict[str, Any]) -> None:
        snapshot = {
            "schemaVersion": 1,
            "profile": self.profile,
            "runDir": str(self.run_dir.resolve()),
            "updatedAt": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            **payload,
        }
        self._atomic_json(self.status_path, snapshot)
        self._atomic_json(self.current_status_path, snapshot)

    def screenshot(self, stage: str, frame: np.ndarray) -> Path:
        self._screenshot_index += 1
        path = self.screenshot_dir / f"{self._screenshot_index:03d}-{stage.lower()}.png"
        save_frame(path, frame)
        self.log("SCREENSHOT_SAVED", stage=stage, path=str(path.relative_to(self.run_dir)))
        return path

    def finish(self, status: str, **payload: Any) -> None:
        if self._finished:
            return
        self._finished = True
        result = {"status": status, "profile": self.profile, **payload}
        self._atomic_json(self.run_dir / "result.json", result)
        self.log("RUN_FINISHED", **result)

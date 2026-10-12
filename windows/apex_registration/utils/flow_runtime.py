"""Task-local operation deadlines, readable even when Playwright stops responding."""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import uuid
import faulthandler
import json
import os
from pathlib import Path
import tempfile
import time


def private_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_operation(workspace: Path) -> dict:
    try:
        value = json.loads((workspace / "flow_operation.json").read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class FlowRuntime:
    def __init__(self, workspace: Path):
        self.workspace = Path(workspace)
        self.operation: dict = {}
        self.trace = None

    def _publish(self, operation: dict) -> None:
        self.operation = operation
        private_json(self.workspace / "flow_operation.json", operation)
        faulthandler.cancel_dump_traceback_later()
        if operation:
            if self.trace is None:
                path = self.workspace / "flow_watchdog.log"
                self.trace = path.open("a")
                path.chmod(0o600)
            # No locals or credentials are included in a traceback.
            self.trace.write(f"operation={operation['name']} stage={operation['stage']}\n")
            self.trace.flush()
            remaining = max(0.1, operation["deadline"] - time.monotonic())
            faulthandler.dump_traceback_later(remaining, file=self.trace)

    def event(self, kind, **data):
        self.workspace.mkdir(parents=True, exist_ok=True)
        record = dict(data, kind=kind, occurredAt=dt.datetime.now(dt.timezone.utc).isoformat(), eventId=uuid.uuid4().hex + uuid.uuid4().hex)
        with (self.workspace / 'diagnostics.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')

    def start(self, name: str, stage: str, message: str, seconds: float = 90) -> None:
        now = time.monotonic()
        self.event('step_start', name=name, stage=stage, message=message, seconds=seconds)
        self._publish({"name": name, "stage": stage, "message": message,
                       "started": now, "deadline": now + seconds, "seconds": seconds})

    @contextmanager
    def step(self, name: str, stage: str, message: str, seconds: float = 90):
        previous = self.operation
        self.start(name, stage, message, seconds)
        try:
            yield
        except BaseException as exc:
            self.event('step_error', name=name, stage=stage, errorType=type(exc).__name__)
            raise
        else:
            self.event('step_end', name=name, stage=stage)
        finally:
            self._publish(previous)

    def record_browser(self, _client, profile_id: str) -> None:
        path = self.workspace / "flow_browsers.json"
        try:
            ids = json.loads(path.read_text())
        except (OSError, ValueError):
            ids = []
        if profile_id not in ids:
            private_json(path, [*ids, profile_id])

    def close(self) -> None:
        self._publish({})
        if self.trace is not None:
            self.trace.close()
            self.trace = None

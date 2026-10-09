"""Small, local launcher protocol. No credentials or network operations here."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
import time

from .atomic_files import replace_with_retry


UPDATE_EXIT = 75


class ManagedUpdateRequested(BaseException):
    """Unwind even through broad business-error handlers, like KeyboardInterrupt."""


class ManagedRuntime:
    def __init__(self, root: Path, session: str):
        self.root = root
        self.session = session
        self.thread = threading.get_ident()
        self.phase = "STARTING"
        self.blocked = False
        self.reason = None
        self.completed = 0
        self.requested = False
        self.operator_stopped = False
        self._next_write = 0.0

    @classmethod
    def from_environment(cls):
        root, session = os.environ.get("APEX_MANAGED_ROOT"), os.environ.get("APEX_MANAGED_SESSION")
        return cls(Path(root), session) if root and session else None

    @staticmethod
    def operator_abort():
        if os.name != "nt":
            return False
        import ctypes
        return bool(ctypes.windll.user32.GetAsyncKeyState(0x77) & 0x8000)

    def pulse(self, phase=None, *, blocked=None, reason=None, boundary=False):
        # A lease/HTTP background heartbeat must not disguise a frozen main loop.
        if threading.get_ident() != self.thread:
            return
        if phase is not None:
            self.phase = phase
        if blocked is not None:
            self.blocked, self.reason = blocked, reason
        now = time.monotonic()
        if now < self._next_write and not boundary:
            return
        self._next_write = now + 1.0
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / "worker.json"
        temporary = path.with_suffix(".tmp")
        payload = {"session": self.session, "pid": os.getpid(), "at": time.time(),
                   "phase": self.phase, "blocked": self.blocked, "reason": self.reason,
                   "completed": self.completed, "operatorStopped": self.operator_stopped}
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle)
            handle.flush()
            os.fsync(handle.fileno())
        replace_with_retry(temporary, path)
        if self.operator_stopped:
            return
        if self.operator_abort():
            # EA waits need the same F8 intent as the in-game input guard.
            self.stop_by_operator()
            raise KeyboardInterrupt("F8")
        try:
            request = json.loads((self.root / "request.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if request.get("session") != self.session:
            return
        if request.get("mode") == "stop":
            raise KeyboardInterrupt("启动器请求停止")
        if self.requested:
            # Play already unwound. Keep heartbeats alive during cleanup;
            # repeating recover here would interrupt the original lease close.
            return
        if request.get("mode") == "recover" or (boundary and request.get("mode") == "boundary"):
            self.requested = True
            raise ManagedUpdateRequested()

    def boundary(self):
        self.pulse("BETWEEN_ACCOUNTS", blocked=False, boundary=True)

    def stop_by_operator(self):
        self.operator_stopped = True
        self.requested = False
        self.pulse(boundary=True)

    def success(self):
        self.completed += 1
        self.pulse(boundary=True)

    def sleep(self, seconds):
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            self.pulse()
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

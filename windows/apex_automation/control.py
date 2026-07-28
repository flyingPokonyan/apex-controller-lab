from __future__ import annotations

from dataclasses import dataclass
import threading


class TaskStopRequested(RuntimeError):
    pass


@dataclass(frozen=True)
class ControlState:
    paused: bool
    stop_requested: bool
    release_requested: bool


class TaskControl:
    """Thread-safe mailbox shared by the local HTTP server and supervisor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._paused = False
        self._stop_requested = False
        self._release_requested = False

    def snapshot(self) -> ControlState:
        with self._lock:
            return ControlState(
                paused=self._paused,
                stop_requested=self._stop_requested,
                release_requested=self._release_requested,
            )

    def pause(self) -> None:
        with self._lock:
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._paused = False

    def stop(self) -> None:
        with self._lock:
            self._stop_requested = True

    def request_release(self) -> None:
        with self._lock:
            self._release_requested = True

    def consume_release(self) -> bool:
        with self._lock:
            requested = self._release_requested
            self._release_requested = False
            return requested

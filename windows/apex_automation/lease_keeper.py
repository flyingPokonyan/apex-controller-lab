from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Callable
import uuid

from .account_provider import (
    AccountLease,
    AccountProvider,
    LeaseState,
    LeaseStaleError,
    LeaseStatus,
)


class LeaseKeeperState(str, Enum):
    CURRENT = "CURRENT"
    UNCERTAIN = "UNCERTAIN"
    STALE = "STALE"
    STOPPED = "STOPPED"


@dataclass(frozen=True)
class LeaseKeeperSnapshot:
    state: LeaseKeeperState
    expires_at: datetime | None
    renew_after: datetime | None
    pending_operation_id: str | None
    error_code: str | None = None


class LeaseKeeper:
    """Renew one fenced lease outside the Pilot frame loop."""

    def __init__(
        self,
        provider: AccountProvider,
        lease: AccountLease,
        *,
        phase: Callable[[], str],
        run_id: Callable[[], str | None],
        on_change: Callable[[LeaseKeeperSnapshot], None] = lambda _: None,
        operation_id_factory: Callable[[], str] = lambda: uuid.uuid4().hex,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        uncertain_retry_s: float = 5.0,
        operation_lock: threading.RLock | None = None,
    ) -> None:
        self.provider = provider
        self.lease = lease
        self.phase = phase
        self.run_id = run_id
        self.on_change = on_change
        self.operation_id_factory = operation_id_factory
        self.now = now
        self.uncertain_retry_s = max(0.1, uncertain_retry_s)
        self.operation_lock = operation_lock or threading.RLock()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._state = LeaseKeeperState.CURRENT
        self._status = LeaseStatus(
            lease_id=lease.lease_id,
            lease_fence=lease.lease_fence,
            account_id=lease.account_id,
            state=LeaseState.ACTIVE,
            expires_at=lease.expires_at,
            renew_after=lease.renew_after,
        )
        self._pending_operation_id: str | None = None
        self._error_code: str | None = None

    def snapshot(self) -> LeaseKeeperSnapshot:
        with self._lock:
            return LeaseKeeperSnapshot(
                state=self._state,
                expires_at=self._status.expires_at,
                renew_after=self._status.renew_after,
                pending_operation_id=self._pending_operation_id,
                error_code=self._error_code,
            )

    def _publish(self) -> None:
        self.on_change(self.snapshot())

    def is_current(self) -> bool:
        snapshot = self.snapshot()
        if snapshot.state is not LeaseKeeperState.CURRENT:
            return False
        return (
            snapshot.expires_at is None
            or self.now() < snapshot.expires_at
        )

    def renew_once(self) -> LeaseKeeperSnapshot:
        with self.operation_lock:
            return self._renew_once_locked()

    def _renew_once_locked(self) -> LeaseKeeperSnapshot:
        with self._lock:
            if self._state is LeaseKeeperState.STOPPED:
                stopped = True
                operation_id = None
            else:
                stopped = False
                if self._pending_operation_id is None:
                    self._pending_operation_id = self.operation_id_factory()
                operation_id = self._pending_operation_id
        if stopped:
            return self.snapshot()
        assert operation_id is not None
        self._publish()

        try:
            status = self.provider.renew(
                self.lease.lease_id,
                self.lease.lease_fence,
                operation_id,
                self.phase(),
                self.run_id(),
            )
            if (
                status.lease_id != self.lease.lease_id
                or status.lease_fence != self.lease.lease_fence
            ):
                raise LeaseStaleError("续租响应的 lease/fence 与当前租约不一致")
            with self._lock:
                self._status = status
                self._pending_operation_id = None
                self._error_code = None
                if status.state is LeaseState.EXPIRED_UNCONFIRMED:
                    self._state = LeaseKeeperState.STALE
                    self._error_code = "LEASE_EXPIRED"
                else:
                    self._state = LeaseKeeperState.CURRENT
        except LeaseStaleError:
            with self._lock:
                self._state = LeaseKeeperState.STALE
                self._error_code = "LEASE_STALE"
        except Exception:
            with self._lock:
                self._state = LeaseKeeperState.UNCERTAIN
                self._error_code = "LEASE_RENEW_FAILED"
        self._publish()
        return self.snapshot()

    def _wait_seconds(self) -> float:
        snapshot = self.snapshot()
        if snapshot.state is LeaseKeeperState.UNCERTAIN:
            return self.uncertain_retry_s
        if snapshot.renew_after is None:
            return self.uncertain_retry_s
        return max(0.0, (snapshot.renew_after - self.now()).total_seconds())

    def _run(self) -> None:
        while not self._stop.is_set():
            delay = self._wait_seconds()
            if self._wake.wait(delay):
                self._wake.clear()
            if self._stop.is_set():
                break
            self.renew_once()
            if self.snapshot().state is LeaseKeeperState.STALE:
                break

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="apex-account-lease-keeper",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, timeout_s: float = 10.0) -> LeaseKeeperSnapshot:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.1, timeout_s))
            if thread.is_alive():
                raise RuntimeError("LeaseKeeper 线程未在超时内退出")
            self._thread = None
        with self._lock:
            self._state = LeaseKeeperState.STOPPED
        self._publish()
        return self.snapshot()

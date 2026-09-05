from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import re
import threading
from typing import Callable
import uuid

from .account_provider import (
    AccountLease,
    AccountProvider,
    LeaseState,
    LeaseStaleError,
    LeaseProviderError,
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
    retryable: bool = False
    failure_count: int = 0
    error_detail: str | None = None
    recovery_required: bool = False


def lease_error_detail(error: BaseException) -> str:
    """Keep typed transport evidence without copying URLs, tokens or bodies."""
    parts: list[str] = []
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 6:
        seen.add(id(current))
        parts.append(type(current).__name__)
        for name in ("code", "reason", "errno", "winerror"):
            value = getattr(current, name, None)
            if type(value) is int or (
                isinstance(value, str) and re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", value)
            ):
                parts.append(f"{name}={value}")
        reason = getattr(current, "reason", None)
        current = reason if isinstance(reason, BaseException) else current.__cause__
    return " / ".join(parts)


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
        self._pending_phase: str | None = None
        self._pending_run_id: str | None = None
        self._pending_recover_expired = False
        self._recovery_required = False
        self._error_code: str | None = None
        self._retryable = False
        self._failure_count = 0
        self._error_detail: str | None = None

    def snapshot(self) -> LeaseKeeperSnapshot:
        with self._lock:
            return LeaseKeeperSnapshot(
                state=self._state,
                expires_at=self._status.expires_at,
                renew_after=self._status.renew_after,
                pending_operation_id=self._pending_operation_id,
                error_code=self._error_code,
                retryable=self._retryable,
                failure_count=self._failure_count,
                error_detail=self._error_detail,
                recovery_required=self._recovery_required,
            )

    def _publish(self) -> None:
        self.on_change(self.snapshot())

    def is_current(self) -> bool:
        snapshot = self.snapshot()
        if snapshot.state is LeaseKeeperState.UNCERTAIN:
            # A failed request does not revoke the time already granted. Only
            # transient failures may use that remaining time; never extend it.
            return bool(
                snapshot.retryable
                and not snapshot.recovery_required
                and snapshot.expires_at is not None
                and self.now() < snapshot.expires_at
            )
        if snapshot.state is not LeaseKeeperState.CURRENT:
            return False
        return (
            snapshot.expires_at is None
            or self.now() < snapshot.expires_at
        )

    def diagnostics(self) -> dict[str, object]:
        snapshot = self.snapshot()
        return {
            "state": snapshot.state.value,
            "errorCode": snapshot.error_code,
            "failureCount": snapshot.failure_count,
            "retryable": snapshot.retryable,
            "recoveryRequired": snapshot.recovery_required,
            "reason": snapshot.error_detail,
            "expiresAt": None if snapshot.expires_at is None else snapshot.expires_at.isoformat(),
        }

    def renew_once(self) -> LeaseKeeperSnapshot:
        with self.operation_lock:
            return self._renew_once_locked()

    def _renew_once_locked(self) -> LeaseKeeperSnapshot:
        phase, run_id = self.phase(), self.run_id()
        with self._lock:
            if self._state in {LeaseKeeperState.STOPPED, LeaseKeeperState.STALE}:
                stopped = True
                operation_id = None
            else:
                stopped = False
                if self._pending_operation_id is None:
                    self._pending_operation_id = self.operation_id_factory()
                    self._pending_phase = phase
                    self._pending_run_id = run_id
                    self._pending_recover_expired = bool(
                        phase == "APEX_PLAYING" and run_id and (
                            self._recovery_required
                            or (self._status.expires_at is not None and self.now() >= self._status.expires_at)
                        )
                    )
                    if self._pending_recover_expired:
                        self._recovery_required = True
                operation_id = self._pending_operation_id
        if stopped:
            return self.snapshot()
        assert operation_id is not None
        assert self._pending_phase is not None
        self._publish()

        try:
            options = {"recover_expired": True} if self._pending_recover_expired else {}
            status = self.provider.renew(
                self.lease.lease_id,
                self.lease.lease_fence,
                operation_id,
                self._pending_phase,
                self._pending_run_id,
                **options,
            )
            if (
                status.lease_id != self.lease.lease_id
                or status.lease_fence != self.lease.lease_fence
                or status.account_id != self.lease.account_id
            ):
                raise LeaseStaleError("续租响应的 lease/fence 与当前租约不一致")
            with self._lock:
                self._status = status
                self._pending_operation_id = None
                self._error_code = None
                self._retryable = False
                self._failure_count = 0
                self._error_detail = None
                self._recovery_required = False
                expired_ack = (
                    status.state is LeaseState.EXPIRED_UNCONFIRMED
                    or (
                        status.state is LeaseState.ACTIVE
                        and status.expires_at is not None and self.now() >= status.expires_at
                    )
                )
                if expired_ack and phase == "APEX_PLAYING" and run_id:
                    # This operation may have succeeded before its ACK was
                    # lost. Its replay does not grant a new TTL. Reconfirm
                    # ownership with a NEW operation instead of looping on it.
                    self._state = LeaseKeeperState.UNCERTAIN
                    self._retryable = True
                    self._recovery_required = True
                    self._error_code = "LEASE_EXPIRED"
                    self._error_detail = "LEASE_RECOVERY_REQUIRED"
                elif status.state not in {LeaseState.ACTIVE, LeaseState.COMPLETION_PENDING}:
                    self._state = LeaseKeeperState.STALE
                    self._error_code = (
                        "LEASE_EXPIRED" if status.state is LeaseState.EXPIRED_UNCONFIRMED
                        else "LEASE_NOT_ACTIVE"
                    )
                    self._error_detail = self._error_code
                else:
                    self._state = LeaseKeeperState.CURRENT
        except LeaseStaleError as error:
            with self._lock:
                self._state = LeaseKeeperState.STALE
                self._error_code = "LEASE_STALE"
                self._retryable = False
                self._failure_count += 1
                self._error_detail = lease_error_detail(error)
        except Exception as error:
            with self._lock:
                self._state = LeaseKeeperState.UNCERTAIN
                self._error_code = error.code if isinstance(error, LeaseProviderError) else "LEASE_RENEW_FAILED"
                self._retryable = (
                    error.retryable if isinstance(error, LeaseProviderError)
                    else isinstance(error, OSError)
                )
                self._failure_count += 1
                self._error_detail = lease_error_detail(error)
                if (
                    isinstance(error, LeaseProviderError) and error.code == "LEASE_EXPIRED"
                    and phase == "APEX_PLAYING" and run_id
                ):
                    # The server explicitly rejected the ordinary renewal;
                    # recovery changes the body and therefore needs a new key.
                    self._pending_operation_id = None
                    self._recovery_required = True
                    self._retryable = True
                if not self._retryable:
                    self._state = LeaseKeeperState.STALE
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

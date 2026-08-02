from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterable, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .atomic_files import replace_with_retry
from .runner_identity import RunnerSettings


REPORTABLE_INCIDENTS = {
    "CAPTURE_ERROR": "CAPTURE_ERROR",
    "OCR_ERROR": "OCR_ERROR",
    "RESOLUTION_MISMATCH": "RESOLUTION_MISMATCH",
    "FOREGROUND_PAUSED": "FOREGROUND_LOST",
    "DECISION_PAUSED": "ACTION_PAUSED",
    "REPORTER_ERROR": "REPORTER_ERROR",
}

LOBBY_STATES = {
    "LOBBY_QUEUEING": "QUEUEING",
    "LOBBY_SELECT_REQUIRED": "LOBBY",
    "LOBBY_READY_TARGET_FILL_ON": "LOBBY",
    "LOBBY_READY_TRAINING": "LOBBY",
    "LOBBY_READY_TARGET": "LOBBY",
    "LOBBY_READY_OTHER": "LOBBY",
}

STATE_PHASES = {
    **LOBBY_STATES,
    "DROPSHIP_FOLLOWING": "DROPSHIP",
    "DROPSHIP_SOLO_JUMPMASTER": "DROPSHIP",
    "LAUNCH_READY": "DROPSHIP",
    "FREEFALL": "FREEFALL",
    "IN_MATCH_ALIVE": "IN_MATCH",
    "SPECTATING": "SPECTATING",
    "POST_MATCH_SUMMARY": "POST_MATCH",
}

LOCAL_METADATA = {
    "schemaVersion",
    "runId",
    "seq",
    "occurredAt",
    "elapsedMs",
    "event",
    "type",
    "payload",
}


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _atomic_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    replace_with_retry(temporary, path)


def _atomic_jsonl(path: Path, records: Iterable[dict[str, object]]) -> None:
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    with temporary.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
        handle.flush()
        os.fsync(handle.fileno())
    replace_with_retry(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, OSError):
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            # A writer may still be appending the final line. It will be read
            # on the next pass; earlier complete records remain usable.
            break
        if not isinstance(value, dict):
            break
        records.append(value)
    return records


def _local_payload(record: dict[str, Any]) -> dict[str, Any]:
    payload = record.get("payload")
    if isinstance(payload, dict):
        return dict(payload)
    return {key: value for key, value in record.items() if key not in LOCAL_METADATA}


def _local_event_name(record: dict[str, Any]) -> str:
    return str(record.get("event", record.get("type", "")))


def _phase_group(phase: str | None) -> str | None:
    return "LOBBY" if phase == "LOBBY_RETURNED" else phase


class ReportTransport(Protocol):
    def send(
        self,
        url: str,
        token: str,
        payload: dict[str, object],
        timeout_s: float,
    ) -> tuple[int, dict[str, Any], dict[str, str]]: ...


class UrllibReportTransport:
    def send(
        self,
        url: str,
        token: str,
        payload: dict[str, object],
        timeout_s: float,
    ) -> tuple[int, dict[str, Any], dict[str, str]]:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        request = Request(
            url,
            data=body,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "apex-controller-runner/1",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=timeout_s) as response:
                raw = response.read()
                try:
                    response_payload = json.loads(raw) if raw else {}
                except json.JSONDecodeError:
                    response_payload = {}
                return (
                    int(response.status),
                    response_payload if isinstance(response_payload, dict) else {},
                    {key.lower(): value for key, value in response.headers.items()},
                )
        except HTTPError as error:
            raw = error.read()
            try:
                response_payload = json.loads(raw) if raw else {}
            except json.JSONDecodeError:
                response_payload = {}
            return (
                int(error.code),
                response_payload if isinstance(response_payload, dict) else {},
                {key.lower(): value for key, value in error.headers.items()},
            )


@dataclass(frozen=True)
class SendOutcome:
    sent: int = 0
    pending: int = 0
    retry_after_s: float | None = None
    terminal: bool = False
    error: str | None = None


class ReportSession:
    """Durable remote-event projection for one immutable local run."""

    def __init__(self, run_dir: Path) -> None:
        self.run_dir = run_dir
        self.events_path = run_dir / "events.jsonl"
        self.outbox_path = run_dir / "report-outbox.jsonl"
        self.state_path = run_dir / "report-state.json"
        self.manifest_path = run_dir / "manifest.json"
        self.manifest = _read_json(self.manifest_path)
        self.state = _read_json(self.state_path)
        self.outbox = _read_jsonl(self.outbox_path)
        self._lock = threading.Lock()
        self._rebuild_derived_state()

    @property
    def run_id(self) -> str | None:
        value = self.manifest.get("runId")
        return str(value) if value else None

    @property
    def reporting(self) -> dict[str, Any]:
        value = self.manifest.get("reporting")
        return value if isinstance(value, dict) else {}

    @property
    def account_id(self) -> str | None:
        value = self.reporting.get("accountId")
        return str(value) if value else None

    @property
    def device_id(self) -> str | None:
        value = self.reporting.get("deviceId")
        return str(value) if value else None

    @property
    def lease_id(self) -> str | None:
        value = self.reporting.get("leaseId")
        return str(value) if value else None

    @property
    def lease_fence(self) -> int | None:
        value = self.reporting.get("leaseFence")
        return value if type(value) is int and value >= 1 else None

    @property
    def enabled(self) -> bool:
        base_enabled = bool(
            self.reporting.get("enabled", False)
            and self.run_id
            and self.account_id
            and self.device_id
        )
        has_lease_context = (
            "leaseId" in self.reporting or "leaseFence" in self.reporting
        )
        return base_enabled and (
            not has_lease_context
            or (self.lease_id is not None and self.lease_fence is not None)
        )

    @property
    def client(self) -> dict[str, object]:
        value = self.manifest.get("client")
        source = value if isinstance(value, dict) else {}

        def required_string(name: str, fallback: str) -> str:
            candidate = source.get(name)
            return candidate if isinstance(candidate, str) and candidate else fallback

        profile = self.manifest.get("profile")
        profile_fallback = (
            profile if isinstance(profile, str) and profile else "unknown"
        )
        return {
            "appVersion": required_string("appVersion", "unknown"),
            "profile": required_string("profile", profile_fallback),
            "configRevision": required_string("configRevision", "unknown"),
        }

    @property
    def accepted_through(self) -> int:
        return max(0, int(self.state.get("acceptedThrough", 0)))

    @property
    def source_through(self) -> int:
        return max(0, int(self.state.get("sourceThrough", 0)))

    @property
    def terminal_error(self) -> str | None:
        value = self.state.get("terminalError")
        return str(value) if value else None

    @property
    def finished(self) -> bool:
        return any(item.get("type") == "RUN_FINISHED" for item in self.outbox)

    def _rebuild_derived_state(self) -> None:
        self._phase: str | None = None
        self._round_number = 0
        for event in self.outbox:
            if event.get("type") != "MATCH_PHASE_CHANGED":
                continue
            payload = event.get("payload")
            if not isinstance(payload, dict):
                continue
            phase = payload.get("phase")
            if phase:
                self._phase = str(phase)
            self._round_number = max(
                self._round_number,
                int(payload.get("roundNumber", 0) or 0),
            )

    def _persist_state(self, **changes: object) -> None:
        self.state.update(changes)
        self.state["schemaVersion"] = 1
        self.state["updatedAt"] = _now_rfc3339()
        _atomic_json(self.state_path, self.state)

    def _append_events(
        self,
        source: dict[str, Any] | None,
        translated: list[tuple[str, dict[str, object]]],
        *,
        occurred_at: str | None = None,
        elapsed_ms: int | None = None,
    ) -> None:
        next_seq = max((int(item.get("seq", 0)) for item in self.outbox), default=0) + 1
        source_seq = None if source is None else int(source.get("seq", 0) or 0)
        if occurred_at is None:
            occurred_at = (
                _now_rfc3339()
                if source is None
                else str(source.get("occurredAt") or _now_rfc3339())
            )
        if elapsed_ms is None:
            elapsed_ms = (
                0 if source is None else max(0, int(source.get("elapsedMs", 0) or 0))
            )
        for event_type, payload in translated:
            self.outbox.append(
                {
                    "seq": next_seq,
                    "sourceSeq": source_seq,
                    "occurredAt": occurred_at,
                    "elapsedMs": elapsed_ms,
                    "type": event_type,
                    "payload": payload,
                }
            )
            next_seq += 1
        if translated:
            _atomic_jsonl(self.outbox_path, self.outbox)
            self._rebuild_derived_state()

    def _phase_event(
        self,
        state: str,
    ) -> tuple[str, dict[str, object]] | None:
        next_phase = STATE_PHASES.get(state)
        if next_phase is None or _phase_group(next_phase) == _phase_group(self._phase):
            return None
        previous = self._phase
        if next_phase == "LOBBY" and _phase_group(previous) not in {
            None,
            "LOBBY",
            "QUEUEING",
        }:
            next_phase = "LOBBY_RETURNED"
        entering_match = next_phase in {"DROPSHIP", "FREEFALL", "IN_MATCH"}
        previous_group = _phase_group(previous)
        if entering_match and previous_group in {
            None,
            "LOBBY",
            "QUEUEING",
            "POST_MATCH",
        }:
            self._round_number += 1
        self._phase = next_phase
        return (
            "MATCH_PHASE_CHANGED",
            {
                "phase": next_phase,
                "previousPhase": previous,
                "roundNumber": self._round_number,
            },
        )

    def _translate(self, record: dict[str, Any]) -> list[tuple[str, dict[str, object]]]:
        name = _local_event_name(record)
        payload = _local_payload(record)
        result: list[tuple[str, dict[str, object]]] = []
        if name == "RUN_STARTED":
            context = self.manifest.get("runContext")
            start_payload = dict(context) if isinstance(context, dict) else {}
            start_payload.update(payload)
            verification = self.reporting.get("identityVerification")
            if isinstance(verification, dict):
                start_payload["identityVerification"] = dict(verification)
            result.append(("RUN_STARTED", start_payload))
        elif name == "RUN_FINISHED":
            result.append(("RUN_FINISHED", payload))
        elif name == "LOBBY_PROGRESS":
            result.append(("LOBBY_PROGRESS", payload))
        elif name == "STATE_DETECTED":
            state = str(payload.get("state", ""))
            result.append(
                (
                    "STATE_CHANGED",
                    {
                        "from": payload.get("previousState"),
                        "to": state,
                        "source": payload.get("source"),
                        "ruleId": payload.get("ruleId"),
                        "confidence": payload.get("confidence", 0.0),
                        "observationVersion": payload.get("observationVersion"),
                    },
                )
            )
            phase_event = self._phase_event(state)
            if phase_event is not None:
                result.append(phase_event)
        elif name in {
            "ACTION_SENT",
            "ACTION_CONFIRMED",
            "ACTION_POSTCONDITION_REJECTED",
            "ACTION_ERROR",
        }:
            if name == "ACTION_SENT" and (
                payload.get("capability") == "in-match-melee"
                or payload.get("trigger") == "periodic"
            ):
                return []
            status = {
                "ACTION_SENT": "SENT",
                "ACTION_CONFIRMED": "CONFIRMED",
                "ACTION_POSTCONDITION_REJECTED": "REJECTED",
                "ACTION_ERROR": "FAILED",
            }[name]
            result.append(
                (
                    "ACTION_RESULT",
                    {
                        "capability": payload.get("capability"),
                        "action": payload.get("action"),
                        "status": status,
                        "attempt": payload.get("attempt"),
                        "originState": payload.get("originState", payload.get("state")),
                        "evidenceState": payload.get("evidenceState"),
                        "reason": payload.get("reason", payload.get("error")),
                    },
                )
            )
        elif name in REPORTABLE_INCIDENTS:
            result.append(
                (
                    "INCIDENT",
                    {
                        "kind": REPORTABLE_INCIDENTS[name],
                        "severity": "ERROR"
                        if name in {"CAPTURE_ERROR", "REPORTER_ERROR"}
                        else "WARNING",
                        "message": payload.get("error", payload.get("reason", name)),
                        "observedState": payload.get("state"),
                        "localEvidencePath": payload.get("path"),
                    },
                )
            )
        elif (
            name == "SCREENSHOT_SAVED"
            and "unknown" in str(payload.get("stage", "")).lower()
        ):
            result.append(
                (
                    "INCIDENT",
                    {
                        "kind": "UNKNOWN_SCREEN",
                        "severity": "WARNING",
                        "message": "未知画面已在 Runner 本地留证",
                        "observedState": None,
                        "localEvidencePath": payload.get("path"),
                    },
                )
            )
        return result

    def ingest(self) -> int:
        if not self.enabled:
            return 0
        with self._lock:
            source_records = _read_jsonl(self.events_path)
            existing_sources = {
                int(item["sourceSeq"])
                for item in self.outbox
                if isinstance(item.get("sourceSeq"), int)
            }
            cursor = self.source_through
            initial_cursor = cursor
            added = 0
            for record in source_records:
                source_seq = int(record.get("seq", 0) or 0)
                if source_seq <= cursor:
                    continue
                if source_seq != cursor + 1:
                    break
                translated = (
                    [] if source_seq in existing_sources else self._translate(record)
                )
                self._append_events(record, translated)
                added += len(translated)
                cursor = source_seq
            if cursor != initial_cursor:
                self._persist_state(sourceThrough=cursor)
            return added

    def append_heartbeat(
        self,
        payload: dict[str, object],
        *,
        elapsed_ms: int,
    ) -> None:
        with self._lock:
            self._append_events(
                None,
                [("HEARTBEAT", payload)],
                elapsed_ms=max(0, elapsed_ms),
            )

    def pending_events(self, limit: int) -> list[dict[str, object]]:
        accepted = self.accepted_through
        return [
            {
                "seq": int(item["seq"]),
                "occurredAt": str(item["occurredAt"]),
                "elapsedMs": int(item["elapsedMs"]),
                "type": str(item["type"]),
                "payload": dict(item.get("payload", {})),
            }
            for item in self.outbox
            if int(item.get("seq", 0)) > accepted
        ][:limit]

    def pending_count(self) -> int:
        return sum(
            1 for item in self.outbox if int(item.get("seq", 0)) > self.accepted_through
        )

    def report_seq(self, event_type: str) -> int | None:
        with self._lock:
            values = [
                int(item["seq"])
                for item in self.outbox
                if item.get("type") == event_type
            ]
        return values[-1] if values else None

    def request_payload(self, events: list[dict[str, object]]) -> dict[str, object]:
        payload: dict[str, object] = {
            "schemaVersion": 1,
            "accountId": self.account_id,
            "deviceId": self.device_id,
            "runId": self.run_id,
            "sentAt": _now_rfc3339(),
            "client": self.client,
            "events": events,
        }
        if self.lease_id is not None and self.lease_fence is not None:
            payload["lease"] = {
                "leaseId": self.lease_id,
                "leaseFence": self.lease_fence,
            }
        return payload

    def acknowledge(self, accepted_through: int) -> None:
        maximum = max((int(item.get("seq", 0)) for item in self.outbox), default=0)
        if accepted_through < self.accepted_through or accepted_through > maximum:
            raise ValueError("服务端 acceptedThrough 超出本地 outbox 范围")
        self._persist_state(
            acceptedThrough=accepted_through,
            lastSuccessAt=_now_rfc3339(),
            lastError=None,
            terminalError=None,
            terminalStatus=None,
        )

    def mark_error(
        self,
        message: str,
        *,
        terminal: bool,
        status: int | None = None,
    ) -> None:
        changes: dict[str, object] = {
            "lastError": message,
            "lastErrorAt": _now_rfc3339(),
        }
        if terminal:
            changes["terminalError"] = message
            changes["terminalStatus"] = status
        self._persist_state(**changes)

    def allow_new_process_retry(self) -> None:
        """Retry fixable terminal errors once after a new play process starts."""

        status = self.state.get("terminalStatus")
        if self.terminal_error and status != 409:
            self._persist_state(terminalError=None, terminalStatus=None)


class RemoteReporter:
    """Background, at-least-once delivery that never blocks the Pilot loop."""

    def __init__(
        self,
        settings: RunnerSettings,
        runs_root: Path,
        current_run_dir: Path,
        *,
        transport: ReportTransport | None = None,
        notify: Callable[[str], None] = lambda _: None,
        heartbeat_interval_s: float = 30.0,
        poll_interval_s: float = 1.0,
        request_timeout_s: float = 5.0,
        batch_size: int = 100,
    ) -> None:
        if not settings.enabled:
            raise ValueError("RemoteReporter 只能在远程上报启用时创建")
        self.settings = settings
        self.runs_root = runs_root
        self.current_run_dir = current_run_dir
        self.transport = transport or UrllibReportTransport()
        self.notify = notify
        self.heartbeat_interval_s = heartbeat_interval_s
        self.poll_interval_s = poll_interval_s
        self.request_timeout_s = request_timeout_s
        self.batch_size = max(1, min(100, batch_size))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._flush_deadline = 0.0
        self._last_heartbeat = time.monotonic()
        self._backoff_steps = (2.0, 5.0, 10.0, 30.0, 60.0)
        self._backoff_index = 0
        self._next_send_at = 0.0
        self._sessions: dict[Path, ReportSession] = {}
        self._ignored_dirs: set[Path] = set()
        self._terminal_notified: set[Path] = set()
        self._discover_sessions()

    def _safe_error(self, value: object) -> str:
        message = str(value)
        token = self.settings.report_token
        return message if not token else message.replace(token, "[REDACTED]")

    def _discover_sessions(self) -> None:
        try:
            directories = [item for item in self.runs_root.iterdir() if item.is_dir()]
        except FileNotFoundError:
            directories = []
        if self.current_run_dir not in directories:
            directories.append(self.current_run_dir)
        for directory in sorted(directories):
            if directory in self._sessions or directory in self._ignored_dirs:
                continue
            try:
                session = ReportSession(directory)
                if session.enabled and session.device_id == self.settings.device_id:
                    session.allow_new_process_retry()
                    self._sessions[directory] = session
                else:
                    self._ignored_dirs.add(directory)
            except (OSError, TypeError, ValueError) as error:
                self._ignored_dirs.add(directory)
                self.notify(
                    f"已隔离损坏的历史上报目录 {directory.name}："
                    f"{self._safe_error(error)}"
                )

    @property
    def current_session(self) -> ReportSession:
        session = self._sessions.get(self.current_run_dir)
        if session is None:
            raise RuntimeError("当前运行 manifest 未包含有效的远程绑定")
        return session

    def _heartbeat_payload(
        self,
        status: dict[str, Any],
    ) -> dict[str, object]:
        return {
            "runtimeState": status.get("runtimeState", "RUNNING"),
            "observedState": status.get("observedState"),
            "foreground": status.get("foreground"),
            "roundNumber": status.get("roundNumber", 0),
            "frames": status.get("frames", 0),
            "actionsSent": status.get("actionsSent", 0),
            "pendingReportCount": self.pending_count(),
        }

    def _maybe_heartbeat(self, now: float) -> None:
        if now - self._last_heartbeat < self.heartbeat_interval_s:
            return
        session = self.current_session
        if self._stop.is_set() or session.finished:
            return
        status = _read_json(self.current_run_dir / "status.json")
        session.append_heartbeat(
            self._heartbeat_payload(status),
            elapsed_ms=int(status.get("elapsedMs", 0) or 0),
        )
        self._last_heartbeat = now

    def _validate_response(
        self,
        session: ReportSession,
        payload: dict[str, Any],
    ) -> int:
        schema_version = payload.get("schemaVersion")
        if type(schema_version) is not int or schema_version != 1:
            raise ValueError("服务端响应 schemaVersion 不是 1")
        for field, expected in (
            ("accountId", session.account_id),
            ("deviceId", session.device_id),
            ("runId", session.run_id),
        ):
            value = payload.get(field)
            if not isinstance(value, str) or value != expected:
                raise ValueError(f"服务端响应 {field} 与请求不一致")
        accepted = payload.get("acceptedThrough")
        if type(accepted) is not int:
            raise ValueError("服务端响应缺少整数 acceptedThrough")
        server_time = payload.get("serverTime")
        if not isinstance(server_time, str) or not server_time:
            raise ValueError("服务端响应缺少 RFC3339 serverTime")
        try:
            parsed_server_time = datetime.fromisoformat(
                server_time[:-1] + "+00:00"
                if server_time.endswith("Z")
                else server_time
            )
        except ValueError as error:
            raise ValueError("服务端响应 serverTime 不是 RFC3339 时间") from error
        if parsed_server_time.tzinfo is None:
            raise ValueError("服务端响应 serverTime 缺少时区")
        return accepted

    def _send_session(self, session: ReportSession) -> SendOutcome:
        if session.terminal_error:
            return SendOutcome(
                pending=session.pending_count(),
                terminal=True,
                error=session.terminal_error,
            )
        events = session.pending_events(self.batch_size)
        if not events:
            return SendOutcome()
        assert self.settings.report_url is not None
        assert self.settings.report_token is not None
        try:
            status, response, headers = self.transport.send(
                self.settings.report_url,
                self.settings.report_token,
                session.request_payload(events),
                self.request_timeout_s,
            )
        except (OSError, URLError, TimeoutError) as error:
            message = self._safe_error(f"网络错误：{error}")
            session.mark_error(message, terminal=False)
            return SendOutcome(pending=session.pending_count(), error=message)

        if status == 200:
            try:
                accepted = self._validate_response(session, response)
                session.acknowledge(accepted)
            except (TypeError, ValueError) as error:
                message = self._safe_error(f"无效上报响应：{error}")
                session.mark_error(message, terminal=False)
                return SendOutcome(
                    pending=session.pending_count(),
                    terminal=False,
                    error=message,
                )
            return SendOutcome(sent=len(events), pending=session.pending_count())

        error_payload = response.get("error")
        detail = error_payload if isinstance(error_payload, dict) else {}
        message = self._safe_error(detail.get("message") or f"HTTP {status}")
        terminal = 400 <= status < 500 and status not in {408, 413, 429}
        session.mark_error(message, terminal=terminal, status=status)
        retry_after: float | None = None
        if status == 429:
            try:
                retry_after = max(0.0, float(headers.get("retry-after", "0")))
            except ValueError:
                retry_after = None
        if status == 413:
            if self.batch_size > 1:
                self.batch_size = max(1, self.batch_size // 2)
            else:
                terminal = True
                session.mark_error(message, terminal=True, status=status)
        return SendOutcome(
            pending=session.pending_count(),
            retry_after_s=retry_after,
            terminal=terminal,
            error=message,
        )

    def process_once(self, *, allow_send: bool = True) -> SendOutcome:
        self._discover_sessions()
        for session in self._sessions.values():
            if not session.finished:
                session.ingest()
        self._maybe_heartbeat(time.monotonic())

        pending = self.pending_count()
        if not allow_send or not pending:
            return SendOutcome(pending=pending)
        now = time.monotonic()
        if now < self._next_send_at:
            return SendOutcome(pending=pending, retry_after_s=self._next_send_at - now)

        total_sent = 0
        retry_after: float | None = None
        last_error: str | None = None
        terminal = False
        for session in sorted(
            self._sessions.values(), key=lambda item: item.run_dir.name
        ):
            outcome = self._send_session(session)
            total_sent += outcome.sent
            terminal = terminal or outcome.terminal
            if (
                outcome.error
                and outcome.terminal
                and session.run_dir not in self._terminal_notified
            ):
                self._terminal_notified.add(session.run_dir)
                self.notify(f"远程上报已停止重试 {session.run_id}：{outcome.error}")
            if outcome.error and not outcome.terminal:
                last_error = outcome.error
                retry_after = outcome.retry_after_s
                break

        if last_error:
            delay = (
                retry_after
                if retry_after is not None
                else self._backoff_steps[
                    min(self._backoff_index, len(self._backoff_steps) - 1)
                ]
            )
            self._backoff_index = min(
                self._backoff_index + 1, len(self._backoff_steps) - 1
            )
            self._next_send_at = time.monotonic() + delay
        elif total_sent:
            self._backoff_index = 0
            self._next_send_at = 0.0
        return SendOutcome(
            sent=total_sent,
            pending=self.pending_count(),
            retry_after_s=retry_after,
            terminal=terminal,
            error=last_error,
        )

    def pending_count(self) -> int:
        return sum(session.pending_count() for session in self._sessions.values())

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                outcome = self.process_once()
                if outcome.error:
                    self.notify(f"远程上报暂未成功：{outcome.error}")
            except Exception as error:
                self.notify(f"远程上报器异常：{self._safe_error(error)}")
            self._wake.wait(self.poll_interval_s)
            self._wake.clear()

        while time.monotonic() < self._flush_deadline:
            try:
                outcome = self.process_once()
            except Exception:
                break
            if outcome.pending == 0 or outcome.error or outcome.terminal:
                break
            time.sleep(min(0.1, max(0.0, self._flush_deadline - time.monotonic())))

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="apex-remote-reporter",
            daemon=True,
        )
        self._thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self, *, flush_timeout_s: float = 5.0) -> int:
        thread = self._thread
        if thread is None:
            self.process_once(allow_send=False)
            return self.pending_count()
        self._flush_deadline = time.monotonic() + max(0.0, flush_timeout_s)
        self._stop.set()
        self._wake.set()
        thread.join(timeout=max(0.5, flush_timeout_s + self.request_timeout_s + 0.5))
        if thread.is_alive():
            raise RuntimeError("远程上报线程未在超时内退出")
        self._thread = None
        return self.pending_count()

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
import json
import os
from pathlib import Path
import secrets
from typing import Any

from .atomic_files import replace_with_retry


class WorkflowPhase(str, Enum):
    CLAIMING = "CLAIMING"
    EA_STARTING = "EA_STARTING"
    EA_SIGNING_IN = "EA_SIGNING_IN"
    EA_IDENTITY_VERIFYING = "EA_IDENTITY_VERIFYING"
    APEX_STARTING = "APEX_STARTING"
    APEX_PLAYING = "APEX_PLAYING"
    APEX_STOPPING = "APEX_STOPPING"
    EA_SIGNING_OUT = "EA_SIGNING_OUT"
    LEASE_COMPLETING = "LEASE_COMPLETING"


class OrchestratorRunState(str, Enum):
    ACTIVE = "ACTIVE"
    PAUSED_RETRYABLE = "PAUSED_RETRYABLE"
    PAUSED_MANUAL = "PAUSED_MANUAL"
    STOPPED = "STOPPED"


class PendingOperation(str, Enum):
    CLAIM = "CLAIM"
    CREDENTIALS = "CREDENTIALS"
    RENEW = "RENEW"
    OTP = "OTP"
    CLOSE = "CLOSE"


@dataclass(frozen=True)
class OrchestrationCheckpoint:
    device_id: str
    workflow_phase: WorkflowPhase = WorkflowPhase.CLAIMING
    run_state: OrchestratorRunState = OrchestratorRunState.ACTIVE
    claim_request_id: str | None = None
    lease_id: str | None = None
    lease_fence: int | None = None
    lease_expires_at: str | None = None
    account_id: str | None = None
    target_level: int | None = None
    resume_phase: WorkflowPhase | None = None
    pending_operation: PendingOperation | None = None
    operation_id: str | None = None
    active_play_run_id: str | None = None
    previous_play_run_ids: tuple[str, ...] = ()
    target_reading: dict[str, object] | None = None
    report_evidence: dict[str, object] | None = None
    result_status: str | None = None
    result_error_code: str | None = None
    cleanup_verified_at: str | None = None
    last_error_code: str | None = None
    updated_at: str | None = None

    def evolve(self, **changes: object) -> "OrchestrationCheckpoint":
        return replace(self, **changes)

    @property
    def has_lease(self) -> bool:
        return (
            self.lease_id is not None
            and self.lease_fence is not None
            and self.account_id is not None
        )

    def to_payload(self) -> dict[str, object]:
        values: dict[str, object | None] = {
            "schemaVersion": 1,
            "deviceId": self.device_id,
            "claimRequestId": self.claim_request_id,
            "leaseId": self.lease_id,
            "leaseFence": self.lease_fence,
            "leaseExpiresAt": self.lease_expires_at,
            "accountId": self.account_id,
            "targetLevel": self.target_level,
            "workflowPhase": self.workflow_phase.value,
            "runState": self.run_state.value,
            "resumePhase": (
                None if self.resume_phase is None else self.resume_phase.value
            ),
            "pendingOperation": (
                None
                if self.pending_operation is None
                else self.pending_operation.value
            ),
            "operationId": self.operation_id,
            "activePlayRunId": self.active_play_run_id,
            "previousPlayRunIds": list(self.previous_play_run_ids),
            "targetReading": self.target_reading,
            "reportEvidence": self.report_evidence,
            "resultStatus": self.result_status,
            "resultErrorCode": self.result_error_code,
            "cleanupVerifiedAt": self.cleanup_verified_at,
            "lastErrorCode": self.last_error_code,
            "updatedAt": self.updated_at or _now(),
        }
        return {key: value for key, value in values.items() if value is not None}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "OrchestrationCheckpoint":
        if payload.get("schemaVersion") != 1:
            raise ValueError("账号编排 checkpoint 只支持 schemaVersion=1")
        device_id = payload.get("deviceId")
        if not isinstance(device_id, str) or not device_id:
            raise ValueError("账号编排 checkpoint 缺少 deviceId")
        previous = payload.get("previousPlayRunIds", [])
        if not isinstance(previous, list) or not all(
            isinstance(item, str) for item in previous
        ):
            raise ValueError("previousPlayRunIds 必须是字符串数组")
        return cls(
            device_id=device_id,
            claim_request_id=_optional_string(payload.get("claimRequestId")),
            lease_id=_optional_string(payload.get("leaseId")),
            lease_fence=_optional_int(payload.get("leaseFence")),
            lease_expires_at=_optional_string(payload.get("leaseExpiresAt")),
            account_id=_optional_string(payload.get("accountId")),
            target_level=_optional_int(payload.get("targetLevel")),
            workflow_phase=WorkflowPhase(
                payload.get("workflowPhase", WorkflowPhase.CLAIMING.value)
            ),
            run_state=OrchestratorRunState(
                payload.get("runState", OrchestratorRunState.ACTIVE.value)
            ),
            resume_phase=(
                None
                if payload.get("resumePhase") is None
                else WorkflowPhase(payload["resumePhase"])
            ),
            pending_operation=(
                None
                if payload.get("pendingOperation") is None
                else PendingOperation(payload["pendingOperation"])
            ),
            operation_id=_optional_string(payload.get("operationId")),
            active_play_run_id=_optional_string(payload.get("activePlayRunId")),
            previous_play_run_ids=tuple(previous),
            target_reading=_optional_mapping(payload.get("targetReading")),
            report_evidence=_optional_mapping(payload.get("reportEvidence")),
            result_status=_optional_string(payload.get("resultStatus")),
            result_error_code=_optional_string(payload.get("resultErrorCode")),
            cleanup_verified_at=_optional_string(payload.get("cleanupVerifiedAt")),
            last_error_code=_optional_string(payload.get("lastErrorCode")),
            updated_at=_optional_string(payload.get("updatedAt")),
        )


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("checkpoint 字符串字段类型错误")
    return value


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if type(value) is not int:
        raise ValueError("checkpoint 整数字段类型错误")
    return value


def _optional_mapping(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("checkpoint 对象字段类型错误")
    return dict(value)


class AtomicCheckpointStore:
    def __init__(
        self,
        path: Path,
        *,
        completed_path: Path | None = None,
    ) -> None:
        self.path = Path(path)
        self.completed_path = (
            Path(completed_path)
            if completed_path is not None
            else self.path.with_name("account-cycle-completed.jsonl")
        )

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(
            f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            replace_with_retry(temporary, path)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def load(self) -> OrchestrationCheckpoint | None:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as error:
            raise ValueError("账号编排 checkpoint 已损坏，必须人工处置") from error
        if not isinstance(payload, dict):
            raise ValueError("账号编排 checkpoint 根节点必须是对象")
        return OrchestrationCheckpoint.from_payload(payload)

    def save(
        self,
        checkpoint: OrchestrationCheckpoint,
    ) -> OrchestrationCheckpoint:
        updated = checkpoint.evolve(updated_at=_now())
        payload = updated.to_payload()
        self._reject_sensitive_keys(payload)
        self._atomic_write(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        )
        return updated

    def archive_completion(self, payload: dict[str, object]) -> None:
        self._reject_sensitive_keys(payload)
        records: list[str] = []
        try:
            records = self.completed_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            pass
        record = {"schemaVersion": 1, **payload, "completedAt": _now()}
        records.append(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
        self._atomic_write(self.completed_path, "\n".join(records) + "\n")

    def clear_completed_lease(
        self,
        checkpoint: OrchestrationCheckpoint,
    ) -> OrchestrationCheckpoint:
        if checkpoint.lease_id is not None:
            self.archive_completion(
                {
                    "deviceId": checkpoint.device_id,
                    "leaseId": checkpoint.lease_id,
                    "leaseFence": checkpoint.lease_fence,
                    "accountId": checkpoint.account_id,
                    "runId": checkpoint.active_play_run_id,
                    "targetReading": checkpoint.target_reading,
                    "reportEvidence": checkpoint.report_evidence,
                }
            )
        return self.save(
            OrchestrationCheckpoint(
                device_id=checkpoint.device_id,
                workflow_phase=WorkflowPhase.CLAIMING,
                run_state=OrchestratorRunState.ACTIVE,
            )
        )

    @staticmethod
    def _reject_sensitive_keys(value: object, trail: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key)
                lowered = name.lower()
                if any(
                    marker in lowered
                    for marker in (
                        "password",
                        "secret",
                        "token",
                        "otp",
                        "loginidentifier",
                    )
                ):
                    location = f"{trail}.{name}" if trail else name
                    raise ValueError(f"checkpoint 不允许敏感字段：{location}")
                AtomicCheckpointStore._reject_sensitive_keys(
                    child,
                    f"{trail}.{name}" if trail else name,
                )
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                AtomicCheckpointStore._reject_sensitive_keys(
                    child,
                    f"{trail}[{index}]",
                )

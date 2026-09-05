from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from enum import Enum
import ipaddress
import json
from typing import Callable, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener


DEFAULT_TASK_TYPE = "LEVEL_TO_TARGET"
MAX_PROVIDER_RESPONSE_BYTES = 1024 * 1024
# The runner and the Provider keep their own clocks, and nothing keeps them in
# step. Anything that must compare across them needs room to be wrong.
CLOCK_SKEW_TOLERANCE_S = 120.0
# A TOTP is only valid to the end of its 30-second window, so a code handed
# over with a second left cannot survive being typed.
MIN_USABLE_OTP_LIFETIME_S = 8.0


def _is_private_test_host(hostname: str | None) -> bool:
    if hostname == "localhost":
        return True
    if hostname is None:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


class LeaseProviderError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "PROVIDER_ERROR",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class LeaseStaleError(LeaseProviderError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "STALE_LEASE",
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)


class IdempotencyConflictError(LeaseProviderError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "IDEMPOTENCY_CONFLICT",
        retryable: bool = False,
    ) -> None:
        super().__init__(message, code=code, retryable=retryable)


class LeaseState(str, Enum):
    ACTIVE = "ACTIVE"
    LEASE_UNCERTAIN = "LEASE_UNCERTAIN"
    EXPIRED_UNCONFIRMED = "EXPIRED_UNCONFIRMED"
    COMPLETION_PENDING = "COMPLETION_PENDING"
    COMPLETED = "COMPLETED"
    CLOSED = "CLOSED"


class OtpMethod(str, Enum):
    TOTP = "TOTP"
    EMAIL = "EMAIL"


@dataclass(frozen=True)
class SecretCredentials:
    login_identifier: str = field(repr=False)
    password: str = field(repr=False)
    # Old Providers only supported TOTP and did not return this field.
    otp_methods: tuple[OtpMethod, ...] = (OtpMethod.TOTP,)


@dataclass(frozen=True)
class OtpCode:
    code: str = field(repr=False)
    challenge_id: str
    received_at: datetime
    expires_at: datetime

    @property
    def lifetime_s(self) -> float:
        """How long the Provider said this code lives.

        Both timestamps come from the Provider, so this is the one duration
        here that no clock difference can distort.
        """

        return (self.expires_at - self.received_at).total_seconds()

    def valid_for(
        self,
        challenge_id: str,
        challenge_started_at: datetime,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Reject a code that belongs to an older challenge.

        `challenge_started_at` is this machine's clock and `received_at` is the
        Provider's, so the two are only ever loosely comparable. A runner whose
        clock ran a few seconds ahead of the server failed this check on every
        single attempt. The challenge id is the exact half of the guarantee —
        it is freshly generated per attempt — and the timestamps only have to
        rule out an answer from a materially older challenge.
        """

        del now  # Absolute expiry cannot be judged across two clocks.
        if self.challenge_id != challenge_id:
            return False
        if self.lifetime_s <= 0:
            return False
        skew = timedelta(seconds=CLOCK_SKEW_TOLERANCE_S)
        return self.received_at >= challenge_started_at - skew


@dataclass(frozen=True)
class AccountLease:
    lease_id: str
    lease_fence: int
    account_id: str
    target_level: int
    expires_at: datetime
    renew_after: datetime
    expected_ea_account_id: str | None = None
    ring_progress: int | None = None
    ring_target: int | None = None

    def __post_init__(self) -> None:
        if self.lease_fence < 1:
            raise ValueError("leaseFence 必须大于 0")
        if self.target_level < 1:
            raise ValueError("targetLevel 必须大于 0")
        if (self.ring_progress is None) != (self.ring_target is None):
            raise ValueError("ringProgress/ringTarget 必须同时存在或同时缺失")
        if self.ring_progress is not None and (
            self.ring_progress < 0
            or self.ring_target is None
            or self.ring_target < 1
            or self.ring_progress > self.ring_target
        ):
            raise ValueError("缩圈历史进度无效")


@dataclass(frozen=True)
class LeaseStatus:
    lease_id: str
    lease_fence: int
    account_id: str
    state: LeaseState
    expires_at: datetime | None = None
    renew_after: datetime | None = None
    retry_after_s: float | None = None
    provider_status: str | None = None

    @property
    def terminal(self) -> bool:
        return self.state in {LeaseState.COMPLETED, LeaseState.CLOSED}


@dataclass(frozen=True)
class CompletionEvidence:
    run_id: str
    level: int
    lobby_progress_seq: int
    run_finished_seq: int

    def __post_init__(self) -> None:
        if self.level < 1:
            raise ValueError("完成证据的等级必须大于 0")
        if self.lobby_progress_seq < 1 or self.run_finished_seq < 1:
            raise ValueError("完成证据的远程序号必须大于 0")


@dataclass(frozen=True)
class CleanupEvidence:
    input_released: bool
    apex_exited: bool
    ea_signed_out: bool
    verified_at: datetime | None = None

    @property
    def complete(self) -> bool:
        return self.input_released and self.apex_exited and self.ea_signed_out


class AccountProvider(Protocol):
    def claim(
        self,
        claim_request_id: str,
        task_type: str,
    ) -> AccountLease | None: ...

    def current(self) -> LeaseStatus | None: ...

    def status(
        self,
        lease_id: str,
        lease_fence: int,
    ) -> LeaseStatus: ...

    def recover(
        self,
        lease_id: str,
        lease_fence: int,
    ) -> AccountLease: ...

    def credentials(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
    ) -> SecretCredentials: ...

    def renew(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        phase: str,
        run_id: str | None,
        *,
        recover_expired: bool = False,
    ) -> LeaseStatus: ...

    def request_otp(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        challenge_id: str,
        challenge_started_at: datetime,
        method: OtpMethod = OtpMethod.TOTP,
    ) -> OtpCode: ...

    def close(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        outcome: str,
        run_id: str | None,
        evidence: CompletionEvidence | None,
        reason_code: str,
        cleanup: CleanupEvidence,
    ) -> LeaseStatus: ...


@dataclass(frozen=True)
class ProviderHttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes = field(repr=False)


class ProviderHttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> ProviderHttpResponse: ...


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibProviderTransport:
    """Small no-redirect HTTPS transport so bearer tokens cannot change hosts."""

    def __init__(self) -> None:
        self._opener = build_opener(_NoRedirectHandler())

    @staticmethod
    def _read_limited(response) -> bytes:
        body = response.read(MAX_PROVIDER_RESPONSE_BYTES + 1)
        if len(body) > MAX_PROVIDER_RESPONSE_BYTES:
            raise LeaseProviderError(
                "Provider 响应超过大小限制",
                code="PROVIDER_RESPONSE_TOO_LARGE",
            )
        return body

    def request(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_s: float,
    ) -> ProviderHttpResponse:
        request = Request(url, data=body, headers=dict(headers), method=method)
        try:
            with self._opener.open(request, timeout=timeout_s) as response:
                return ProviderHttpResponse(
                    status=int(response.status),
                    headers=dict(response.headers.items()),
                    body=self._read_limited(response),
                )
        except HTTPError as error:
            try:
                response_body = self._read_limited(error)
            finally:
                error.close()
            return ProviderHttpResponse(
                status=int(error.code),
                headers=dict(error.headers.items()) if error.headers else {},
                body=response_body,
            )
        except (URLError, TimeoutError, OSError) as error:
            raise LeaseProviderError(
                "无法连接账号租约服务",
                code="PROVIDER_UNREACHABLE",
                retryable=True,
            ) from error


class HttpAccountProvider:
    """HTTP implementation of the fenced account-lease contract."""

    _STATE_MAP = {
        "CLAIMED": LeaseState.ACTIVE,
        "RUNNING": LeaseState.ACTIVE,
        "COMPLETION_PENDING": LeaseState.COMPLETION_PENDING,
        "EXPIRED_UNCONFIRMED": LeaseState.EXPIRED_UNCONFIRMED,
        "COMPLETED": LeaseState.COMPLETED,
        "FAILED": LeaseState.CLOSED,
        "RELEASED": LeaseState.CLOSED,
    }

    def __init__(
        self,
        lease_url: str,
        provider_token: str,
        *,
        client_version: str,
        timeout_s: float = 15.0,
        transport: ProviderHttpTransport | None = None,
    ) -> None:
        parsed = urlparse(str(lease_url))
        local_http = parsed.scheme == "http" and _is_private_test_host(parsed.hostname)
        if (
            not parsed.netloc
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or (parsed.scheme != "https" and not local_http)
        ):
            raise ValueError("leaseUrl 必须是 HTTPS 完整接口 URL（本机或私网联调除外）")
        token = str(provider_token)
        if not 1 <= len(token) <= 512:
            raise ValueError("providerToken 长度必须在 1 到 512 之间")
        version = str(client_version).strip()
        if not 1 <= len(version) <= 64:
            raise ValueError("clientVersion 长度必须在 1 到 64 之间")
        self.lease_url = str(lease_url).rstrip("/")
        self._provider_token = token
        self.client_version = version
        self.timeout_s = max(1.0, float(timeout_s))
        self.transport = transport or UrllibProviderTransport()
        self.claim_retry_after_s: float | None = None
        self._lease_accounts: dict[str, str] = {}

    def __repr__(self) -> str:
        return (
            f"HttpAccountProvider(lease_url={self.lease_url!r}, "
            f"client_version={self.client_version!r}, provider_token=<redacted>)"
        )

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        lowered = name.lower()
        for key, value in headers.items():
            if str(key).lower() == lowered:
                return str(value)
        return None

    @classmethod
    def _retry_after(cls, response: ProviderHttpResponse) -> float | None:
        raw = cls._header(response.headers, "Retry-After")
        if not raw:
            return None
        try:
            return max(0.0, float(raw))
        except ValueError:
            try:
                moment = parsedate_to_datetime(raw)
            except (TypeError, ValueError, OverflowError):
                return None
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (moment.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds(),
            )

    @staticmethod
    def _decode_json(body: bytes) -> dict[str, object]:
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise LeaseProviderError(
                "Provider 返回了无效 JSON",
                code="INVALID_PROVIDER_RESPONSE",
            ) from error
        if not isinstance(value, dict):
            raise LeaseProviderError(
                "Provider 响应根节点不是对象",
                code="INVALID_PROVIDER_RESPONSE",
            )
        return value

    def _request(
        self,
        method: str,
        suffix: str = "",
        *,
        payload: dict[str, object] | None = None,
        operation_id: str | None = None,
        allow_no_content: bool = False,
        timeout_s: float | None = None,
    ) -> tuple[dict[str, object] | None, ProviderHttpResponse]:
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._provider_token}",
        }
        body = None
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            ).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if operation_id is not None:
            if not 1 <= len(operation_id) <= 128:
                raise LeaseProviderError(
                    "幂等操作 ID 长度无效",
                    code="INVALID_OPERATION_ID",
                )
            headers["Idempotency-Key"] = operation_id
        try:
            response = self.transport.request(
                method,
                f"{self.lease_url}{suffix}",
                headers,
                body,
                self.timeout_s if timeout_s is None else max(1.0, float(timeout_s)),
            )
        except LeaseProviderError:
            raise
        except Exception as error:
            raise LeaseProviderError(
                "账号租约请求失败",
                code="PROVIDER_UNREACHABLE",
                retryable=True,
            ) from error

        if response.status == 204:
            if allow_no_content:
                return None, response
            raise LeaseProviderError(
                "Provider 意外返回空响应",
                code="INVALID_PROVIDER_RESPONSE",
            )

        try:
            decoded = self._decode_json(response.body)
        except LeaseProviderError as error:
            if not 200 <= response.status < 300:
                raise LeaseProviderError(
                    f"Provider HTTP {response.status}",
                    code=f"HTTP_{response.status}",
                    retryable=response.status == 429 or response.status >= 500,
                ) from error
            raise
        if not 200 <= response.status < 300:
            error_payload = decoded.get("error")
            details = error_payload if isinstance(error_payload, dict) else {}
            code = str(details.get("code") or f"HTTP_{response.status}")[:128]
            message = str(details.get("message") or "Provider 请求被拒绝")[:512]
            retryable = (
                details.get("retryable") is True
                or response.status == 429
                or response.status >= 500
            )
            error_type = LeaseProviderError
            if code in {"STALE_LEASE", "LEASE_NOT_ACTIVE"}:
                error_type = LeaseStaleError
            elif code == "IDEMPOTENCY_CONFLICT":
                error_type = IdempotencyConflictError
            raise error_type(message, code=code, retryable=retryable)
        return decoded, response

    @staticmethod
    def _string(payload: Mapping[str, object], name: str) -> str:
        value = payload.get(name)
        if not isinstance(value, str) or not value:
            raise LeaseProviderError(
                f"Provider 响应缺少 {name}",
                code="INVALID_PROVIDER_RESPONSE",
            )
        return value

    @staticmethod
    def _positive_int(payload: Mapping[str, object], name: str) -> int:
        value = payload.get(name)
        if type(value) is not int or value < 1:
            raise LeaseProviderError(
                f"Provider 响应中的 {name} 无效",
                code="INVALID_PROVIDER_RESPONSE",
            )
        return value

    @staticmethod
    def _optional_int(
        payload: Mapping[str, object], name: str, *, minimum: int
    ) -> int | None:
        value = payload.get(name)
        if value is None:
            return None
        if type(value) is not int or value < minimum:
            raise LeaseProviderError(
                f"Provider 响应中的 {name} 无效",
                code="INVALID_PROVIDER_RESPONSE",
            )
        return value

    @staticmethod
    def _time(value: object, name: str, *, required: bool) -> datetime | None:
        if value is None and not required:
            return None
        if not isinstance(value, str):
            raise LeaseProviderError(
                f"Provider 响应中的 {name} 无效",
                code="INVALID_PROVIDER_RESPONSE",
            )
        try:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
            )
        except ValueError as error:
            raise LeaseProviderError(
                f"Provider 响应中的 {name} 无效",
                code="INVALID_PROVIDER_RESPONSE",
            ) from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise LeaseProviderError(
                f"Provider 响应中的 {name} 缺少时区",
                code="INVALID_PROVIDER_RESPONSE",
            )
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _rfc3339(value: datetime) -> str:
        if value.tzinfo is None or value.utcoffset() is None:
            raise LeaseProviderError(
                "challengeStartedAt 必须包含时区",
                code="INVALID_CHALLENGE_TIME",
            )
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _validate_identity(self, payload: Mapping[str, object]) -> str:
        identity = payload.get("expectedIdentity")
        if not isinstance(identity, dict) or identity.get("platform") != "ea":
            raise LeaseProviderError(
                "托管租约必须返回 EA 身份",
                code="INVALID_EXPECTED_IDENTITY",
            )
        account_id = identity.get("accountId")
        if not isinstance(account_id, str) or not account_id:
            raise LeaseProviderError(
                "托管租约缺少可验证的 EA 稳定账号 ID",
                code="INVALID_EXPECTED_IDENTITY",
            )
        return account_id

    def _lease(self, payload: Mapping[str, object]) -> AccountLease:
        if payload.get("schemaVersion") != 1:
            raise LeaseProviderError(
                "Provider schemaVersion 不受支持",
                code="INVALID_PROVIDER_RESPONSE",
            )
        expires_at = self._time(payload.get("expiresAt"), "expiresAt", required=True)
        renew_after = self._time(payload.get("renewAfter"), "renewAfter", required=True)
        assert expires_at is not None and renew_after is not None
        ring_progress = self._optional_int(payload, "ringProgress", minimum=0)
        ring_target = self._optional_int(payload, "ringTarget", minimum=1)
        if (ring_progress is None) != (ring_target is None) or (
            ring_progress is not None
            and ring_target is not None
            and ring_progress > ring_target
        ):
            raise LeaseProviderError(
                "Provider 响应中的缩圈历史进度无效",
                code="INVALID_PROVIDER_RESPONSE",
            )
        lease = AccountLease(
            lease_id=self._string(payload, "leaseId"),
            lease_fence=self._positive_int(payload, "leaseFence"),
            account_id=self._string(payload, "accountId"),
            target_level=self._positive_int(payload, "targetLevel"),
            expires_at=expires_at,
            renew_after=renew_after,
            expected_ea_account_id=self._validate_identity(payload),
            ring_progress=ring_progress,
            ring_target=ring_target,
        )
        known_account = self._lease_accounts.get(lease.lease_id)
        if known_account is not None and known_account != lease.account_id:
            raise LeaseStaleError(
                "Provider 响应的 accountId 与已知租约不一致",
                code="STALE_LEASE",
            )
        self._lease_accounts[lease.lease_id] = lease.account_id
        return lease

    def _status(
        self,
        payload: Mapping[str, object],
        *,
        lease_id: str | None = None,
        lease_fence: int | None = None,
        retry_after_s: float | None = None,
    ) -> LeaseStatus:
        if payload.get("schemaVersion") != 1:
            raise LeaseProviderError(
                "Provider schemaVersion 不受支持",
                code="INVALID_PROVIDER_RESPONSE",
            )
        raw_state = self._string(payload, "status")
        try:
            state = self._STATE_MAP[raw_state]
        except KeyError as error:
            raise LeaseProviderError(
                f"Provider 返回未知租约状态 {raw_state}",
                code="INVALID_PROVIDER_RESPONSE",
            ) from error
        actual_id = self._string(payload, "leaseId")
        actual_fence = self._positive_int(payload, "leaseFence")
        if lease_id is not None and actual_id != lease_id:
            raise LeaseStaleError("Provider 响应的 leaseId 不一致", code="STALE_LEASE")
        if lease_fence is not None and actual_fence != lease_fence:
            raise LeaseStaleError("Provider 响应的 leaseFence 不一致", code="STALE_LEASE")
        account_id = self._string(payload, "accountId")
        known_account = self._lease_accounts.get(actual_id)
        if known_account is not None and known_account != account_id:
            raise LeaseStaleError(
                "Provider 响应的 accountId 与已知租约不一致",
                code="STALE_LEASE",
            )
        self._lease_accounts[actual_id] = account_id
        return LeaseStatus(
            lease_id=actual_id,
            lease_fence=actual_fence,
            account_id=account_id,
            state=state,
            expires_at=self._time(payload.get("expiresAt"), "expiresAt", required=False),
            renew_after=self._time(payload.get("renewAfter"), "renewAfter", required=False),
            retry_after_s=retry_after_s,
            provider_status=raw_state,
        )

    @staticmethod
    def _lease_suffix(lease_id: str, action: str = "") -> str:
        suffix = f"/{quote(lease_id, safe='')}"
        return f"{suffix}/{action}" if action else suffix

    def claim(self, claim_request_id: str, task_type: str) -> AccountLease | None:
        if task_type != DEFAULT_TASK_TYPE:
            raise LeaseProviderError(
                f"不支持任务类型 {task_type}",
                code="UNSUPPORTED_TASK_TYPE",
            )
        payload, response = self._request(
            "POST",
            payload={
                "schemaVersion": 1,
                "taskType": task_type,
                "clientVersion": self.client_version,
            },
            operation_id=claim_request_id,
            allow_no_content=True,
        )
        self.claim_retry_after_s = self._retry_after(response)
        return None if payload is None else self._lease(payload)

    def current(self) -> LeaseStatus | None:
        payload, response = self._request(
            "GET", "/current", allow_no_content=True
        )
        self.claim_retry_after_s = self._retry_after(response)
        return None if payload is None else self._status(payload)

    def status(self, lease_id: str, lease_fence: int) -> LeaseStatus:
        payload, response = self._request("GET", self._lease_suffix(lease_id))
        assert payload is not None
        return self._status(
            payload,
            lease_id=lease_id,
            lease_fence=lease_fence,
            retry_after_s=self._retry_after(response),
        )

    def recover(self, lease_id: str, lease_fence: int) -> AccountLease:
        payload, _ = self._request("GET", self._lease_suffix(lease_id))
        assert payload is not None
        lease = self._lease(payload)
        if lease.lease_id != lease_id or lease.lease_fence != lease_fence:
            raise LeaseStaleError("恢复租约时 lease/fence 不一致", code="STALE_LEASE")
        return lease

    def credentials(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
    ) -> SecretCredentials:
        payload, _ = self._request(
            "POST",
            self._lease_suffix(lease_id, "credentials"),
            payload={"schemaVersion": 1, "leaseFence": lease_fence},
            operation_id=operation_id,
        )
        assert payload is not None
        raw_methods = payload.get("otpMethods")
        if raw_methods is None:
            otp_methods = (OtpMethod.TOTP,)
        elif not isinstance(raw_methods, list) or not raw_methods:
            raise LeaseProviderError(
                "Provider 返回了无效的 OTP 方法",
                code="INVALID_PROVIDER_RESPONSE",
            )
        else:
            try:
                otp_methods = tuple(
                    dict.fromkeys(OtpMethod(str(item)) for item in raw_methods)
                )
            except (TypeError, ValueError) as error:
                raise LeaseProviderError(
                    "Provider 返回了未知的 OTP 方法",
                    code="INVALID_PROVIDER_RESPONSE",
                ) from error
        return SecretCredentials(
            login_identifier=self._string(payload, "loginIdentifier"),
            password=self._string(payload, "password"),
            otp_methods=otp_methods,
        )

    def renew(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        phase: str,
        run_id: str | None,
        *,
        recover_expired: bool = False,
    ) -> LeaseStatus:
        request: dict[str, object] = {
            "schemaVersion": 1,
            "leaseFence": lease_fence,
            "phase": phase,
        }
        if run_id is not None:
            request["runId"] = run_id
        if recover_expired:
            request["recoverExpired"] = True
        payload, response = self._request(
            "POST",
            self._lease_suffix(lease_id, "renew"),
            payload=request,
            operation_id=operation_id,
        )
        assert payload is not None
        return self._status(
            payload,
            lease_id=lease_id,
            lease_fence=lease_fence,
            retry_after_s=self._retry_after(response),
        )

    def request_otp(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        challenge_id: str,
        challenge_started_at: datetime,
        method: OtpMethod = OtpMethod.TOTP,
    ) -> OtpCode:
        body: dict[str, object] = {
            "schemaVersion": 1,
            "leaseFence": lease_fence,
            "challengeId": challenge_id,
            "challengeStartedAt": self._rfc3339(challenge_started_at),
        }
        if method is not OtpMethod.TOTP:
            # Sent only when it changes the answer. The Provider validates
            # request bodies exactly and rejects unknown fields with 400, so a
            # Runner that always announced the method would demand a Provider
            # that already knows about methods — and would take the
            # authenticator path down with it on any rollback.
            body["method"] = method.value
        payload, _ = self._request(
            "POST",
            self._lease_suffix(lease_id, "otp-challenges"),
            payload=body,
            operation_id=operation_id,
            timeout_s=max(
                self.timeout_s,
                90.0 if method is OtpMethod.EMAIL else self.timeout_s,
            ),
        )
        assert payload is not None
        response_challenge = payload.get("challengeId")
        if response_challenge != challenge_id:
            raise LeaseProviderError(
                "OTP challengeId 与请求不一致",
                code="OTP_CHALLENGE_MISMATCH",
            )
        received_at = self._time(payload.get("receivedAt"), "receivedAt", required=True)
        expires_at = self._time(payload.get("expiresAt"), "expiresAt", required=True)
        assert received_at is not None and expires_at is not None
        otp = OtpCode(
            code=self._string(payload, "code"),
            challenge_id=challenge_id,
            received_at=received_at,
            expires_at=expires_at,
        )
        if not otp.valid_for(challenge_id, challenge_started_at):
            raise LeaseProviderError(
                "Provider 返回了旧的或已过期的 OTP",
                code="OTP_EXPIRED",
            )
        return otp

    def close(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        outcome: str,
        run_id: str | None,
        evidence: CompletionEvidence | None,
        reason_code: str,
        cleanup: CleanupEvidence,
    ) -> LeaseStatus:
        if outcome not in {"TARGET_REACHED", "FAILED", "RELEASED"}:
            raise LeaseProviderError(
                f"不支持关闭结果 {outcome}",
                code="UNSUPPORTED_OUTCOME",
            )
        if not cleanup.complete:
            raise LeaseProviderError(
                "本地清理证据不完整，拒绝关闭租约",
                code="CLEANUP_UNCONFIRMED",
            )
        request: dict[str, object] = {
            "schemaVersion": 1,
            "leaseFence": lease_fence,
            "outcome": outcome,
            "cleanup": {
                "apexProcessesStopped": cleanup.apex_exited,
                "eaSignedOut": cleanup.ea_signed_out,
                "verifiedAt": self._rfc3339(cleanup.verified_at or datetime.now(timezone.utc)),
            },
            "reasonCode": reason_code,
        }
        if run_id is not None:
            request["runId"] = run_id
        if outcome == "TARGET_REACHED":
            if evidence is None or run_id != evidence.run_id:
                raise LeaseProviderError(
                    "达到目标时缺少绑定的上报证据",
                    code="REPORT_EVIDENCE_MISSING",
                )
            request.update(
                {
                    "lobbyProgressSeq": evidence.lobby_progress_seq,
                    "runFinishedSeq": evidence.run_finished_seq,
                }
            )
        payload, response = self._request(
            "POST",
            self._lease_suffix(lease_id, "close"),
            payload=request,
            operation_id=operation_id,
        )
        assert payload is not None
        return self._status(
            payload,
            lease_id=lease_id,
            lease_fence=lease_fence,
            retry_after_s=self._retry_after(response),
        )

class FakeAccountProvider:
    """Deterministic Provider for orchestration and crash-recovery tests."""

    def __init__(
        self,
        leases: list[AccountLease] | None = None,
        *,
        credentials: dict[str, SecretCredentials] | None = None,
        otp_factory: (
            Callable[[str, datetime], OtpCode] | None
        ) = None,
        complete_on_close: bool = True,
    ) -> None:
        self._available = list(leases or [])
        self._leases = {lease.lease_id: lease for lease in self._available}
        self._credentials = dict(credentials or {})
        self._otp_factory = otp_factory
        self.complete_on_close = complete_on_close
        self._claim_results: dict[str, AccountLease | None] = {}
        self._operation_results: dict[
            tuple[str, str],
            tuple[tuple[object, ...], object],
        ] = {}
        self._statuses: dict[str, LeaseStatus] = {}
        self._current_lease_id: str | None = None
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    @staticmethod
    def lease(
        account_id: str,
        *,
        lease_id: str | None = None,
        fence: int = 1,
        target_level: int = 20,
        now: datetime | None = None,
    ) -> AccountLease:
        current = now or datetime.now(timezone.utc)
        return AccountLease(
            lease_id=lease_id or f"lease_{account_id}",
            lease_fence=fence,
            account_id=account_id,
            target_level=target_level,
            expires_at=current + timedelta(minutes=10),
            renew_after=current + timedelta(minutes=3),
        )

    def _lease(self, lease_id: str, lease_fence: int) -> AccountLease:
        lease = self._leases.get(lease_id)
        if lease is None or lease.lease_fence != lease_fence:
            raise LeaseStaleError("租约不存在或 fence 已过时")
        return lease

    def _status_for(self, lease: AccountLease) -> LeaseStatus:
        return self._statuses.get(
            lease.lease_id,
            LeaseStatus(
                lease_id=lease.lease_id,
                lease_fence=lease.lease_fence,
                account_id=lease.account_id,
                state=LeaseState.ACTIVE,
                expires_at=lease.expires_at,
                renew_after=lease.renew_after,
            ),
        )

    def _idempotent(
        self,
        name: str,
        operation_id: str,
        fingerprint: tuple[object, ...],
        execute: Callable[[], object],
    ) -> object:
        key = (name, operation_id)
        previous = self._operation_results.get(key)
        if previous is not None:
            previous_fingerprint, result = previous
            if previous_fingerprint != fingerprint:
                raise IdempotencyConflictError(
                    f"{name} 的 operationId 被用于不同请求"
                )
            return result
        result = execute()
        self._operation_results[key] = (fingerprint, result)
        return result

    def claim(
        self,
        claim_request_id: str,
        task_type: str,
    ) -> AccountLease | None:
        self.calls.append(("claim", (claim_request_id, task_type)))
        if claim_request_id in self._claim_results:
            return self._claim_results[claim_request_id]
        current = self.current()
        if current is not None:
            lease = self._leases[current.lease_id]
            self._claim_results[claim_request_id] = lease
            return lease
        lease = self._available.pop(0) if self._available else None
        self._claim_results[claim_request_id] = lease
        if lease is not None:
            self._current_lease_id = lease.lease_id
            self._statuses[lease.lease_id] = self._status_for(lease)
        return lease

    def current(self) -> LeaseStatus | None:
        self.calls.append(("current", ()))
        if self._current_lease_id is None:
            return None
        status = self._statuses[self._current_lease_id]
        return None if status.terminal else status

    def status(
        self,
        lease_id: str,
        lease_fence: int,
    ) -> LeaseStatus:
        self.calls.append(("status", (lease_id, lease_fence)))
        lease = self._lease(lease_id, lease_fence)
        return self._status_for(lease)

    def recover(
        self,
        lease_id: str,
        lease_fence: int,
    ) -> AccountLease:
        self.calls.append(("recover", (lease_id, lease_fence)))
        return self._lease(lease_id, lease_fence)

    def credentials(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
    ) -> SecretCredentials:
        self.calls.append(("credentials", (lease_id, lease_fence, operation_id)))
        lease = self._lease(lease_id, lease_fence)

        def load() -> SecretCredentials:
            try:
                return self._credentials[lease.account_id]
            except KeyError as error:
                raise LeaseProviderError("测试账号没有配置凭据") from error

        return self._idempotent(
            "credentials",
            operation_id,
            (lease_id, lease_fence),
            load,
        )  # type: ignore[return-value]

    def renew(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        phase: str,
        run_id: str | None,
        *,
        recover_expired: bool = False,
    ) -> LeaseStatus:
        self.calls.append(
            ("renew", (lease_id, lease_fence, operation_id, phase, run_id))
        )
        lease = self._lease(lease_id, lease_fence)

        def renew_lease() -> LeaseStatus:
            now = datetime.now(timezone.utc)
            status = LeaseStatus(
                lease_id=lease.lease_id,
                lease_fence=lease.lease_fence,
                account_id=lease.account_id,
                state=LeaseState.ACTIVE,
                expires_at=now + timedelta(minutes=10),
                renew_after=now + timedelta(minutes=3),
            )
            self._statuses[lease.lease_id] = status
            return status

        return self._idempotent(
            "renew",
            operation_id,
            (lease_id, lease_fence, phase, run_id, recover_expired),
            renew_lease,
        )  # type: ignore[return-value]

    def request_otp(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        challenge_id: str,
        challenge_started_at: datetime,
        method: OtpMethod = OtpMethod.TOTP,
    ) -> OtpCode:
        self.calls.append(
            (
                "request_otp",
                (
                    lease_id,
                    lease_fence,
                    operation_id,
                    challenge_id,
                    challenge_started_at,
                    method,
                ),
            )
        )
        self._lease(lease_id, lease_fence)

        def request() -> OtpCode:
            if self._otp_factory is None:
                raise LeaseProviderError("测试 Provider 没有配置 OTP")
            otp = self._otp_factory(challenge_id, challenge_started_at)
            if not otp.valid_for(challenge_id, challenge_started_at):
                raise LeaseProviderError("Provider 返回了旧的或已过期的 OTP")
            return otp

        return self._idempotent(
            "request_otp",
            operation_id,
            (lease_id, lease_fence, challenge_id, challenge_started_at, method),
            request,
        )  # type: ignore[return-value]

    def close(
        self,
        lease_id: str,
        lease_fence: int,
        operation_id: str,
        outcome: str,
        run_id: str | None,
        evidence: CompletionEvidence | None,
        reason_code: str,
        cleanup: CleanupEvidence,
    ) -> LeaseStatus:
        self.calls.append(
            (
                "close",
                (
                    lease_id,
                    lease_fence,
                    operation_id,
                    outcome,
                    run_id,
                    evidence,
                    reason_code,
                    cleanup,
                ),
            )
        )
        lease = self._lease(lease_id, lease_fence)

        def close_lease() -> LeaseStatus:
            state = (
                LeaseState.COMPLETED
                if self.complete_on_close and cleanup.complete
                else LeaseState.COMPLETION_PENDING
            )
            status = LeaseStatus(
                lease_id=lease.lease_id,
                lease_fence=lease.lease_fence,
                account_id=lease.account_id,
                state=state,
                expires_at=lease.expires_at,
                renew_after=lease.renew_after,
            )
            self._statuses[lease.lease_id] = status
            if status.terminal:
                self._current_lease_id = None
            return status

        return self._idempotent(
            "close",
            operation_id,
            (
                lease_id,
                lease_fence,
                outcome,
                run_id,
                evidence,
                reason_code,
                cleanup,
            ),
            close_lease,
        )  # type: ignore[return-value]

    def complete(self, lease_id: str) -> None:
        lease = self._leases[lease_id]
        self._statuses[lease_id] = LeaseStatus(
            lease_id=lease.lease_id,
            lease_fence=lease.lease_fence,
            account_id=lease.account_id,
            state=LeaseState.COMPLETED,
        )
        if self._current_lease_id == lease_id:
            self._current_lease_id = None

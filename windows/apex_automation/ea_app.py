from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Callable, Protocol

from .account_provider import OtpCode, SecretCredentials


class EaAppAutomationError(RuntimeError):
    reason_code = "EA_UI_UNKNOWN"


class EaCaptchaRequired(EaAppAutomationError):
    reason_code = "CAPTCHA"


class EaIdentityMismatch(EaAppAutomationError):
    reason_code = "IDENTITY_MISMATCH"


class EaLoginRejected(EaAppAutomationError):
    """EA answered the credentials with a visible error."""

    reason_code = "LOGIN_INVALID"


class EaApexStartFailed(EaAppAutomationError):
    reason_code = "APEX_START_FAILED"


class EaUiState(str, Enum):
    STOPPED = "STOPPED"
    LOGIN = "LOGIN"
    OTP = "OTP"
    SIGNED_IN = "SIGNED_IN"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class EaIdentityFact:
    ea_account_id: str
    source: str
    verified: bool


@dataclass(frozen=True)
class OtpChallenge:
    challenge_id: str
    started_at: datetime


@dataclass(frozen=True)
class ApexExitEvidence:
    requested_normal_close: bool
    all_processes_exited: bool


class EaAppDriver(Protocol):
    def ensure_started(self) -> EaUiState: ...

    def current_identity(self) -> EaIdentityFact | None: ...

    def sign_in(
        self,
        credentials: SecretCredentials,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
    ) -> EaIdentityFact: ...

    def verify_identity(self, expected_ea_account_id: str) -> EaIdentityFact: ...

    def start_apex(self) -> None: ...

    def stop_apex(self) -> ApexExitEvidence: ...

    def sign_out(self) -> bool: ...


class UnavailableEaAppDriver:
    """Fail-safe placeholder until the target Windows UIA tree is surveyed."""

    def _unavailable(self) -> None:
        raise EaAppAutomationError(
            "EA App UIA 驱动尚未完成实机控件勘察，已拒绝执行登录动作"
        )

    def ensure_started(self) -> EaUiState:
        self._unavailable()
        return EaUiState.UNKNOWN

    def current_identity(self) -> EaIdentityFact | None:
        self._unavailable()
        return None

    def sign_in(
        self,
        credentials: SecretCredentials,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
    ) -> EaIdentityFact:
        self._unavailable()
        raise AssertionError("unreachable")

    def verify_identity(self, expected_ea_account_id: str) -> EaIdentityFact:
        self._unavailable()
        raise AssertionError("unreachable")

    def start_apex(self) -> None:
        self._unavailable()

    def stop_apex(self) -> ApexExitEvidence:
        self._unavailable()
        raise AssertionError("unreachable")

    def sign_out(self) -> bool:
        self._unavailable()
        return False

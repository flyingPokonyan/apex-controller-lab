"""Version 1 diagnostic allowlist. Keep the Forge and Runner copies identical.

Only fixed workflow labels, opaque IDs and finite numeric summaries may cross
this boundary. No raw OCR, account identifiers, paths, URLs or error text.
"""
from __future__ import annotations

from datetime import datetime, timezone
import math
import re

EA_STEPS = frozenset(['OTHER',
 'account-banned',
 'account-banned-close',
 'account-banned-close-failed',
 'account-submit-retry',
 'account-submitted',
 'account-typed',
 'apex-cloud-data-local',
 'apex-cloud-data-stuck',
 'apex-download-required',
 'apex-entry',
 'apex-entry-missing',
 'apex-install-complete',
 'apex-install-complete-timeout',
 'apex-install-confirm-missing',
 'apex-install-direct-play',
 'apex-install-download',
 'apex-install-download-missing',
 'apex-install-first-transition-timeout',
 'apex-install-next-missing',
 'apex-install-options',
 'apex-install-second-transition-timeout',
 'apex-install-terms',
 'apex-library-play',
 'apex-play',
 'apex-play-after-update',
 'apex-play-missing',
 'apex-update',
 'awaiting-banned',
 'awaiting-captcha',
 'awaiting-email',
 'awaiting-expired_session',
 'awaiting-otp',
 'awaiting-otp_method',
 'awaiting-password',
 'awaiting-signed_in',
 'awaiting-unknown',
 'captcha',
 'cloud-upload-error-ack',
 'cloud-upload-error-action-missing',
 'cloud-upload-error-dismissed',
 'cloud-upload-error-stuck',
 'expired-session',
 'identity-mismatch',
 'identity-timeout',
 'identity-verified',
 'library-tour-close',
 'library-tour-dismissed',
 'library-tour-stuck',
 'login-rejected',
 'otp-email-code-page',
 'otp-exhausted',
 'otp-method-authenticator',
 'otp-method-email',
 'otp-method-unavailable',
 'otp-submitted',
 'otp-totp-code-page',
 'password-recovery-account-verified',
 'password-recovery-failed',
 'password-recovery-login-ready',
 'password-recovery-password-typed',
 'password-recovery-start',
 'password-recovery-new-password',
 'password-recovery-success',
 'password-recovery-verified',
 'password-typed',
 'preflight',
 'preflight-unknown',
 'restart-complete',
 'restart-help-missing',
 'restart-item-missing',
 'restart-menu-open',
 'restart-requested',
 'signed-in',
 'signed-out',
 'signin-account-page-ready',
 'signin-account-reset-failed',
 'signin-back-missing',
 'signin-back-to-account',
 'signin-reset-start',
 'signin-start',
 'signin-timeout',
 'signin-wrong-page',
 'signout-cloud-sync-closed',
 'signout-cloud-sync-skip',
 'signout-cloud-sync-skip-failed',
 'signout-cloud-sync-timeout',
 'signout-cloud-upload-retry',
 'signout-cloud-upload-retry-exhausted',
 'signout-confirm',
 'signout-confirm-failed',
 'signout-item-failed',
 'signout-menu',
 'signout-menu-focus-failed',
 'signout-menu-missing',
 'signout-not-signed-in',
 'signout-timeout'])
EA_PAGES = frozenset(['BANNED', 'CAPTCHA', 'EMAIL', 'EXPIRED_SESSION', 'NONE', 'OTP', 'OTP_METHOD', 'PASSWORD', 'RECOVERY_ACCOUNT', 'RESET_PASSWORD', 'RESET_SUCCESS', 'SIGNED_IN', 'UNKNOWN'])
PHASES = frozenset(['CLAIMING', 'EA_STARTING', 'EA_SIGNING_IN', 'EA_IDENTITY_VERIFYING', 'APEX_STARTING', 'APEX_PLAYING', 'APEX_STOPPING', 'EA_SIGNING_OUT', 'LEASE_COMPLETING'])


def keys(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError("invalid diagnostic fields")


def number(value, *, integer=False, minimum=0, maximum=1e15):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid diagnostic number")
    if integer and type(value) is not int:
        raise ValueError("invalid diagnostic integer")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError("diagnostic number out of range")
    return value


def label(value, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("invalid diagnostic label")
    return value


def flag(value):
    if type(value) is not bool:
        raise ValueError("invalid diagnostic flag")
    return value


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40 or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})", value
    ):
        raise ValueError("invalid diagnostic timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def opaque(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError("invalid diagnostic identity")
    return value


def validate_event(event):
    keys(event, {"eventId", "type", "occurredAt", "leaseId", "runId", "payload"})
    opaque(event["eventId"], r"[0-9a-f]{64}")
    if event["leaseId"] is not None:
        opaque(event["leaseId"], r"lease_[a-zA-Z0-9_-]{1,128}")
    if event["runId"] is not None:
        opaque(event["runId"], r"[0-9]{8}-[0-9]{6}-[0-9a-f]{8}")
    payload = event["payload"]
    kind = event["type"]
    if kind == "EA_STEP":
        keys(payload, {"step", "page", "previousStep", "previousStepElapsedMs"})
        label(payload["step"], EA_STEPS)
        label(payload["page"], EA_PAGES)
        if payload["previousStep"] is not None:
            label(payload["previousStep"], EA_STEPS)
        number(payload["previousStepElapsedMs"])
    elif kind == "EA_PERFORMANCE":
        keys(payload, {"windowMs", "metrics"}, {"page", "step"})
        number(payload["windowMs"])
        if "page" in payload:
            label(payload["page"], EA_PAGES)
        if "step" in payload:
            label(payload["step"], EA_STEPS)
        metrics = payload["metrics"]
        if not isinstance(metrics, dict) or not metrics or metrics.keys() - {"eaCapture", "eaOcr"}:
            raise ValueError("invalid EA metric names")
        for sample in metrics.values():
            keys(sample, {"count", "totalMs", "meanMs", "maxMs", "recentP95Ms"})
            number(sample["count"], integer=True, minimum=1)
            for name in ("totalMs", "meanMs", "maxMs", "recentP95Ms"):
                number(sample[name])
    elif kind == "WORKFLOW_PHASE":
        keys(payload, {"previousPhase", "phase", "durationMs"})
        label(payload["previousPhase"], PHASES)
        label(payload["phase"], PHASES)
        number(payload["durationMs"])
    elif kind == "HTTP_UPLOAD_SUMMARY":
        keys(payload, {"kind", "status", "count", "totalMs", "maxMs", "pendingMax", "eventCount", "imageBytesApproxTotal", "windowStart", "windowEnd"})
        label(payload["kind"], {"report", "evidence"})
        if payload["status"] is not None:
            number(payload["status"], integer=True, minimum=100, maximum=599)
        number(payload["count"], integer=True, minimum=1)
        for name in ("totalMs", "maxMs", "pendingMax", "eventCount", "imageBytesApproxTotal"):
            number(payload[name])
        for name in ("windowStart", "windowEnd"):
            payload[name] = timestamp(payload[name])
        if payload["windowStart"] > payload["windowEnd"]:
            raise ValueError("invalid diagnostic window")
    elif kind == "NOTIFICATION_CANDIDATE":
        keys(payload, {"kind", "recognisedOwner", "closeFound"})
        label(payload["kind"], {"hand-input", "uu-remote"})
        flag(payload["recognisedOwner"])
        flag(payload["closeFound"])
    elif kind == "NOTIFICATION_CLOSED":
        keys(payload, {"kind", "disappeared", "apexForeground", "attempt"})
        label(payload["kind"], {"hand-input", "uu-remote"})
        flag(payload["disappeared"])
        flag(payload["apexForeground"])
        number(payload["attempt"], integer=True, minimum=1, maximum=2)
    elif kind == "NOTIFICATION_CLOSE_DISABLED":
        keys(payload, {"reason"})
        label(payload["reason"], {"capture-coordinate-mapping", "backend-error"})
    else:
        raise ValueError("unknown diagnostic type")
    return {**event, "occurredAt": timestamp(event["occurredAt"])}

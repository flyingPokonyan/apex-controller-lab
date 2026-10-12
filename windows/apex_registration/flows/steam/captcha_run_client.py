"""Small client for CaptchaRun's Steam-specific hCaptcha task.

Steam's ``refreshcaptcha`` endpoint and CaptchaRun's ``HCaptchaSteam`` task do
not use the same site key. CaptchaRun requires a fixed integration key. Current
successful task responses may contain only a token, so callers retain Steam's
session gid as a fallback.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import time
from typing import Any

import requests


API_BASE = "https://api.captcha-run.com/v2/tasks"
STEAM_REFERER = "https://store.steampowered.com/join/"
CAPTCHA_RUN_STEAM_SITE_KEY = "e18a349a-46c2-46a0-87a8-74be79345c92"


class CaptchaRunError(RuntimeError):
    """CaptchaRun rejected a task or returned an unusable result."""


@dataclass(frozen=True)
class CaptchaRunSolution:
    token: str
    gid: str | None


def _authorization(api_key: str) -> str:
    value = api_key.strip()
    if not value:
        raise CaptchaRunError("CaptchaRun API key is empty")
    if value.lower().startswith("bearer "):
        return value
    return f"Bearer {value}"


def _redact(value: Any) -> Any:
    """Remove credentials and solved tokens before putting a response in logs."""
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key.lower() in {
                "authorization",
                "clientkey",
                "key",
                "login",
                "password",
                "proxy",
                "token",
                "captcha_key",
                "grecaptcharesponse",
            }:
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact(item)
        return redacted
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _error_payload(value: Any) -> str:
    if isinstance(value, dict):
        diagnostic_keys = {
            "id",
            "taskId",
            "status",
            "statusCode",
            "reason",
            "fail_full_reason",
            "message",
            "error",
            "price",
            "deducted",
            "response",
        }
        value = {key: item for key, item in value.items() if key in diagnostic_keys}
    return json.dumps(_redact(value), ensure_ascii=False, separators=(",", ":"))[:1200]


def solve_hcaptcha_steam(
    api_key: str,
    *,
    timeout: float = 300,
    poll_interval: float = 5,
    http: Any | None = None,
) -> CaptchaRunSolution:
    """Create and poll one HCaptchaSteam task.

    Proxy fields are intentionally omitted.  As of the current CaptchaRun
    documentation, sending them makes this task type fail.
    """
    client = http or requests.Session()
    headers = {
        "Authorization": _authorization(api_key),
        "Content-Type": "application/json",
    }
    payload = {
        "isInvisible": False,
        "captchaType": "HCaptchaSteam",
        "siteKey": CAPTCHA_RUN_STEAM_SITE_KEY,
        "siteReferer": STEAM_REFERER,
    }

    try:
        create = client.post(API_BASE, headers=headers, json=payload, timeout=30)
        create_data = create.json()
    except Exception as exc:
        raise CaptchaRunError(f"CaptchaRun task creation failed: {exc}") from exc

    task_id = create_data.get("taskId") if isinstance(create_data, dict) else None
    if getattr(create, "status_code", 500) >= 400 or not task_id:
        raise CaptchaRunError(
            f"CaptchaRun task creation rejected (HTTP {getattr(create, 'status_code', '?')}): "
            f"{_error_payload(create_data)}"
        )

    deadline = time.monotonic() + timeout
    result_url = f"{API_BASE}/{task_id}"
    last_status = "unknown"
    while time.monotonic() < deadline:
        if poll_interval:
            time.sleep(poll_interval)
        try:
            response = client.get(result_url, headers=headers, timeout=30)
            result = response.json()
        except Exception as exc:
            last_status = f"poll error: {exc}"
            continue

        if not isinstance(result, dict):
            last_status = "non-object response"
            continue

        status = str(result.get("status") or "").casefold()
        last_status = status or "missing"
        if status == "success":
            body = result.get("response") or {}
            if not isinstance(body, dict):
                raise CaptchaRunError(
                    f"CaptchaRun success response has invalid shape: {_error_payload(result)}"
                )
            token = body.get("token") or body.get("captcha_key") or body.get("gRecaptchaResponse")
            gid = body.get("gid")
            if not token:
                raise CaptchaRunError(
                    f"CaptchaRun success response has no token: {_error_payload(result)}"
                )
            return CaptchaRunSolution(
                token=str(token),
                gid=str(gid) if gid is not None else None,
            )
        if status == "fail":
            raise CaptchaRunError(f"CaptchaRun task failed: {_error_payload(result)}")

    raise CaptchaRunError(
        f"CaptchaRun task {task_id} timed out; last status={last_status}"
    )

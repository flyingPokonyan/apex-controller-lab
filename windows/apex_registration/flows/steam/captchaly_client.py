"""Captchaly hCaptcha Enterprise client used as a Steam fallback."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

import requests


API_BASE = "https://v1.captchaly.com"


class CaptchalyError(RuntimeError):
    """Captchaly rejected a request or returned no usable token."""


@dataclass(frozen=True)
class CaptchalySolution:
    token: str
    deducted: float | None
    duration: float | None


def _authorization(api_key: str) -> str:
    value = api_key.strip()
    if not value:
        raise CaptchalyError("Captchaly API key is empty")
    if value.lower().startswith("bearer "):
        return value
    return f"Bearer {value}"


def _safe_error(response: Any, payload: Any) -> str:
    if isinstance(payload, dict):
        keep = {
            key: value
            for key, value in payload.items()
            if key.lower() not in {
                "token",
                "apikey",
                "api_key",
                "authorization",
                "proxy",
            }
        }
        text = json.dumps(keep, ensure_ascii=False, separators=(",", ":"))
    else:
        text = str(payload)
    return f"HTTP {getattr(response, 'status_code', '?')}: {text[:1000]}"


def solve_hcaptcha_enterprise(
    api_key: str,
    *,
    sitekey: str,
    website_url: str,
    proxy: str | None = None,
    timeout: float = 130,
    http: Any | None = None,
) -> CaptchalySolution:
    """Request one Enterprise token.

    The solver HTTP call stays direct (no inherited env proxy). Steam join
    checks that the token IP matches the submit IP, so pass the same
    residential proxy Captchaly should use while solving.
    """
    if not sitekey.strip():
        raise CaptchalyError("Steam sitekey is empty")

    client = http or requests.Session()
    if http is None:
        # Do not inherit HTTP(S)_PROXY from the shell for the Captchaly API
        # itself; the optional ``proxy`` query is only for the solve session.
        client.trust_env = False
    params = {
        "sitekey": sitekey,
        "url": website_url,
    }
    proxy_url = (proxy or "").strip()
    if proxy_url:
        params["proxy"] = proxy_url
    response = client.get(
        f"{API_BASE}/hcaptcha-enterprise",
        headers={"Authorization": _authorization(api_key)},
        params=params,
        timeout=timeout,
    )
    try:
        payload = response.json()
    except Exception as exc:
        raise CaptchalyError(
            f"Captchaly returned non-JSON data (HTTP {getattr(response, 'status_code', '?')})"
        ) from exc

    if getattr(response, "status_code", 500) >= 400:
        raise CaptchalyError(f"Captchaly task failed: {_safe_error(response, payload)}")
    if not isinstance(payload, dict) or not payload.get("token"):
        raise CaptchalyError(f"Captchaly response has no token: {_safe_error(response, payload)}")

    deducted = payload.get("deducted")
    duration = payload.get("duration")
    return CaptchalySolution(
        token=str(payload["token"]),
        deducted=float(deducted) if deducted is not None else None,
        duration=float(duration) if duration is not None else None,
    )

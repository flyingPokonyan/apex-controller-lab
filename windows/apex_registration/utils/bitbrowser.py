"""BitBrowser local API for the desktop registration path.

Linux registration stays on Roxy. This client is used only when
``GAME_ACCOUNTS_DESKTOP_BROWSER=bitbrowser``.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any
from urllib.parse import unquote, urlparse

import requests


class BitBrowserError(RuntimeError):
    pass


def uses_bitbrowser(engine: str | None = None) -> bool:
    raw = (
        engine
        if engine is not None
        else os.environ.get("GAME_ACCOUNTS_DESKTOP_BROWSER", "")
    )
    return str(raw).strip().lower() in {"bitbrowser", "bit"}


def proxy_fields(proxy_url: str) -> dict[str, Any]:
    parsed = urlparse((proxy_url or "").strip())
    if not parsed.hostname or not parsed.port:
        raise BitBrowserError("BitBrowser proxy URL is missing a host or port")
    scheme = (parsed.scheme or "").lower()
    proxy_type = "socks5" if scheme.startswith("socks") else "http"
    return {
        "proxyMethod": 2,
        "proxyType": proxy_type,
        "host": parsed.hostname,
        "port": int(parsed.port),
        "proxyUserName": unquote(parsed.username or ""),
        "proxyPassword": unquote(parsed.password or ""),
    }


def _chrome_major() -> str:
    raw = os.environ.get("GAME_ACCOUNTS_BIT_CORE", "").strip()
    return raw if raw.isdigit() else "130"


def _fingerprint() -> dict[str, Any]:
    """One Windows Chrome. Language and timezone follow the proxy exit."""
    major = _chrome_major()
    # BitBrowser samples osVersion inside the given set. "11,10" stays on
    # Windows 11 and 10; a blank value also draws 8 and 7.
    return {
        "coreProduct": "chrome",
        "coreVersion": major,
        "ostype": "PC",
        "os": "Win32",
        "osVersion": "11,10",
        "version": major,
        "userAgent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36"
        ),
        "isIpCreateTimeZone": True,
        "isIpCreatePosition": True,
        "isIpCreateLanguage": True,
        "isIpCreateDisplayLanguage": True,
        "webRTC": "0",
    }


class BitBrowserClient:
    def __init__(self, base_url: str | None = None, timeout: float = 120):
        self.base_url = (
            base_url
            or os.environ.get("GAME_ACCOUNTS_BIT_API", "").strip()
            or "http://127.0.0.1:54345"
        ).rstrip("/")
        self.timeout = timeout
        self.api_key = os.environ.get("GAME_ACCOUNTS_BIT_API_KEY", "").strip()
        self.session = requests.Session()
        self.session.trust_env = False

    def _request(self, path: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}{path}"
        try:
            response = self.session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", **({"x-api-key": self.api_key} if self.api_key else {})},
                timeout=self.timeout,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            raise BitBrowserError(f"BitBrowser API unreachable: {exc}") from exc
        except ValueError as exc:
            raise BitBrowserError("BitBrowser API returned non-JSON") from exc
        if not body.get("success"):
            raise BitBrowserError(str(body.get("msg") or body)[:300])
        return body.get("data")

    def health(self) -> bool:
        try:
            self._request("/browser/list", {"page": 0, "pageSize": 1})
        except BitBrowserError:
            return False
        return True

    def ensure_open(
        self,
        *,
        seed: str,
        proxy_url: str,
        on_profile=None,
    ) -> dict[str, str]:
        del seed
        if not self.health():
            raise BitBrowserError(
                f"BitBrowser local API is not running (expected {self.base_url})"
            )
        name = f"apex-bit-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}"
        payload = {
            "name": name,
            "remark": "apex-forge EA",
            "browserFingerPrint": _fingerprint(),
        }
        payload.update(proxy_fields(proxy_url))
        created = self._request("/browser/update", payload)
        profile_id = str((created or {}).get("id") or "")
        if not profile_id:
            raise BitBrowserError("BitBrowser create did not return an id")
        if on_profile:
            on_profile(self, profile_id)
        opened = self._request("/browser/open", {"id": profile_id})
        ws = str((opened or {}).get("ws") or "").strip()
        if not ws:
            self.delete(profile_id)
            raise BitBrowserError("BitBrowser open did not return a CDP endpoint")
        return {"id": profile_id, "name": name, "cdp": ws}

    def close(self, profile_id: str) -> None:
        try:
            self._request("/browser/close", {"id": str(profile_id)})
        except BitBrowserError:
            return

    def delete(self, profile_id: str) -> None:
        self.close(profile_id)
        self._request("/browser/delete", {"id": str(profile_id)})

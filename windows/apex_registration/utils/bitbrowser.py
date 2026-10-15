"""BitBrowser local API for the desktop registration path.

Linux registration stays on Roxy. This client is used only when
``GAME_ACCOUNTS_DESKTOP_BROWSER=bitbrowser``.
"""
from __future__ import annotations

import contextlib
import os
from pathlib import Path
import random
import tempfile
import time
from typing import Any, Callable
from urllib.parse import unquote, urlparse

import requests


class BitBrowserError(RuntimeError):
    pass


class BitBrowserBusy(BitBrowserError):
    """The Local API answered but refused the call as too frequent."""


# Every registration child and the worker share one Local API on the machine.
# BitBrowser rejects bursts ("请求频繁"), so calls take turns with a gap.
MIN_INTERVAL = 1.0
BUSY_DELAYS = (2.0, 4.0, 8.0, 15.0)
UNREACHABLE_DELAYS = (2.0, 5.0)
BUSY_MARKERS = ("请求频繁", "频率", "too many", "too frequent", "rate limit")


def is_busy(message: object) -> bool:
    text = str(message or "").casefold()
    return any(marker in text for marker in BUSY_MARKERS)


def _lock_dir() -> Path:
    return Path(os.environ.get("APEX_BITBROWSER_LOCK_DIR") or tempfile.gettempdir())


def _acquire(handle) -> None:
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.2)
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _release(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def api_slot():
    """Hold the machine-wide Local API turn, at least MIN_INTERVAL after the last call."""
    directory = _lock_dir()
    directory.mkdir(parents=True, exist_ok=True)
    stamp = directory / "apex-bitbrowser-api.last"
    with open(directory / "apex-bitbrowser-api.lock", "a+b") as handle:
        _acquire(handle)
        try:
            try:
                last = float(stamp.read_text(encoding="ascii") or 0)
            except (OSError, ValueError):
                last = 0.0
            wait = last + MIN_INTERVAL - time.time()
            if wait > 0:
                time.sleep(min(wait, MIN_INTERVAL))
            yield
        finally:
            try:
                stamp.write_text(repr(time.time()), encoding="ascii")
            finally:
                _release(handle)


def call_local_api(send: Callable[[], dict], *, unreachable: tuple[type[BaseException], ...]) -> dict:
    """Run one Local API request in turn; retry refused bursts and brief outages.

    ``send`` returns the decoded JSON body. A body that is still busy after the
    retries is returned as-is for the caller to raise.
    """
    busy_delays, down_delays = list(BUSY_DELAYS), list(UNREACHABLE_DELAYS)
    while True:
        try:
            with api_slot():
                body = send()
        except unreachable:
            if not down_delays:
                raise
            time.sleep(down_delays.pop(0))
            continue
        if isinstance(body, dict) and body.get("success") is not True and is_busy(body.get("msg")) and busy_delays:
            time.sleep(busy_delays.pop(0))
            continue
        return body


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

        def send() -> dict:
            response = self.session.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json", **({"x-api-key": self.api_key} if self.api_key else {})},
                timeout=self.timeout,
            )
            response.raise_for_status()
            return response.json()

        try:
            body = call_local_api(send, unreachable=(requests.ConnectionError,))
        except requests.ConnectionError as exc:
            raise BitBrowserError(
                f"BitBrowser local API is not running (expected {self.base_url})"
            ) from exc
        except requests.RequestException as exc:
            raise BitBrowserError(f"BitBrowser API unreachable: {type(exc).__name__}") from exc
        except ValueError as exc:
            raise BitBrowserError("BitBrowser API returned non-JSON") from exc
        if not isinstance(body, dict):
            raise BitBrowserError("BitBrowser API returned non-JSON")
        if not body.get("success"):
            message = str(body.get("msg") or body)[:300]
            raise (BitBrowserBusy if is_busy(message) else BitBrowserError)(message)
        return body.get("data")

    def health(self) -> bool:
        try:
            self._request("/browser/list", {"page": 0, "pageSize": 1})
        except BitBrowserBusy:
            return True  # It answered; only the call rate was refused.
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
        # No separate health call: each extra request competes for the same rate.
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
        # queue=True lets BitBrowser itself space concurrent opens (6.0.4+).
        try:
            opened = self._request("/browser/open", {"id": profile_id, "queue": True})
        except BitBrowserError:
            with contextlib.suppress(BitBrowserError):
                self.delete(profile_id)
            raise
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

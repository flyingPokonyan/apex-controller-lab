"""RoxyBrowser local API: create/open fingerprint windows and hand CDP to Playwright.

Roxy is the browser (fingerprint + proxy). Playwright still drives the page
via ``connect_over_cdp``. Never call ``chromium.launch()`` for this engine, and
never overlay a second Playwright context/proxy on the opened profile.
"""

from __future__ import annotations

import hashlib
import os
import random
import time
from typing import Any
from urllib.parse import unquote, urlparse

import requests

from apex_registration.utils.browser_profile import LOCALE, TIMEZONE, geolocation_for, viewport_for


def rewrite_loopback_host(url: str, api_base: str) -> str:
    """Point CDP at the Roxy API host when it is not this process.

    Roxy reports ``ws://127.0.0.1:<port>/devtools/...``. That loopback is
    correct when Playwright shares the sidecar network namespace, but not
    when the API is reached at ``host.docker.internal`` or another hostname.
    """
    raw = (url or "").strip()
    host = urlparse(api_base or "").hostname or ""
    if not raw or host in {"", "127.0.0.1", "localhost", "::1"}:
        return raw
    parsed = urlparse(raw)
    if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        return raw
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return parsed._replace(netloc=netloc).geturl()


class RoxyBrowserError(RuntimeError):
    pass


def uses_playwright_engine(engine: str | None = None) -> bool:
    value = (
        engine
        if engine is not None
        else os.environ.get("GAME_ACCOUNTS_EA_REGISTER_ENGINE", "roxybrowser")
    )
    return str(value).strip().lower() in {"playwright", "pw", "browser"}


def uses_roxybrowser_engine(engine: str | None = None) -> bool:
    """Roxy unless the engine is explicitly Playwright or protocol.

    Unknown values, empty values, and leftover names like ``bitbrowser`` stay
    on Roxy. Silent Chromium fallback is how 1024 SOCKS5 auth used to crash.
    """
    value = (
        engine
        if engine is not None
        else os.environ.get("GAME_ACCOUNTS_EA_REGISTER_ENGINE", "roxybrowser")
    )
    raw = str(value).strip().lower()
    if raw in {"playwright", "pw", "browser", "protocol"}:
        return False
    return True


def roxy_host() -> str:
    """``linux`` is the container sidecar. ``windows`` is a real Roxy desktop.

    The Linux path has to invent a screen, font set, and Direct3D GPU. Setting
    ``GAME_ACCOUNTS_ROXY_HOST=windows`` on that container drops those pins and
    leaks the Linux renderer. Only set it in a process running on Windows.
    """
    raw = os.environ.get("GAME_ACCOUNTS_ROXY_HOST", "linux").strip().lower()
    if raw in {"windows", "win", "win32", "native"}:
        return "windows"
    return "linux"


def fresh_profile_each_run() -> bool:
    """A reused directory keeps the cookies from a failed submit.

    Windows trials always open a new profile. Linux keeps the mailbox-stable
    directory so a retry does not change hardware. Set the env to override.
    """
    raw = os.environ.get("GAME_ACCOUNTS_ROXY_FRESH_PROFILE", "").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    return roxy_host() == "windows"


def profile_name_for(seed: str) -> str:
    digest = hashlib.sha256((seed or "").strip().lower().encode("utf-8")).hexdigest()
    return f"apex-ea-{digest[:12]}"


def fresh_profile_name() -> str:
    return f"apex-ea-win-{int(time.time() * 1000)}-{random.randrange(0x10000):04x}"


def proxy_info(proxy_url: str | None) -> dict[str, Any]:
    raw = (proxy_url or "").strip()
    if not raw or raw.lower() in {"direct", "none", "noproxy"}:
        return {
            "proxyMethod": "custom",
            "proxyCategory": "noproxy",
            "ipType": "IPV4",
            "protocol": "noproxy",
            "host": "",
            "port": "",
            "proxyUserName": "",
            "proxyPassword": "",
        }
    parsed = urlparse(raw)
    if not parsed.hostname:
        raise RoxyBrowserError("proxy URL is missing a host")
    scheme = (parsed.scheme or "http").lower()
    if scheme in {"socks5", "socks5h"}:
        category = "SOCKS5"
        default_port = 1080
    elif scheme == "https":
        category = "HTTPS"
        default_port = 443
    else:
        category = "HTTP"
        default_port = 80
    return {
        "proxyMethod": "custom",
        "proxyCategory": category,
        "ipType": "IPV4",
        "protocol": category,
        "host": parsed.hostname,
        "port": str(int(parsed.port or default_port)),
        "proxyUserName": unquote(parsed.username or ""),
        "proxyPassword": unquote(parsed.password or ""),
    }


# Roxy documents this Chromium switch as ignored, but sending it is harmless.
# Steam OAuth recovery is URL capture + ensure_live_steam_oauth_page, not this flag.
OPEN_BROWSER_ARGS = ["--disable-popup-blocking"]


# Roxy rejects resolutions that are not in its desktop list. The window is
# openWidth/openHeight; the screen is resolutionX/Y. A window larger than the
# screen is visible to any page script, so the screen has to contain the window.
_ROXY_SCREENS = (
    (1400, 900),
    (1600, 900),
    (1600, 1200),
    (1920, 1080),
    (1920, 1200),
    (2048, 1152),
    (2560, 1440),
)
# webGLInfo left blank lets Roxy fall through to the Linux host renderer
# (OpenGL / PCIe/SSE2). Pin a Windows Direct3D11 string instead.
_WINDOWS_WEBGL = (
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (NVIDIA)",
        "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060 6GB Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (Intel)",
        "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
    (
        "Google Inc. (AMD)",
        "ANGLE (AMD, AMD Radeon RX 580 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    ),
)


def screen_for(seed: str, viewport: dict[str, int] | None = None) -> tuple[int, int]:
    """Stable Roxy screen size that contains this window."""
    view = viewport if viewport is not None else viewport_for(seed)
    width = int(view["width"])
    height = int(view["height"])
    fitting = [item for item in _ROXY_SCREENS if item[0] >= width and item[1] >= height]
    if not fitting:
        fitting = [(1920, 1080)]
    digest = hashlib.sha256((seed or "apex").encode("utf-8")).digest()
    return fitting[digest[3] % len(fitting)]


def webgl_for(seed: str) -> tuple[str, str]:
    """Stable Windows WebGL vendor and renderer for one mailbox."""
    digest = hashlib.sha256((seed or "apex").encode("utf-8")).digest()
    return _WINDOWS_WEBGL[digest[4] % len(_WINDOWS_WEBGL)]


def _windows_fingerprint(seed: str) -> dict[str, Any]:
    """Let the desktop Roxy build the hardware fingerprint.

    Language, timezone, and coordinates follow the proxy exit. The caller
    only opens a window after that exit is one of HK, US, or JP, so the
    page and the EA country dropdown land on the same place. Screen, GPU,
    fonts, and kernel stay with the machine.
    """
    del seed
    return {
        "randomFingerprint": True,
        "isLanguageBaseIp": True,
        "isDisplayLanguageBaseIp": True,
        "isTimeZone": True,
        "isPositionBaseIp": True,
        "fontType": False,
        "webGL": True,
        "canvas": True,
    }


def fingerprint_info(seed: str = "") -> dict[str, Any]:
    """Windows Chrome that stays a Hong Kong user even if the exit lies.

    ``GAME_ACCOUNTS_ROXY_HOST=windows`` leaves screen, GPU, and kernel to the
    desktop Roxy. The default below is the Linux sidecar costume.
    """
    if roxy_host() == "windows":
        return _windows_fingerprint(seed)
    return _linux_fingerprint(seed)


def _linux_fingerprint(seed: str = "") -> dict[str, Any]:
    """Spoofed Windows Chrome for the Debian sidecar.

    Language-from-IP made EA/Steam render Indonesian (or other) copy when a
    1024 "US" exit was actually ID. Timezone-from-IP made EA default the
    country dropdown to Japan. Pin language, timezone, geolocation to HK;
    keep WebRTC disabled so the host IP never leaks.

    ``fontType`` False follows the sidecar font set. Randomizing does not
    stop canvas from measuring the fonts actually installed in the image.
    ``randomFingerprint`` False keeps the HK language/timezone/geo pins from
    being re-rolled on every open. Resolution, WebGL and the geo pin are
    stable per ``seed`` so the same mailbox does not jump hardware on retry.
    """
    geo = geolocation_for(seed)
    viewport = viewport_for(seed)
    screen_w, screen_h = screen_for(seed, viewport)
    vendor, renderer = webgl_for(seed)
    return {
        "randomFingerprint": False,
        "portScanProtect": False,
        "isLanguageBaseIp": False,
        "isDisplayLanguageBaseIp": False,
        "language": LOCALE,
        "displayLanguage": LOCALE,
        "isTimeZone": False,
        "timeZone": f"GMT+08:00 {TIMEZONE}",
        "position": 1,
        "isPositionBaseIp": False,
        "longitude": str(geo["longitude"]),
        "latitude": str(geo["latitude"]),
        "precisionPos": "100",
        "fontType": False,
        "webRTC": 2,
        "webGL": True,
        "webGLInfo": True,
        "webGLManufacturer": vendor,
        "webGLRender": renderer,
        "webGpu": "webgl",
        "canvas": True,
        "stopOpenIP": False,
        "stopOpenPosition": False,
        "stopOpenNet": False,
        "resolutionType": True,
        "resolutionX": str(screen_w),
        "resolutionY": str(screen_h),
        "openWidth": str(viewport["width"]),
        "openHeight": str(viewport["height"]),
    }


def cdp_endpoint(open_data: dict[str, Any] | None) -> str:
    payload = open_data or {}
    ws = payload.get("ws")
    if isinstance(ws, str) and ws.strip():
        value = ws.strip()
        if value.startswith(("ws://", "wss://", "http://", "https://")):
            return value
        return f"http://{value}"
    if isinstance(ws, dict):
        for key in ("puppeteer", "playwright", "selenium"):
            value = str(ws.get(key) or "").strip()
            if value:
                if value.startswith(("ws://", "wss://", "http://", "https://")):
                    return value
                return f"http://{value}"
    http = str(payload.get("http") or "").strip()
    if http:
        if http.startswith(("http://", "https://")):
            return http
        return f"http://{http}"
    raise RoxyBrowserError("RoxyBrowser open response missing CDP endpoint")


# Roxy desktop workspace ids are snowflakes. A JSON number above this is
# rounded by the Electron client, and /browser/create then says the team
# does not exist. Small Linux sidecar ids stay integers.
_JS_MAX_SAFE_INTEGER = 9007199254740991


def _as_workspace_id(value: Any) -> int | str:
    raw = str(value or "").strip()
    if raw.isdigit():
        number = int(raw)
        if number <= _JS_MAX_SAFE_INTEGER:
            return number
        return raw
    return raw


class RoxyBrowserClient:
    def __init__(
        self,
        base_url: str | None = None,
        token: str | None = None,
        timeout: float = 120,
    ):
        self.base_url = (
            base_url
            or os.environ.get("GAME_ACCOUNTS_ROXY_API", "").strip()
            or "http://127.0.0.1:50100"
        ).rstrip("/")
        self.token = (
            token
            if token is not None
            else os.environ.get("GAME_ACCOUNTS_ROXY_TOKEN", "").strip()
        )
        self.timeout = timeout
        self.session = requests.Session()
        self.session.trust_env = False
        self._workspace_id: int | str | None = None

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["token"] = self.token
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.base_url}{path if path.startswith('/') else '/' + path}"
        last_error: RoxyBrowserError | None = None
        for attempt in range(4):
            try:
                response = self.session.request(
                    method,
                    url,
                    params=params,
                    json=payload if payload is not None else None,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
                response.raise_for_status()
                body = response.json()
            except requests.RequestException as exc:
                raise RoxyBrowserError(f"RoxyBrowser API unreachable: {exc}") from exc
            except ValueError as exc:
                raise RoxyBrowserError("RoxyBrowser API returned non-JSON") from exc
            code = body.get("code")
            if code in (0, "0"):
                return body.get("data")
            msg = str(body.get("msg") or "RoxyBrowser API failed")
            lowered = msg.lower()
            if ("too many" in lowered or "rate" in lowered) and attempt < 3:
                time.sleep(1.2)
                last_error = RoxyBrowserError(msg)
                continue
            raise RoxyBrowserError(msg)
        raise last_error or RoxyBrowserError("RoxyBrowser API failed")

    def health(self) -> bool:
        """True only when Local API answers and a workspace is reachable.

        GET /health can succeed while the client is logged out or the API
        switch is off for browser routes. A workspace list is the cheapest
        authenticated probe.
        """
        try:
            self._request("GET", "/health")
            self.workspace_id()
        except RoxyBrowserError:
            return False
        return True

    def workspace_id(self) -> int | str:
        if self._workspace_id is not None:
            return self._workspace_id
        configured = os.environ.get("GAME_ACCOUNTS_ROXY_WORKSPACE_ID", "").strip()
        if configured:
            self._workspace_id = _as_workspace_id(configured)
            return self._workspace_id
        data = self._request("GET", "/browser/workspace") or {}
        rows = data.get("rows") if isinstance(data, dict) else []
        if not rows:
            raise RoxyBrowserError("RoxyBrowser returned no workspace")
        self._workspace_id = _as_workspace_id(rows[0].get("id"))
        return self._workspace_id

    def _project_id(self) -> str:
        configured = os.environ.get("GAME_ACCOUNTS_ROXY_PROJECT_ID", "").strip()
        if configured:
            return configured
        data = self._request("GET", "/browser/workspace") or {}
        rows = data.get("rows") if isinstance(data, dict) else []
        if not rows:
            return ""
        details = rows[0].get("project_details") or []
        if not details:
            return ""
        return str(details[0].get("projectId") or "").strip()

    def add_profile(self, *, name: str, proxy_url: str, seed: str = "") -> str:
        workspace_id = self.workspace_id()
        host = roxy_host()
        payload: dict[str, Any] = {
            "workspaceId": workspace_id,
            "windowName": name,
            "os": os.environ.get("GAME_ACCOUNTS_ROXY_OS", "Windows").strip() or "Windows",
            "windowRemark": "apex-forge EA",
            "proxyInfo": proxy_info(proxy_url),
            "fingerInfo": fingerprint_info(seed or name),
        }
        # The sidecar pins Windows 11 and kernel 153. A desktop profile picks
        # Windows or macOS and lets Roxy supply the matching OS version.
        if host == "linux":
            payload["osVersion"] = (
                os.environ.get("GAME_ACCOUNTS_ROXY_OS_VERSION", "11").strip() or "11"
            )
            kernel = os.environ.get("GAME_ACCOUNTS_ROXY_KERNEL", "153").strip()
        else:
            payload["os"] = random.choice(("Windows", "macOS"))
            kernel = os.environ.get("GAME_ACCOUNTS_ROXY_KERNEL", "").strip()
        if kernel:
            payload["coreVersion"] = kernel
        project_id = self._project_id()
        if project_id:
            payload["projectId"] = _as_workspace_id(project_id)
        data = self._request("POST", "/browser/create", payload=payload)
        profile_id = None
        if isinstance(data, dict):
            profile_id = data.get("dirId") or data.get("id")
        if not profile_id:
            raise RoxyBrowserError("RoxyBrowser create did not return a dirId")
        return str(profile_id)

    def list_profiles(self, page: int = 1, page_size: int = 100) -> list[dict[str, Any]]:
        data = self._request(
            "GET",
            "/browser/list_v3",
            params={
                "workspaceId": self.workspace_id(),
                "page_index": page,
                "page_size": page_size,
            },
        ) or {}
        if isinstance(data, dict):
            rows = data.get("rows") or []
        else:
            rows = data or []
        return [row for row in rows if isinstance(row, dict)]

    def find_profile_id(self, name: str) -> str | None:
        for row in self.list_profiles():
            if str(row.get("windowName") or "") == name:
                return str(row.get("dirId") or row.get("id") or "") or None
        return None

    def update_proxy(self, profile_id: str, proxy_url: str, *, seed: str = "") -> None:
        self.update_profile(profile_id, proxy_url=proxy_url, seed=seed)

    def update_profile(self, profile_id: str, proxy_url: str, *, seed: str = "") -> None:
        self._request(
            "POST",
            "/browser/mdf",
            payload={
                "workspaceId": self.workspace_id(),
                "dirId": str(profile_id),
                "proxyInfo": proxy_info(proxy_url),
                "fingerInfo": fingerprint_info(seed),
            },
        )

    def open(self, profile_id: str) -> dict[str, str]:
        last_error: RoxyBrowserError | None = None
        for attempt in range(8):
            try:
                open_payload: dict[str, Any] = {
                    "workspaceId": self.workspace_id(),
                    "dirId": str(profile_id),
                    "args": list(OPEN_BROWSER_ARGS),
                }
                if roxy_host() == "windows":
                    open_payload["forceOpen"] = True
                data = self._request(
                    "POST",
                    "/browser/open",
                    payload=open_payload,
                )
            except RoxyBrowserError as exc:
                msg = str(exc).lower()
                if "updating" in msg or "download" in msg or "not ready" in msg:
                    last_error = exc
                    time.sleep(5)
                    continue
                raise
            if not isinstance(data, dict):
                raise RoxyBrowserError("RoxyBrowser open returned no data")
            return {
                "id": profile_id,
                "cdp": rewrite_loopback_host(cdp_endpoint(data), self.base_url),
            }
        raise last_error or RoxyBrowserError(
            "RoxyBrowser open timed out while kernel updated"
        )

    def close(self, profile_id: str) -> None:
        try:
            self._request("POST", "/browser/close", payload={"dirId": str(profile_id)})
        except RoxyBrowserError:
            return

    def delete(self, profile_id: str) -> None:
        """Close then delete so a free/paid quota slot can be reused.

        Close is best-effort: Playwright may already have torn the window down.
        A failed delete must surface so callers can log or retry; swallowing it
        left closed ``apex-ea-*`` configs occupying the workspace.
        """
        self.close(profile_id)
        self._request(
            "POST",
            "/browser/delete",
            payload={
                "workspaceId": self.workspace_id(),
                "dirIds": [str(profile_id)],
            },
        )

    def ensure_open(
        self,
        *,
        seed: str,
        proxy_url: str,
        on_profile=None,
    ) -> dict[str, str]:
        if not self.health():
            raise RoxyBrowserError(
                f"RoxyBrowser local API is not running (expected {self.base_url})"
            )
        if fresh_profile_each_run():
            return self._open_fresh(seed=seed, proxy_url=proxy_url, on_profile=on_profile)
        name = profile_name_for(seed)
        profile_id = self.find_profile_id(name)
        if profile_id:
            try:
                self.update_profile(profile_id, proxy_url, seed=seed)
            except RoxyBrowserError:
                try:
                    self.delete(profile_id)
                except RoxyBrowserError:
                    pass
                profile_id = None
        if not profile_id:
            profile_id = self.add_profile(name=name, proxy_url=proxy_url, seed=seed)
        # Record before open(): a hung /browser/open is the usual SIGKILL
        # path, and the parent can only delete IDs written to flow_browsers.json.
        if on_profile:
            on_profile(self, str(profile_id))
        try:
            opened = self.open(profile_id)
        except RoxyBrowserError:
            try:
                self.delete(profile_id)
            except RoxyBrowserError:
                pass
            profile_id = self.add_profile(name=name, proxy_url=proxy_url, seed=seed)
            if on_profile:
                on_profile(self, str(profile_id))
            opened = self.open(profile_id)
        opened["name"] = name
        return opened

    def _open_fresh(self, *, seed: str, proxy_url: str, on_profile=None) -> dict[str, str]:
        """Create a new directory for this attempt. Do not reopen an old one."""
        name = fresh_profile_name()
        profile_id = self.add_profile(name=name, proxy_url=proxy_url, seed=seed)
        if on_profile:
            on_profile(self, str(profile_id))
        try:
            opened = self.open(profile_id)
        except RoxyBrowserError:
            try:
                self.delete(profile_id)
            except RoxyBrowserError:
                pass
            name = fresh_profile_name()
            profile_id = self.add_profile(name=name, proxy_url=proxy_url, seed=seed)
            if on_profile:
                on_profile(self, str(profile_id))
            opened = self.open(profile_id)
        opened["name"] = name
        return opened

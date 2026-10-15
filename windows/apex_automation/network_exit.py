"""Report where this machine's traffic leaves, so Forge can compare it with each account.

Two probes: through the Windows system proxy (EA App follows it) and without
any proxy (game UDP goes there unless a Clash TUN captures it). Only public
network facts are sent, never proxy addresses or credentials. Reporting is
best effort and never blocks or fails a login or a game.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import json
import re
import sys
import threading
from typing import Callable
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener
import uuid

from .account_provider import HttpAccountProvider, LeaseProviderError

PROBES = (
    "https://ipinfo.io/json",
    "http://ip-api.com/json/?fields=status,countryCode,regionName,city,query,as,org",
)
PERIOD_S = 1800.0
FIRST_PERIODIC_S = 300.0
UNSAFE_TEXT = re.compile(r"://|@|[\x00-\x1f]|(?i:password|token|secret|authorization)\s*[:=]")


def parse_proxy_server(server: str) -> str | None:
    """WinINET ProxyServer: "host:port" or "http=host:port;https=host:port"."""
    value = (server or "").strip()
    if "=" in value:
        entries = dict(part.split("=", 1) for part in value.split(";") if "=" in part)
        value = (entries.get("https") or entries.get("http") or "").strip()
    if not value:
        return None
    return value if "://" in value else "http://" + value


def system_proxy() -> str | None:
    """The proxy EA App follows, or None when the Windows system proxy is off."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
            server = winreg.QueryValueEx(key, "ProxyServer")[0] if enabled else ""
    except OSError:
        return None
    return parse_proxy_server(server) if enabled else None


def _text(value: object, maximum: int = 120) -> str | None:
    text = str(value or "").strip()[:maximum]
    return text if text and not UNSAFE_TEXT.search(text) else None


def parse_geo(body: bytes) -> dict:
    """Read ipinfo.io or ip-api.com JSON into public facts."""
    data = json.loads(body)
    if not isinstance(data, dict):
        return {}
    try:
        ip = str(ipaddress.ip_address(str(data.get("ip") or data.get("query") or "")))
    except ValueError:
        ip = None
    country = str(data.get("country") or data.get("countryCode") or "").upper()
    # ipinfo puts "AS9269 HKBN" in org; ip-api puts it in "as" and a bare name in org.
    owners = [str(data.get(key) or "") for key in ("org", "as")]
    owner = next((value for value in owners if value.startswith("AS")), owners[0] or owners[1])
    match = re.match(r"AS(\d+)\s*(.*)", owner)
    asn = int(match.group(1)) if match and 0 < int(match.group(1)) <= 4294967295 else None
    return {
        "ip": ip,
        "country": country if re.fullmatch(r"[A-Z]{2}", country) else None,
        "region": _text(data.get("region") or data.get("regionName")),
        "city": _text(data.get("city")),
        "asn": asn,
        "org": _text(match.group(2) if match else owner, 200),
    }


def _fetch(url: str, proxy: str | None, timeout: float) -> bytes:
    # No ambient proxies or bypass lists: each probe takes exactly one path.
    opener = build_opener(ProxyHandler({}))
    request = Request(url, headers={"Accept": "application/json", "User-Agent": "apex-runner"})
    if proxy:
        parsed = urlparse(proxy)
        request.set_proxy(parsed.netloc, parsed.scheme or "http")
    with opener.open(request, timeout=timeout) as response:
        return response.read(65536)


def probe(proxy: str | None, *, fetch: Callable[..., bytes] = _fetch, timeout: float = 8.0) -> dict:
    for url in PROBES:
        try:
            facts = parse_geo(fetch(url, proxy, timeout))
        except Exception:
            continue
        if facts.get("ip") or facts.get("country"):
            return facts
    return {"error": "probe_failed"}


def observe(*, proxy_lookup: Callable[[], str | None] = system_proxy, probe_path=probe) -> dict:
    proxy = proxy_lookup()
    paths = [{"path": "system", **probe_path(proxy)}] if proxy else []
    paths.append({"path": "direct", **probe_path(None)})
    return {"systemProxy": bool(proxy), "paths": paths}


class ExitReporter:
    """Send observations to Forge; a few unsent ones are retried with the next."""

    def __init__(self, provider: HttpAccountProvider, *, observe_exit: Callable[[], dict] = observe,
                 notify: Callable[[str], None] = print) -> None:
        base = provider.lease_url.rsplit("/v1/runner/account-leases", 1)[0]
        self.api = HttpAccountProvider(base + "/v1/runner/network-exits", provider._provider_token,
                                       client_version=provider.client_version, transport=provider.transport)
        self.observe_exit = observe_exit
        self.notify = notify
        self._pending: list[dict] = []
        self._lock = threading.Lock()

    def report(self, reason: str, lease: tuple[str, int] | None, *, clash=None) -> dict:
        """``clash`` is a facts dict, or a reader called here, off the account cycle."""
        if callable(clash):
            try:
                clash = clash()
            except Exception:
                clash = None
        observation = self.observe_exit()
        body = {
            "schemaVersion": 1,
            "observationId": uuid.uuid4().hex,
            "observedAt": dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
            "reason": reason,
            "lease": {"leaseId": lease[0], "leaseFence": lease[1]} if lease else None,
            "systemProxy": observation["systemProxy"],
            "paths": observation["paths"],
            "clash": clash,
        }
        main = next((p for p in body["paths"] if p["path"] == ("system" if body["systemProxy"] else "direct")), {})
        self.notify(f"网络出口：{main.get('country') or '未知'} {main.get('city') or ''} {main.get('ip') or ''}".strip())
        with self._lock:
            self._pending = (self._pending + [body])[-10:]
            while self._pending:
                item = self._pending[0]
                try:
                    self.api._request("POST", "", payload=item, operation_id=item["observationId"])
                except LeaseProviderError as error:
                    if error.retryable:
                        break  # Keep it for the next observation.
                except Exception:
                    break
                self._pending.pop(0)
        return body

    def report_later(self, reason: str, lease: tuple[str, int] | None, *, clash=None) -> None:
        threading.Thread(target=self._quiet, args=(reason, lease, clash), daemon=True,
                         name="network-exit-report").start()

    def _quiet(self, reason, lease, clash) -> None:
        try:
            self.report(reason, lease, clash=clash)
        except Exception as error:  # Reporting must never affect the account cycle.
            self.notify(f"网络出口上报失败：{type(error).__name__}")


class ExitMonitor:
    """Re-check the exit while a lease runs. It never switches nodes."""

    def __init__(self, reporter: ExitReporter, lease: tuple[str, int], *, clash=None,
                 active: Callable[[], bool] = lambda: True,
                 first_s: float = FIRST_PERIODIC_S, period_s: float = PERIOD_S) -> None:
        self.reporter, self.lease, self.clash, self.active = reporter, lease, clash, active
        self.first_s, self.period_s = first_s, period_s
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="network-exit-monitor")

    def start(self) -> "ExitMonitor":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        wait = self.first_s
        while not self._stop.wait(wait):
            if not self.active():
                return  # The lease ended through a path that did not stop us.
            facts = None
            if self.clash is not None:
                try:
                    facts = self.clash.game_route()
                except Exception:
                    facts = None
            self.reporter._quiet("periodic", self.lease, facts)
            wait = self.period_s

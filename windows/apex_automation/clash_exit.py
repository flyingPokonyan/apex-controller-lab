"""Switch the local Clash selector at an account boundary.

Node names and the controller address live in the machine's private runner
config, under ``clash``. Without that object the runner keeps its current
exit and does not call Clash.
"""
from __future__ import annotations

import ipaddress
import json
import sys
from pathlib import Path, PureWindowsPath
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


PLAY_COUNTRIES = ("HK", "US", "JP")
GAME_PROCESSES = ("EADesktop.exe", "r5apex.exe")


class ExitNotReady(RuntimeError):
    """The exit is not the account's country, so login must not start."""

    reason_code = "EXIT_COUNTRY_MISMATCH"


class ClashExitSwitch:
    def __init__(
        self,
        *,
        api_base: str,
        secret: str,
        group: str,
        nodes: Mapping[str, str],
        proxy_url: str,
        verify_url: str,
        transport: Callable[..., tuple[int, bytes]] | None = None,
        system_proxy_enabled: Callable[[], bool] | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.secret = secret
        self.group = group
        self.nodes = dict(nodes)
        self.proxy_url = proxy_url
        self.verify_url = verify_url
        self._transport = transport or _urllib_transport
        self._system_proxy_enabled = system_proxy_enabled or windows_system_proxy_enabled

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ClashExitSwitch:
        api_base = _loopback_url(payload.get("apiBase"), "apiBase", schemes={"http", "https"})
        proxy_url = _loopback_url(payload.get("proxyUrl"), "proxyUrl", schemes={"http"})
        verify_url = _public_https(payload.get("verifyUrl"))
        group = payload.get("group")
        if not isinstance(group, str) or not group.strip():
            raise ValueError("clash.group 必须是节点组名称")
        secret = payload.get("secret", "")
        if not isinstance(secret, str):
            raise ValueError("clash.secret 必须是字符串")
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, dict):
            raise ValueError("clash.nodes 必须按国家给出节点名")
        nodes: dict[str, str] = {}
        for country in PLAY_COUNTRIES:
            name = raw_nodes.get(country)
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"clash.nodes 缺少 {country}")
            nodes[country] = name.strip()
        return cls(
            api_base=api_base,
            secret=secret,
            group=group.strip(),
            nodes=nodes,
            proxy_url=proxy_url,
            verify_url=verify_url,
        )

    def prepare(self, country: str) -> dict:
        """Select the country, drop old tunnels, and stop unless the exit matches."""
        selected = (country or "").strip().upper()
        if selected not in self.nodes:
            raise ExitNotReady(f"租约国家 {selected or '空'} 没有对应节点")
        if self._system_proxy_enabled():
            raise ExitNotReady("系统代理仍开着，游戏只能走 TUN")
        self._require_tun_rules()
        self._api(
            "PUT",
            f"/proxies/{quote(self.group, safe='')}",
            {"name": self.nodes[selected]},
        )
        self._close_game_connections()
        body = self._body("GET", self.verify_url, proxy=self.proxy_url)
        observed = observed_country(body)
        if observed != selected:
            raise ExitNotReady(f"实际出口是 {observed or '未知'}，账号国家是 {selected}")
        return {'country': observed, 'ip': observed_ip(body)}

    def _close_game_connections(self) -> None:
        connections = _json_object(self._api("GET", "/connections")).get("connections")
        if not isinstance(connections, list):
            raise ExitNotReady("Clash 连接响应无法解析")
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            metadata = connection.get("metadata")
            if not isinstance(metadata, dict):
                continue
            process = str(metadata.get("process") or metadata.get("processPath") or "")
            if PureWindowsPath(process).name.casefold() not in {exe.casefold() for exe in GAME_PROCESSES}:
                continue
            identity = connection.get("id")
            if isinstance(identity, str) and identity:
                self._api("DELETE", f"/connections/{quote(identity, safe='')}")

    def _require_tun_rules(self) -> None:
        config = _json_object(self._api("GET", "/configs"))
        tun = config.get("tun")
        if not isinstance(tun, dict) or tun.get("enable") is not True:
            raise ExitNotReady("Clash 没有只开 TUN")
        if config.get("ipv6") is not False:
            raise ExitNotReady("Clash 仍开启 IPv6，可能直连泄露")
        if str(config.get("mode") or "").lower() != "rule":
            raise ExitNotReady("Clash 不在规则模式")
        rules = _rule_list(self._api("GET", "/rules"))
        for exe in GAME_PROCESSES:
            if not _routes_through_group(rules, self.group, exe=exe):
                raise ExitNotReady(
                    f"Clash 规则没有确保 {exe} 使用 {self.group}；"
                    f"请将 PROCESS-NAME,{exe},{self.group} 放在分流规则前"
                )
        host = urlparse(self.verify_url).hostname or ""
        if not _routes_through_group(
            rules, self.group, exe=PureWindowsPath(sys.executable).name,
            process_path=sys.executable, host=host,
        ):
            raise ExitNotReady(
                f"出口检测与游戏没有使用同一节点组；"
                f"请将 DOMAIN,{host},{self.group} 放在分流规则前"
            )

    def _api(self, method: str, path: str, payload: dict[str, str] | None = None) -> bytes:
        status, body = self._exchange(
            method, f"{self.api_base}{path}", payload=payload, proxy=None, controller=True,
        )
        if not 200 <= status < 300:
            raise ExitNotReady(f"Clash {method} {path} 返回 HTTP {status}")
        return body

    def _body(self, method: str, url: str, *, proxy: str | None = None) -> bytes:
        status, body = self._exchange(method, url, payload=None, proxy=proxy)
        if not 200 <= status < 300:
            raise ExitNotReady(f"出口检查返回 HTTP {status}")
        return body

    def _exchange(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, str] | None,
        proxy: str | None,
        controller: bool = False,
    ) -> tuple[int, bytes]:
        headers = {}
        if controller and self.secret:
            headers["Authorization"] = f"Bearer {self.secret}"
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            return self._transport(method, url, headers=headers, body=body, timeout=20, proxy=proxy)
        except (OSError, TimeoutError, URLError) as error:
            source = "本机 Clash" if controller else "出口检测服务"
            raise ExitNotReady(f"{source}没有响应") from error


def load_exit_switch(path: Path | None) -> ClashExitSwitch | None:
    """Return None when the private config has no clash section."""
    if path is None or not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Runner 私有配置无法读取") from error
    if not isinstance(payload, dict) or "clash" not in payload:
        return None
    clash = payload["clash"]
    if not isinstance(clash, dict):
        raise ValueError("clash 配置必须是对象")
    return ClashExitSwitch.from_mapping(clash)


def observed_country(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if len(text) == 2 and text.isascii() and text.isalpha():
        return text.upper()
    try:
        payload = json.loads(text or "{}")
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    for key in ("country", "countryCode", "country_code"):
        value = str(payload.get(key) or "").strip().upper()
        if len(value) == 2 and value.isascii() and value.isalpha():
            return value
    return ""


def observed_ip(body: bytes) -> str:
    try:
        value = json.loads(body)
        raw = (value.get('ip') or value.get('query')) if isinstance(value, dict) else None
        return str(ipaddress.ip_address(raw)) if isinstance(raw, str) else ''
    except (ValueError, UnicodeError):
        return ''


def windows_system_proxy_enabled() -> bool:
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            enabled, _kind = winreg.QueryValueEx(key, "ProxyEnable")
    except OSError:
        return False
    return bool(enabled)


def _loopback_url(value: object, name: str, *, schemes: set[str]) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"clash.{name} 必须是本机地址")
    parsed = urlparse(value.strip())
    host = parsed.hostname
    if parsed.scheme not in schemes or parsed.username or parsed.password or not host:
        raise ValueError(f"clash.{name} 必须是不带账号的本机地址")
    if host != "localhost":
        try:
            address = ipaddress.ip_address(host)
        except ValueError as error:
            raise ValueError(f"clash.{name} 必须是本机地址") from error
        if not address.is_loopback:
            raise ValueError(f"clash.{name} 必须是本机地址")
    return value.strip().rstrip("/")


def _public_https(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("clash.verifyUrl 必须是 https 地址")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or parsed.username or not parsed.hostname:
        raise ValueError("clash.verifyUrl 必须是不带账号的 https 地址")
    return value.strip()


def _json_object(body: bytes) -> dict:
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ExitNotReady("Clash 配置响应无法解析") from error
    if not isinstance(payload, dict):
        raise ExitNotReady("Clash 配置响应无法解析")
    return payload


def _rule_list(body: bytes) -> list:
    rules = _json_object(body).get("rules")
    if not isinstance(rules, list):
        raise ExitNotReady("Clash 规则响应无法解析")
    return rules


def _routes_through_group(
    rules: list, group: str, *, exe: str, process_path: str = "", host: str = "",
) -> bool:
    """Prove a route from the ordered API rules, without guessing rule-set contents.

    An earlier rule that may match must use the same selector. Game destinations
    and executable paths are not known here, so put exact PROCESS-NAME rules
    before general routing rules. The probe also needs a route to that selector.
    """
    for rule in rules:
        if not isinstance(rule, dict):
            return False
        extra = rule.get("extra")
        if isinstance(extra, dict) and extra.get("disabled") is True:
            continue
        # The controller API returns ProcessName/ProcessPath, not YAML spelling.
        kind = str(rule.get("type") or "").replace("-", "").casefold()
        payload = str(rule.get("payload") or "").casefold()
        matches: bool | None = None
        if kind == "processname":
            matches = payload == exe.casefold()
        elif kind == "processpath":
            if process_path:
                matches = PureWindowsPath(payload) == PureWindowsPath(process_path.casefold())
            elif PureWindowsPath(payload).name != exe.casefold():
                matches = False
        elif kind == "match":
            matches = True
        elif host:
            target = host.casefold().rstrip(".")
            domain = payload.rstrip(".")
            if kind == "domain":
                matches = target == domain
            elif kind == "domainsuffix":
                matches = target == domain or target.endswith("." + domain)
            elif kind == "domainkeyword":
                matches = domain in target
        if matches is False:
            continue
        if rule.get("proxy") != group:
            return False
        if matches is True:
            return True
    return False


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # A controller redirect must not forward its secret. A probe redirect
        # may also change the hostname and invalidate the checked routing rule.
        return None


def _urllib_transport(method, url, *, headers, body, timeout, proxy):
    # Explicitly disable ambient/Windows system proxies for the controller API.
    opener = build_opener(ProxyHandler({}), _NoRedirect())
    request = Request(url, data=body, headers=headers, method=method)
    if proxy:
        # ProxyHandler honors NO_PROXY and Windows bypass rules even for an
        # explicit proxy. This probe must use the configured Clash listener.
        parsed = urlparse(proxy)
        request.set_proxy(parsed.netloc, parsed.scheme)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()

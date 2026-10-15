"""Pick a working node in the account's country at an account boundary.

Optional: only machines whose private runner config has a ``clash`` object
switch. The Runner never switches while Apex runs, tests a node's delay
before using it, and spreads accounts over the country's nodes. Problems are
returned as facts for Forge to show; they never stop a login.
"""
from __future__ import annotations

import ipaddress
import json
from pathlib import Path, PureWindowsPath
import random
import subprocess
import sys
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import ProxyHandler, Request, build_opener

APEX_PROCESSES = ("r5apex.exe", "r5apex_dx12.exe")
GAME_PROCESSES = (*APEX_PROCESSES, "EADesktop.exe")
GROUP_TYPES = {"selector", "urltest", "fallback", "loadbalance", "relay", "compatible", "pass", "direct", "reject", "rejectdrop"}
NAMES = {
    "HK": ("香港", "hong kong", "hongkong"),
    "US": ("美国", "united states", "usa"),
    "JP": ("日本", "japan", "东京", "大阪"),
}
DELAY_URL = "http://www.gstatic.com/generate_204"
MAX_TESTS = 6


class ClashUnavailable(RuntimeError):
    pass


def node_country(name: str) -> str | None:
    """Country from a flag emoji first, then from common names."""
    letters = [chr(ord(ch) - 0x1F1E6 + ord("A")) for ch in name if 0x1F1E6 <= ord(ch) <= 0x1F1FF]
    if len(letters) >= 2:
        return "".join(letters[:2])
    lowered = name.casefold()
    for country, words in NAMES.items():
        if any(word in lowered for word in words):
            return country
    return None


def apex_running() -> bool:
    if sys.platform != "win32":
        return False
    for executable in APEX_PROCESSES:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {executable}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, errors="replace", timeout=10, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode != 0 or executable.lower() in result.stdout.lower():
            return True  # Unknown counts as running: never switch under a game.
    return False


class ClashExitSwitch:
    def __init__(
        self,
        *,
        api_base: str,
        secret: str,
        group: str,
        transport: Callable[..., tuple[int, bytes]] | None = None,
        game_running: Callable[[], bool] = apex_running,
        rng: random.Random | None = None,
    ) -> None:
        self.api_base = api_base.rstrip("/")
        self.secret = secret
        self.group = group
        self._transport = transport or _urllib_transport
        self._game_running = game_running
        self._rng = rng or random.Random()
        self._recent: list[str] = []

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ClashExitSwitch:
        api_base = _loopback_url(payload.get("apiBase"))
        group = payload.get("group")
        if not isinstance(group, str) or not group.strip():
            raise ValueError("clash.group 必须是游戏流量使用的节点组名称")
        secret = payload.get("secret", "")
        if not isinstance(secret, str):
            raise ValueError("clash.secret 必须是字符串")
        return cls(api_base=api_base, secret=secret, group=group.strip())

    def prepare(self, country: str) -> dict:
        """Switch between accounts. Returns the facts sent to Forge."""
        facts = {"group": self.group, "node": None, "country": country, "delayMs": None,
                 "gameConnections": None, "gameViaNode": None, "error": None}
        try:
            try:
                running = self._game_running()
            except Exception:
                running = True  # Unknown counts as running.
            if running:
                facts["error"] = "game_running"
                return facts
            proxies = self._proxies()
            if self.group not in proxies:
                facts["error"] = "group_missing"
                return facts
            routes = candidate_routes(proxies, self.group, country)
            self._rng.shuffle(routes)
            # Spread accounts: nodes used most recently go to the back.
            recent = self._recent
            routes.sort(key=lambda route: len(recent) - recent.index(route[-1][1]) if route[-1][1] in recent else 0)
            for route in routes[:MAX_TESTS]:
                node = route[-1][1]
                delay = self._delay(node)
                if delay is None:
                    continue
                for group, member in route:
                    self._api("PUT", f"/proxies/{quote(group, safe='')}", {"name": member})
                if self._selected_leaf(self._proxies()) != node:
                    facts["error"] = "switch_failed"
                    return facts
                self._close_game_connections()
                self._recent = [node] + [name for name in self._recent if name != node][:4]
                facts.update(node=node, delayMs=delay)
                return facts
            facts["error"] = "no_node"
        except ClashUnavailable:
            facts["error"] = "clash_unreachable"
        return facts

    def game_route(self) -> dict:
        """Whether live game connections go through the group's current node."""
        facts = {"group": self.group, "node": None, "country": None, "delayMs": None,
                 "gameConnections": None, "gameViaNode": None, "error": None}
        try:
            node = self._selected_leaf(self._proxies())
            facts.update(node=node, country=node_country(node) if node else None)
            chains = [chain for chain, _ in self._game_chains()]
            facts["gameConnections"] = len(chains)
            if chains and node:
                facts["gameViaNode"] = all(node in chain for chain in chains)
        except ClashUnavailable:
            facts["error"] = "clash_unreachable"
        return facts

    def _proxies(self) -> dict:
        payload = _json(self._api("GET", "/proxies")).get("proxies")
        if not isinstance(payload, dict):
            raise ClashUnavailable("proxies")
        return payload

    def _selected_leaf(self, proxies: dict) -> str | None:
        name, seen = self.group, set()
        while name in proxies and _is_group(proxies[name]) and name not in seen:
            seen.add(name)
            name = proxies[name].get("now")
        return name if isinstance(name, str) and name not in seen else None

    def _delay(self, node: str) -> int | None:
        try:
            body = self._api("GET", f"/proxies/{quote(node, safe='')}/delay?timeout=4000&url={quote(DELAY_URL, safe='')}")
        except ClashUnavailable:
            return None  # A timed-out node answers with an error status.
        delay = _json(body).get("delay")
        return delay if type(delay) is int and 0 < delay <= 60000 else None

    def _game_chains(self):
        connections = _json(self._api("GET", "/connections")).get("connections") or []
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            metadata = connection.get("metadata") or {}
            process = str(metadata.get("process") or metadata.get("processPath") or "")
            if PureWindowsPath(process).name.casefold() in {name.casefold() for name in GAME_PROCESSES}:
                yield [str(item) for item in connection.get("chains") or []], connection.get("id")

    def _close_game_connections(self) -> None:
        for _, identity in list(self._game_chains()):
            if isinstance(identity, str) and identity:
                self._api("DELETE", f"/connections/{quote(identity, safe='')}")

    def _api(self, method: str, path: str, payload: dict | None = None) -> bytes:
        headers = {"Authorization": f"Bearer {self.secret}"} if self.secret else {}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            status, data = self._transport(method, f"{self.api_base}{path}", headers=headers, body=body, timeout=10)
        except (OSError, TimeoutError, URLError) as error:
            raise ClashUnavailable(path) from error
        if not 200 <= status < 300:
            raise ClashUnavailable(f"{method} {path} {status}")
        return data


def candidate_routes(proxies: dict, group: str, country: str) -> list[list[tuple[str, str]]]:
    """Ways to select a node of ``country``: directly, or through one member selector."""
    routes, seen = [], set()
    members = proxies.get(group, {}).get("all") or []
    for member in members:
        info = proxies.get(member)
        if not isinstance(info, dict):
            continue
        if not _is_group(info):
            if node_country(member) == country and member not in seen:
                seen.add(member)
                routes.append([(group, member)])
        elif str(info.get("type", "")).casefold() == "selector":
            for leaf in info.get("all") or []:
                leaf_info = proxies.get(leaf)
                if isinstance(leaf_info, dict) and not _is_group(leaf_info) and node_country(leaf) == country and leaf not in seen:
                    seen.add(leaf)
                    routes.append([(group, member), (member, leaf)])
    return routes


def _is_group(info: dict) -> bool:
    return str(info.get("type", "")).replace("-", "").casefold() in GROUP_TYPES or "all" in info


def _json(body: bytes) -> dict:
    try:
        value = json.loads(body.decode("utf-8") or "{}")
    except (UnicodeError, json.JSONDecodeError) as error:
        raise ClashUnavailable("invalid json") from error
    return value if isinstance(value, dict) else {}


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


def _loopback_url(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("clash.apiBase 必须是本机地址")
    parsed = urlparse(value.strip())
    host = parsed.hostname
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not host:
        raise ValueError("clash.apiBase 必须是不带账号的本机地址")
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    if not loopback:
        raise ValueError("clash.apiBase 必须是本机地址")
    return value.strip().rstrip("/")


def _urllib_transport(method, url, *, headers, body, timeout):
    # The controller is local; ambient and system proxies must not carry its secret.
    opener = build_opener(ProxyHandler({}))
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()

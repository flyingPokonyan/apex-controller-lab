"""Read, and when Forge allows it, switch the machine's own Clash at account boundaries.

No setup: the Runner finds Clash Verge's local controller (named pipe, Unix
socket or loopback HTTP) from its runtime config, and the game group from
the final MATCH rule. A private ``clash`` object may still name them.

Switching happens only when the lease says ``exitSwitch`` and Apex is not
running. It picks a delay-tested node of the account's country and spreads
accounts over nodes. Every problem is returned as a fact for Forge to show;
nothing here stops a login.
"""
from __future__ import annotations

import http.client
import io
import ipaddress
import json
import os
from pathlib import Path, PureWindowsPath
import random
import re
import socket
import subprocess
import sys
import threading
import time
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
VERGE_ID = "io.github.clash-verge-rev.clash-verge-rev"
DEFAULT_PIPE = r"\\.\pipe\verge-mihomo"
DEFAULT_SOCKET = "/tmp/verge/verge-mihomo.sock"


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


# --- Finding the local controller -------------------------------------------------

def _yaml_scalar(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value.split(" #", 1)[0].strip()


def read_controller_settings(text: str) -> dict[str, str]:
    """Top-level controller keys of a Clash/mihomo YAML config."""
    settings = {}
    for key in ("external-controller", "external-controller-pipe", "external-controller-unix", "secret"):
        match = re.search(rf"^{re.escape(key)}:[ \t]*(.*)$", text, re.M)
        if match:
            settings[key] = _yaml_scalar(match.group(1))
    return settings


def verge_config_paths() -> list[Path]:
    homes = []
    if os.environ.get("APPDATA"):
        homes.append(Path(os.environ["APPDATA"]) / VERGE_ID)
    if os.environ.get("LOCALAPPDATA"):
        homes.append(Path(os.environ["LOCALAPPDATA"]) / VERGE_ID)
    home = Path.home()
    homes += [home / "Library/Application Support" / VERGE_ID, home / ".local/share" / VERGE_ID]
    return [base / name for base in homes for name in ("clash-verge.yaml", "config.yaml")]


def candidate_endpoints(*, api_base: str | None = None, secret: str = "",
                        paths: list[Path] | None = None) -> list[tuple[str, str, str]]:
    """(kind, address, secret) in the order to try: explicit, from config files, defaults."""
    found: list[tuple[str, str, str]] = []
    if api_base:
        found.append(("http", api_base, secret))
    for path in verge_config_paths() if paths is None else paths:
        try:
            settings = read_controller_settings(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        key = settings.get("secret", "")
        if settings.get("external-controller-pipe"):
            found.append(("pipe", settings["external-controller-pipe"], key))
        if settings.get("external-controller-unix"):
            found.append(("unix", settings["external-controller-unix"], key))
        if settings.get("external-controller"):
            address = settings["external-controller"]
            if address.startswith(":"):
                address = "127.0.0.1" + address
            found.append(("http", address if "://" in address else "http://" + address, key))
    found.append(("pipe", DEFAULT_PIPE, "") if os.name == "nt" else ("unix", DEFAULT_SOCKET, ""))
    found += [("http", "http://127.0.0.1:9097", secret), ("http", "http://127.0.0.1:9090", secret)]
    usable, seen = [], set()
    for kind, address, key in found:
        if (kind, address) in seen or (kind == "http" and not _loopback(address)):
            continue
        if kind == "pipe" and os.name != "nt" or kind == "unix" and not hasattr(socket, "AF_UNIX"):
            continue
        seen.add((kind, address))
        usable.append((kind, address, key))
    return usable


def _loopback(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or not parsed.hostname:
        return False
    try:
        return parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


class _Stream:
    """Lets http.client parse a response from a pipe or socket file."""

    def __init__(self, reader):
        self.reader = reader

    def makefile(self, *_args, **_kwargs):
        return self.reader


def _request_bytes(method: str, path: str, headers: dict, body: bytes | None) -> bytes:
    lines = [f"{method} {path} HTTP/1.1", "Host: localhost", "Connection: close"]
    lines += [f"{name}: {value}" for name, value in headers.items()]
    lines.append(f"Content-Length: {len(body or b'')}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + (body or b"")


def _read_response(reader, method: str) -> tuple[int, bytes]:
    response = http.client.HTTPResponse(_Stream(reader), method=method)
    response.begin()
    return response.status, response.read()


def _unix_exchange(address, method, path, headers, body, timeout):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(address)
        sock.sendall(_request_bytes(method, path, headers, body))
        return _read_response(sock.makefile("rb"), method)


def _pipe_exchange(address, method, path, headers, body, timeout):
    # A Windows named pipe has no read timeout; the caller's thread bounds it.
    deadline = time.monotonic() + timeout
    while True:
        try:
            raw = open(address, "r+b", buffering=0)
            break
        except OSError as error:
            if getattr(error, "winerror", None) == 231 and time.monotonic() < deadline:  # ERROR_PIPE_BUSY
                time.sleep(0.05)
                continue
            raise
    with raw:
        raw.write(_request_bytes(method, path, headers, body))
        return _read_response(io.BufferedReader(raw), method)


def _http_exchange(address, method, path, headers, body, timeout):
    # Loopback only; ambient and system proxies must not carry the secret.
    opener = build_opener(ProxyHandler({}))
    request = Request(address.rstrip("/") + path, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        with error:
            return error.code, error.read()


EXCHANGES = {"unix": _unix_exchange, "pipe": _pipe_exchange, "http": _http_exchange}


def _bounded(call, timeout):
    result: dict = {}

    def run():
        try:
            result["value"] = call()
        except BaseException as error:  # Re-raised in the caller's thread.
            result["error"] = error

    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout + 1)
    if worker.is_alive():
        raise TimeoutError("clash controller did not answer")
    if "error" in result:
        raise result["error"]
    return result["value"]


class ClashApi:
    """The local mihomo controller, found on first use and re-found after failures."""

    def __init__(self, endpoints: Callable[[], list[tuple[str, str, str]]] = candidate_endpoints,
                 exchanges: Mapping[str, Callable] = EXCHANGES) -> None:
        self._endpoints = endpoints
        self._exchanges = exchanges
        self._current: tuple[str, str, str] | None = None

    def _send(self, endpoint, method, path, body, timeout):
        kind, address, secret = endpoint
        headers = {"Authorization": f"Bearer {secret}"} if secret else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        exchange = self._exchanges[kind]
        return _bounded(lambda: exchange(address, method, path, headers, body, timeout), timeout)

    def _connect(self):
        for endpoint in self._endpoints():
            try:
                status, _ = self._send(endpoint, "GET", "/version", None, 3)
            except (OSError, TimeoutError, URLError, http.client.HTTPException):
                continue
            if status == 200:
                self._current = endpoint
                return endpoint
        raise ClashUnavailable("not_found")

    def request(self, method: str, path: str, payload: dict | None = None, *, timeout: float = 10) -> tuple[int, bytes]:
        endpoint = self._current or self._connect()
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        try:
            return self._send(endpoint, method, path, body, timeout)
        except (OSError, TimeoutError, URLError, http.client.HTTPException) as error:
            self._current = None
            raise ClashUnavailable("unreachable") from error


# --- Selection -------------------------------------------------------------------

class ClashExitSwitch:
    def __init__(self, *, api: ClashApi | None = None, group: str | None = None,
                 game_running: Callable[[], bool] = apex_running, rng: random.Random | None = None) -> None:
        self.api = api or ClashApi()
        self.configured_group = group
        self._game_running = game_running
        self._rng = rng or random.Random()
        self._recent: list[str] = []
        self._group: str | None = None

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ClashExitSwitch:
        api_base = payload.get("apiBase")
        if api_base is not None and (not isinstance(api_base, str) or not _loopback(api_base.strip())):
            raise ValueError("clash.apiBase 必须是本机地址")
        secret = payload.get("secret", "")
        if not isinstance(secret, str):
            raise ValueError("clash.secret 必须是字符串")
        group = payload.get("group")
        if group is not None and (not isinstance(group, str) or not group.strip()):
            raise ValueError("clash.group 必须是节点组名称")
        base = api_base.strip().rstrip("/") if isinstance(api_base, str) else None
        api = ClashApi(lambda: candidate_endpoints(api_base=base, secret=secret))
        return cls(api=api, group=group.strip() if isinstance(group, str) else None)

    def _facts(self, **values) -> dict:
        facts = {"group": self._group or self.configured_group, "node": None, "country": None, "delayMs": None,
                 "gameConnections": None, "gameViaNode": None, "error": None}
        facts.update(values)
        return facts

    def prepare(self, country: str, *, avoid=(), prefer=()) -> dict:
        """Switch between accounts. Returns the facts sent to Forge.

        Forge lists this account's own earlier nodes (``prefer``) and the nodes
        other accounts used lately on any machine (``avoid``, least recently
        used first). They only order the choice: an avoided node is still
        used when nothing else answers.
        """
        try:
            try:
                running = self._game_running()
            except Exception:
                running = True  # Unknown counts as running.
            if running:
                return self._facts(country=country, error="game_running")
            proxies = self._proxies()
            group = self._resolve_group()
            if group not in proxies:
                return self._facts(country=country, error="group_missing")
            routes = candidate_routes(proxies, group, country)
            self._rng.shuffle(routes)
            # Spread accounts: Forge's lists first, then this machine's own
            # recent picks go to the back.
            recent, avoid, prefer = self._recent, list(avoid), list(prefer)

            def rank(route):
                node = route[-1][1]
                if node in prefer:
                    return 0, prefer.index(node)
                if node in avoid:
                    return 2, avoid.index(node)
                return 1, len(recent) - recent.index(node) if node in recent else 0
            routes.sort(key=rank)
            for route in routes[:MAX_TESTS]:
                node = route[-1][1]
                delay = self._delay(node)
                if delay is None:
                    continue
                for selector, member in route:
                    self._call("PUT", f"/proxies/{quote(selector, safe='')}", {"name": member})
                if selected_leaf(self._proxies(), group) != node:
                    return self._facts(country=country, error="switch_failed")
                self._close_connections(GAME_PROCESSES)
                self._recent = [node] + [name for name in self._recent if name != node][:4]
                return self._facts(node=node, country=country, delayMs=delay)
            return self._facts(country=country, error="no_node")
        except ClashUnavailable as error:
            return self._facts(country=country, error="clash_not_found" if str(error) == "not_found" else "clash_unreachable")

    def current(self) -> dict:
        """Read-only: the node game traffic would use now."""
        try:
            node = selected_leaf(self._proxies(), self._resolve_group())
            return self._facts(node=node, country=node_country(node) if node else None)
        except ClashUnavailable as error:
            return self._facts(error="clash_not_found" if str(error) == "not_found" else "clash_unreachable")

    def game_route(self) -> dict:
        """Read-only: whether live Apex connections go through Clash's current node."""
        facts = self.current()
        if facts["error"]:
            return facts
        try:
            chains = [chain for chain, _ in self._connections(APEX_PROCESSES)]
        except ClashUnavailable:
            return self._facts(error="clash_unreachable")
        facts["gameConnections"] = len(chains)
        if chains and facts["node"]:
            facts["gameViaNode"] = all(facts["node"] in chain for chain in chains)
        return facts

    def _call(self, method: str, path: str, payload: dict | None = None) -> bytes:
        status, data = self.api.request(method, path, payload)
        if not 200 <= status < 300:
            raise ClashUnavailable(f"{method} {path} {status}")
        return data

    def _proxies(self) -> dict:
        payload = _json(self._call("GET", "/proxies")).get("proxies")
        if not isinstance(payload, dict):
            raise ClashUnavailable("proxies")
        return payload

    def _resolve_group(self) -> str:
        if self.configured_group:
            self._group = self.configured_group
        elif self._group is None:
            # The final MATCH rule names the group that unmatched traffic,
            # including Apex game servers, falls through to.
            rules = _json(self._call("GET", "/rules")).get("rules") or []
            target = next((rule.get("proxy") for rule in reversed(rules)
                           if isinstance(rule, dict) and str(rule.get("type", "")).casefold() == "match"), None)
            self._group = target if isinstance(target, str) and target else "GLOBAL"
        return self._group

    def _delay(self, node: str) -> int | None:
        try:
            status, body = self.api.request(
                "GET", f"/proxies/{quote(node, safe='')}/delay?timeout=4000&url={quote(DELAY_URL, safe='')}", timeout=8)
        except ClashUnavailable:
            return None
        delay = _json(body).get("delay") if status == 200 else None
        return delay if type(delay) is int and 0 < delay <= 60000 else None

    def _connections(self, processes):
        wanted = {name.casefold() for name in processes}
        connections = _json(self._call("GET", "/connections")).get("connections") or []
        for connection in connections:
            if not isinstance(connection, dict):
                continue
            metadata = connection.get("metadata") or {}
            process = str(metadata.get("process") or metadata.get("processPath") or "")
            if PureWindowsPath(process).name.casefold() in wanted:
                yield [str(item) for item in connection.get("chains") or []], connection.get("id")

    def _close_connections(self, processes) -> None:
        for _, identity in list(self._connections(processes)):
            if isinstance(identity, str) and identity:
                self._call("DELETE", f"/connections/{quote(identity, safe='')}")


def selected_leaf(proxies: dict, group: str) -> str | None:
    name, seen = group, set()
    while name in proxies and _is_group(proxies[name]) and name not in seen:
        seen.add(name)
        name = proxies[name].get("now")
    return name if isinstance(name, str) and name not in seen else None


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


def load_exit_switch(path: Path | None) -> ClashExitSwitch:
    """Auto-discovering by default; a private ``clash`` object may pin address or group."""
    if path is None or not path.exists():
        return ClashExitSwitch()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Runner 私有配置无法读取") from error
    if not isinstance(payload, dict) or "clash" not in payload:
        return ClashExitSwitch()
    clash = payload["clash"]
    if not isinstance(clash, dict):
        raise ValueError("clash 配置必须是对象")
    return ClashExitSwitch.from_mapping(clash)

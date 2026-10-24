"""Clash switching picks a working node of the account's country, only between accounts."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import random
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import unquote

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.account_orchestrator import AccountOrchestrator
from apex_automation.account_provider import FakeAccountProvider
from apex_automation import clash_exit
from apex_automation.clash_exit import (
    ClashApi,
    ClashExitSwitch,
    ClashUnavailable,
    candidate_endpoints,
    candidate_routes,
    load_exit_switch,
    node_country,
    read_controller_settings,
)
from apex_automation.ea_app import EaAppAutomationError, EaIdentityFact, EaUiState
from apex_automation.lease_keeper import LeaseKeeperSnapshot, LeaseKeeperState
from apex_automation.orchestration_state import AtomicCheckpointStore

HK = ["🇭🇰 日用 专线 香港[0.2]", "🇭🇰 高级 专线 香港 01", "🇭🇰 高级 专线 香港 02", "🇭🇰 高级 专线 香港 03"]
US = ["🇺🇸 高级 专线 美国 01", "🇺🇸 高级 专线 美国 02"]
JP = ["🇯🇵 高级 专线 日本 01"]
INFO = ["到期: 2026-11-13", "流量: 15.63 GB / 400 GB"]
# A second airport merged into Proxies with a proxy-provider ("flower").
FLOWER_HK = ["🇭🇰 香港高级 IEPL 专线 1", "🇭🇰 香港标准 IEPL 专线 2"]


class FakeClash:
    """The subset of the mihomo controller API the Runner uses, as a ClashApi."""

    def __init__(self, *, dead=(), connections=None, down=False, flower=(), flower_udp=True):
        leaves = HK + US + JP + INFO + ["🇸🇬 高级 专线 狮城 01"]
        self.proxies = {name: {"type": "AnyTLS", "udp": True} for name in leaves}
        # Like mihomo: provider nodes are group members but not in /proxies.
        self.providers = {
            "default": {"vehicleType": "Compatible", "proxies": [{"name": "Proxies", "type": "Selector"}]},
            "flower": {"vehicleType": "HTTP", "proxies": [{"name": name, "type": "Trojan", "udp": flower_udp}
                                                          for name in flower]},
        }
        self.proxies.update({
            "DIRECT": {"type": "Direct"},
            "Proxies": {"type": "Selector", "now": INFO[0], "all": leaves + list(flower)},
            "HK": {"type": "Selector", "now": HK[0], "all": HK},
            "US": {"type": "Selector", "now": US[0], "all": US},
            "JP": {"type": "Selector", "now": JP[0], "all": JP},
            # Same shape as the YToo profile: Final -> Proxies -> node.
            "Final": {"type": "Selector", "now": "Proxies", "all": ["Proxies", "DIRECT"]},
        })
        self.rules = [{"type": "GeoIP", "payload": "CN", "proxy": "DIRECT"}, {"type": "Match", "payload": "", "proxy": "Final"}]
        self.dead = set(dead)
        self.connections = connections or []
        self.down = down
        self.calls = []

    def request(self, method, path, payload=None, *, timeout=10):
        if self.down:
            raise ClashUnavailable("not_found")
        path = unquote(path)
        self.calls.append((method, path))
        if method == "GET" and path == "/proxies":
            return 200, json.dumps({"proxies": self.proxies}).encode()
        if method == "GET" and path == "/rules":
            return 200, json.dumps({"rules": self.rules}).encode()
        if method == "GET" and path == "/providers/proxies":
            return 200, json.dumps({"providers": self.providers}).encode()
        if method == "GET" and path.endswith("/delay?timeout=4000&url=http://www.gstatic.com/generate_204"):
            node = path[len("/proxies/"):].split("/delay")[0]
            if node not in self.proxies:
                return 404, b'{"message":"resource not found"}'
            return (504, b'{"message":"timeout"}') if node in self.dead else (200, b'{"delay":87}')
        if method == "GET" and path.startswith("/providers/proxies/flower/") and \
                path.endswith("/healthcheck?timeout=4000&url=http://www.gstatic.com/generate_204"):
            node = path[len("/providers/proxies/flower/"):].split("/healthcheck")[0]
            return (504, b'{"message":"timeout"}') if node in self.dead else (200, b'{"delay":63}')
        if method == "PUT" and path.startswith("/proxies/"):
            self.proxies[path[len("/proxies/"):]]["now"] = payload["name"]
            return 204, b""
        if method == "GET" and path == "/connections":
            return 200, json.dumps({"connections": self.connections}).encode()
        if method == "DELETE" and path.startswith("/connections/"):
            return 204, b""
        raise AssertionError(f"unexpected {method} {path}")

    def selected(self, group="Final"):
        name = group
        while "now" in self.proxies.get(name, {}):
            name = self.proxies[name]["now"]
        return name

    def writes(self):
        return [call for call in self.calls if call[0] in {"PUT", "DELETE"}]


def switch(clash, *, running=False, seed=1, group=None):
    return ClashExitSwitch(api=clash, group=group, game_running=lambda: running, rng=random.Random(seed))


class NodeChoiceTests(unittest.TestCase):
    def test_country_comes_from_flags_or_names_and_info_entries_are_not_nodes(self):
        self.assertEqual(node_country(HK[1]), "HK")
        self.assertEqual(node_country(US[0]), "US")
        self.assertEqual(node_country("Tokyo 日本 IPLC"), "JP")
        self.assertIsNone(node_country(INFO[0]))
        routes = candidate_routes(FakeClash().proxies, "Final", "HK")
        self.assertEqual(sorted(route[-1][1] for route in routes), sorted(HK))
        self.assertEqual({route[0] for route in routes}, {("Final", "Proxies")})

    def test_group_comes_from_the_final_match_rule(self):
        clash = FakeClash()
        runner = switch(clash)
        self.assertEqual(runner.current()["group"], "Final")
        self.assertEqual(runner.current()["node"], INFO[0])
        self.assertEqual(clash.writes(), [])

    def test_switch_uses_a_live_node_of_the_country_and_closes_old_game_connections(self):
        clash = FakeClash(dead=HK[:3], connections=[
            {"id": "c1", "metadata": {"process": "r5apex.exe"}, "chains": [INFO[0], "Proxies", "Final"]},
            {"id": "c2", "metadata": {"processPath": r"C:\\Program Files\\Chrome\\chrome.exe"}, "chains": ["DIRECT"]},
        ])
        facts = switch(clash).prepare("HK")
        self.assertEqual((facts["node"], facts["delayMs"], facts["error"], facts["group"]), (HK[3], 87, None, "Final"))
        self.assertEqual(clash.selected(), HK[3])
        self.assertIn(("DELETE", "/connections/c1"), clash.calls)
        self.assertNotIn(("DELETE", "/connections/c2"), clash.calls)

    def test_never_switches_while_apex_runs(self):
        clash = FakeClash()
        self.assertEqual(switch(clash, running=True).prepare("HK")["error"], "game_running")
        self.assertEqual(clash.calls, [])
        broken = ClashExitSwitch(api=clash, game_running=Mock(side_effect=OSError("tasklist")))
        self.assertEqual(broken.prepare("HK")["error"], "game_running")

    def test_no_live_node_missing_clash_or_group_keeps_the_current_selection(self):
        clash = FakeClash(dead=US)
        self.assertEqual(switch(clash).prepare("US")["error"], "no_node")
        self.assertEqual(clash.writes(), [])
        self.assertEqual(switch(FakeClash(down=True)).prepare("HK")["error"], "clash_not_found")
        self.assertEqual(switch(FakeClash(down=True)).game_route()["error"], "clash_not_found")
        self.assertEqual(switch(FakeClash(), group="游戏出口").prepare("HK")["error"], "group_missing")

    def test_consecutive_accounts_spread_over_nodes(self):
        runner = switch(FakeClash())
        chosen = [runner.prepare("HK")["node"] for _ in range(4)]
        self.assertEqual(len(set(chosen)), 4)

    def test_nodes_other_accounts_used_go_last_and_the_accounts_own_node_first(self):
        runner = switch(FakeClash())
        self.assertEqual(runner.prepare("HK", avoid=HK[1:])["node"], HK[0])
        self.assertEqual(switch(FakeClash()).prepare("HK", prefer=[HK[2]], avoid=[HK[0]])["node"], HK[2])

    def test_a_merged_airport_s_nodes_are_tested_through_their_provider_and_used(self):
        clash = FakeClash(flower=FLOWER_HK, dead=[FLOWER_HK[0]])
        routes = candidate_routes(switch(clash)._proxies(), "Final", "HK")
        self.assertEqual(sorted(route[-1][1] for route in routes), sorted(HK + FLOWER_HK))
        facts = switch(clash).prepare("HK", avoid=HK)
        self.assertEqual((facts["node"], facts["delayMs"], facts["error"]), (FLOWER_HK[1], 63, None))
        self.assertEqual(clash.selected(), FLOWER_HK[1])
        self.assertEqual(switch(clash).current()["node"], FLOWER_HK[1])

    def test_by_default_the_account_keeps_its_own_node_on_either_airport(self):
        clash = FakeClash(flower=FLOWER_HK)
        self.assertEqual(switch(clash).prepare("HK", prefer=[HK[1]])["node"], HK[1])
        self.assertEqual(switch(clash).prepare("HK", prefer=[FLOWER_HK[0]])["node"], FLOWER_HK[0])

    @patch.object(clash_exit, "PREFERRED_NODE_MARK", "IEPL")
    def test_iepl_nodes_come_first_but_not_onto_another_account_s_ip(self):
        clash = FakeClash(flower=FLOWER_HK)
        # The account's own YToo node gives way to an IEPL node nobody used.
        self.assertIn(switch(clash).prepare("HK", prefer=[HK[1]])["node"], FLOWER_HK)
        # Its own IEPL node comes first of all.
        self.assertEqual(switch(clash).prepare("HK", prefer=[HK[1], FLOWER_HK[1]])["node"], FLOWER_HK[1])
        # IEPL nodes other accounts used rank after every unused YToo node.
        self.assertEqual(switch(clash).prepare("HK", prefer=[HK[2]], avoid=FLOWER_HK)["node"], HK[2])
        self.assertNotIn(switch(clash).prepare("HK", avoid=FLOWER_HK)["node"], FLOWER_HK)
        # No IEPL node answers: the account keeps its own node.
        self.assertEqual(switch(FakeClash(flower=FLOWER_HK, dead=FLOWER_HK)).prepare("HK", prefer=[HK[3]])["node"], HK[3])
        with patch.object(clash_exit, "PREFERRED_NODE_MARK", ""):
            self.assertEqual(switch(clash).prepare("HK", prefer=[HK[1]])["node"], HK[1])

    def test_nodes_that_cannot_carry_udp_are_never_game_exits(self):
        clash = FakeClash(flower=FLOWER_HK, flower_udp=False)
        clash.proxies[HK[1]]["udp"] = False
        nodes = {route[-1][1] for route in candidate_routes(switch(clash)._proxies(), "Final", "HK")}
        self.assertEqual(nodes, {HK[0], HK[2], HK[3]})
        self.assertIn(switch(clash).prepare("HK", avoid=[HK[0], HK[2], HK[3]])["node"], {HK[0], HK[2], HK[3]})

    def test_an_avoided_node_is_still_used_when_nothing_else_answers(self):
        # Least recently used first: HK 02 was used longest ago by another account.
        clash = FakeClash(dead=[HK[0]])
        facts = switch(clash).prepare("HK", avoid=[HK[2], HK[1], HK[3]])
        self.assertEqual((facts["node"], facts["error"]), (HK[2], None))

    def test_game_route_counts_only_apex_connections(self):
        clash = FakeClash()
        runner = switch(clash)
        node = runner.prepare("JP")["node"]
        writes = len(clash.writes())
        ea = {"id": "e", "metadata": {"process": "EADesktop.exe"}, "chains": [node, "Proxies", "Final"]}
        clash.connections = [ea, {"id": "c", "metadata": {"process": "r5apex.exe"}, "chains": [node, "Proxies", "Final"]}]
        self.assertEqual((runner.game_route()["gameConnections"], runner.game_route()["gameViaNode"]), (1, True))
        clash.connections = [ea, {"id": "c", "metadata": {"process": "r5apex_dx12.exe"}, "chains": ["DIRECT"]}]
        facts = runner.game_route()
        self.assertEqual((facts["node"], facts["country"], facts["gameConnections"], facts["gameViaNode"]), (node, "JP", 1, False))
        # An accelerator that takes Apex traffic leaves only EA App connections in Clash.
        clash.connections = [ea]
        facts = runner.game_route()
        self.assertEqual((facts["gameConnections"], facts["gameViaNode"]), (0, None))
        self.assertEqual(len(clash.writes()), writes)  # Reading the route never writes.


class DiscoveryTests(unittest.TestCase):
    def test_controller_settings_are_read_from_verge_runtime_config(self):
        text = "\n".join([
            "mixed-port: 7897",
            "external-controller: ''",
            "external-controller-pipe: \\\\.\\pipe\\verge-mihomo",
            "external-controller-unix: /tmp/verge/verge-mihomo.sock",
            "secret: 'abc''def'",
            "tun:",
            "  enable: true",
        ])
        settings = read_controller_settings(text)
        self.assertEqual(settings["external-controller"], "")
        self.assertEqual(settings["external-controller-pipe"], r"\\.\pipe\verge-mihomo")
        self.assertEqual(settings["secret"], "abc'def")
        self.assertEqual(read_controller_settings('external-controller-pipe: "\\\\\\\\.\\\\pipe\\\\x"')["external-controller-pipe"], r"\\.\pipe\x")

    def test_endpoints_prefer_the_config_then_defaults_and_stay_local(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "clash-verge.yaml"
            config.write_text("external-controller: 127.0.0.1:9191\nexternal-controller-unix: /tmp/x.sock\n"
                              "external-controller-pipe: \\\\.\\pipe\\custom\nsecret: s3\n", encoding="utf-8")
            endpoints = candidate_endpoints(paths=[config])
            far = candidate_endpoints(api_base="http://10.0.0.5:9090", paths=[])
        local = "pipe" if os.name == "nt" else "unix"
        first = endpoints[0]
        self.assertEqual(first[0], local)
        self.assertIn(("http", "http://127.0.0.1:9191", "s3"), endpoints)
        self.assertEqual(endpoints[-2:], [("http", "http://127.0.0.1:9097", ""), ("http", "http://127.0.0.1:9090", "")])
        self.assertNotIn("http://10.0.0.5:9090", [address for _, address, _ in far])

    def test_api_uses_first_answering_endpoint_and_sends_secret(self):
        seen = []

        def http(address, method, path, headers, body, timeout):
            seen.append((address, path, headers.get("Authorization")))
            if address.endswith(":9097"):
                raise ConnectionRefusedError()
            return 200, b'{"version":"v1.19"}'

        api = ClashApi(lambda: [("http", "http://127.0.0.1:9097", ""), ("http", "http://127.0.0.1:9090", "k")],
                       exchanges={"http": http})
        self.assertEqual(api.request("GET", "/proxies")[0], 200)
        self.assertEqual(seen[-1], ("http://127.0.0.1:9090", "/proxies", "Bearer k"))
        with self.assertRaises(ClashUnavailable) as caught:
            ClashApi(lambda: [], exchanges={}).request("GET", "/proxies")
        self.assertEqual(str(caught.exception), "not_found")

    def test_chunked_http_response_is_read_from_a_stream(self):
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\n{\"a\":\r\n2\r\n1}\r\n0\r\n\r\n"
        self.assertEqual(clash_exit._read_response(io.BytesIO(raw), "GET"), (200, b'{"a":1}'))

    @unittest.skipUnless(hasattr(socket, "AF_UNIX") and os.name != "nt", "Unix socket controller")
    def test_unix_socket_exchange(self):
        directory = tempfile.mkdtemp(dir="/tmp")
        self.addCleanup(lambda: __import__("shutil").rmtree(directory, ignore_errors=True))
        path = os.path.join(directory, "c.sock")
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        received = []

        def serve():
            conn, _ = server.accept()
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(4096)
                received.append(data)
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")
            server.close()

        threading.Thread(target=serve, daemon=True).start()
        self.assertEqual(clash_exit._unix_exchange(path, "GET", "/version", {}, None, 5), (200, b"{}"))
        self.assertTrue(received[0].startswith(b"GET /version HTTP/1.1\r\n"))

    @unittest.skipUnless(os.name == "nt", "Windows named pipe controller")
    def test_named_pipe_exchange(self):
        import ctypes
        import uuid
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                            wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID]
        kernel.ConnectNamedPipe.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        kernel.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        for name in ("FlushFileBuffers", "DisconnectNamedPipe", "CloseHandle"):
            getattr(kernel, name).argtypes = [wintypes.HANDLE]
        name = r"\\.\pipe\apex-test-" + uuid.uuid4().hex
        handle = kernel.CreateNamedPipeW(name, 3, 0, 1, 65536, 65536, 0, None)  # duplex, byte mode
        self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
        received = []

        def serve():
            kernel.ConnectNamedPipe(handle, None)
            buffer, count, data = ctypes.create_string_buffer(65536), wintypes.DWORD(), b""
            while b"\r\n\r\n" not in data:
                kernel.ReadFile(handle, buffer, 65536, ctypes.byref(count), None)
                data += buffer.raw[:count.value]
            received.append(data)
            reply = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n5\r\n{\"a\":\r\n2\r\n1}\r\n0\r\n\r\n"
            kernel.WriteFile(handle, reply, len(reply), ctypes.byref(count), None)
            kernel.FlushFileBuffers(handle)
            kernel.DisconnectNamedPipe(handle)
            kernel.CloseHandle(handle)

        threading.Thread(target=serve, daemon=True).start()
        self.assertEqual(clash_exit._pipe_exchange(name, "GET", "/version", {}, None, 5), (200, b'{"a":1}'))
        self.assertTrue(received[0].startswith(b"GET /version HTTP/1.1\r\n"))


class ConfigTests(unittest.TestCase):
    def test_without_private_config_the_runner_discovers_clash_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-cycle.private.json"
            self.assertIsNone(load_exit_switch(path).configured_group)
            path.write_text(json.dumps({"schemaVersion": 1, "deviceId": "dev"}), encoding="utf-8")
            self.assertIsInstance(load_exit_switch(path), ClashExitSwitch)
            path.write_text(json.dumps({"clash": {"apiBase": "http://10.1.2.3:9097", "secret": "do-not-log"}}), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_exit_switch(path)
            self.assertNotIn("do-not-log", str(caught.exception))
            path.write_text(json.dumps({"clash": {"group": "游戏出口"}}), encoding="utf-8")
            self.assertEqual(load_exit_switch(path).configured_group, "游戏出口")

    def test_old_lease_without_a_country_is_hong_kong_and_switching_is_off(self):
        from datetime import timedelta
        from apex_automation.account_provider import HttpAccountProvider, ProviderHttpResponse

        now = datetime.now(timezone.utc)
        stamp = lambda delta: (now + timedelta(seconds=delta)).isoformat().replace("+00:00", "Z")

        def provider(**extra):
            body = {"schemaVersion": 1, "leaseId": "lease_1", "leaseFence": 7, "accountId": "acct_1",
                    "status": "CLAIMED", "targetLevel": 20, "expectedIdentity": {"platform": "ea", "accountId": "ea_1"},
                    "issuedAt": stamp(0), "renewAfter": stamp(30), "expiresAt": stamp(120), "serverTime": stamp(0), **extra}
            transport = Mock()
            transport.request.return_value = ProviderHttpResponse(200, {}, json.dumps(body).encode())
            return HttpAccountProvider("https://runner.example/v1/runner/account-leases", "provider-secret-token",
                                       client_version="0.5.1", transport=transport)

        old = provider().claim("claim_1", "LEVEL_TO_TARGET")
        self.assertEqual((old.exit_country, old.exit_switch), ("HK", False))
        new = provider(exitCountry="jp", exitSwitch=True).claim("claim_2", "LEVEL_TO_TARGET")
        self.assertEqual((new.exit_country, new.exit_switch), ("JP", True))
        self.assertEqual((old.exit_avoid_nodes, old.exit_prefer_nodes), ((), ()))
        hinted = provider(exitAvoidNodes=[HK[1], 7, ""], exitPreferNodes=[HK[2]]).claim("claim_3", "LEVEL_TO_TARGET")
        self.assertEqual((hinted.exit_avoid_nodes, hinted.exit_prefer_nodes), ((HK[1],), (HK[2],)))
        # A malformed hint never rejects the lease.
        self.assertEqual(provider(exitAvoidNodes="HK").claim("claim_4", "LEVEL_TO_TARGET").exit_avoid_nodes, ())

    def test_http_controller_ignores_ambient_proxies(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(self.path)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with patch.dict(os.environ, {"HTTP_PROXY": "http://127.0.0.1:9", "NO_PROXY": ""}):
                status, _ = clash_exit._http_exchange(f"http://127.0.0.1:{server.server_port}", "GET", "/proxies",
                                                      {}, None, 5)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual((status, received), (200, ["/proxies"]))


class Driver:
    def __init__(self, log, *, signed_in):
        self.log, self.signed_in = log, signed_in

    def ensure_started(self):
        self.log.append("ea.start")
        return EaUiState.SIGNED_IN if self.signed_in else EaUiState.LOGIN

    def current_page_is_banned(self):
        return False

    def current_identity(self):
        return EaIdentityFact("ea_1", "fake", True) if self.signed_in else None

    def sign_in(self, credentials, otp_supplier):
        self.log.append("ea.sign_in")

    def verify_identity(self, expected):
        return EaIdentityFact(expected, "fake", True)

    def start_apex(self):
        raise EaAppAutomationError("stop before play")

    def stop_apex(self, *, settle_launch=False):
        from apex_automation.ea_app import ApexExitEvidence
        return ApexExitEvidence(True, True)

    def sign_out(self):
        return True

    def clear_cache(self, *, before_clear):
        before_clear()
        return True


class IdleKeeper:
    def __init__(self, _provider, lease, **_kwargs):
        self.lease = lease

    def start(self):
        return None

    def stop(self, *, timeout_s=10.0):
        return self.snapshot()

    def wake(self):
        return None

    def is_current(self):
        return True

    def diagnostics(self):
        return {}

    def snapshot(self):
        return LeaseKeeperSnapshot(state=LeaseKeeperState.CURRENT, expires_at=self.lease.expires_at,
                                   renew_after=self.lease.renew_after, pending_operation_id=None, error_code=None)


class ExitOrchestratorTests(unittest.TestCase):
    def _run(self, *, signed_in, switch, reporter=None, country="HK", allowed=True):
        lease = replace(FakeAccountProvider.lease("acct_1", now=datetime.now(timezone.utc)),
                        expected_ea_account_id="ea_1", exit_country=country, exit_switch=allowed)
        with tempfile.TemporaryDirectory() as directory:
            log = []
            # The fake provider cannot hand out credentials, so a reached login stops right after this note.
            notes = lambda message: log.append(message)
            AccountOrchestrator(
                provider=FakeAccountProvider([lease]), ea_driver=Driver(log, signed_in=signed_in),
                play_session=object(), checkpoint_store=AtomicCheckpointStore(Path(directory) / "status.json"),
                device_id="device_1", capture_source=object(), notify=notes,
                exit_switch=switch, exit_reporter=reporter, lease_keeper_factory=IdleKeeper,
            ).run_once()
            return log, lease

    def test_switch_failure_never_blocks_the_login_and_is_reported(self):
        reporter = Mock()
        clash = FakeClash(dead=JP)
        log, lease = self._run(signed_in=False, switch=switch(clash), reporter=reporter, country="JP")
        self.assertIn("游戏出口未切换（no_node），沿用当前节点", log)
        self.assertIn("EA 开始自动登录", log)
        reason, lease_ref = reporter.report_later.call_args.args
        self.assertEqual((reason, lease_ref), ("login", (lease.lease_id, lease.lease_fence)))
        self.assertEqual(reporter.report_later.call_args.kwargs["clash"]["error"], "no_node")

    def test_lease_hints_reach_the_switch(self):
        clash = FakeClash()
        lease = replace(FakeAccountProvider.lease("acct_1", now=datetime.now(timezone.utc)),
                        expected_ea_account_id="ea_1", exit_country="HK", exit_switch=True,
                        exit_avoid_nodes=tuple(HK[:3]))
        with tempfile.TemporaryDirectory() as directory:
            AccountOrchestrator(
                provider=FakeAccountProvider([lease]), ea_driver=Driver([], signed_in=False),
                play_session=object(), checkpoint_store=AtomicCheckpointStore(Path(directory) / "status.json"),
                device_id="device_1", capture_source=object(), notify=lambda _: None,
                exit_switch=switch(clash), lease_keeper_factory=IdleKeeper,
            ).run_once()
        self.assertEqual(clash.selected(), HK[3])

    def test_switch_happens_before_sign_in(self):
        clash = FakeClash()
        log, _ = self._run(signed_in=False, switch=switch(clash))
        switched = next(i for i, item in enumerate(log) if item.startswith("游戏出口切换到 🇭🇰"))
        self.assertLess(switched, log.index("EA 开始自动登录"))
        self.assertEqual(node_country(clash.selected()), "HK")

    def test_without_device_permission_the_runner_only_reads_clash(self):
        clash = FakeClash()
        reporter = Mock()
        log, _ = self._run(signed_in=False, switch=switch(clash), reporter=reporter, allowed=False)
        self.assertFalse(any(item.startswith("游戏出口") for item in log))
        self.assertIn("EA 开始自动登录", log)
        reader = reporter.report_later.call_args.kwargs["clash"]
        self.assertEqual(reader()["node"], INFO[0])
        self.assertEqual(clash.writes(), [])

    def test_an_already_signed_in_account_does_not_switch_mid_session(self):
        runner = Mock()
        reporter = Mock()
        log, _ = self._run(signed_in=True, switch=runner, reporter=reporter)
        runner.prepare.assert_not_called()
        reporter.report_later.assert_not_called()
        self.assertNotIn("EA 开始自动登录", log)

if __name__ == "__main__":
    unittest.main()

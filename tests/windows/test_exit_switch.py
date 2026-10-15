"""Clash switching picks a working node of the account's country, only between accounts."""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
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
from apex_automation.clash_exit import (
    ClashExitSwitch,
    candidate_routes,
    load_exit_switch,
    node_country,
    _urllib_transport,
)
from apex_automation.ea_app import EaAppAutomationError, EaIdentityFact, EaUiState
from apex_automation.lease_keeper import LeaseKeeperSnapshot, LeaseKeeperState
from apex_automation.orchestration_state import AtomicCheckpointStore

HK = ["🇭🇰 日用 专线 香港[0.2]", "🇭🇰 高级 专线 香港 01", "🇭🇰 高级 专线 香港 02", "🇭🇰 高级 专线 香港 03"]
US = ["🇺🇸 高级 专线 美国 01", "🇺🇸 高级 专线 美国 02"]
JP = ["🇯🇵 高级 专线 日本 01"]
INFO = ["到期: 2026-11-13", "流量: 15.63 GB / 400 GB"]


class FakeClash:
    """The subset of the mihomo controller API the Runner uses."""

    def __init__(self, *, dead=(), connections=None, down=False):
        leaves = HK + US + JP + INFO + ["🇸🇬 高级 专线 狮城 01"]
        self.proxies = {name: {"type": "AnyTLS", "udp": True} for name in leaves}
        self.proxies.update({
            "DIRECT": {"type": "Direct"},
            "Proxies": {"type": "Selector", "now": INFO[0], "all": leaves},
            "HK": {"type": "Selector", "now": HK[0], "all": HK},
            "US": {"type": "Selector", "now": US[0], "all": US},
            "JP": {"type": "Selector", "now": JP[0], "all": JP},
            "Final": {"type": "Selector", "now": "Proxies", "all": ["Proxies", "DIRECT", "HK", "US", "JP"]},
        })
        self.dead = set(dead)
        self.connections = connections or []
        self.down = down
        self.calls = []
        self.headers = []

    def __call__(self, method, url, *, headers, body, timeout):
        if self.down:
            raise OSError("connection refused")
        path = unquote(url.split("127.0.0.1:9097", 1)[1])
        self.calls.append((method, path))
        self.headers.append(headers)
        if method == "GET" and path == "/proxies":
            return 200, json.dumps({"proxies": self.proxies}).encode()
        if method == "GET" and path.endswith("/delay?timeout=4000&url=http://www.gstatic.com/generate_204"):
            node = path[len("/proxies/"):].split("/delay")[0]
            return (504, b'{"message":"timeout"}') if node in self.dead else (200, b'{"delay":87}')
        if method == "PUT" and path.startswith("/proxies/"):
            self.proxies[path[len("/proxies/"):]]["now"] = json.loads(body)["name"]
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

    def puts(self):
        return [call for call in self.calls if call[0] == "PUT"]


def switch(clash, *, running=False, seed=1):
    return ClashExitSwitch(api_base="http://127.0.0.1:9097", secret="local-secret", group="Final",
                           transport=clash, game_running=lambda: running, rng=random.Random(seed))


class NodeChoiceTests(unittest.TestCase):
    def test_country_comes_from_flags_or_names_and_info_entries_are_not_nodes(self):
        self.assertEqual(node_country(HK[1]), "HK")
        self.assertEqual(node_country(US[0]), "US")
        self.assertEqual(node_country("Tokyo 日本 IPLC"), "JP")
        self.assertIsNone(node_country(INFO[0]))
        routes = candidate_routes(FakeClash().proxies, "Final", "HK")
        self.assertEqual(sorted(route[-1][1] for route in routes), sorted(HK))

    def test_switch_uses_a_live_node_of_the_country_and_closes_old_game_connections(self):
        clash = FakeClash(dead=HK[:3], connections=[
            {"id": "c1", "metadata": {"process": "r5apex.exe"}, "chains": [INFO[0], "Proxies"]},
            {"id": "c2", "metadata": {"processPath": r"C:\\Program Files\\Chrome\\chrome.exe"}, "chains": ["DIRECT"]},
        ])
        facts = switch(clash).prepare("HK")
        self.assertEqual(facts["node"], HK[3])
        self.assertEqual(facts["delayMs"], 87)
        self.assertIsNone(facts["error"])
        self.assertEqual(clash.selected(), HK[3])
        self.assertIn(("DELETE", "/connections/c1"), clash.calls)
        self.assertNotIn(("DELETE", "/connections/c2"), clash.calls)
        self.assertEqual(clash.headers[0]["Authorization"], "Bearer local-secret")

    def test_never_switches_while_apex_runs(self):
        clash = FakeClash()
        facts = switch(clash, running=True).prepare("HK")
        self.assertEqual(facts["error"], "game_running")
        self.assertEqual(clash.calls, [])
        broken = ClashExitSwitch(api_base="http://127.0.0.1:9097", secret="", group="Final", transport=clash,
                                 game_running=Mock(side_effect=OSError("tasklist")))
        self.assertEqual(broken.prepare("HK")["error"], "game_running")

    def test_no_live_node_or_unreachable_clash_keeps_the_current_selection(self):
        clash = FakeClash(dead=US)
        self.assertEqual(switch(clash).prepare("US")["error"], "no_node")
        self.assertEqual(clash.puts(), [])
        self.assertEqual(switch(FakeClash(down=True)).prepare("HK")["error"], "clash_unreachable")
        missing = ClashExitSwitch(api_base="http://127.0.0.1:9097", secret="", group="游戏出口",
                                  transport=FakeClash(), game_running=lambda: False)
        self.assertEqual(missing.prepare("HK")["error"], "group_missing")

    def test_consecutive_accounts_spread_over_nodes(self):
        runner = switch(FakeClash())
        chosen = [runner.prepare("HK")["node"] for _ in range(4)]
        self.assertEqual(len(set(chosen)), 4)

    def test_game_route_tells_whether_game_connections_use_the_node(self):
        clash = FakeClash()
        runner = switch(clash)
        node = runner.prepare("JP")["node"]
        clash.connections = [{"id": "c", "metadata": {"process": "r5apex.exe"}, "chains": [node, "JP", "Final"]}]
        self.assertEqual(runner.game_route()["gameViaNode"], True)
        clash.connections = [{"id": "c", "metadata": {"process": "r5apex.exe"}, "chains": ["DIRECT"]}]
        facts = runner.game_route()
        self.assertEqual((facts["node"], facts["country"], facts["gameConnections"], facts["gameViaNode"]), (node, "JP", 1, False))
        clash.connections = []
        self.assertIsNone(runner.game_route()["gameViaNode"])


class ConfigTests(unittest.TestCase):
    def test_private_config_without_clash_stays_disabled_and_secret_is_not_echoed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-cycle.private.json"
            path.write_text(json.dumps({"schemaVersion": 1, "deviceId": "dev"}), encoding="utf-8")
            self.assertIsNone(load_exit_switch(path))
            path.write_text(json.dumps({"clash": {"apiBase": "http://10.1.2.3:9097", "secret": "do-not-log", "group": "Final"}}), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_exit_switch(path)
            self.assertNotIn("do-not-log", str(caught.exception))
            path.write_text(json.dumps({"clash": {"apiBase": "http://127.0.0.1:9097", "secret": "s", "group": "Final"}}), encoding="utf-8")
            self.assertEqual(load_exit_switch(path).group, "Final")

    def test_old_lease_without_a_country_is_hong_kong(self):
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

        self.assertEqual(provider().claim("claim_1", "LEVEL_TO_TARGET").exit_country, "HK")
        self.assertEqual(provider(exitCountry="jp").claim("claim_2", "LEVEL_TO_TARGET").exit_country, "JP")

    def test_controller_transport_ignores_ambient_proxies(self):
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
                status, _ = _urllib_transport("GET", f"http://127.0.0.1:{server.server_port}/proxies",
                                              headers={}, body=None, timeout=5)
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
    def _run(self, *, signed_in, switch, reporter=None, country="HK"):
        lease = replace(FakeAccountProvider.lease("acct_1", now=datetime.now(timezone.utc)),
                        expected_ea_account_id="ea_1", exit_country=country)
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

    def test_switch_happens_before_sign_in(self):
        clash = FakeClash()
        log, _ = self._run(signed_in=False, switch=switch(clash))
        switched = next(i for i, item in enumerate(log) if item.startswith("游戏出口切换到 🇭🇰"))
        self.assertLess(switched, log.index("EA 开始自动登录"))
        self.assertEqual(node_country(clash.selected()), "HK")

    def test_an_already_signed_in_account_does_not_switch_mid_session(self):
        runner = Mock()
        reporter = Mock()
        log, _ = self._run(signed_in=True, switch=runner, reporter=reporter)
        runner.prepare.assert_not_called()
        reporter.report_later.assert_not_called()
        self.assertNotIn("EA 开始自动登录", log)


if __name__ == "__main__":
    unittest.main()

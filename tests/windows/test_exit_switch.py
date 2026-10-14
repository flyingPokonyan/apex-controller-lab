"""Account-boundary Clash switching stays off until a machine configures it."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.account_orchestrator import (
    AccountCycleOutcome,
    AccountOrchestrator,
)
from apex_automation.account_provider import FakeAccountProvider
from apex_automation.clash_exit import (
    ClashExitSwitch,
    ExitNotReady,
    load_exit_switch,
    observed_country,
    observed_ip,
    _routes_through_group,
    _urllib_transport,
)
from apex_automation.ea_app import (
    EaAppAutomationError,
    EaIdentityFact,
    EaUiState,
)
from apex_automation.lease_keeper import LeaseKeeperSnapshot, LeaseKeeperState
from apex_automation.orchestration_state import AtomicCheckpointStore


def configs(**changes) -> dict:
    payload = {
        "mode": "rule",
        "ipv6": False,
        "tun": {"enable": True},
    }
    payload.update(changes)
    return payload


def rules() -> dict:
    return {
        "rules": [
            {"type": "ProcessName", "payload": "EADesktop.exe", "proxy": "节点选择"},
            {"type": "ProcessName", "payload": "r5apex.exe", "proxy": "节点选择"},
            {"type": "Domain", "payload": "ipinfo.io", "proxy": "节点选择"},
        ]
    }


class RecordingTransport:
    def __init__(self, responses: dict[tuple[str, str], tuple[int, bytes]]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str]] = []
        self.requests: list[dict] = []

    def __call__(self, method, url, *, headers, body, timeout, proxy):
        self.calls.append((method, url))
        self.requests.append({"headers": dict(headers), "body": body, "proxy": proxy})
        for (expected_method, suffix), response in self.responses.items():
            if method == expected_method and url.endswith(suffix):
                return response
        raise AssertionError(f"unexpected {method} {url}")


def switch(transport, *, system_proxy=False) -> ClashExitSwitch:
    return ClashExitSwitch(
        api_base="http://127.0.0.1:9090",
        secret="local-secret",
        group="节点选择",
        nodes={"HK": "香港", "US": "美国", "JP": "日本"},
        proxy_url="http://127.0.0.1:7890",
        verify_url="https://ipinfo.io/json",
        transport=transport,
        system_proxy_enabled=lambda: system_proxy,
    )


@contextmanager
def local_http_server():
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.path, dict(self.headers)))
            self.send_response(302 if self.path == "/redirect" else 200)
            if self.path == "/redirect":
                self.send_header("Location", "/leaked")
            self.end_headers()
            self.wfile.write(b"US")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", received
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class ClashExitTests(unittest.TestCase):
    def test_matching_country_selects_the_node_and_closes_only_game_connections(self):
        transport = RecordingTransport({
            ("GET", "/configs"): (200, json.dumps(configs()).encode()),
            ("GET", "/rules"): (200, json.dumps(rules()).encode()),
            ("PUT", "/proxies/%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9"): (204, b""),
            ("GET", "/connections"): (200, json.dumps({"connections": [
                {"id": "ea", "metadata": {"process": "EADesktop.exe"}},
                {"id": "apex", "metadata": {"processPath": r"C:\Games\r5apex.exe"}},
                {"id": "forge", "metadata": {"process": "python.exe"}},
                {"id": "other", "metadata": {"process": "EADesktop.exe.backup"}},
                {"id": "unknown", "metadata": {}},
            ]}).encode()),
            ("DELETE", "/connections/ea"): (204, b""),
            ("DELETE", "/connections/apex"): (204, b""),
            ("GET", "/json"): (200, b'{"country":"JP","ip":"192.0.2.3"}'),
        })
        fact = switch(transport).prepare("jp")
        self.assertEqual(fact, {'country': 'JP', 'ip': '192.0.2.3'})
        methods = [method for method, _url in transport.calls]
        self.assertEqual(methods, ["GET", "GET", "PUT", "GET", "DELETE", "DELETE", "GET"])
        put = transport.calls[2][1]
        self.assertIn("%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9", put)
        self.assertEqual(json.loads(transport.requests[2]["body"]), {"name": "日本"})
        for request in transport.requests[:-1]:
            self.assertEqual(request["headers"]["Authorization"], "Bearer local-secret")
            self.assertIsNone(request["proxy"])
        probe = transport.requests[-1]
        self.assertNotIn("Authorization", probe["headers"])
        self.assertEqual(probe["proxy"], "http://127.0.0.1:7890")

    def test_another_game_or_probe_group_does_not_pass_country_verification(self):
        for index in range(3):
            with self.subTest(rule=index):
                split = rules()
                split["rules"][index]["proxy"] = "另一个代理组"
                transport = RecordingTransport({
                    ("GET", "/configs"): (200, json.dumps(configs()).encode()),
                    ("GET", "/rules"): (200, json.dumps(split).encode()),
                })
                with self.assertRaises(ExitNotReady):
                    switch(transport).prepare("HK")
                self.assertTrue(all(method == "GET" for method, _ in transport.calls))

    def test_missing_probe_route_does_not_use_the_default_policy_as_evidence(self):
        split = rules()
        split["rules"].pop()
        split["rules"].append({"type": "Match", "payload": "", "proxy": "DIRECT"})
        transport = RecordingTransport({
            ("GET", "/configs"): (200, json.dumps(configs()).encode()),
            ("GET", "/rules"): (200, json.dumps(split).encode()),
        })
        with self.assertRaises(ExitNotReady):
            switch(transport).prepare("HK")

    def test_rule_precedence_disabled_rules_and_exact_process_names(self):
        for earlier in [
            {"type": "Match", "payload": "", "proxy": "DIRECT"},
            {"type": "RuleSet", "payload": "game", "proxy": "其他组"},
            {"type": "DomainSuffix", "payload": "ea.com", "proxy": "DIRECT"},
        ]:
            with self.subTest(earlier=earlier):
                self.assertFalse(_routes_through_group(
                    [earlier] + rules()["rules"], "节点选择", exe="EADesktop.exe",
                ))
        for kind in ("ProcessName", "PROCESS-NAME"):
            with self.subTest(kind=kind):
                self.assertTrue(_routes_through_group([
                    {"type": kind, "payload": "EADesktop.exe", "proxy": "DIRECT", "extra": {"disabled": True}},
                    {"type": kind, "payload": "EADesktop.exe.backup", "proxy": "DIRECT"},
                    {"type": kind, "payload": "EADESKTOP.EXE", "proxy": "节点选择"},
                ], "节点选择", exe="EADesktop.exe"))
        self.assertFalse(_routes_through_group([
            {"type": "ProcessName", "payload": "old-EADesktop.exe", "proxy": "节点选择"},
        ], "节点选择", exe="EADesktop.exe"))

    def test_process_path_needs_the_actual_path_or_a_following_name_rule(self):
        route = {"type": "ProcessPath", "payload": r"C:\EA\EADesktop.exe", "proxy": "节点选择"}
        self.assertFalse(_routes_through_group([route], "节点选择", exe="EADesktop.exe"))
        self.assertTrue(_routes_through_group(
            [route], "节点选择", exe="EADesktop.exe", process_path=r"c:\ea\EADesktop.exe",
        ))
        self.assertTrue(_routes_through_group(
            [route] + rules()["rules"], "节点选择", exe="EADesktop.exe",
        ))

    def test_probe_domain_suffix_uses_label_boundaries(self):
        route = {"type": "DomainSuffix", "payload": "ipinfo.io", "proxy": "节点选择"}
        for host, allowed in [("ipinfo.io", True), ("api.ipinfo.io", True), ("fakeipinfo.io", False)]:
            with self.subTest(host=host):
                self.assertEqual(_routes_through_group(
                    [route], "节点选择", exe="python.exe", host=host,
                ), allowed)

    def test_redirect_is_not_an_api_success(self):
        transport = RecordingTransport({("GET", "/configs"): (302, b"")})
        with self.assertRaisesRegex(ExitNotReady, "HTTP 302"):
            switch(transport).prepare("HK")

    def test_country_response_must_be_a_complete_country_code(self):
        for body in (b"US service unavailable", b"JP error", b"Hong Kong", b"<html>US</html>"):
            with self.subTest(body=body):
                self.assertEqual(observed_country(body), "")

    def test_controller_transport_ignores_ambient_proxies_and_does_not_redirect(self):
        with local_http_server() as (url, received), patch(
            "urllib.request.getproxies", return_value={"http": "http://127.0.0.1:1"},
        ), patch("urllib.request.proxy_bypass", return_value=False):
            status, _ = _urllib_transport(
                "GET", url + "/redirect", headers={"Authorization": "Bearer test-only"},
                body=None, timeout=2, proxy=None,
            )
        self.assertEqual(status, 302)
        self.assertEqual([path for path, _ in received], ["/redirect"])

    def test_explicit_probe_proxy_cannot_be_bypassed_by_no_proxy(self):
        with local_http_server() as (url, received), patch.dict(os.environ, {"no_proxy": "*"}):
            status, body = _urllib_transport(
                "GET", "http://probe.invalid/country", headers={}, body=None, timeout=2, proxy=url,
            )
        self.assertEqual((status, body), (200, b"US"))
        self.assertEqual(received[0][0], "http://probe.invalid/country")
        self.assertNotIn("Authorization", received[0][1])

    def test_https_probe_keeps_its_destination_for_connect(self):
        with patch("apex_automation.clash_exit.build_opener") as build:
            response = build.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b"JP"
            _urllib_transport(
                "GET", "https://probe.invalid/country", headers={}, body=None,
                timeout=2, proxy="http://127.0.0.1:7890",
            )
            request = build.return_value.open.call_args.args[0]
        self.assertEqual(request.host, "127.0.0.1:7890")
        self.assertEqual(request._tunnel_host, "probe.invalid")
        self.assertEqual(request.type, "https")

    def test_wrong_country_stops_before_login_can_use_it(self):
        transport = RecordingTransport({
            ("GET", "/configs"): (200, json.dumps(configs()).encode()),
            ("GET", "/rules"): (200, json.dumps(rules()).encode()),
            ("PUT", "/proxies/%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9"): (204, b""),
            ("GET", "/connections"): (200, b'{"connections":[]}'),
            ("GET", "/json"): (200, b"US\n"),
        })
        with self.assertRaises(ExitNotReady) as caught:
            switch(transport).prepare("HK")
        self.assertIn("US", str(caught.exception))
        self.assertIn("HK", str(caught.exception))

    def test_probe_ip_is_validated(self):
        self.assertEqual(observed_ip(b'{"ip":"2001:db8::12"}'), '2001:db8::12')
        self.assertEqual(observed_ip(b'{"query":"192.0.2.2"}'), '192.0.2.2')
        self.assertEqual(observed_ip(b'{"ip":"https://user:secret@host"}'), '')

    def test_system_proxy_or_direct_game_rule_refuses_to_switch(self):
        leaked = rules()
        leaked["rules"][0]["proxy"] = "DIRECT"
        transport = RecordingTransport({
            ("GET", "/configs"): (200, json.dumps(configs()).encode()),
            ("GET", "/rules"): (200, json.dumps(leaked).encode()),
        })
        with self.assertRaises(ExitNotReady):
            switch(transport).prepare("HK")
        self.assertNotIn(("PUT", "http://127.0.0.1:9090/proxies/%E8%8A%82%E7%82%B9%E9%80%89%E6%8B%A9"), transport.calls)
        with self.assertRaises(ExitNotReady):
            switch(transport, system_proxy=True).prepare("HK")
        ipv6 = RecordingTransport({
            ("GET", "/configs"): (200, json.dumps(configs(ipv6=True)).encode()),
        })
        with self.assertRaises(ExitNotReady):
            switch(ipv6).prepare("HK")

    def test_private_config_without_clash_stays_disabled(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-cycle.private.json"
            path.write_text(json.dumps({"schemaVersion": 1, "deviceId": "dev"}), encoding="utf-8")
            self.assertIsNone(load_exit_switch(path))
            path.write_text(json.dumps({
                "clash": {
                    "apiBase": "http://127.0.0.1:9090",
                    "secret": "do-not-log",
                    "group": "节点选择",
                    "nodes": {"HK": "香港"},
                    "proxyUrl": "http://10.1.2.3:7890",
                    "verifyUrl": "https://ipinfo.io/json",
                }
            }), encoding="utf-8")
            with self.assertRaises(ValueError) as caught:
                load_exit_switch(path)
        self.assertNotIn("do-not-log", str(caught.exception))
        self.assertEqual(observed_country(b'{"countryCode":"hk"}'), "HK")

    def test_old_lease_without_a_country_is_hong_kong(self):
        from datetime import timedelta
        from apex_automation.account_provider import HttpAccountProvider, ProviderHttpResponse

        now = datetime.now(timezone.utc)

        def payload(**extra):
            body = {
                "schemaVersion": 1,
                "leaseId": "lease_1",
                "leaseFence": 7,
                "accountId": "acct_1",
                "status": "CLAIMED",
                "targetLevel": 20,
                "expectedIdentity": {"platform": "ea", "accountId": "ea_1"},
                "issuedAt": now.isoformat().replace("+00:00", "Z"),
                "renewAfter": (now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
                "expiresAt": (now + timedelta(seconds=120)).isoformat().replace("+00:00", "Z"),
                "serverTime": now.isoformat().replace("+00:00", "Z"),
            }
            body.update(extra)
            return body

        class Transport:
            def __init__(self, body):
                self.body = body

            def request(self, method, url, headers, body, timeout_s):
                return ProviderHttpResponse(200, {}, json.dumps(self.body).encode())

        provider = HttpAccountProvider(
            "https://runner.example/v1/runner/account-leases",
            "provider-secret-token",
            client_version="0.5.1",
            transport=Transport(payload()),
        )
        self.assertEqual(provider.claim("claim_1", "LEVEL_TO_TARGET").exit_country, "HK")
        provider = HttpAccountProvider(
            "https://runner.example/v1/runner/account-leases",
            "provider-secret-token",
            client_version="0.5.1",
            transport=Transport(payload(exitCountry="jp")),
        )
        self.assertEqual(provider.claim("claim_2", "LEVEL_TO_TARGET").exit_country, "JP")


class Driver:
    def __init__(self, log: list[str], *, signed_in: bool) -> None:
        self.log = log
        self.signed_in = signed_in

    def ensure_started(self) -> EaUiState:
        self.log.append("ea.start")
        return EaUiState.SIGNED_IN if self.signed_in else EaUiState.LOGIN

    def current_page_is_banned(self) -> bool:
        return False

    def current_identity(self):
        self.log.append("ea.current_identity")
        if not self.signed_in:
            return None
        return EaIdentityFact("ea_1", "fake", True)

    def sign_in(self, credentials, otp_supplier):
        self.log.append("ea.sign_in")

    def verify_identity(self, expected):
        return EaIdentityFact(expected, "fake", True)

    def start_apex(self) -> None:
        raise EaAppAutomationError("stop before play")

    def stop_apex(self, *, settle_launch=False):
        from apex_automation.ea_app import ApexExitEvidence
        return ApexExitEvidence(True, True)

    def sign_out(self) -> bool:
        return True

    def clear_cache(self, *, before_clear) -> bool:
        before_clear()
        return True


class IdleKeeper:
    def __init__(self, _provider, lease, **_kwargs):
        self.lease = lease

    def start(self) -> None:
        return None

    def stop(self, *, timeout_s: float = 10.0):
        return self.snapshot()

    def wake(self) -> None:
        return None

    def is_current(self) -> bool:
        return True

    def diagnostics(self) -> dict[str, object]:
        return {}

    def snapshot(self) -> LeaseKeeperSnapshot:
        return LeaseKeeperSnapshot(
            state=LeaseKeeperState.CURRENT,
            expires_at=self.lease.expires_at,
            renew_after=self.lease.renew_after,
            pending_operation_id=None,
            error_code=None,
        )


class ExitOrchestratorTests(unittest.TestCase):
    def _run(self, *, signed_in: bool, switch):
        lease = replace(
            FakeAccountProvider.lease("acct_1", now=datetime.now(timezone.utc)),
            expected_ea_account_id="ea_1",
            exit_country="HK",
        )
        provider = FakeAccountProvider([lease])
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(Path(directory) / "account-cycle-status.json")
            log: list[str] = []
            result = AccountOrchestrator(
                provider=provider,
                ea_driver=Driver(log, signed_in=signed_in),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                notify=lambda _message: None,
                exit_switch=switch,
                lease_keeper_factory=IdleKeeper,
            ).run_once()
            return result, log, provider, store.load()

    def test_mismatch_pauses_without_login_or_the_next_account(self):
        class Switch:
            def prepare(self, country):
                raise ExitNotReady("实际出口是 US，账号国家是 HK")

        result, log, provider, checkpoint = self._run(signed_in=False, switch=Switch())
        self.assertEqual(result.outcome, AccountCycleOutcome.PAUSED)
        self.assertEqual(result.error_code, "EXIT_COUNTRY_MISMATCH")
        self.assertNotIn("ea.sign_in", log)
        self.assertFalse(any(call[0] == "close" for call in provider.calls))
        self.assertTrue(checkpoint.has_lease)

    def test_an_already_signed_in_account_does_not_switch_mid_session(self):
        class Switch:
            def prepare(self, country):
                raise AssertionError(country)

        _result, log, _provider, _checkpoint = self._run(signed_in=True, switch=Switch())
        self.assertNotIn("ea.sign_in", log)


if __name__ == "__main__":
    unittest.main()

"""Exit observations carry only public facts and never disturb the account cycle."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))

from apex_automation.account_provider import HttpAccountProvider, ProviderHttpResponse
from apex_automation.network_exit import (
    ExitMonitor,
    ExitReporter,
    observe,
    parse_geo,
    parse_proxy_server,
    probe,
)

IPINFO = b'{"ip":"203.0.113.7","city":"Hong Kong","region":"Central and Western","country":"HK","org":"AS9269 Hong Kong Broadband Network Ltd."}'
IPAPI = b'{"status":"success","countryCode":"CN","regionName":"Guangdong","city":"Shenzhen","query":"198.51.100.9","as":"AS4134 CHINANET-BACKBONE","org":"Chinanet"}'


class ProbeTests(unittest.TestCase):
    def test_both_geo_services_become_the_same_public_facts(self):
        self.assertEqual(parse_geo(IPINFO), {"ip": "203.0.113.7", "country": "HK", "region": "Central and Western",
                                             "city": "Hong Kong", "asn": 9269, "org": "Hong Kong Broadband Network Ltd."})
        facts = parse_geo(IPAPI)
        self.assertEqual((facts["ip"], facts["country"], facts["asn"], facts["region"]), ("198.51.100.9", "CN", 4134, "Guangdong"))
        unsafe = parse_geo(b'{"ip":"bad","country":"hk1","org":"http://u:p@proxy"}')
        self.assertEqual((unsafe["ip"], unsafe["country"], unsafe["org"]), (None, None, None))

    def test_system_proxy_setting_forms(self):
        self.assertEqual(parse_proxy_server("127.0.0.1:7897"), "http://127.0.0.1:7897")
        self.assertEqual(parse_proxy_server("http=127.0.0.1:7890;https=127.0.0.1:7897;socks=127.0.0.1:1080"), "http://127.0.0.1:7897")
        self.assertIsNone(parse_proxy_server(""))

    def test_probe_falls_back_then_reports_failure_without_error_text(self):
        calls = []

        def fetch(url, proxy, timeout):
            calls.append((url, proxy))
            if "ipinfo" in url:
                raise OSError("Connection refused by 10.0.0.1")
            return IPAPI

        self.assertEqual(probe("http://127.0.0.1:7897", fetch=fetch)["country"], "CN")
        self.assertEqual([proxy for _, proxy in calls], ["http://127.0.0.1:7897"] * 2)
        self.assertEqual(probe(None, fetch=Mock(side_effect=OSError("x"))), {"error": "probe_failed"})

    def test_observe_probes_the_system_proxy_only_when_it_is_on(self):
        seen = []
        path = lambda proxy: (seen.append(proxy), {"country": "HK"})[1]
        self.assertEqual([p["path"] for p in observe(proxy_lookup=lambda: "http://127.0.0.1:7897", probe_path=path)["paths"]], ["system", "direct"])
        self.assertEqual(seen, ["http://127.0.0.1:7897", None])
        result = observe(proxy_lookup=lambda: None, probe_path=path)
        self.assertEqual((result["systemProxy"], [p["path"] for p in result["paths"]]), (False, ["direct"]))


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.transport = Mock()
        self.transport.request.return_value = ProviderHttpResponse(200, {}, b'{"accepted":true}')
        provider = HttpAccountProvider("https://forge.example/v1/runner/account-leases", "pvd_secret",
                                       client_version="test", transport=self.transport)
        observation = {"systemProxy": False, "paths": [{"path": "direct", "ip": "203.0.113.7", "country": "HK"}]}
        self.reporter = ExitReporter(provider, observe_exit=lambda: observation, notify=lambda _m: None)

    def sent(self):
        return [json.loads(call.args[3]) for call in self.transport.request.call_args_list]

    def test_report_posts_the_lease_bound_observation(self):
        clash = {"group": "Final", "node": "🇭🇰 高级 专线 香港 01", "country": "HK", "delayMs": 87,
                 "gameConnections": None, "gameViaNode": None, "error": None}
        self.reporter.report("login", ("lease_1", 3), clash=clash)
        method, url, headers, _body, _timeout = self.transport.request.call_args.args
        self.assertEqual((method, url), ("POST", "https://forge.example/v1/runner/network-exits"))
        self.assertEqual(headers["Authorization"], "Bearer pvd_secret")
        body = self.sent()[0]
        self.assertEqual(headers["Idempotency-Key"], body["observationId"])
        self.assertEqual((body["reason"], body["lease"], body["clash"]), ("login", {"leaseId": "lease_1", "leaseFence": 3}, clash))
        self.assertNotIn("7897", json.dumps(body))

    def test_offline_observations_are_resent_with_the_next_and_rejections_dropped(self):
        self.transport.request.side_effect = [OSError("offline"),
                                              ProviderHttpResponse(200, {}, b"{}"), ProviderHttpResponse(200, {}, b"{}")]
        first = self.reporter.report("login", None)
        second = self.reporter.report("periodic", None)
        ids = [body["observationId"] for body in self.sent()]
        self.assertEqual(ids, [first["observationId"], first["observationId"], second["observationId"]])
        self.transport.request.side_effect = [ProviderHttpResponse(404, {}, b'{"error":{"code":"NOT_FOUND"}}'),
                                              ProviderHttpResponse(200, {}, b"{}")]
        self.reporter.report("periodic", None)
        self.reporter.report("periodic", None)
        self.assertEqual(self.reporter._pending, [])

    def test_background_report_never_raises(self):
        self.reporter.observe_exit = Mock(side_effect=RuntimeError("boom"))
        notes = []
        self.reporter.notify = notes.append
        self.reporter._quiet("login", None, None)
        self.assertEqual(notes, ["网络出口上报失败：RuntimeError"])

    def test_monitor_rechecks_with_game_route_until_stopped(self):
        done = threading.Event()
        reporter = Mock()
        reporter._quiet.side_effect = lambda *args: done.set()
        clash = Mock()
        clash.game_route.return_value = {"gameViaNode": True}
        monitor = ExitMonitor(reporter, ("lease_1", 1), clash=clash, first_s=0.01, period_s=60).start()
        self.assertTrue(done.wait(5))
        monitor.stop()
        reporter._quiet.assert_called_with("periodic", ("lease_1", 1), {"gameViaNode": True})

    def test_monitor_quits_once_its_lease_is_no_longer_current(self):
        reporter = Mock()
        monitor = ExitMonitor(reporter, ("lease_1", 1), active=lambda: False, first_s=0.01, period_s=0.01).start()
        monitor._thread.join(5)
        self.assertFalse(monitor._thread.is_alive())
        reporter._quiet.assert_not_called()


if __name__ == "__main__":
    unittest.main()

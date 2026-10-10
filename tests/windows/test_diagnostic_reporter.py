from pathlib import Path
import json
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.diagnostic_reporter import DiagnosticReporter
from apex_automation.runner_identity import RunnerSettings
from apex_automation.ea_evidence import EaLoginEvidence
from apex_automation.diagnostics import PerformanceMetrics


class Transport:
    def __init__(self):
        self.requests = []
        self.error = None
        self.response = None

    def send(self, url, token, payload, timeout):
        self.requests.append(payload)
        if self.error:
            raise self.error
        if self.response:
            return self.response
        return 200, {"schemaVersion": 1, "deviceId": payload["deviceId"],
                     "acceptedEventIds": [event["eventId"] for event in payload["events"]]}, {}


class DiagnosticReporterTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = RunnerSettings(enabled=True, device_id="dev_1",
            report_url="https://runner.example/v1/runner/reports", report_token="private-token")
        self.transport = Transport()
        self.notices = []
        self.worker = self.build()

    def build(self):
        return DiagnosticReporter(self.settings, self.root, transport=self.transport, notify=self.notices.append)

    def ea(self):
        evidence = EaLoginEvidence(self.root / "ea-login", save_screenshots=False)
        evidence.timing("WORKFLOW_PHASE", leaseId="lease_1", previousPhase="CLAIMING", phase="EA_STARTING", durationMs=25)
        evidence.step("signin-start", page="PASSWORD")
        metrics = PerformanceMetrics(evidence.timing)
        metrics.add("eaOcr", 85)
        metrics.flush(force=True, page="PASSWORD")
        return evidence

    def run_dir(self, device_id="dev_1"):
        path = self.root / "20261008-090000-a1234567"
        path.mkdir(exist_ok=True)
        (path / "manifest.json").write_text(json.dumps({"reporting": {"deviceId": device_id, "leaseId": "lease_1", "accountLabel": "sensitive@example.test"}}))
        return path

    def test_pre_run_ea_is_sent_without_game_run(self):
        self.ea()
        self.assertEqual(self.worker.process_once(), 0)
        events = self.transport.requests[0]["events"]
        self.assertEqual({e["type"] for e in events}, {"WORKFLOW_PHASE", "EA_STEP", "EA_PERFORMANCE"})
        self.assertTrue(all(e["leaseId"] == "lease_1" and e["runId"] is None for e in events))
        self.assertTrue(self.worker.url.endswith("/v1/runner/diagnostics"))

    def test_password_recovery_pages_survive_collection_and_upload(self):
        evidence = EaLoginEvidence(self.root / "ea-login", save_screenshots=False)
        pairs = [("password-recovery-account-verified", "RECOVERY_ACCOUNT"),
                 ("password-recovery-password-typed", "RESET_PASSWORD"),
                 ("password-recovery-success", "RESET_SUCCESS")]
        for step, page in pairs:
            evidence.step(step, page=page)
        self.assertEqual(self.worker.process_once(), 0)
        events = self.transport.requests[0]["events"]
        self.assertEqual([(e["payload"]["step"], e["payload"]["page"]) for e in events], pairs)

    def test_offline_restart_replays_same_ids_and_atomic_cursor(self):
        self.ea()
        self.transport.error = OSError("private-token https://private-url/secret")
        self.assertEqual(self.worker.process_once(), 3)
        ids = [e["eventId"] for e in self.transport.requests[-1]["events"]]
        self.assertNotIn("private-token", str(self.notices))
        self.assertNotIn("private-url", str(self.notices))
        self.transport.error = None
        restarted = self.build()
        self.assertEqual(restarted.process_once(), 0)
        self.assertEqual([e["eventId"] for e in self.transport.requests[-1]["events"]], ids)
        restarted.process_once()
        self.assertEqual(len(self.transport.requests), 2)

    def test_server_accepted_but_response_lost_replays_ids(self):
        self.ea()
        self.transport.error = TimeoutError("accepted but response lost")
        self.worker.process_once()
        original = self.transport.requests[-1]
        self.transport.error = None
        self.build().process_once()
        self.assertEqual(self.transport.requests[-1], original)

    def test_invalid_ack_and_auth_failure_do_not_delete_pending(self):
        self.ea()
        self.transport.response = (200, {"schemaVersion": 1, "deviceId": "dev_other", "acceptedEventIds": []}, {})
        self.assertEqual(self.worker.process_once(), 3)
        self.transport.response = (401, {"error": {"code": "INVALID_REPORTER_TOKEN"}}, {})
        restarted = self.build()
        self.assertEqual(restarted.process_once(), 3)
        self.assertEqual(len(self.build().state["pending"]), 3)

    def test_permanent_failure_is_isolated_and_other_records_can_continue(self):
        self.ea()
        self.transport.response = (403, {"error": {"code": "LEASE_NOT_ALLOWED"}}, {})
        self.worker.process_once()
        self.assertEqual(len(self.worker.state["pending"]), 3)
        self.worker._next_send_at = 0
        self.worker.process_once()
        self.assertEqual(len(self.worker.state["pending"]), 2)
        self.assertEqual(len(self.worker.state["quarantine"]), 1)
        self.transport.response = None
        self.worker._next_send_at = 0
        self.worker.process_once()
        self.worker.process_once()
        self.assertEqual(len(self.worker.state["pending"]), 0)

    def test_http_aggregate_preserves_real_request_counts_and_costs(self):
        directory = self.run_dir()
        records = [{"at": "2026-10-08T09:00:01+08:00", "kind": "events", "status": 200,
                    "durationMs": value, "pendingEvents": pending, "eventCount": 5, "imageBytesApprox": 0} for value, pending in [(100, 6), (300, 12)]]
        (directory / "report-timings.jsonl").write_text("".join(json.dumps(r)+"\n" for r in records))
        self.worker.process_once()
        event = self.transport.requests[-1]["events"][0]
        self.assertEqual(event["type"], "HTTP_UPLOAD_SUMMARY")
        self.assertEqual({k: event["payload"][k] for k in ("count", "totalMs", "maxMs", "pendingMax", "eventCount")},
                         {"count": 2, "totalMs": 400, "maxMs": 300, "pendingMax": 12, "eventCount": 10})
        self.assertNotIn("sensitive@", json.dumps(self.transport.requests))

    def test_notification_fields_are_allowlisted_and_other_device_ignored(self):
        directory = self.run_dir()
        event = {"type": "NOTIFICATION_CANDIDATE", "occurredAt": "2026-10-08T09:00:01+08:00",
                 "payload": {"kind": "uu-remote", "recognisedOwner": True, "closeFound": True,
                             "executable": "sensitive@example.test", "rect": [1,2,3,4]}}
        (directory / "events.jsonl").write_text(json.dumps(event)+"\n")
        self.worker.process_once()
        self.assertEqual(self.transport.requests[0]["events"][0]["payload"], {"kind": "uu-remote", "recognisedOwner": True, "closeFound": True})
        self.assertNotIn("sensitive@", json.dumps(self.transport.requests))
        self.run_dir(device_id="dev_other")
        (directory / "events.jsonl").write_text(json.dumps(event)+"\n"+json.dumps({**event,"occurredAt":"2026-10-08T09:00:02+08:00"})+"\n")
        self.worker.process_once()
        self.assertEqual(len(self.transport.requests), 1)

    def test_rotated_daily_log_does_not_replay_and_partial_line_waits(self):
        self.ea()
        self.worker.process_once()
        path = next((self.root / "ea-login" / "timings").glob("*.jsonl"))
        content = path.read_text()
        path.replace(path.with_suffix(".previous.jsonl"))
        record = json.loads(content.splitlines()[0])
        record["at"] = "2026-10-08T09:10:00+08:00"
        encoded = json.dumps(record)
        path.write_text(encoded)
        self.worker.process_once()
        self.assertEqual(len(self.transport.requests), 1)
        with path.open("a") as stream:
            stream.write("\n")
        self.worker.process_once()
        self.assertEqual(len(self.transport.requests[-1]["events"]), 1)

    def test_unknown_step_is_redacted_and_raw_text_is_never_forwarded(self):
        self.ea()
        path = next((self.root / "ea-login" / "timings").glob("*.jsonl"))
        record = {"at":"2026-10-08T09:00:01+08:00", "type":"EA_STEP", "leaseId":None,
                  "step":"secret@example.test", "page":"PASSWORD", "previousStep":None,
                  "previousStepElapsedMs":15, "rawText":"also-secret@example.test"}
        with path.open("a") as stream:
            stream.write(json.dumps(record)+"\n")
        self.worker.process_once()
        self.assertNotIn("secret@", json.dumps(self.transport.requests))
        self.assertEqual(self.transport.requests[-1]["events"][-1]["payload"]["step"], "OTHER")

    def test_corrupt_outbox_is_preserved_and_full_outbox_stops_collecting(self):
        self.worker.path.parent.mkdir(parents=True)
        self.worker.path.write_text("broken json")
        with self.assertRaises(ValueError):
            self.build()
        self.assertEqual(self.worker.path.read_text(), "broken json")
        self.ea()
        self.worker.state["pending"] = [None] * 2000
        self.worker.collect()
        self.assertEqual(self.worker.state["cursors"], {})

    def test_shutdown_collects_last_local_records_without_network(self):
        self.worker._stop.set()
        self.ea()
        self.worker._run()
        self.assertEqual(len(self.build().state["pending"]), 3)
        self.assertEqual(self.transport.requests, [])

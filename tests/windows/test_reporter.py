from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.reporter import RemoteReporter, UrllibReportTransport
from apex_automation.runner_identity import RunnerSettings


class FakeTransport:
    def __init__(self, statuses: list[int] | None = None) -> None:
        self.statuses = list(statuses or [200])
        self.requests: list[dict[str, Any]] = []
        self.tokens: list[str] = []

    def send(self, url, token, payload, timeout_s):
        self.requests.append(payload)
        self.tokens.append(token)
        status = self.statuses.pop(0) if self.statuses else 200
        if status != 200:
            return status, {"error": {"message": f"temporary {status}"}}, {}
        return (
            200,
            {
                "schemaVersion": 1,
                "accountId": payload["accountId"],
                "deviceId": payload["deviceId"],
                "runId": payload["runId"],
                "acceptedThrough": payload["events"][-1]["seq"],
                "serverTime": "2026-07-31T12:00:01.000+08:00",
            },
            {},
        )


class FakeHttpResponse:
    status = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload
        self.headers = {"Content-Type": "application/json"}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class EchoingErrorTransport:
    def send(self, url, token, payload, timeout_s):
        return (
            403,
            {"error": {"message": f"rejected token {token}"}},
            {},
        )


class FixedResponseTransport:
    def __init__(
        self,
        response: dict[str, object] | None = None,
        *,
        status: int = 200,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.response = response
        self.status = status
        self.headers = headers or {}

    def send(self, url, token, payload, timeout_s):
        response = {
            "schemaVersion": 1,
            "accountId": payload["accountId"],
            "deviceId": payload["deviceId"],
            "runId": payload["runId"],
            "acceptedThrough": payload["events"][-1]["seq"],
            "serverTime": "2026-07-31T12:00:01.000+08:00",
        }
        if self.response is not None:
            response.update(self.response)
        return self.status, response, self.headers


class EvidenceAwareTransport:
    def __init__(self, *, evidence_status: int = 200) -> None:
        self.urls: list[str] = []
        self.requests: list[dict[str, Any]] = []
        self.evidence_status = evidence_status

    def send(self, url, token, payload, timeout_s):
        self.urls.append(url)
        self.requests.append(payload)
        if "events" in payload:
            return (
                200,
                {
                    "schemaVersion": 1,
                    "accountId": payload["accountId"],
                    "deviceId": payload["deviceId"],
                    "runId": payload["runId"],
                    "acceptedThrough": payload["events"][-1]["seq"],
                    "serverTime": "2026-07-31T12:00:01.000+08:00",
                },
                {},
            )
        if self.evidence_status != 200:
            return (
                self.evidence_status,
                {"error": {"message": "evidence unavailable"}},
                {},
            )
        return (
            200,
            {
                "schemaVersion": 1,
                "accountId": payload["accountId"],
                "deviceId": payload["deviceId"],
                "runId": payload["runId"],
                "sourceSequence": payload["sourceSequence"],
                "evidenceId": "evi_1",
                "url": "https://images.example/evi_1.jpg",
                "serverTime": "2026-07-31T12:00:01.000+08:00",
            },
            {},
        )


class RejectOldAccountTransport(FakeTransport):
    def send(self, url, token, payload, timeout_s):
        if payload["accountId"] == "acct_old":
            self.requests.append(payload)
            self.tokens.append(token)
            return 403, {"error": {"message": "old account revoked"}}, {}
        return super().send(url, token, payload, timeout_s)


def write_run(
    root: Path,
    name: str,
    *,
    account_id: str,
    device_id: str = "dev_1",
    events: list[tuple[str, dict[str, object]]] | None = None,
) -> Path:
    run_dir = root / name
    run_dir.mkdir()
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runId": name,
                "profile": "play-test",
                "client": {
                    "appVersion": "test",
                    "profile": "play-test",
                    "configRevision": "sha256:test",
                },
                "reporting": {
                    "enabled": True,
                    "accountId": account_id,
                    "deviceId": device_id,
                    "reportUrl": "https://runner.example/reports",
                    "identityVerification": {
                        "status": "VERIFIED",
                        "observedPlatform": "steam",
                        "observedPlatformAccountId": "76561198000000000",
                        "message": None,
                    },
                },
            }
        ),
        encoding="utf-8",
    )
    occurred_at = "2026-07-31T12:00:00.000+08:00"
    records = []
    for seq, (event_type, payload) in enumerate(events or [], start=1):
        records.append(
            {
                "schemaVersion": 1,
                "runId": name,
                "seq": seq,
                "occurredAt": occurred_at,
                "elapsedMs": seq * 100,
                "type": event_type,
                "payload": payload,
            }
        )
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records),
        encoding="utf-8",
    )
    return run_dir


def settings(account_id: str = "acct_current") -> RunnerSettings:
    return RunnerSettings(
        enabled=True,
        account_id=account_id,
        device_id="dev_1",
        report_url="https://runner.example/reports",
        report_token="private-token",
    )


class ReporterTest(unittest.TestCase):
    def test_stdlib_http_transport_sends_bearer_json_without_logging_token(
        self,
    ) -> None:
        response = FakeHttpResponse({"acceptedThrough": 1})
        with patch("apex_automation.reporter.urlopen", return_value=response) as send:
            status, payload, headers = UrllibReportTransport().send(
                "https://runner.example/v1/runner/reports",
                "private-token",
                {"schemaVersion": 1, "events": [{"seq": 1}]},
                3.0,
            )

        request = send.call_args.args[0]
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"acceptedThrough": 1})
        self.assertEqual(headers["content-type"], "application/json")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(
            json.loads(request.data), {"schemaVersion": 1, "events": [{"seq": 1}]}
        )
        self.assertEqual(send.call_args.kwargs["timeout"], 3.0)

    def test_projects_filtered_local_events_to_a_contiguous_remote_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_current",
                account_id="acct_current",
                events=[
                    ("RUN_STARTED", {"mode": "play"}),
                    (
                        "STATE_DETECTED",
                        {
                            "state": "LOBBY_READY_TARGET",
                            "previousState": None,
                            "source": "gameStates",
                            "ruleId": "lobby-ready-target",
                            "confidence": 0.99,
                            "observationVersion": 1,
                        },
                    ),
                    (
                        "ACTION_SENT",
                        {
                            "capability": "in-match-melee",
                            "action": "meleeScanCode",
                            "state": "IN_MATCH_ALIVE",
                        },
                    ),
                    (
                        "LOBBY_PROGRESS",
                        {
                            "reason": "INITIAL",
                            "level": 9,
                            "xpCurrentApprox": 3430,
                            "xpRequiredApprox": 8150,
                            "rawText": "3.43K/8.15K",
                            "confidence": 0.98,
                            "changed": False,
                            "deltaApprox": None,
                            "readStatus": "OK",
                        },
                    ),
                    (
                        "SCREENSHOT_SAVED",
                        {
                            "stage": "unknown",
                            "path": "screenshots/009-unknown.png",
                        },
                    ),
                    ("RUN_FINISHED", {"status": "STOPPED"}),
                ],
            )
            transport = FakeTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            outcome = reporter.process_once()

            self.assertEqual(outcome.pending, 0)
            self.assertEqual(len(transport.requests), 1)
            remote_events = transport.requests[0]["events"]
            self.assertEqual(
                [event["type"] for event in remote_events],
                [
                    "RUN_STARTED",
                    "STATE_CHANGED",
                    "MATCH_PHASE_CHANGED",
                    "LOBBY_PROGRESS",
                    "INCIDENT",
                    "RUN_FINISHED",
                ],
            )
            self.assertEqual(
                [event["seq"] for event in remote_events],
                [1, 2, 3, 4, 5, 6],
            )
            self.assertEqual(
                remote_events[0]["payload"]["identityVerification"]["status"],
                "VERIFIED",
            )
            self.assertEqual(
                remote_events[-2]["payload"]["localEvidencePath"],
                "screenshots/009-unknown.png",
            )
            state = json.loads(
                (run_dir / "report-state.json").read_text(encoding="utf-8")
            )
            self.assertEqual(state["sourceThrough"], 6)
            self.assertEqual(state["acceptedThrough"], 6)
            self.assertNotIn(
                "private-token", (run_dir / "report-outbox.jsonl").read_text()
            )

    def test_uploads_compact_evidence_after_report_and_removes_upload_copy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_evidence",
                account_id="acct_current",
                events=[
                    ("RUN_STARTED", {"mode": "play"}),
                    (
                        "EVIDENCE_SAVED",
                        {
                            "stage": "live",
                            "category": "live",
                            "path": "evidence/0001-live.jpg",
                            "width": 960,
                            "height": 540,
                        },
                    ),
                ],
            )
            evidence_dir = run_dir / "evidence"
            evidence_dir.mkdir()
            image_path = evidence_dir / "0001-live.jpg"
            image_path.write_bytes(b"\xff\xd8\xffsmall-jpeg\xff\xd9")
            transport = EvidenceAwareTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            outcome = reporter.process_once()

            self.assertEqual(outcome.pending, 0)
            self.assertEqual(
                transport.urls,
                [
                    "https://runner.example/reports",
                    "https://runner.example/evidence",
                ],
            )
            evidence_request = transport.requests[1]
            self.assertEqual(evidence_request["sourceSequence"], 2)
            self.assertEqual(evidence_request["category"], "live")
            self.assertFalse(image_path.exists())
            state = json.loads((run_dir / "report-state.json").read_text())
            self.assertEqual(state["evidenceSourceThrough"], 2)

    def test_evidence_failure_does_not_back_off_normal_reports(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_evidence_retry",
                account_id="acct_current",
                events=[
                    ("RUN_STARTED", {"mode": "play"}),
                    (
                        "EVIDENCE_SAVED",
                        {
                            "stage": "live",
                            "category": "live",
                            "path": "evidence/0001-live.jpg",
                            "width": 960,
                            "height": 540,
                        },
                    ),
                ],
            )
            evidence_dir = run_dir / "evidence"
            evidence_dir.mkdir()
            (evidence_dir / "0001-live.jpg").write_bytes(
                b"\xff\xd8\xffsmall-jpeg\xff\xd9"
            )
            transport = EvidenceAwareTransport(evidence_status=503)
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            first = reporter.process_once()
            with (run_dir / "events.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "schemaVersion": 1,
                            "runId": "run_evidence_retry",
                            "seq": 3,
                            "occurredAt": "2026-07-31T12:00:03.000+08:00",
                            "elapsedMs": 300,
                            "type": "STATE_DETECTED",
                            "payload": {
                                "state": "IN_MATCH_ALIVE",
                                "previousState": None,
                                "source": "gameStates",
                                "confidence": 0.99,
                                "observationVersion": 1,
                            },
                        }
                    )
                    + "\n"
                )
            second = reporter.process_once()

            self.assertIsNone(first.error)
            self.assertGreater(first.pending, 0)
            self.assertGreater(second.sent, 0)
            self.assertEqual(transport.urls[-1], "https://runner.example/reports")
            self.assertEqual(reporter._next_send_at, 0.0)

    def test_managed_run_binds_the_lease_fence_to_each_report_batch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_managed",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "account-cycle"})],
            )
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["reporting"].update(
                {"leaseId": "lease_1", "leaseFence": 17}
            )
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            transport = FakeTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            reporter.process_once()

            self.assertEqual(
                transport.requests[0]["lease"],
                {"leaseId": "lease_1", "leaseFence": 17},
            )

    def test_ring_progress_is_structured_and_bound_to_the_current_round(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_ring_progress",
                account_id="acct_current",
                events=[
                    (
                        "STATE_DETECTED",
                        {
                            "state": "DROPSHIP_SOLO_JUMPMASTER",
                            "previousState": "LOBBY_QUEUEING",
                            "confidence": 0.98,
                        },
                    ),
                    (
                        "RING_PROGRESS",
                        {
                            "completed": 12,
                            "required": 30,
                            "rawText": "经历缩圈 12/30",
                            "confidence": 0.94,
                        },
                    ),
                ],
            )
            transport = FakeTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            reporter.process_once()

            event = next(
                item
                for item in transport.requests[0]["events"]
                if item["type"] == "RING_PROGRESS"
            )
            self.assertEqual(
                event["payload"],
                {
                    "completed": 12,
                    "required": 30,
                    "rawText": "经历缩圈 12/30",
                    "confidence": 0.94,
                    "roundNumber": 1,
                },
            )

    def test_a_retry_reuses_the_same_sequence_and_does_not_duplicate_outbox(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_retry",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            transport = FakeTransport([503, 200])
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            first = reporter.process_once()
            reporter._next_send_at = 0.0
            second = reporter.process_once()

            self.assertEqual(first.pending, 1)
            self.assertEqual(second.pending, 0)
            self.assertEqual(
                transport.requests[0]["events"],
                transport.requests[1]["events"],
            )
            outbox = (run_dir / "report-outbox.jsonl").read_text().splitlines()
            self.assertEqual(len(outbox), 1)

    def test_success_response_must_match_the_openapi_contract_before_ack(self) -> None:
        invalid_responses = (
            {"acceptedThrough": True},
            {"schemaVersion": True},
            {"serverTime": ""},
            {"serverTime": "2026-07-31T12:00:01"},
        )
        for index, invalid_response in enumerate(invalid_responses):
            with self.subTest(response=invalid_response):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    run_dir = write_run(
                        root,
                        f"run_invalid_{index}",
                        account_id="acct_current",
                        events=[("RUN_STARTED", {"mode": "play"})],
                    )
                    reporter = RemoteReporter(
                        settings(),
                        root,
                        run_dir,
                        transport=FixedResponseTransport(invalid_response),
                        heartbeat_interval_s=9999,
                    )

                    outcome = reporter.process_once()

                    self.assertEqual(outcome.pending, 1)
                    state = json.loads(
                        (run_dir / "report-state.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(state.get("acceptedThrough", 0), 0)
                    self.assertIn("无效上报响应", state["lastError"])

    def test_legacy_manifest_client_fallback_still_matches_request_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_legacy",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest.pop("client")
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            transport = FakeTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            reporter.process_once()

            self.assertEqual(
                transport.requests[0]["client"],
                {
                    "appVersion": "unknown",
                    "profile": "play-test",
                    "configRevision": "unknown",
                },
            )

    def test_partial_legacy_client_is_normalized_to_the_request_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_partial_client",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            manifest_path = run_dir / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["client"] = {
                "appVersion": "legacy",
                "profile": "",
                "unexpected": "must-not-be-sent",
            }
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            transport = FakeTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            reporter.process_once()

            self.assertEqual(
                transport.requests[0]["client"],
                {
                    "appVersion": "legacy",
                    "profile": "play-test",
                    "configRevision": "unknown",
                },
            )

    def test_retry_after_zero_does_not_fall_back_to_local_delay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_rate_limited",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=FixedResponseTransport(
                    status=429,
                    headers={"retry-after": "0"},
                ),
                heartbeat_interval_s=9999,
            )

            outcome = reporter.process_once()

            self.assertEqual(outcome.retry_after_s, 0.0)
            self.assertLessEqual(reporter._next_send_at, reporter._last_heartbeat + 1.0)

    def test_server_errors_cannot_echo_the_private_token_to_disk_or_console(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            run_dir = write_run(
                root,
                "run_error",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            notices: list[str] = []
            reporter = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=EchoingErrorTransport(),
                notify=notices.append,
                heartbeat_interval_s=9999,
            )

            outcome = reporter.process_once()
            state_text = (run_dir / "report-state.json").read_text()

            self.assertTrue(outcome.terminal)
            self.assertNotIn("private-token", state_text)
            self.assertNotIn("private-token", "\n".join(notices))
            self.assertIn("[REDACTED]", state_text)

            restarted = RemoteReporter(
                settings(),
                root,
                run_dir,
                transport=FakeTransport(),
                heartbeat_interval_s=9999,
            )
            retry = restarted.process_once()
            self.assertEqual(retry.pending, 0)

    def test_historical_runs_keep_their_original_account_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = write_run(
                root,
                "run_001",
                account_id="acct_old",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            current = write_run(
                root,
                "run_002",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            transport = FakeTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                current,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            outcome = reporter.process_once()

            self.assertEqual(outcome.pending, 0)
            self.assertEqual(
                [request["accountId"] for request in transport.requests],
                ["acct_old", "acct_current"],
            )
            self.assertTrue((old / "report-state.json").exists())
            self.assertEqual(transport.tokens, ["private-token", "private-token"])

    def test_a_corrupt_historical_session_cannot_block_the_current_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            corrupt = write_run(
                root,
                "run_001",
                account_id="acct_old",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            (corrupt / "report-outbox.jsonl").write_text(
                json.dumps(
                    {
                        "seq": 1,
                        "sourceSeq": 1,
                        "occurredAt": "2026-07-31T12:00:00.000+08:00",
                        "elapsedMs": 100,
                        "type": "MATCH_PHASE_CHANGED",
                        "payload": {
                            "phase": "IN_MATCH",
                            "roundNumber": "not-an-integer",
                        },
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            current = write_run(
                root,
                "run_002",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            notices: list[str] = []
            transport = FakeTransport()

            reporter = RemoteReporter(
                settings(),
                root,
                current,
                transport=transport,
                notify=notices.append,
                heartbeat_interval_s=9999,
            )
            outcome = reporter.process_once()

            self.assertEqual(outcome.pending, 0)
            self.assertEqual(
                [request["accountId"] for request in transport.requests],
                ["acct_current"],
            )
            self.assertTrue(any("run_001" in notice for notice in notices))

    def test_a_revoked_historical_account_does_not_block_the_current_account(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            old = write_run(
                root,
                "run_001",
                account_id="acct_old",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            current = write_run(
                root,
                "run_002",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            transport = RejectOldAccountTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                current,
                transport=transport,
                heartbeat_interval_s=9999,
            )

            outcome = reporter.process_once()

            self.assertTrue(outcome.terminal)
            self.assertEqual(
                [request["accountId"] for request in transport.requests],
                ["acct_old", "acct_current"],
            )
            old_state = json.loads((old / "report-state.json").read_text())
            current_state = json.loads((current / "report-state.json").read_text())
            self.assertEqual(old_state["terminalError"], "old account revoked")
            self.assertEqual(current_state["acceptedThrough"], 1)

    def test_heartbeat_uses_runtime_elapsed_time_and_never_follows_finish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            running = write_run(
                root,
                "run_001",
                account_id="acct_current",
                events=[("RUN_STARTED", {"mode": "play"})],
            )
            (running / "status.json").write_text(
                json.dumps(
                    {
                        "runtimeState": "RUNNING",
                        "elapsedMs": 32100,
                        "frames": 99,
                        "actionsSent": 4,
                    }
                ),
                encoding="utf-8",
            )
            transport = FakeTransport()
            reporter = RemoteReporter(
                settings(),
                root,
                running,
                transport=transport,
                heartbeat_interval_s=0,
            )

            reporter.process_once(allow_send=False)
            outbox = [
                json.loads(line)
                for line in (running / "report-outbox.jsonl").read_text().splitlines()
            ]
            self.assertEqual(outbox[-1]["type"], "HEARTBEAT")
            self.assertEqual(outbox[-1]["elapsedMs"], 32100)

            finished = write_run(
                root,
                "run_002",
                account_id="acct_current",
                events=[
                    ("RUN_STARTED", {"mode": "play"}),
                    ("RUN_FINISHED", {"status": "PLAYED"}),
                ],
            )
            finished_reporter = RemoteReporter(
                settings(),
                root,
                finished,
                transport=FakeTransport(),
                heartbeat_interval_s=0,
            )
            finished_reporter.process_once(allow_send=False)
            finished_types = [
                json.loads(line)["type"]
                for line in (finished / "report-outbox.jsonl").read_text().splitlines()
            ]
            self.assertEqual(finished_types[-1], "RUN_FINISHED")
            with patch.object(
                finished_reporter.current_session,
                "ingest",
            ) as ingest_finished:
                finished_reporter.process_once(allow_send=False)
            ingest_finished.assert_not_called()


if __name__ == "__main__":
    unittest.main()

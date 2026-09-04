from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.recorder import RunRecorder


def read_events(recorder: RunRecorder) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in recorder.events_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class RunRecorderTest(unittest.TestCase):
    def test_creates_unique_run_ids_and_a_token_free_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.dict(os.environ, {"APEX_REPORT_TOKEN": "must-not-leak"}):
                first = RunRecorder(root, "play-profile")
                second = RunRecorder(root, "play-profile")

            self.assertNotEqual(first.run_id, second.run_id)
            self.assertRegex(
                first.run_id,
                r"^\d{8}-\d{6}-[0-9a-f]{8}$",
            )
            manifest_text = first.manifest_path.read_text(encoding="utf-8")
            manifest = json.loads(manifest_text)
            self.assertEqual(manifest["runId"], first.run_id)
            self.assertEqual(manifest["profile"], "play-profile")
            self.assertNotIn("must-not-leak", manifest_text)
            self.assertNotIn("token", manifest_text.lower())

    def test_initial_manifest_and_start_payload_are_persisted_before_reporting(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(
                Path(directory),
                "play-profile",
                manifest={
                    "client": {"appVersion": "test"},
                    "reporting": {
                        "enabled": True,
                        "accountId": "acct_1",
                        "deviceId": "dev_1",
                    },
                },
                start_payload={"mode": "play", "resolution": [2560, 1440]},
            )

            manifest = json.loads(recorder.manifest_path.read_text(encoding="utf-8"))
            start = read_events(recorder)[0]
            self.assertEqual(manifest["reporting"]["accountId"], "acct_1")
            self.assertEqual(manifest["runId"], recorder.run_id)
            self.assertEqual(start["payload"]["mode"], "play")
            self.assertEqual(start["payload"]["resolution"], [2560, 1440])

    def test_manifest_rejects_secret_fields_and_immutable_updates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "敏感字段"):
                RunRecorder(
                    root,
                    "play-profile",
                    manifest={"reporting": {"reportToken": "must-not-write"}},
                )

            recorder = RunRecorder(root, "play-profile")
            with self.assertRaisesRegex(ValueError, "不可修改"):
                recorder.update_manifest({"runId": "different"})

    def test_uses_one_authoritative_event_stream_with_a_stable_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "play-profile")
            recorder.log(
                "CUSTOM",
                value=7,
                schemaVersion=999,
                seq=-1,
                type="PAYLOAD_VALUE",
            )

            events = read_events(recorder)
            self.assertEqual(recorder.actions_path, recorder.events_path)
            self.assertFalse((recorder.run_dir / "actions.jsonl").exists())
            self.assertEqual([event["seq"] for event in events], [1, 2])
            self.assertEqual(
                set(events[1]),
                {
                    "schemaVersion",
                    "runId",
                    "seq",
                    "occurredAt",
                    "elapsedMs",
                    "type",
                    "payload",
                },
            )
            self.assertEqual(events[1]["schemaVersion"], 1)
            self.assertEqual(events[1]["runId"], recorder.run_id)
            self.assertEqual(events[1]["type"], "CUSTOM")
            self.assertEqual(
                events[1]["payload"],
                {
                    "value": 7,
                    "schemaVersion": 999,
                    "seq": -1,
                    "type": "PAYLOAD_VALUE",
                },
            )

    def test_concurrent_logging_produces_contiguous_sequences(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "threaded")
            thread_count = 8
            events_per_thread = 50

            def write_events(thread_index: int) -> None:
                for event_index in range(events_per_thread):
                    recorder.log(
                        "WORK",
                        thread=thread_index,
                        index=event_index,
                    )

            threads = [
                threading.Thread(target=write_events, args=(index,))
                for index in range(thread_count)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            recorder.finish("DONE")

            events = read_events(recorder)
            expected_count = 2 + thread_count * events_per_thread
            self.assertEqual(len(events), expected_count)
            self.assertEqual(
                [event["seq"] for event in events],
                list(range(1, expected_count + 1)),
            )
            self.assertEqual(events[0]["type"], "RUN_STARTED")
            self.assertEqual(events[-1]["type"], "RUN_FINISHED")

    def test_lifecycle_events_and_each_screenshot_are_recorded_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "play-profile")

            recorder.log("RUN_STARTED", capabilitySet="ignored-duplicate")
            with patch(
                "apex_automation.recorder._save_frame",
                side_effect=lambda path, frame: path.write_bytes(b"png"),
            ):
                first = recorder.screenshot("LOBBY", object())
                recorder.log("SCREENSHOT_SAVED", stage="LOBBY", path=str(first))
                second = recorder.screenshot("MATCH", object())
                recorder.log(
                    "SCREENSHOT_SAVED",
                    stage="MATCH",
                    path=str(second.relative_to(recorder.run_dir)),
                )

            recorder.finish("STOPPED", reason="test")
            recorder.finish("FAILED", reason="duplicate")
            recorder.log("RUN_FINISHED", status="duplicate")
            recorder.log("AFTER_FINISH")

            events = read_events(recorder)
            types = [event["type"] for event in events]
            self.assertEqual(types.count("RUN_STARTED"), 1)
            self.assertEqual(types.count("RUN_FINISHED"), 1)
            self.assertEqual(types.count("SCREENSHOT_SAVED"), 2)
            self.assertNotIn("AFTER_FINISH", types)
            screenshot_payloads = [
                event["payload"]
                for event in events
                if event["type"] == "SCREENSHOT_SAVED"
            ]
            self.assertEqual(
                [payload["path"] for payload in screenshot_payloads],
                [
                    "screenshots/001-lobby.png",
                    "screenshots/002-match.png",
                ],
            )

    def test_compact_evidence_records_upload_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "play-profile")

            def save_jpeg(path, frame):
                path.write_bytes(b"\xff\xd8\xffsmall-jpeg\xff\xd9")
                return 960, 540

            with patch(
                "apex_automation.recorder._save_evidence_frame",
                side_effect=save_jpeg,
            ):
                path = recorder.evidence("live", object(), category="live")

            event = read_events(recorder)[-1]
            self.assertEqual(event["type"], "EVIDENCE_SAVED")
            self.assertEqual(event["payload"]["category"], "live")
            self.assertEqual(event["payload"]["width"], 960)
            self.assertEqual(event["payload"]["height"], 540)
            self.assertEqual(event["payload"]["sizeBytes"], path.stat().st_size)
            self.assertEqual(len(event["payload"]["sha256"]), 64)

    def test_concurrent_finish_writes_one_result_and_one_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "play-profile")
            threads = [
                threading.Thread(
                    target=recorder.finish,
                    args=(f"STATUS_{index}",),
                    kwargs={"worker": index},
                )
                for index in range(12)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            events = read_events(recorder)
            finishes = [event for event in events if event["type"] == "RUN_FINISHED"]
            result = json.loads(recorder.result_path.read_text(encoding="utf-8"))
            self.assertEqual(len(finishes), 1)
            self.assertEqual(finishes[0]["payload"]["status"], result["status"])
            self.assertEqual(finishes[0]["payload"]["worker"], result["worker"])

    def test_status_and_result_are_atomic_and_include_run_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "play-profile")
            recorder.write_status({"runtimeState": "RUNNING"})
            recorder.finish("STOPPED", reason="test")

            status = json.loads(recorder.status_path.read_text(encoding="utf-8"))
            current = json.loads(
                recorder.current_status_path.read_text(encoding="utf-8")
            )
            result = json.loads(recorder.result_path.read_text(encoding="utf-8"))
            self.assertEqual(status, current)
            self.assertEqual(status["runId"], recorder.run_id)
            self.assertEqual(status["runtimeState"], "RUNNING")
            self.assertEqual(result["runId"], recorder.run_id)
            self.assertEqual(result["status"], "STOPPED")
            self.assertEqual(result["reason"], "test")
            self.assertEqual(
                list(recorder.run_dir.glob(".*.tmp")),
                [],
            )


if __name__ == "__main__":
    unittest.main()

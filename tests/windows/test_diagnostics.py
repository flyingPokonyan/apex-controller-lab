from pathlib import Path
from datetime import datetime
import sys
import json
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.diagnostics import PerformanceMetrics
from apex_automation.ea_evidence import EaLoginEvidence


class DiagnosticsTest(unittest.TestCase):
    def test_same_second_directory_suffix_does_not_exceed_retention(self):
        with tempfile.TemporaryDirectory() as directory, \
                patch("apex_automation.ea_evidence.datetime") as clock, \
                patch("apex_automation.ea_evidence.secrets.token_hex", return_value="00000000"):
            clock.now.return_value = datetime(2026, 10, 7, 22, 30, 0)
            root = Path(directory)
            evidence = EaLoginEvidence(root, keep_attempts=1, save_screenshots=False)
            (root / (evidence.dir.name + "-ffffffff")).mkdir()
            timings = root / "timings"
            timings.mkdir()
            daily_log = timings / "2026-10-07.jsonl"
            daily_log.write_text("retained timing record\n")

            current = evidence.rotate()

            self.assertEqual([p for p in root.iterdir() if p.name != "timings"], [current])
            self.assertTrue(current.is_dir())
            self.assertEqual(daily_log.read_text(), "retained timing record\n")

    def test_metrics_aggregate_without_writing_each_frame(self):
        now = [0.0]
        events = []
        metrics = PerformanceMetrics(lambda event, **p: events.append((event,p)), clock=lambda: now[0])
        for i in range(1000):
            metrics.add("ocr", i)
            metrics.flush()
        self.assertEqual(events, [])
        self.assertEqual(len(metrics.samples["ocr"]), 256)
        now[0] = 30
        metrics.flush(observedState="LOBBY_READY")
        sample = events[0][1]["metrics"]["ocr"]
        self.assertEqual(sample["count"], 1000)
        self.assertEqual(sample["meanMs"], 499.5)
        self.assertEqual(sample["maxMs"], 999)
        self.assertEqual(metrics.totals, {})

    def test_diagnostic_write_failure_does_not_fail_the_session(self):
        def fail(*a, **k):
            raise OSError("disk full")
        metrics = PerformanceMetrics(fail)
        metrics.add("capture", 10)
        metrics.flush(force=True)

    def test_ea_timing_is_bound_to_public_lease_and_survives_attempt_pruning(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = EaLoginEvidence(root, keep_attempts=1, save_screenshots=False)
            evidence.timing("WORKFLOW_PHASE", leaseId="lease_123456789abcdef", phase="EA_STARTING", durationMs=123)
            evidence.step("login-started", page="LOGIN")
            for _ in range(3):
                evidence.rotate()
            logs = list((root / "timings").glob("*.jsonl"))
            records = [json.loads(line) for line in logs[0].read_text().splitlines()]
            self.assertEqual(records[1]["leaseId"], "lease_123456789abcdef")
            self.assertIn("previousStepElapsedMs", records[1])
            self.assertEqual(len([p for p in root.iterdir() if p.name != "timings"]), 1)
            with self.assertRaises(ValueError):
                evidence.timing("EA_STEP", rawText="private@example.com")

from pathlib import Path
import sys
import json
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.diagnostics import PerformanceMetrics
from apex_automation.ea_evidence import EaLoginEvidence


class DiagnosticsTest(unittest.TestCase):
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

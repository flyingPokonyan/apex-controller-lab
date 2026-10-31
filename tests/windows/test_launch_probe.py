"""The launch-stall probe reports only fixed labels and numbers, and never fails a launch."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation import launch_probe
from apex_automation.diagnostic_schema import validate_event

NOW = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone.utc)


def completed(stdout="", code=0):
    return Mock(returncode=code, stdout=stdout)


class LaunchProbeTest(unittest.TestCase):
    def test_processes_are_counted_by_label_with_oldest_age_and_total_memory(self):
        rows = [
            {"n": "r5apex_dx12.exe", "s": "2026-10-31T11:50:00.0000000Z", "m": 2 * 2**30},
            {"n": "R5Apex.exe", "s": "2026-10-31T11:59:00Z", "m": 2**20},
            {"n": "eaanticheat.gameservicelauncher.exe", "s": "2026-10-31T11:59:30Z", "m": 10 * 2**20},
            {"n": "chrome.exe", "s": "2026-10-31T10:00:00Z", "m": 2**30},
        ]
        summary = launch_probe.summarize_processes(rows, NOW)
        self.assertEqual(summary["game"], {"count": 2, "ageS": 600.0, "memMb": 2049.0})
        self.assertEqual(summary["antiCheatLauncher"], {"count": 1, "ageS": 30.0, "memMb": 10.0})
        self.assertEqual(summary["eaDesktop"], {"count": 0, "ageS": None, "memMb": None})
        self.assertEqual(set(summary), set(launch_probe.LAUNCH_PROCESSES))

    def test_crash_records_are_counted_per_program_family_only(self):
        counts = launch_probe.summarize_crashes(
            ["r5apex.exe", "R5Apex_dx12.exe", "EAAntiCheat.GameService.exe", "EADesktop.exe", "chrome.exe", ""])
        self.assertEqual(counts, {"game": 2, "antiCheat": 1, "eaDesktop": 1})

    def test_probe_reads_single_object_json_and_passes_the_allowlist(self):
        process = json.dumps({"n": "eadesktop.exe", "s": "2026-10-31T11:00:00Z", "m": 300 * 2**20})
        run = Mock(side_effect=[completed(process), completed("")])
        facts = launch_probe.probe(run=run, now=lambda: NOW)
        self.assertTrue(facts["probeOk"])
        self.assertEqual(facts["processes"]["eaDesktop"]["count"], 1)
        self.assertEqual(facts["crashes"], {"game": 0, "antiCheat": 0, "eaDesktop": 0})
        event = {"eventId": "a" * 64, "type": "APEX_LAUNCH_STALL", "occurredAt": "2026-10-31T12:00:00Z",
                 "leaseId": "lease_1", "runId": None, "payload": {"relaunched": True, "waitedMs": 180000, **facts}}
        validate_event(event)

    def test_a_failing_query_marks_the_probe_and_still_returns_every_label(self):
        run = Mock(side_effect=[completed("", code=1), OSError("no powershell")])
        facts = launch_probe.probe(run=run, now=lambda: NOW)
        self.assertFalse(facts["probeOk"])
        self.assertEqual(facts["processes"]["game"]["count"], 0)
        self.assertEqual(set(facts["crashes"]), set(launch_probe.LAUNCH_CRASHES))

    def test_the_allowlist_refuses_text_and_unknown_labels(self):
        facts = launch_probe.probe(run=Mock(return_value=completed("")), now=lambda: NOW)
        base = {"eventId": "b" * 64, "type": "APEX_LAUNCH_STALL", "occurredAt": "2026-10-31T12:00:00Z",
                "leaseId": None, "runId": None}
        bad = [
            {"relaunched": False, "waitedMs": 1, **facts, "message": "EA said no"},
            {"relaunched": False, "waitedMs": 1, **facts,
             "processes": {**facts["processes"], "chrome": {"count": 1, "ageS": None, "memMb": None}}},
            {"relaunched": False, "waitedMs": 1, **facts,
             "crashes": {**facts["crashes"], "game": "r5apex.exe"}},
        ]
        for payload in bad:
            with self.subTest(payload=sorted(payload)):
                with self.assertRaises(ValueError):
                    validate_event({**base, "payload": payload})


if __name__ == "__main__":
    unittest.main()

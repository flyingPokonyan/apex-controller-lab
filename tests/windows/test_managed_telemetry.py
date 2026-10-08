import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from managed_launcher import write_json
from managed_telemetry import VersionPublisher, snapshot


class VersionPublisherTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.state_root = self.root / "windows/runs/managed"
        self.config = self.root / "windows/account-cycle.private.json"
        write_json(self.config, {"reportUrl": "https://forge.example/v1/runner/reports", "reportToken": "test-token", "deviceId": "dev_1"})
        write_json(self.state_root / "launcher.json", {"stage": "WAITING_FIX", "running": "a" * 40, "installed": "a" * 40, "target": "b" * 40})
        self.send = Mock()
        self.publisher = VersionPublisher(self.root, self.state_root, write_json=write_json, send=self.send)

    def test_failed_worker_can_report_without_importing_game_or_having_worker(self):
        self.publisher.process_once()
        url, token, payload = self.send.call_args.args
        self.assertEqual(url, "https://forge.example/v1/runner/version-status")
        self.assertEqual(payload["stage"], "WAITING_FIX")
        self.assertIsNone(payload["runningCommit"])
        self.assertFalse(payload["workerResponding"])
        self.assertNotIn(token, (self.state_root / "version-report.json").read_text())

    def test_network_failure_retries_latest_snapshot_without_blocking_launcher(self):
        self.send.side_effect = [OSError("sensitive URL"), None]
        self.publisher.process_once()
        self.assertNotIn("sensitive", (self.state_root / "version-report.json").read_text())
        write_json(self.state_root / "launcher.json", {"stage": "INSTALLING", "target": "c" * 40})
        self.publisher.next_send = 0
        self.publisher.process_once()
        self.assertEqual(self.send.call_args.args[2]["targetCommit"], "c" * 40)
        self.assertFalse(self.publisher.failed)

    def test_new_launcher_generation_is_later_even_if_clock_moves_back(self):
        self.publisher.process_once()
        with patch("managed_telemetry.time.time_ns", return_value=0):
            newer = VersionPublisher(self.root, self.state_root, write_json=write_json, send=self.send)
        self.assertGreater(newer.generation, self.publisher.generation)

    def test_downloaded_or_spawned_version_is_not_claimed_as_running(self):
        data = snapshot({"installed": "a" * 40, "running": "a" * 40, "pid": 123}, device_id="dev", generation=1, sequence=1, now=10)
        self.assertIsNone(data["runningCommit"])
        self.assertFalse(data["workerResponding"])

    def test_stale_heartbeat_does_not_claim_healthy_worker(self):
        data = snapshot({"running": "a" * 40, "pid": 123, "workerConfirmedAt": 1, "workerHeartbeatAt": 1}, device_id="dev", generation=1, sequence=1, now=500)
        self.assertEqual(data["runningCommit"], "a" * 40)
        self.assertFalse(data["workerResponding"])

    def test_no_cross_origin_redirect_or_insecure_configuration(self):
        write_json(self.config, {"reportUrl": "http://remote.example/reports", "reportToken": "test", "deviceId": "dev"})
        self.publisher.process_once()
        self.send.assert_not_called()
        self.assertTrue(self.publisher.failed)

    def test_unchanged_status_is_not_sent_every_second(self):
        self.publisher.process_once()
        self.publisher.process_once()
        self.assertEqual(self.send.call_count, 1)

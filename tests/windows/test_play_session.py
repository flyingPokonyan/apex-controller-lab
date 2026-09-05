from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capabilities import CapabilitySet
from apex_automation.config import RunnerConfig
from apex_automation.play_session import PlaySessionRunner, SessionIdentity
from apex_automation.progression_policy import ContinuePlayPolicy
from apex_automation.runner_identity import IdentityVerification, RunnerSettings


class FakeSender:
    def __init__(self) -> None:
        self.releases = 0

    def click(self, x: int, y: int) -> None:
        pass

    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None:
        pass

    def release_all(self) -> None:
        self.releases += 1


class FakeGuard:
    def ensure_not_aborted(self) -> None:
        pass

    def ensure_target_foreground(self) -> None:
        pass

    def target_is_foreground(self) -> bool:
        return True


class FakePilot:
    dispatchers: list[object] = []
    sources: list[object] = []
    outcome = "COMPLETED"

    def __init__(self, config, source, sender, guard, recorder, **kwargs) -> None:
        self.recorder = recorder
        self.dispatchers.append(kwargs["dispatcher"])
        self.sources.append(source)
        self.frames = 5
        self.actions_sent = 2
        self.rounds_started = 1
        self.rounds_returned_to_lobby = 1
        self.observed_state = "LOBBY_READY_TARGET"
        self.target_reading = None
        self.started = 0.0
        self.monotonic = lambda: 1.0

    def run(self, duration_s=None) -> str:
        return self.outcome

    def write_summary(self) -> Path:
        path = self.recorder.run_dir / "pilot-summary.json"
        path.write_text("{}\n", encoding="utf-8")
        return path


def config(path: Path) -> RunnerConfig:
    return RunnerConfig(
        path=path,
        profile="test",
        environment={"width": 2560, "height": 1440, "language": "zh-CN"},
        timing={},
        actions={},
        states={},
        offline_cases=(),
        guided_offline_cases=(),
        ocr={},
        guided_ocr={},
        overlay_ocr={},
        task={},
        control_api={},
        game_states={},
        lobby_progress={"enabled": False},
        capability_set="capabilities.json",
    )


class PlaySessionRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        FakePilot.dispatchers.clear()
        FakePilot.sources.clear()
        FakePilot.outcome = "COMPLETED"
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.sender = FakeSender()
        self.verification = IdentityVerification(status="VERIFIED")
        self.settings = RunnerSettings(
            enabled=False,
            account_id="acct_1",
            device_id="device_1",
        )
        self.runner = PlaySessionRunner(
            config=config(self.root / "profile.json"),
            capabilities=CapabilitySet(()),
            actions={},
            state_detector=object(),
            overlay_detector=None,
            ocr_provider=object(),
            settings=self.settings,
            sender=self.sender,
            guard=FakeGuard(),
            runs_root=self.root / "runs",
            app_version="test",
            config_revision="sha256:test",
            notify=lambda _: None,
        )

    def test_each_run_rebuilds_dispatcher_recorder_and_run_id(self) -> None:
        identity = SessionIdentity.from_runner_settings(
            self.settings,
            self.verification,
        )
        with patch("apex_automation.play_session.CapabilityPilot", FakePilot):
            first = self.runner.run(identity, ContinuePlayPolicy(), object())
            second = self.runner.run(identity, ContinuePlayPolicy(), object())

        self.assertNotEqual(first.run_id, second.run_id)
        self.assertIsNot(FakePilot.dispatchers[0], FakePilot.dispatchers[1])
        self.assertEqual(first.status, "PLAYED")
        self.assertEqual(first.frames, 5)
        self.assertEqual(self.sender.releases, 2)
        for result in (first, second):
            events = [
                json.loads(line)["type"]
                for line in (result.run_dir / "events.jsonl").read_text().splitlines()
            ]
            self.assertEqual(events.count("RUN_STARTED"), 1)
            self.assertEqual(events.count("RUN_FINISHED"), 1)

    def test_managed_session_requires_remote_reporting(self) -> None:
        identity = SessionIdentity(
            account_id="acct_1",
            device_id="device_1",
            identity_verification=self.verification,
            lease_id="lease_1",
            lease_fence=3,
            target_level=20,
        )

        with self.assertRaisesRegex(ValueError, "必须启用远程上报"):
            self.runner.run(identity, ContinuePlayPolicy(), object())

    def test_only_the_apex_pilot_receives_the_normalized_capture_wrapper(self) -> None:
        self.runner.config.environment["captureResolution"] = [1920, 1080]
        identity = SessionIdentity.from_runner_settings(
            self.settings,
            self.verification,
        )
        raw_source = object()

        with patch("apex_automation.play_session.CapabilityPilot", FakePilot):
            result = self.runner.run(identity, ContinuePlayPolicy(), raw_source)

        apex_source = FakePilot.sources[-1]
        self.assertIs(apex_source.source, raw_source)
        self.assertEqual(apex_source.capture_size, (1920, 1080))
        self.assertEqual(apex_source.reference_size, (2560, 1440))
        manifest = json.loads((result.run_dir / "manifest.json").read_text())
        self.assertEqual(manifest["runContext"]["resolution"], [1920, 1080])
        self.assertEqual(
            manifest["runContext"]["referenceResolution"],
            [2560, 1440],
        )

    def test_resolution_failure_stops_the_local_run_and_keeps_a_specific_reason(self) -> None:
        identity = SessionIdentity.from_runner_settings(
            self.settings,
            self.verification,
        )
        FakePilot.outcome = "ENVIRONMENT_INVALID"

        with patch("apex_automation.play_session.CapabilityPilot", FakePilot):
            result = self.runner.run(identity, ContinuePlayPolicy(), object())

        self.assertEqual(result.status, "PLAYED")
        self.assertEqual(result.error_code, "RESOLUTION_MISMATCH")
        self.assertEqual(result.exit_code, 1)

    def test_a_known_page_stall_keeps_its_specific_error_code(self) -> None:
        identity = SessionIdentity.from_runner_settings(
            self.settings,
            self.verification,
        )
        FakePilot.outcome = "STALLED_KNOWN"

        with patch("apex_automation.play_session.CapabilityPilot", FakePilot):
            result = self.runner.run(identity, ContinuePlayPolicy(), object())

        self.assertEqual(result.status, "PLAYED")
        self.assertEqual(result.error_code, "KNOWN_STATE_STALL_UNRECOVERED")
        self.assertIn("已知页面", result.error)

    def test_lease_loss_finishes_with_a_specific_error_for_managed_cleanup(self) -> None:
        identity = SessionIdentity.from_runner_settings(self.settings, self.verification)
        FakePilot.outcome = "LEASE_UNRECOVERED"
        with patch("apex_automation.play_session.CapabilityPilot", FakePilot):
            result = self.runner.run(identity, ContinuePlayPolicy(), object())
        self.assertEqual(result.status, "PLAYED")
        self.assertEqual(result.error_code, "LEASE_UNRECOVERED")
        self.assertEqual(result.exit_code, 1)
        events = [
            json.loads(line)
            for line in (result.run_dir / "events.jsonl").read_text().splitlines()
        ]
        finished = next(event for event in events if event["type"] == "RUN_FINISHED")
        self.assertEqual(finished["payload"]["errorCode"], "LEASE_UNRECOVERED")

    def test_managed_identity_requires_complete_lease_tuple(self) -> None:
        with self.assertRaisesRegex(ValueError, "leaseId/leaseFence"):
            SessionIdentity(
                account_id="acct_1",
                device_id="device_1",
                identity_verification=self.verification,
                lease_id="lease_1",
                target_level=20,
            )


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.control import TaskControl
from apex_automation.control_server import LocalControlServer, read_event_page
from apex_automation.ocr_obstacles import (
    OcrObstacleDetector,
    OcrToken,
    normalize_ocr_text,
    parse_regions,
)
from apex_automation.recorder import RunRecorder


class FakeProvider:
    def __init__(self, values: dict[tuple[int, int, int, int], tuple[OcrToken, ...]]):
        self.values = values

    def read(self, frame: np.ndarray, region) -> tuple[OcrToken, ...]:
        return self.values.get(region.roi, ())


class RegionParsingTest(unittest.TestCase):
    def test_bare_array_still_loads_as_a_multi_line_region(self) -> None:
        regions = parse_regions({"titleCenter": [320, 80, 2240, 760]})
        self.assertEqual(regions["titleCenter"].roi, (320, 80, 2240, 760))
        self.assertFalse(regions["titleCenter"].single_line)

    def test_object_form_carries_the_single_line_promise(self) -> None:
        regions = parse_regions(
            {"lobbyModeName": {"roi": [60, 1060, 420, 1140], "singleLine": True}}
        )
        self.assertEqual(regions["lobbyModeName"].roi, (60, 1060, 420, 1140))
        self.assertTrue(regions["lobbyModeName"].single_line)

    def test_inverted_or_short_regions_are_rejected(self) -> None:
        for bad in ([420, 1060, 60, 1140], [60, 1140, 420, 1060], [60, 1060, 420]):
            with self.subTest(roi=bad):
                with self.assertRaisesRegex(ValueError, "OCR 区域无效"):
                    parse_regions({"bad": bad})


class OcrRulesTest(unittest.TestCase):
    rules_path = REPOSITORY_ROOT / "windows" / "config" / "obstacle-rules.zh-CN.json"

    def setUp(self) -> None:
        payload = json.loads(self.rules_path.read_text(encoding="utf-8"))
        self.regions = {
            name: tuple(values)
            for name, values in payload["regions"].items()
        }
        self.frame = np.zeros((1440, 2560, 3), dtype=np.uint8)

    def detector(self, *, include_button: bool = True) -> OcrObstacleDetector:
        values = {
            self.regions["titleCenter"]: (OcrToken(" 获得：奖励！", 0.97),),
        }
        if include_button:
            values[self.regions["bottomCenter"]] = (OcrToken("继续", 0.96),)
        return OcrObstacleDetector.from_path(FakeProvider(values), self.rules_path)

    def test_normalization_removes_spacing_and_punctuation(self) -> None:
        self.assertEqual(normalize_ocr_text(" 获得：奖 励！"), "获得奖励")

    def test_rule_requires_page_title_and_button_region(self) -> None:
        analysis = self.detector(include_button=True).analyze(self.frame)
        self.assertIsNotNone(analysis.decision)
        self.assertEqual(analysis.decision.rule_id, "reward-continue")

        missing_button = self.detector(include_button=False).analyze(self.frame)
        self.assertIsNone(missing_button.decision)

    def test_reticle_unlock_uses_the_space_rule_before_generic_reward(self) -> None:
        values = {
            self.regions["titleCenter"]: (OcrToken("光圈已解锁", 0.99),),
            # SPACE is small enough that OCR may omit it; the exact title and
            # the visible command still uniquely identify this page.
            self.regions["bottomCenter"]: (OcrToken("继续", 0.98),),
        }
        detector = OcrObstacleDetector.from_path(FakeProvider(values), self.rules_path)

        decision = detector.analyze(self.frame).decision

        self.assertIsNotNone(decision)
        self.assertEqual(decision.rule_id, "reticle-unlock-space-continue")
        self.assertEqual(decision.state, "RETICLE_UNLOCKED")

    def test_match_quality_survey_is_closed_without_answering_it(self) -> None:
        values = {
            self.regions["titleCenter"]: (OcrToken("比赛质量调查", 0.99),),
            self.regions["bottomCenter"]: (OcrToken("ESC 关闭调查", 0.98),),
        }
        detector = OcrObstacleDetector.from_path(FakeProvider(values), self.rules_path)

        decision = detector.analyze(self.frame).decision

        self.assertIsNotNone(decision)
        self.assertEqual(decision.rule_id, "match-quality-survey-close")
        self.assertEqual(decision.state, "MATCH_QUALITY_SURVEY")
        self.assertEqual(decision.action.name, "escapeScanCode")

    def test_bare_unlocked_text_is_not_assumed_to_use_enter(self) -> None:
        values = {
            self.regions["titleCenter"]: (OcrToken("武器已解锁", 0.99),),
            self.regions["bottomCenter"]: (OcrToken("继续", 0.98),),
        }
        detector = OcrObstacleDetector.from_path(FakeProvider(values), self.rules_path)

        self.assertIsNone(detector.analyze(self.frame).decision)

    def test_low_confidence_text_never_authorizes_rule(self) -> None:
        values = {
            self.regions["titleCenter"]: (OcrToken("奖励", 0.40),),
            self.regions["bottomCenter"]: (OcrToken("继续", 0.99),),
        }
        detector = OcrObstacleDetector.from_path(FakeProvider(values), self.rules_path)
        self.assertIsNone(detector.analyze(self.frame).decision)

    def test_dictionary_rejects_non_whitelisted_key_action(self) -> None:
        payload = json.loads(self.rules_path.read_text(encoding="utf-8"))
        payload["rules"][0]["action"]["name"] = "launchScanCode"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "unsafe-rules.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "安全键白名单"):
                OcrObstacleDetector.from_path(FakeProvider({}), path)


class LocalControlServerTest(unittest.TestCase):
    def test_event_page_reads_the_versioned_recorder_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "test-profile")
            recorder.log("STATE_DETECTED", state="LOBBY_READY_TARGET")

            events = read_event_page(recorder.events_path, after=1)

            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["eventId"], 2)
            self.assertEqual(events[0]["event"], "STATE_DETECTED")
            self.assertEqual(
                events[0]["payload"],
                {"state": "LOBBY_READY_TARGET"},
            )

    def test_local_dashboard_reads_status_and_requires_token_for_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "test-profile")
            recorder.write_status(
                {
                    "runtimeState": "OBSERVING",
                    "foreground": False,
                    "observedState": None,
                    "goalStage": "NOT_STARTED",
                    "lastScreenshot": None,
                }
            )
            control = TaskControl()
            server = LocalControlServer("127.0.0.1", 0, recorder, control)
            server.start()
            try:
                with urlopen(server.url + "/", timeout=3) as response:
                    dashboard = response.read().decode("utf-8")
                self.assertIn("Apex Runner", dashboard)

                with urlopen(server.url + "/api/status", timeout=3) as response:
                    status = json.loads(response.read())
                self.assertEqual(status["runtimeState"], "OBSERVING")

                bad_request = Request(
                    server.url + "/api/tasks/current/pause",
                    method="POST",
                )
                with self.assertRaises(HTTPError) as forbidden:
                    urlopen(bad_request, timeout=3)
                self.assertEqual(forbidden.exception.code, 403)
                forbidden.exception.close()

                pause_request = Request(
                    server.url + "/api/tasks/current/pause",
                    method="POST",
                    headers={"X-Control-Token": server.token},
                )
                with urlopen(pause_request, timeout=3) as response:
                    self.assertEqual(response.status, 202)
                self.assertTrue(control.snapshot().paused)
            finally:
                server.stop()


if __name__ == "__main__":
    unittest.main()

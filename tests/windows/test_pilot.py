from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capabilities import CapabilityDispatcher, CapabilitySet
from apex_automation.config import load_config
from apex_automation.ocr_obstacles import OcrToken
from apex_automation.ocr_states import OcrStateDetector
from apex_automation.pilot import CapabilityPilot


PLAY_CONFIG = REPOSITORY_ROOT / "windows" / "config" / "play-2560x1440.zh-CN.json"
CAPABILITIES = REPOSITORY_ROOT / "windows" / "config" / "enter-game.zh-CN.json"
STATES = REPOSITORY_ROOT / "windows" / "config" / "game-states.zh-CN.json"

FRAME = np.zeros((1440, 2560, 3), dtype=np.uint8)


class ScriptedProvider:
    """Returns whatever the test says the screen currently reads."""

    def __init__(self) -> None:
        self.readings: dict[str, tuple[str, float]] = {}

    def read(self, frame: np.ndarray, region) -> tuple[OcrToken, ...]:
        item = self.readings.get(region.name)
        return () if item is None else (OcrToken(item[0], item[1]),)


class FakeSender:
    def __init__(self) -> None:
        self.calls: list[tuple[object, ...]] = []

    def click(self, x: int, y: int) -> None:
        self.calls.append(("click", x, y))

    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None:
        self.calls.append(("tap", scan_code, duration_ms))

    def release_all(self) -> None:
        self.calls.append(("releaseAll",))


class FakeGuard:
    def __init__(self) -> None:
        self.foreground = True
        self.foreground_checks = 0

    def ensure_not_aborted(self) -> None:
        return None

    def ensure_target_foreground(self) -> None:
        self.foreground_checks += 1
        if not self.foreground:
            raise AssertionError("在前台检查失败的情况下仍然发送了输入")

    def target_is_foreground(self) -> bool:
        return self.foreground


class FakeRecorder:
    def __init__(self, directory: Path) -> None:
        self.run_dir = directory
        self.events: list[tuple[str, dict[str, object]]] = []

    def log(self, event: str, **payload: object) -> None:
        self.events.append((event, payload))

    def screenshot(self, stage: str, frame: np.ndarray) -> Path:
        return self.run_dir / f"{stage}.png"

    def names(self) -> list[str]:
        return [event for event, _ in self.events]


class FakeSource:
    def grab(self) -> np.ndarray:
        return FRAME


class PilotTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.provider = ScriptedProvider()
        self.sender = FakeSender()
        self.guard = FakeGuard()
        self.recorder = FakeRecorder(Path(self.temporary.name))
        self.now = 0.0
        self.payload = json.loads(CAPABILITIES.read_text(encoding="utf-8"))
        self.pilot = CapabilityPilot(
            load_config(PLAY_CONFIG),
            FakeSource(),
            self.sender,
            self.guard,
            self.recorder,
            state_detector=OcrStateDetector.from_path(self.provider, STATES),
            dispatcher=CapabilityDispatcher(CapabilitySet.from_payload(self.payload)),
            actions=dict(self.payload["actions"]),
            sleep=lambda _: None,
            monotonic=lambda: self.now,
        )

    def screen(self, **readings: tuple[str, float]) -> None:
        self.provider.readings = dict(readings)

    def test_the_shipped_capability_set_is_executable_as_written(self) -> None:
        # Construction validates every action name and kind, so reaching this
        # line means no capability can fail halfway through sending an input.
        self.assertGreater(len(self.pilot.dispatcher.capabilities.capabilities), 0)

    def test_a_capability_naming_a_missing_action_is_rejected_before_any_frame(self) -> None:
        with self.assertRaisesRegex(ValueError, "引用了未定义的动作"):
            CapabilityPilot(
                load_config(PLAY_CONFIG),
                FakeSource(),
                self.sender,
                self.guard,
                self.recorder,
                state_detector=OcrStateDetector.from_path(self.provider, STATES),
                dispatcher=CapabilityDispatcher(CapabilitySet.from_payload(self.payload)),
                actions={},
            )

    def test_an_unrecognised_screen_sends_nothing(self) -> None:
        self.screen()
        record = self.pilot.step()
        self.assertIsNone(record["state"])
        self.assertEqual(record["decision"]["reason"], "NO_STATE")
        self.assertEqual(self.sender.calls, [])

    def test_a_queueing_lobby_waits_because_it_owns_no_capability(self) -> None:
        self.screen(lobbyPrimaryButton=("取消", 1.0))
        record = self.pilot.step()
        self.assertEqual(record["state"], "LOBBY_QUEUEING")
        self.assertEqual(record["decision"]["reason"], "NO_CAPABILITY")
        self.assertEqual(self.sender.calls, [])

    def test_a_ready_lobby_clicks_ready_once_and_then_waits_for_the_queue(self) -> None:
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("未上榜", 1.0))
        self.pilot.step()
        self.assertEqual(self.sender.calls, [("click", 1280, 1295)])
        sent = next(p for e, p in self.recorder.events if e == "ACTION_SENT")
        self.assertEqual(sent["capability"], "lobby-start-match")

        # The screen has not repainted yet. An unchanged frame is not evidence
        # of failure, so nothing may be re-sent and nothing may be rejected.
        self.now = 0.3
        self.pilot.step()
        self.assertEqual(len(self.sender.calls), 1)
        self.assertNotIn("ACTION_POSTCONDITION_REJECTED", self.recorder.names())

        self.now = 1.0
        self.screen(lobbyPrimaryButton=("取消", 1.0))
        record = self.pilot.step()
        self.assertEqual(record["state"], "LOBBY_QUEUEING")
        self.assertIn("ACTION_CONFIRMED", self.recorder.names())
        self.assertEqual(len(self.sender.calls), 1)

    def test_a_wrong_postcondition_is_recorded_rather_than_treated_as_success(self) -> None:
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("未上榜", 1.0))
        self.pilot.step()
        # Pressing ready cannot land on the post-match screen; if it looks like
        # it did, the click went somewhere unintended.
        self.now = 1.0
        self.screen(postMatchHints=("SPACE继续 TAB返回大厅", 0.97))
        self.pilot.step()
        rejected = next(
            p for e, p in self.recorder.events if e == "ACTION_POSTCONDITION_REJECTED"
        )
        self.assertEqual(rejected["capability"], "lobby-start-match")
        self.assertEqual(rejected["evidenceState"], "POST_MATCH_SUMMARY")

    def test_losing_the_foreground_releases_input_and_forgets_the_pending_action(self) -> None:
        self.screen(dropshipPrompt=("LCTRL单独发射", 0.91))
        self.pilot.step()
        self.assertEqual(self.sender.calls, [("tap", 29, 80)])
        self.assertIsNotNone(self.pilot.dispatcher.pending)

        self.guard.foreground = False
        self.now = 0.3
        record = self.pilot.step()
        self.assertEqual(record["skipped"], "NOT_FOREGROUND")
        self.assertIn(("releaseAll",), self.sender.calls)
        # Anything could have happened to the game while it was not ours, so
        # the outstanding LCTRL must not be confirmed by whatever shows up.
        self.assertIsNone(self.pilot.dispatcher.pending)

    def test_the_dropship_toggle_is_never_pressed_twice(self) -> None:
        # LCTRL rejoins the squad when repeated, so a screen that stays put
        # must not produce a second press even after the window expires.
        self.screen(dropshipPrompt=("LCTRL单独发射", 0.91))
        for moment in (0.0, 0.3, 3.5, 7.0, 11.0):
            self.now = moment
            self.pilot.step()
        self.assertEqual(self.sender.calls.count(("tap", 29, 80)), 1)
        self.assertIn("DECISION_PAUSED", self.recorder.names())

    def test_melee_repeats_while_the_match_screen_persists(self) -> None:
        self.screen(squadCountAlive=("20 剩余小队数量", 0.98))
        fired = 0
        for step in range(20):
            self.now = step * 1.0
            if self.pilot.step()["decision"]["kind"] == "fire":
                fired += 1
        self.assertGreater(fired, 1)
        self.assertTrue(all(call[0] == "tap" and call[1] == 47 for call in self.sender.calls))

    def test_a_run_always_releases_input_when_it_ends(self) -> None:
        self.screen(squadCountAlive=("20 剩余小队数量", 0.98))
        steps = iter(range(200))

        def monotonic() -> float:
            return next(steps) * 0.5

        self.pilot.monotonic = monotonic
        self.pilot.started = 0.0
        self.pilot.run(duration_s=3.0)
        self.assertEqual(self.sender.calls[-1], ("releaseAll",))


if __name__ == "__main__":
    unittest.main()

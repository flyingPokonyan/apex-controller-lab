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
from apex_automation.progression import LobbyProgressionReader
from apex_automation.progression_policy import TargetLevelPolicy
from apex_automation.safety import ForegroundLost


PLAY_CONFIG = REPOSITORY_ROOT / "windows" / "config" / "play-2560x1440.zh-CN.json"
CAPABILITIES = REPOSITORY_ROOT / "windows" / "config" / "enter-game.zh-CN.json"
STATES = REPOSITORY_ROOT / "windows" / "config" / "game-states.zh-CN.json"
OVERLAYS = REPOSITORY_ROOT / "windows" / "config" / "play-overlays.zh-CN.json"

FRAME = np.zeros((1440, 2560, 3), dtype=np.uint8)


class ScriptedProvider:
    """Returns whatever the test says the screen currently reads."""

    def __init__(self) -> None:
        self.readings: dict[str, tuple[str, float]] = {}
        self.error: Exception | None = None

    def read(self, frame: np.ndarray, region) -> tuple[OcrToken, ...]:
        if self.error is not None:
            raise self.error
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
        path = self.run_dir / f"{stage}.png"
        self.log("SCREENSHOT_SAVED", stage=stage, path=str(path))
        return path

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
        self.overlay_provider = ScriptedProvider()
        self.sender = FakeSender()
        self.guard = FakeGuard()
        self.recorder = FakeRecorder(Path(self.temporary.name))
        self.now = 0.0
        self.sleeps: list[float] = []
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
            sleep=self.sleeps.append,
            monotonic=lambda: self.now,
        )

    def enable_overlays(self) -> None:
        self.pilot = CapabilityPilot(
            load_config(PLAY_CONFIG),
            FakeSource(),
            self.sender,
            self.guard,
            self.recorder,
            state_detector=OcrStateDetector.from_path(self.provider, STATES),
            overlay_detector=OcrStateDetector.from_path(self.overlay_provider, OVERLAYS),
            dispatcher=CapabilityDispatcher(CapabilitySet.from_payload(self.payload)),
            actions=dict(self.payload["actions"]),
            sleep=self.sleeps.append,
            monotonic=lambda: self.now,
        )

    def screen(self, **readings: tuple[str, float]) -> None:
        self.provider.readings = dict(readings)

    def overlay_screen(self, text: str = "", confidence: float = 1.0) -> None:
        self.overlay_provider.readings = {} if not text else {"fullFrame": (text, confidence)}

    def enable_progression(self, *, max_attempts: int = 3) -> None:
        self.pilot.progression_reader = LobbyProgressionReader(self.provider)
        self.pilot.progression_stabilizer.reset()
        self.pilot.progression_max_attempts = max_attempts

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

    def test_a_screen_with_no_rule_keeps_one_frame_of_itself(self) -> None:
        # Future game updates can introduce a page no rule knows yet, so the
        # runner must preserve one frame without clicking it blindly.
        self.screen()
        self.pilot.step()
        self.assertNotIn("SCREENSHOT_SAVED", self.recorder.names())

        self.now = 20.0
        self.pilot.step()
        saved = next(p for e, p in self.recorder.events if e == "SCREENSHOT_SAVED")
        self.assertEqual(saved["stage"], "unknown")

        # One per episode, not one per frame, or a loading screen alone eats
        # the whole screenshot budget.
        self.now = 40.0
        self.pilot.step()
        self.assertEqual(self.recorder.names().count("SCREENSHOT_SAVED"), 1)

        # A recognised screen ends the episode, so the next stall is captured.
        self.now = 41.0
        self.screen(lobbyPrimaryButton=("取消", 1.0))
        self.pilot.step()
        self.now = 60.0
        self.screen()
        self.pilot.step()
        self.now = 80.0
        self.pilot.step()
        self.assertEqual(self.recorder.names().count("SCREENSHOT_SAVED"), 3)

    def test_one_unknown_stretch_keeps_a_frame_of_each_screen_in_it(self) -> None:
        # 20260730-232551 spent 142 seconds in a single unknown stretch that
        # ran from the pre-match lobby through legend select and loading to
        # the whole dropship flight. Keeping only its first frame lost the
        # one screen that actually needed a rule.
        frames = {"current": np.zeros((1440, 2560, 3), dtype=np.uint8)}

        class ShiftingSource:
            def grab(self) -> np.ndarray:
                return frames["current"]

        self.pilot.source = ShiftingSource()
        self.screen()
        self.pilot.step()
        self.now = 20.0
        self.pilot.step()
        self.assertEqual(self.recorder.names().count("SCREENSHOT_SAVED"), 1)

        # Same screen a sample later: nothing new to keep.
        self.now = 45.0
        self.pilot.step()
        self.assertEqual(self.recorder.names().count("SCREENSHOT_SAVED"), 1)

        # A different screen inside the same stretch must be kept.
        frames["current"] = np.full((1440, 2560, 3), 200, dtype=np.uint8)
        self.now = 70.0
        self.pilot.step()
        self.assertEqual(self.recorder.names().count("SCREENSHOT_SAVED"), 2)
        self.assertEqual(self.pilot.counters["unknownScreens"], 1)
        self.assertEqual(self.pilot.counters["unknownSamples"], 1)

    def test_losing_the_foreground_mid_action_pauses_instead_of_ending_the_run(self) -> None:
        # 20260730-232551 finished as FAILED with no summary because alt-tab
        # between the frame's foreground check and the send raised out of the
        # loop. Handing the window back has to be enough to carry on.
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("进化版机器人大逃杀", 1.0))
        self.guard.ensure_target_foreground = self._raise_foreground_lost

        record = self.pilot.step()

        self.assertEqual(record["skipped"], "NOT_FOREGROUND")
        self.assertIn("FOREGROUND_PAUSED", self.recorder.names())
        self.assertNotIn(("click", 1280, 1295), self.sender.calls)
        self.assertIsNone(self.pilot.dispatcher.pending)

    @staticmethod
    def _raise_foreground_lost() -> None:
        raise ForegroundLost("前台程序不是 Apex")

    def test_a_solo_jumpmaster_still_launches_itself(self) -> None:
        # Without fill the squad is one player, so nobody can be followed or
        # detached from and neither dropship state can ever match. The launch
        # prompt is the only thing left to key on.
        self.screen(dropshipLaunchPrompt=("发射", 0.98))
        record = self.pilot.step()

        self.assertEqual(record["state"], "DROPSHIP_SOLO_JUMPMASTER")
        self.assertEqual(record["decision"]["capability"], "dropship-launch")
        self.assertEqual(self.sender.calls, [("tap", 18, 80)])

    def test_a_following_dropship_still_detaches_before_launching(self) -> None:
        # The solo rule must not shadow the squad path: a followed dropship
        # shows both lines, and LCTRL has to come first.
        self.screen(
            dropshipPrompt=("LCTRL单独发射", 0.91),
            dropshipLaunchPrompt=("建议", 0.98),
        )
        record = self.pilot.step()

        self.assertEqual(record["state"], "DROPSHIP_FOLLOWING")
        self.assertEqual(self.sender.calls, [("tap", 29, 2000)])

    def test_a_queueing_lobby_waits_because_it_owns_no_capability(self) -> None:
        self.screen(lobbyPrimaryButton=("取消", 1.0))
        record = self.pilot.step()
        self.assertEqual(record["state"], "LOBBY_QUEUEING")
        self.assertEqual(record["decision"]["reason"], "NO_CAPABILITY")
        self.assertEqual(self.sender.calls, [])

    def test_lobby_progress_is_confirmed_before_starting_the_match(self) -> None:
        self.enable_progression()
        self.screen(
            lobbyPrimaryButton=("准备", 1.0),
            lobbyModeName=("进化版机器人大逃杀", 1.0),
            lobbyLevel=("9", 0.99),
            lobbyXp=("7.87K / 8.15K", 0.98),
        )

        first = self.pilot.step()
        self.now = 0.3
        second = self.pilot.step()

        self.assertEqual(first["decision"]["reason"], "LOBBY_PROGRESS")
        self.assertEqual(second["decision"]["capability"], "lobby-start-match")
        self.assertEqual(self.sender.calls, [("click", 1280, 1295)])
        progress = next(
            payload
            for event, payload in self.recorder.events
            if event == "LOBBY_PROGRESS"
        )
        self.assertEqual(progress["level"], 9)
        self.assertEqual(progress["xpCurrentApprox"], 7870)
        self.assertEqual(progress["readStatus"], "OK")

    def test_failed_lobby_progress_does_not_block_play_indefinitely(self) -> None:
        self.enable_progression(max_attempts=3)
        self.screen(
            lobbyPrimaryButton=("准备", 1.0),
            lobbyModeName=("进化版机器人大逃杀", 1.0),
            lobbyLevel=("?", 0.99),
            lobbyXp=("?", 0.99),
        )

        first = self.pilot.step()
        self.now = 0.3
        second = self.pilot.step()
        self.now = 0.6
        third = self.pilot.step()

        self.assertEqual(first["decision"]["reason"], "LOBBY_PROGRESS")
        self.assertEqual(second["decision"]["reason"], "LOBBY_PROGRESS")
        self.assertEqual(third["decision"]["capability"], "lobby-start-match")
        progress = next(
            payload
            for event, payload in self.recorder.events
            if event == "LOBBY_PROGRESS"
        )
        self.assertEqual(progress["readStatus"], "FAILED")
        self.assertIsNone(progress["level"])

    def test_target_level_stops_before_the_ready_capability_is_dispatched(self) -> None:
        self.enable_progression()
        self.pilot.progression_policy = TargetLevelPolicy(20)
        self.screen(
            lobbyPrimaryButton=("准备", 1.0),
            lobbyModeName=("进化版机器人大逃杀", 1.0),
            lobbyLevel=("20", 0.99),
            lobbyXp=("1.44K / 3.90K", 0.98),
        )

        first = self.pilot.step()
        self.now = 0.3
        second = self.pilot.step()

        self.assertEqual(first["decision"]["reason"], "LOBBY_PROGRESS")
        self.assertEqual(second["decision"]["reason"], "TARGET_REACHED")
        self.assertEqual(self.pilot.session_outcome, "TARGET_REACHED")
        self.assertNotIn(("click", 1280, 1295), self.sender.calls)
        self.assertIn("ACCOUNT_TARGET_REACHED", self.recorder.names())

    def test_managed_failed_progress_pauses_before_dispatch_and_rechecks_later(self) -> None:
        self.enable_progression(max_attempts=2)
        self.pilot.progression_policy = TargetLevelPolicy(20)
        self.screen(
            lobbyPrimaryButton=("准备", 1.0),
            lobbyModeName=("进化版机器人大逃杀", 1.0),
            lobbyLevel=("?", 0.99),
            lobbyXp=("?", 0.99),
        )

        self.pilot.step()
        self.now = 0.3
        paused = self.pilot.step()
        self.now = 1.0
        still_paused = self.pilot.step()
        self.now = 5.4
        retrying = self.pilot.step()

        self.assertEqual(paused["decision"]["reason"], "PAUSE_UNCERTAIN")
        self.assertEqual(still_paused["decision"]["reason"], "PAUSE_UNCERTAIN")
        self.assertEqual(retrying["decision"]["reason"], "LOBBY_PROGRESS")
        self.assertNotIn(("click", 1280, 1295), self.sender.calls)
        self.assertEqual(self.recorder.names().count("PROGRESSION_PAUSED"), 1)

    def test_target_read_while_queueing_defers_until_a_safe_lobby(self) -> None:
        self.enable_progression()
        self.pilot.progression_policy = TargetLevelPolicy(20)
        self.screen(
            lobbyPrimaryButton=("取消", 1.0),
            lobbyLevel=("20", 0.99),
            lobbyXp=("1.44K / 3.90K", 0.98),
        )

        self.pilot.step()
        self.now = 0.3
        queued = self.pilot.step()
        self.now = 0.6
        self.screen(
            lobbyPrimaryButton=("准备", 1.0),
            lobbyModeName=("进化版机器人大逃杀", 1.0),
            lobbyLevel=("20", 0.99),
            lobbyXp=("1.44K / 3.90K", 0.98),
        )
        safe = self.pilot.step()

        self.assertEqual(
            queued["decision"]["reason"],
            "DEFER_UNTIL_SAFE_LOBBY",
        )
        self.assertEqual(safe["decision"]["reason"], "TARGET_REACHED")
        self.assertNotIn(("click", 1280, 1295), self.sender.calls)

    def test_a_known_guide_overlay_blocks_the_lobby_and_uses_its_own_click(self) -> None:
        self.enable_overlays()
        # The underlying lobby still reads 训练 + 选择 under this overlay. The
        # old linear runner clicked through it; overlay OCR must win first.
        self.screen(lobbyPrimaryButton=("选择", 1.0), lobbyModeName=("训练", 1.0))
        self.overlay_screen("账户 账号经验值 奖励进度 继续")

        first = self.pilot.step()
        self.assertIsNone(first["state"])
        self.assertEqual(self.sender.calls, [])

        self.now = 1.0
        second = self.pilot.step()
        self.assertEqual(second["state"], "GUIDE_ACCOUNT")
        self.assertEqual(self.sender.calls, [("click", 610, 150)])

    def test_a_generic_activity_page_is_stable_before_escape_is_sent(self) -> None:
        self.enable_overlays()
        self.screen()
        self.overlay_provider.readings = {
            "titleCenter": ("最新活动", 0.99),
            "bottomRight": ("ESC 返回", 0.99),
        }

        self.pilot.step()
        self.now = 1.5
        candidate = self.pilot.step()
        self.assertIsNone(candidate["state"])
        self.assertEqual(self.sender.calls, [])

        self.now = 2.5
        confirmed = self.pilot.step()
        self.assertEqual(confirmed["state"], "GENERIC_MODAL")
        self.assertEqual(self.sender.calls, [("tap", 1, 80)])

    def test_a_clear_overlay_scan_allows_the_lobby_action_immediately(self) -> None:
        self.enable_overlays()
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("进化版机器人大逃杀", 1.0))

        record = self.pilot.step()
        self.assertEqual(record["state"], "LOBBY_READY_TARGET")
        self.assertEqual(self.sender.calls, [("click", 1280, 1295)])

    def test_overlay_ocr_failure_blocks_the_underlying_lobby_action(self) -> None:
        self.enable_overlays()
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("进化版机器人大逃杀", 1.0))
        self.overlay_provider.error = RuntimeError("offline OCR unavailable")

        record = self.pilot.step()
        self.assertIsNone(record["state"])
        self.assertEqual(record["decision"]["reason"], "NO_STATE")
        self.assertEqual(self.sender.calls, [])
        error = next(payload for event, payload in self.recorder.events if event == "OVERLAY_OCR_ANALYZED")
        self.assertEqual(error["source"], "overlayOcr")
        self.assertIn("offline OCR unavailable", str(error["error"]))

    def _settle_overlay(self, first: float = 0.0) -> dict[str, object]:
        """Run the three steps a two-observation overlay rule needs."""
        self.now = first
        self.pilot.step()
        self.now = first + 1.5
        self.pilot.step()
        self.now = first + 2.5
        return self.pilot.step()

    def test_a_page_that_only_offers_escape_is_closed_with_escape(self) -> None:
        # 20260730-215512 stalled on the post-match reward page: its title sits
        # left of titleCenter and it carries no 继续 button at all, so the
        # reward rule could not match and ENTER would have been the wrong key
        # regardless. The page names its own key in the corner.
        self.enable_overlays()
        self.screen()
        self.overlay_provider.readings = {"bottomLeftBack": ("ESC 返回", 0.98)}

        record = self._settle_overlay()
        self.assertEqual(record["state"], "FULLSCREEN_ESC_BACK")
        self.assertEqual(self.sender.calls, [("tap", 1, 80)])

    def test_the_generic_back_rule_never_backs_out_of_the_mode_panel(self) -> None:
        # The mode panel carries the same 「ESC 返回」 hint and is a screen the
        # runner has to stay on and act within, so a rule this broad may only
        # speak where the fast detector said nothing.
        self.enable_overlays()
        self.screen(modePanelTargetCard=("进化版机器人大逃杀", 1.0))
        self.overlay_provider.readings = {"bottomLeftBack": ("ESC 返回", 0.98)}

        record = self._settle_overlay()
        self.assertEqual(record["state"], "MODE_PANEL_TARGET_VISIBLE")
        self.assertNotIn(("tap", 1, 80), self.sender.calls)
        self.assertEqual({call for call in self.sender.calls}, {("click", 1750, 696)})
        self.assertIn("OVERLAY_RULE_OUTRANKED", self.recorder.names())

    def test_a_ticked_fill_box_is_unticked_before_the_match_is_started(self) -> None:
        self.screen(
            lobbyPrimaryButton=("准备", 1.0),
            lobbyModeName=("进化版机器人大逃杀", 1.0),
            lobbyFillLabel=("补满", 1.0),
        )
        ticked = np.zeros((1440, 2560, 3), dtype=np.uint8)
        ticked[212:233, 75:97] = 255

        class TickedSource:
            def grab(self) -> np.ndarray:
                return ticked

        self.pilot.source = TickedSource()
        record = self.pilot.step()

        self.assertEqual(record["state"], "LOBBY_READY_TARGET_FILL_ON")
        self.assertEqual(self.sender.calls, [("click", 86, 222)])

    def test_an_unticked_fill_box_goes_straight_to_starting_the_match(self) -> None:
        self.screen(
            lobbyPrimaryButton=("准备", 1.0),
            lobbyModeName=("进化版机器人大逃杀", 1.0),
            lobbyFillLabel=("补满", 1.0),
        )
        record = self.pilot.step()

        self.assertEqual(record["state"], "LOBBY_READY_TARGET")
        self.assertEqual(self.sender.calls, [("click", 1280, 1295)])

    def test_spectating_a_living_squad_cannot_freeze_the_match_summary(self) -> None:
        # The whole of 20260730-215512's second match: dying while the squad
        # lived flickered SPECTATING in and out, which latched the session-wide
        # cycle pause and left TAB unpressed on the summary for 78 seconds.
        for index in range(8):
            self.now = index * 6.0
            self.screen(spectateTabs=("观战小队", 0.99), squadCountAlive=("37", 0.99))
            self.pilot.step()
            self.now += 3.0
            self.screen()
            self.pilot.step()

        self.now += 3.0
        self.screen(postMatchHints=("SPACE继续 TAB返回大厅 ESC打开菜单", 0.97))
        record = self.pilot.step()

        self.assertEqual(record["state"], "POST_MATCH_SUMMARY")
        self.assertEqual(record["decision"]["capability"], "post-match-return-lobby")
        self.assertIn(("tap", 15, 80), self.sender.calls)

    def test_auth_overlay_is_recognised_but_never_automated(self) -> None:
        self.enable_overlays()
        self.screen()
        self.overlay_screen("双重身份登录 输入您的代码 输入6位数代码")

        self.pilot.step()
        self.now = 1.5
        self.pilot.step()
        self.now = 2.5
        record = self.pilot.step()
        self.assertEqual(record["state"], "AUTH_REQUIRED")
        self.assertEqual(record["decision"]["reason"], "NO_CAPABILITY")
        self.assertEqual(self.sender.calls, [])

    def test_a_ready_lobby_clicks_ready_once_and_then_waits_for_the_queue(self) -> None:
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("进化版机器人大逃杀", 1.0))
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
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("进化版机器人大逃杀", 1.0))
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

    def test_post_match_taps_tab_then_holds_space_before_waiting_for_lobby(self) -> None:
        self.screen(postMatchHints=("SPACE继续 TAB返回大厅", 0.97))
        record = self.pilot.step()

        self.assertEqual(record["state"], "POST_MATCH_SUMMARY")
        self.assertEqual(
            self.sender.calls,
            [("tap", 15, 80), ("tap", 57, 2000)],
        )
        self.assertEqual(self.sleeps, [1.5])
        sent = next(p for e, p in self.recorder.events if e == "ACTION_SENT")
        self.assertEqual(sent["capability"], "post-match-return-lobby")
        self.assertEqual(
            sent["steps"],
            [
                {"type": "tapKey", "scanCode": 15, "durationMs": 80},
                {"type": "wait", "durationMs": 1500},
                {"type": "tapKey", "scanCode": 57, "durationMs": 2000},
            ],
        )

        self.now = 4.0
        self.screen(lobbyPrimaryButton=("准备", 1.0), lobbyModeName=("进化版机器人大逃杀", 1.0))
        self.pilot.step()
        confirmed = next(p for e, p in self.recorder.events if e == "ACTION_CONFIRMED")
        self.assertEqual(confirmed["capability"], "post-match-return-lobby")
        self.assertEqual(confirmed["evidenceState"], "LOBBY_READY_TARGET")

    def test_a_capability_may_hold_its_key_longer_than_the_default_tap(self) -> None:
        # An 80ms tap of this same scan code crouches in the firing range but
        # did nothing on the dropship, so the duration has to be per action.
        self.screen(dropshipPrompt=("LCTRL单独发射", 0.91))
        self.pilot.step()
        detach = next(
            c for c in self.pilot.dispatcher.capabilities.capabilities if c.id == "dropship-detach"
        )
        self.assertEqual(detach.hold_ms, 2000)
        self.assertEqual(self.sender.calls, [("tap", 29, detach.hold_ms)])
        self.assertGreater(detach.hold_ms, self.pilot.key_tap_ms)

    def test_losing_the_foreground_releases_input_and_forgets_the_pending_action(self) -> None:
        self.screen(dropshipPrompt=("LCTRL单独发射", 0.91))
        self.pilot.step()
        self.assertEqual(len(self.sender.calls), 1)
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
        detach = self.payload["actions"]["detachScanCode"]
        taps = [call for call in self.sender.calls if call[0] == "tap" and call[1] == detach]
        self.assertEqual(len(taps), 1)
        self.assertIn("DECISION_PAUSED", self.recorder.names())

    def test_melee_repeats_while_the_match_screen_persists(self) -> None:
        self.screen(squadCountAlive=("20 剩余小队数量", 0.98))
        fired = 0
        for step in range(20):
            self.now = step * 1.0
            if self.pilot.step()["decision"]["kind"] == "fire":
                fired += 1
        self.assertGreater(fired, 1)
        melee = self.payload["actions"]["meleeScanCode"]
        self.assertTrue(all(call[0] == "tap" and call[1] == melee for call in self.sender.calls))

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

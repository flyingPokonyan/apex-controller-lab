from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capabilities import CapabilityDispatcher, CapabilitySet
from apex_automation.ocr_obstacles import (
    SAFE_KEY_ACTIONS,
    FullFrameOcrProvider,
    OcrObstacleDetector,
    OcrPositionUnavailable,
    OcrToken,
    RapidOcrProvider,
    parse_regions,
)
from apex_automation.ocr_states import OcrStateDetector
from apex_automation.pilot import producible_states


CONFIG = REPOSITORY_ROOT / "windows" / "config" / "enter-game.zh-CN.json"
STATES = REPOSITORY_ROOT / "windows" / "config" / "game-states.zh-CN.json"
OVERLAYS = REPOSITORY_ROOT / "windows" / "config" / "play-overlays.zh-CN.json"
OBSTACLES = REPOSITORY_ROOT / "windows" / "config" / "obstacle-rules.zh-CN.json"


class FakeLayoutProvider:
    def __init__(self, tokens: tuple[OcrToken, ...]) -> None:
        self.tokens = tokens
        self.calls = 0

    def read_with_boxes(self, frame: np.ndarray) -> tuple[OcrToken, ...]:
        self.calls += 1
        return self.tokens


class MissingBoxProvider:
    def __init__(self, values: dict[str, tuple[OcrToken, ...]]) -> None:
        self.values = values
        self.positioned_calls = 0
        self.region_calls: list[str] = []

    def read_with_boxes(self, frame: np.ndarray) -> tuple[OcrToken, ...]:
        self.positioned_calls += 1
        raise OcrPositionUnavailable("no boxes")

    def read(self, frame: np.ndarray, region) -> tuple[OcrToken, ...]:
        self.region_calls.append(region.name)
        return self.values.get(region.name, ())


class RecordingRapidEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, bool]] = []

    def __call__(self, frame: np.ndarray, **options: bool):
        self.calls.append(options)
        if options["use_det"]:
            return SimpleNamespace(
                txts=("新闻",),
                scores=(0.99,),
                boxes=np.asarray([[[1, 2], [11, 2], [11, 12], [1, 12]]]),
            )
        return SimpleNamespace(txts=("准备",), scores=(0.99,), boxes=None)


class EnterGameCapabilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(CONFIG.read_text(encoding="utf-8"))
        self.capabilities = CapabilitySet.from_payload(self.payload)

    def _dispatcher(self) -> CapabilityDispatcher:
        return CapabilityDispatcher(self.capabilities, jitter=lambda low, high: (low + high) // 2)

    @staticmethod
    def _button_terms(state: str) -> tuple[str, ...]:
        """What a state's own rule says the lobby primary button reads."""
        states = json.loads(STATES.read_text(encoding="utf-8"))
        for rule in states["rules"]:
            if rule["state"] != state:
                continue
            return tuple(
                term
                for requirement in rule["requirements"]
                if requirement["region"] == "lobbyPrimaryButton"
                for term in requirement.get("all", ())
            )
        return ()

    def test_every_action_referenced_by_a_capability_exists(self) -> None:
        actions = self.payload["actions"]
        for capability in self.capabilities.capabilities:
            with self.subTest(capability=capability.id):
                self.assertIn(capability.action, actions)

    def test_every_state_and_postcondition_is_one_the_detector_can_produce(self) -> None:
        # Asks the same function the launcher asks, so the shipped capability
        # set cannot pass here and then be rejected at startup on the target
        # machine. That includes `STALLED_UNKNOWN`, which no dictionary
        # produces because it is not a page: the watchdog raises it when
        # nothing has matched a frame for minutes.
        known = producible_states(
            OcrStateDetector.from_path(object(), STATES),
            OcrStateDetector.from_path(object(), OVERLAYS),
        )
        known.update(rule.state for rule in OcrStateDetector.from_path(object(), STATES).rules)
        known.update(OcrObstacleDetector.from_path(object(), OBSTACLES).states)
        for capability in self.capabilities.capabilities:
            for state in (*capability.states, *capability.allowed_next_states):
                with self.subTest(capability=capability.id, state=state):
                    self.assertIn(state, known, f"{capability.id} 引用了无法产生的状态 {state}")

    def test_every_safe_obstacle_rule_has_the_same_capability_contract(self) -> None:
        detector = OcrObstacleDetector.from_path(object(), OBSTACLES)
        overlay_detector = OcrStateDetector.from_path(object(), OVERLAYS)
        overlay_rules = {rule.state: rule for rule in overlay_detector.rules if rule.enabled}
        for rule in detector.rules:
            if not rule.enabled:
                continue
            self.assertIn(rule.state, overlay_rules)
            self.assertEqual(
                [
                    (item.region, item.any_terms, item.all_terms, item.min_confidence)
                    for item in overlay_rules[rule.state].requirements
                ],
                [
                    (item.region, item.any_terms, item.all_terms, item.min_confidence)
                    for item in rule.requirements
                ],
            )
            matches = [
                capability
                for capability in self.capabilities.for_state(rule.state)
                if capability.action == rule.action.name
                and capability.kind == rule.action.kind
                and capability.confirm_ms == rule.action.confirm_ms
                and capability.max_attempts == rule.action.max_attempts
            ]
            with self.subTest(rule=rule.rule_id):
                self.assertEqual(len(matches), 1)

    def test_every_fallback_requires_current_frame_ocr_evidence(self) -> None:
        safety = OcrObstacleDetector.from_path(object(), OBSTACLES)
        overlays = OcrStateDetector.from_path(object(), OVERLAYS)
        safe_states = {rule.state for rule in safety.rules if rule.enabled}
        evidence = {
            (
                requirement.region,
                requirement.any_terms,
                requirement.all_terms,
                requirement.min_confidence,
            )
            for rule in overlays.rules
            if rule.enabled
            for requirement in rule.requirements
        }
        fallbacks = [
            item
            for item in self.capabilities.capabilities
            if item.fallback_for
        ]
        self.assertTrue(fallbacks)
        for capability in fallbacks:
            with self.subTest(capability=capability.id):
                if capability.kind == "key":
                    self.assertIn(capability.action, SAFE_KEY_ACTIONS)
                    self.assertTrue(set(capability.states).issubset(safe_states))
                    self.assertIsNotNone(capability.evidence)
                    item = capability.evidence
                    assert item is not None
                    self.assertIn(
                        (
                            item.region,
                            item.any_terms,
                            item.all_terms,
                            item.min_confidence,
                        ),
                        evidence,
                    )
                    continue

                self.assertEqual(capability.kind, "clickText")
                action = self.payload["actions"][capability.action]
                self.assertIsInstance(action, dict)
                self.assertTrue(action["any"])
                self.assertNotIn("fallbackScanCode", action)

    def test_known_overlay_pages_are_all_routed_into_the_capability_set(self) -> None:
        detector = OcrStateDetector.from_path(object(), OVERLAYS)
        overlay_states = detector.states
        self.assertEqual(
            set(detector.regions),
            {
                "fullFrame",
                "challengePanelTasks",
                "titleCenter",
                "bottomCenter",
                "bottomRight",
                "bottomLeftBack",
            },
        )
        self.assertEqual(self.capabilities.for_state("AUTH_REQUIRED"), ())
        for state in overlay_states - {"AUTH_REQUIRED"}:
            with self.subTest(state=state):
                capabilities = self.capabilities.for_state(state)
                primary = [item for item in capabilities if not item.fallback_for]
                self.assertEqual(len(primary), 1)
                self.assertTrue(
                    all(primary[0].id in item.fallback_for for item in capabilities[1:])
                )

        self.assertEqual(self.payload["actions"]["escapeScanCode"], 1)
        self.assertEqual(self.payload["actions"]["guideAccountContinue"], [610, 150])
        self.assertEqual(self.payload["actions"]["guideFriendsContinue"], [480, 252])
        self.assertEqual(self.payload["actions"]["guideSettingsContinue"], [1730, 155])
        self.assertEqual(self.payload["actions"]["guideProgressContinue"], [1560, 290])

    def test_known_page_recovery_excludes_operational_game_states(self) -> None:
        recovery = next(
            item
            for item in self.capabilities.capabilities
            if item.id == "recover-known-page-by-visible-prompt"
        )
        excluded = {
            "LOBBY_READY_TARGET",
            "LOBBY_SELECT_REQUIRED",
            "MODE_PANEL_TARGET_VISIBLE",
            "MODE_PANEL_TARGET_HOVERED",
            "LAUNCH_READY",
            "DROPSHIP_SOLO_JUMPMASTER",
            "IN_MATCH_ALIVE",
            "STALLED_UNKNOWN",
        }

        self.assertTrue(excluded.isdisjoint(recovery.states))
        self.assertEqual(len(recovery.states), len(recovery.fallback_for))

    def test_one_full_frame_pass_preserves_the_old_obstacle_regions(self) -> None:
        provider = FakeLayoutProvider(
            (
                OcrToken("最新活动", 0.98, (400, 100, 620, 160)),
                OcrToken("ESC 返回", 0.97, (1700, 1000, 1900, 1060)),
            )
        )
        cached = FullFrameOcrProvider(provider)
        frame = np.zeros((1440, 2560, 3), dtype=np.uint8)

        analysis = OcrStateDetector.from_path(cached, OVERLAYS).analyze(frame)
        self.assertIsNotNone(analysis.decision)
        self.assertEqual(analysis.decision.state, "GENERIC_MODAL")
        self.assertEqual(provider.calls, 1)

        regions = parse_regions(
            {
                "fullFrame": [0, 0, 2560, 1440],
                "titleCenter": [320, 80, 2240, 760],
                "bottomRight": [1360, 760, 2560, 1440],
            }
        )
        cached.begin_frame(frame)
        self.assertEqual([t.text for t in cached.read(frame, regions["titleCenter"])], ["最新活动"])
        self.assertEqual([t.text for t in cached.read(frame, regions["bottomRight"])], ["ESC 返回"])
        self.assertEqual(len(cached.read(frame, regions["fullFrame"])), 2)
        self.assertEqual(provider.calls, 2)

        # A new analysis must invalidate the cache even when the capture
        # backend reuses the same ndarray object.
        cached.begin_frame(frame)
        cached.read(frame, regions["fullFrame"])
        self.assertEqual(provider.calls, 3)

    def test_full_frame_ocr_explicitly_reenables_detection_after_fast_state_ocr(self) -> None:
        engine = RecordingRapidEngine()
        provider = RapidOcrProvider()
        provider._engine = engine
        regions = parse_regions(
            {
                "fast": {"roi": [0, 0, 20, 20], "singleLine": True},
                "full": [0, 0, 40, 40],
            }
        )
        frame = np.zeros((40, 40, 3), dtype=np.uint8)

        provider.read(frame, regions["fast"])
        positioned = provider.read_with_boxes(frame)

        self.assertFalse(engine.calls[0]["use_det"])
        self.assertTrue(engine.calls[1]["use_det"])
        self.assertEqual(positioned[0].roi, (1, 2, 11, 12))

    def test_missing_full_frame_boxes_fall_back_to_the_original_regions(self) -> None:
        provider = MissingBoxProvider(
            {
                "fullFrame": (
                    OcrToken("新闻", 0.99),
                    OcrToken("掌控游戏", 0.99),
                    OcrToken("ESC 返回", 0.99),
                )
            }
        )
        detector = OcrStateDetector.from_path(FullFrameOcrProvider(provider), OVERLAYS)
        analysis = detector.analyze(np.zeros((1440, 2560, 3), dtype=np.uint8))

        self.assertIsNone(analysis.error)
        self.assertEqual(analysis.decision.state, "NEWS_WELCOME")
        self.assertEqual(provider.positioned_calls, 1)
        self.assertIn("fullFrame", provider.region_calls)

    def test_the_captured_news_page_is_safe_to_close_with_escape(self) -> None:
        provider = FakeLayoutProvider(
            (
                OcrToken("新闻", 1.0, (850, 20, 980, 75)),
                OcrToken("掌控游戏", 1.0, (390, 670, 700, 740)),
                OcrToken("ESC 返回", 1.0, (55, 1040, 190, 1100)),
            )
        )
        analysis = OcrStateDetector.from_path(
            FullFrameOcrProvider(provider), OVERLAYS
        ).analyze(np.zeros((1440, 2560, 3), dtype=np.uint8))

        self.assertIsNone(analysis.error)
        self.assertEqual(analysis.decision.state, "NEWS_WELCOME")
        action = self.capabilities.for_state("NEWS_WELCOME")[0]
        self.assertEqual(self.payload["actions"][action.action], 1)

    def test_a_modal_over_the_lobby_is_handled_before_the_lobby(self) -> None:
        dispatcher = self._dispatcher()
        decision = dispatcher.decide("CLIMB_SETTINGS_MODAL", 1, 0.0)
        self.assertEqual(decision.capability.id, "dismiss-climb-settings")

    def test_a_training_lobby_never_touches_the_primary_button(self) -> None:
        # The 2026-07-28 log records this exactly: a click at the primary
        # button on a training lobby put the runner in the firing range 28
        # seconds later. That button reads 准备 there, not 选择, so the mode
        # panel has to be opened from the card instead.
        dispatcher = self._dispatcher()
        decision = dispatcher.decide("LOBBY_READY_TRAINING", 1, 0.0)
        self.assertEqual(decision.capability.id, "lobby-change-mode")
        primary_button = self.payload["actions"]["startMatchClick"]
        for capability in self.capabilities.for_state("LOBBY_READY_TRAINING"):
            with self.subTest(capability=capability.id):
                self.assertNotEqual(self.payload["actions"][capability.action], primary_button)

    def test_mode_selection_takes_the_card_then_the_confirm_button(self) -> None:
        dispatcher = self._dispatcher()
        first = dispatcher.decide("MODE_PANEL_TARGET_VISIBLE", 1, 0.0)
        self.assertEqual(first.capability.action, "targetCardClick")
        dispatcher.confirm_pending("MODE_PANEL_TARGET_HOVERED")
        dispatcher.note_state("MODE_PANEL_TARGET_HOVERED", 1.0)
        second = dispatcher.decide("MODE_PANEL_TARGET_HOVERED", 2, 1.0)
        self.assertEqual(second.capability.action, "targetConfirmClick")

    def test_only_the_target_mode_lobby_may_press_the_primary_button(self) -> None:
        dispatcher = self._dispatcher()
        decision = dispatcher.decide("LOBBY_READY_TARGET", 1, 0.0)
        self.assertEqual(decision.capability.id, "lobby-start-match")

        # One box, three different buttons depending on what it reads. 选择
        # opens the mode panel and 准备 commits to whatever mode the card is
        # showing, so pressing it is only safe where the rule also pins the
        # mode. Everything allowed to click that box is listed here, and a
        # new entry has to justify itself against the label.
        safe_to_click = {"LOBBY_SELECT_REQUIRED": "选择", "LOBBY_READY_TARGET": "准备"}
        primary_button = self.payload["actions"]["startMatchClick"]
        for capability in self.capabilities.capabilities:
            if self.payload["actions"].get(capability.action) != primary_button:
                continue
            for state in capability.states:
                with self.subTest(capability=capability.id, state=state):
                    self.assertIn(state, safe_to_click)
                    self.assertIn(safe_to_click[state], self._button_terms(state))

    def test_pressing_ready_is_only_safe_where_the_rule_pins_the_mode_name(self) -> None:
        # 准备 queues into whatever the card happens to be showing, so the one
        # state allowed to press it has to prove which mode that is. Retargeting
        # the runner at a different mode is a matter of editing the pinned name
        # in five places; this is what stops a sixth from being forgotten.
        states = json.loads(STATES.read_text(encoding="utf-8"))
        rule = next(r for r in states["rules"] if r["state"] == "LOBBY_READY_TARGET")
        mode_terms = [
            term
            for requirement in rule["requirements"]
            if requirement["region"] == "lobbyModeName"
            for term in requirement.get("all", ())
        ]
        self.assertTrue(mode_terms, "目标大厅规则没有钉死模式名，按「准备」就成了赌当前卡片")

        panel = next(r for r in states["rules"] if r["state"] == "MODE_PANEL_TARGET_VISIBLE")
        panel_terms = [
            term
            for requirement in panel["requirements"]
            for term in requirement.get("all", ())
        ]
        # The card the runner picks and the lobby it then presses 准备 on have
        # to name the same mode, or it selects one thing and queues another.
        self.assertEqual(set(mode_terms), set(panel_terms))

    def test_turning_off_fill_squad_is_a_toggle_the_lobby_itself_confirms(self) -> None:
        fill = next(c for c in self.capabilities.capabilities if c.id == "lobby-disable-fill")
        # Clicking a checkbox twice puts it back, so this can never be retried
        # blind. Its postcondition is free: with the tick gone the same lobby
        # stops matching the fill rule and falls through to the plain one.
        self.assertEqual(fill.action_class, "toggle")
        self.assertEqual(fill.max_attempts, 1)
        self.assertEqual(fill.allowed_next_states, ("LOBBY_READY_TARGET",))
        self.assertNotEqual(
            self.payload["actions"][fill.action],
            self.payload["actions"]["startMatchClick"],
        )

    def test_a_filled_lobby_unticks_the_box_instead_of_queueing(self) -> None:
        dispatcher = self._dispatcher()
        decision = dispatcher.decide("LOBBY_READY_TARGET_FILL_ON", 1, 0.0)
        self.assertEqual(decision.capability.id, "lobby-disable-fill")
        self.assertEqual(self.capabilities.for_state("LOBBY_READY_TARGET_FILL_ON"), (decision.capability,))

    def test_starting_a_match_is_a_commit_confirmed_by_the_queue_screen(self) -> None:
        start = next(c for c in self.capabilities.capabilities if c.id == "lobby-start-match")
        self.assertEqual(start.action_class, "commit")
        self.assertEqual(start.allowed_next_states, ("LOBBY_QUEUEING",))
        # The button flips within ~100ms of the click, and the only real
        # matchmaking queue on record lasted over 43s. The window has to land
        # inside that, not merely be longer than the transition.
        self.assertLess(start.confirm_ms, 43_000)

    def test_queueing_waits_instead_of_pressing_anything(self) -> None:
        # Cancel is the only thing on that screen, so any capability here would
        # be a way to leave the queue we just joined.
        self.assertEqual(self.capabilities.for_state("LOBBY_QUEUEING"), ())
        self.assertEqual(self._dispatcher().decide("LOBBY_QUEUEING", 1, 0.0).reason, "NO_CAPABILITY")

    def test_ready_is_not_retried_once_the_lobby_is_gone(self) -> None:
        # Legend select and loading name no state, so the click's postcondition
        # is still outstanding when the dropship finally appears. Retrying then
        # would click the lobby button's coordinates inside a live match.
        dispatcher = self._dispatcher()
        dispatcher.note_state("LOBBY_READY_TARGET", 0.0)
        self.assertEqual(dispatcher.decide("LOBBY_READY_TARGET", 1, 0.0).kind, "fire")
        dispatcher.note_state("DROPSHIP_FOLLOWING", 90.0)
        decision = dispatcher.decide("DROPSHIP_FOLLOWING", 2, 90.0)
        self.assertEqual(decision.reason, "NO_CAPABILITY")

    def test_the_jump_waits_out_part_of_the_flight_but_never_all_of_it(self) -> None:
        # Pressing E on sight landed every match on the same spot at the start
        # of the flight path, where the first ring never reaches. The wait has
        # to stay inside the flight: ride past its end and the game force-drops
        # at the far edge, which is the same problem mirrored. The operator
        # puts the whole dropship run at under 30 seconds, so a wait anywhere
        # near half a minute is not "late", it is "never jumped".
        launch = next(c for c in self.capabilities.capabilities if c.id == "dropship-launch")
        self.assertGreater(launch.delay_ms, 0)
        self.assertLess(launch.delay_ms + launch.delay_jitter_ms, 25_000)
        self.assertEqual(self.capabilities.for_state("DROPSHIP_FOLLOWING"), ())
        self.assertIn("DROPSHIP_SOLO_JUMPMASTER", launch.states)

    def test_confirming_a_mode_is_a_commit_that_must_be_verified(self) -> None:
        confirm = next(c for c in self.capabilities.capabilities if c.id == "mode-panel-confirm-hovered")
        self.assertEqual(confirm.action_class, "commit")
        # Both target-lobby readings count: whether 补满 happens to be ticked
        # when the panel closes is unrelated to whether the mode took.
        self.assertEqual(
            set(confirm.allowed_next_states),
            {"LOBBY_READY_TARGET", "LOBBY_READY_TARGET_FILL_ON"},
        )

    def test_melee_repeats_while_alive_and_stops_everywhere_else(self) -> None:
        dispatcher = self._dispatcher()
        first = dispatcher.decide("IN_MATCH_ALIVE", 1, 0.0)
        self.assertEqual(first.capability.action, "meleeScanCode")
        # The tactical ability shares this screen and keeps its own ten second
        # interval, so a decision taken while melee cools down belongs to it.
        second = dispatcher.decide("IN_MATCH_ALIVE", 1, 0.1)
        self.assertEqual(second.capability.action, "tacticalScanCode")
        self.assertEqual(dispatcher.decide("IN_MATCH_ALIVE", 1, 1.0).reason, "PERIODIC_COOLDOWN")
        self.assertEqual(dispatcher.decide("IN_MATCH_ALIVE", 1, 6.0).kind, "fire")

        for state in ("SPECTATING", "POST_MATCH_SUMMARY", "LOBBY_READY_TARGET"):
            with self.subTest(state=state):
                decision = self._dispatcher().decide(state, 1, 0.0)
                self.assertNotEqual(
                    None if decision.capability is None else decision.capability.action,
                    "meleeScanCode",
                )

    def test_spectating_has_no_capability_at_all(self) -> None:
        self.assertEqual(self.capabilities.for_state("SPECTATING"), ())
        self.assertEqual(self._dispatcher().decide("SPECTATING", 1, 0.0).reason, "NO_CAPABILITY")

    def test_every_assumed_key_binding_is_written_down(self) -> None:
        # A wrong scan code and an input that never arrives look identical on
        # screen: the action is sent, nothing happens. Melee was configured as
        # V while the account had it on N, and that cost a whole session to
        # find. Whatever the codes are, the file has to say what they assume.
        bindings = self.payload["_keyBindings"]
        for capability in self.capabilities.capabilities:
            action = self.payload["actions"][capability.action]
            if capability.kind == "key":
                codes = [action]
            elif capability.kind == "sequence":
                codes = [
                    step["scanCode"]
                    for step in action
                    if step["type"] == "tapKey"
                ]
            else:
                continue
            for code in codes:
                with self.subTest(capability=capability.id, scanCode=code):
                    self.assertIn(str(code), bindings)

    def test_post_match_returns_to_the_lobby_rather_than_toggling_matchmaking(self) -> None:
        capability = next(c for c in self.capabilities.capabilities if c.id == "post-match-return-lobby")
        self.assertEqual(capability.kind, "sequence")
        self.assertEqual(
            self.payload["actions"][capability.action],
            [
                {"type": "tapKey", "scanCode": 15, "durationMs": 80},
                {"type": "wait", "durationMs": 1500},
                {"type": "tapKey", "scanCode": 57, "durationMs": 2000},
            ],
        )
        self.assertIn("LOBBY_READY_TARGET", capability.allowed_next_states)


if __name__ == "__main__":
    unittest.main()

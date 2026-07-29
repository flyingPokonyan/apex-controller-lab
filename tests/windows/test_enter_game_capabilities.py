from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capabilities import CapabilityDispatcher, CapabilitySet
from apex_automation.ocr_states import OcrStateDetector


CONFIG = REPOSITORY_ROOT / "windows" / "config" / "enter-game.zh-CN.json"
STATES = REPOSITORY_ROOT / "windows" / "config" / "game-states.zh-CN.json"


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
        known = {rule.state for rule in OcrStateDetector.from_path(object(), STATES).rules}
        # CONTINUE still comes from a template rather than from the dictionary.
        known.add("CONTINUE")
        for capability in self.capabilities.capabilities:
            for state in (*capability.states, *capability.allowed_next_states):
                with self.subTest(capability=capability.id, state=state):
                    self.assertIn(state, known, f"{capability.id} 引用了无法产生的状态 {state}")

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
        self.assertEqual(first.capability.action, "unrankedCardClick")
        dispatcher.confirm_pending("MODE_PANEL_TARGET_HOVERED")
        dispatcher.note_state("MODE_PANEL_TARGET_HOVERED", 1.0)
        second = dispatcher.decide("MODE_PANEL_TARGET_HOVERED", 2, 1.0)
        self.assertEqual(second.capability.action, "unrankedConfirmClick")

    def test_only_the_unranked_lobby_may_press_the_primary_button(self) -> None:
        dispatcher = self._dispatcher()
        decision = dispatcher.decide("LOBBY_READY_UNRANKED", 1, 0.0)
        self.assertEqual(decision.capability.id, "lobby-start-match")

        # One box, three different buttons depending on what it reads. 选择
        # opens the mode panel and 准备 commits to whatever mode the card is
        # showing, so pressing it is only safe where the rule also pins the
        # mode. Everything allowed to click that box is listed here, and a
        # new entry has to justify itself against the label.
        safe_to_click = {"LOBBY_SELECT_REQUIRED": "选择", "LOBBY_READY_UNRANKED": "准备"}
        primary_button = self.payload["actions"]["startMatchClick"]
        for capability in self.capabilities.capabilities:
            if self.payload["actions"].get(capability.action) != primary_button:
                continue
            for state in capability.states:
                with self.subTest(capability=capability.id, state=state):
                    self.assertIn(state, safe_to_click)
                    self.assertIn(safe_to_click[state], self._button_terms(state))

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
        dispatcher.note_state("LOBBY_READY_UNRANKED", 0.0)
        self.assertEqual(dispatcher.decide("LOBBY_READY_UNRANKED", 1, 0.0).kind, "fire")
        dispatcher.note_state("DROPSHIP_FOLLOWING", 90.0)
        decision = dispatcher.decide("DROPSHIP_FOLLOWING", 2, 90.0)
        self.assertEqual(decision.capability.id, "dropship-detach")

    def test_confirming_a_mode_is_a_commit_that_must_be_verified(self) -> None:
        confirm = next(c for c in self.capabilities.capabilities if c.id == "mode-panel-confirm-hovered")
        self.assertEqual(confirm.action_class, "commit")
        self.assertEqual(confirm.allowed_next_states, ("LOBBY_READY_UNRANKED",))

    def test_melee_repeats_while_alive_and_stops_everywhere_else(self) -> None:
        dispatcher = self._dispatcher()
        first = dispatcher.decide("IN_MATCH_ALIVE", 1, 0.0)
        self.assertEqual(first.capability.action, "meleeScanCode")
        self.assertEqual(dispatcher.decide("IN_MATCH_ALIVE", 1, 1.0).reason, "PERIODIC_COOLDOWN")
        self.assertEqual(dispatcher.decide("IN_MATCH_ALIVE", 1, 4.0).kind, "fire")

        for state in ("SPECTATING", "POST_MATCH_SUMMARY", "LOBBY_READY_UNRANKED"):
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
            if capability.kind != "key":
                continue
            code = self.payload["actions"][capability.action]
            with self.subTest(capability=capability.id, scanCode=code):
                self.assertIn(str(code), bindings)

    def test_post_match_returns_to_the_lobby_rather_than_toggling_matchmaking(self) -> None:
        capability = next(c for c in self.capabilities.capabilities if c.id == "post-match-return-lobby")
        self.assertEqual(self.payload["actions"][capability.action], 15)  # TAB
        self.assertIn("LOBBY_READY_UNRANKED", capability.allowed_next_states)


if __name__ == "__main__":
    unittest.main()

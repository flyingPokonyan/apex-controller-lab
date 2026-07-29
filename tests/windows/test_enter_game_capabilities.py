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

    def test_a_training_lobby_changes_mode_instead_of_pressing_ready(self) -> None:
        # Pressing ready here starts the tutorial, which is the first step of
        # the lobby/tutorial loop that the ordered model ran forever.
        dispatcher = self._dispatcher()
        decision = dispatcher.decide("LOBBY_READY_TRAINING", 1, 0.0)
        self.assertEqual(decision.capability.id, "lobby-open-mode-panel")

    def test_mode_selection_takes_the_card_then_the_confirm_button(self) -> None:
        dispatcher = self._dispatcher()
        first = dispatcher.decide("MODE_PANEL_TARGET_VISIBLE", 1, 0.0)
        self.assertEqual(first.capability.action, "unrankedCardClick")
        dispatcher.confirm_pending("MODE_PANEL_TARGET_SELECTED")
        dispatcher.note_state("MODE_PANEL_TARGET_SELECTED", 1.0)
        second = dispatcher.decide("MODE_PANEL_TARGET_SELECTED", 2, 1.0)
        self.assertEqual(second.capability.action, "unrankedConfirmClick")

    def test_confirming_a_mode_is_a_commit_that_must_be_verified(self) -> None:
        confirm = next(c for c in self.capabilities.capabilities if c.id == "mode-panel-confirm-target")
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

    def test_post_match_returns_to_the_lobby_rather_than_toggling_matchmaking(self) -> None:
        capability = next(c for c in self.capabilities.capabilities if c.id == "post-match-return-lobby")
        self.assertEqual(self.payload["actions"][capability.action], 15)  # TAB
        self.assertIn("LOBBY_READY_UNRANKED", capability.allowed_next_states)


if __name__ == "__main__":
    unittest.main()

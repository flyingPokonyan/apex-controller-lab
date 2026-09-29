from __future__ import annotations

from pathlib import Path
import sys
import unittest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capabilities import Capability, Decision, PendingAction
from apex_automation.intro_unlock import (
    CONFIRM_LEAVE,
    ENTER_RANGE,
    IntroUnlock,
    LEAVE_RANGE_ESC,
    RETURN_LOBBY,
)


def fire(capability_id: str, attempt: int = 1) -> Decision:
    capability = Capability(
        id=capability_id,
        priority=1,
        states=("LOBBY_READY_TRAINING",),
        action="modeCardClick",
        kind="click",
        action_class="idempotent",
        confirm_ms=1000,
        max_attempts=3,
    )
    return Decision("fire", capability=capability, attempt=attempt, reason="ON_STATE")


class IntroUnlockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.intro = IntroUnlock()

    def latch(self) -> None:
        self.intro.observe(level=1, leased=True, state="LOBBY_READY_TRAINING", rounds_returned=0)

    def test_a_level_one_lease_probes_the_mode_card_once_then_enters_the_range(self) -> None:
        self.latch()
        self.assertIsNone(self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode"), 0, None))
        waiting = Decision("wait", reason="AWAITING_POSTCONDITION")
        self.assertIsNone(self.intro.command("LOBBY_READY_TRAINING", waiting, 1, None))

        command = self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 2), 4, None)

        self.assertIsNotNone(command)
        assert command is not None
        self.assertEqual(command.capability, ENTER_RANGE)
        self.assertEqual(command.undo_id, "lobby-change-mode")
        self.assertEqual(self.intro.phase, "leave_range")

    def test_esc_waits_out_the_load_and_does_not_repeat_immediately(self) -> None:
        self.latch()
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode"), 0, None)
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 2), 1, None)

        self.assertIsNone(self.intro.command(None, Decision("wait", reason="NO_STATE"), 2, None))
        first = self.intro.command(None, Decision("wait", reason="NO_STATE"), 20, None)
        assert first is not None
        self.assertEqual(first.capability, LEAVE_RANGE_ESC)
        self.assertIsNone(self.intro.command(None, Decision("wait", reason="NO_STATE"), 21, None))

    def test_esc_does_not_fire_again_while_the_leave_menu_is_changing(self) -> None:
        # 20260928 Kit07Kit: the second Esc opened 设置/返回大厅, the click on
        # 返回大厅 was sent, and two seconds later a third Esc closed the menu
        # because the retry deadline from the previous Esc had just expired.
        self.latch()
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode"), 0, None)
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 2), 1, None)
        opened = self.intro.command(None, Decision("wait", reason="NO_STATE"), 20, None)
        assert opened is not None
        self.assertEqual(opened.capability, LEAVE_RANGE_ESC)

        self.assertIsNone(self.intro.command("LEAVE_MATCH_MENU", fire(RETURN_LOBBY.id), 41, None))
        self.assertIsNone(self.intro.command(None, Decision("wait", reason="NO_STATE"), 43, None))
        self.assertEqual(self.intro._esc_sent, 1)

    def test_the_leave_menu_is_left_to_its_own_capability(self) -> None:
        self.latch()
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode"), 0, None)
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 2), 1, None)

        self.assertIsNone(self.intro.command("LEAVE_MATCH_MENU", fire(RETURN_LOBBY.id), 30, None))
        forced = self.intro.command("LEAVE_MATCH_CONFIRM", Decision("wait", reason="NO_CAPABILITY"), 30, None)
        assert forced is not None
        self.assertEqual(forced.capability, CONFIRM_LEAVE)

    def test_a_leave_confirmation_is_not_sent_again_while_it_is_still_landing(self) -> None:
        # 20260929 BrianBuckley: the dispatcher had already clicked 是 and was
        # waiting out the 8s confirm. Intro treated that wait as "not clicked"
        # and sent a second 是 3.6s later.
        self.latch()
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode"), 0, None)
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 2), 1, None)
        pending = PendingAction(
            capability=CONFIRM_LEAVE,
            origin_state="LEAVE_MATCH_CONFIRM",
            origin_observation_version=1,
            attempt=1,
            retry_at=38.0,
        )
        waiting = Decision(
            "wait",
            capability=CONFIRM_LEAVE,
            reason="AWAITING_POSTCONDITION",
        )

        self.assertIsNone(self.intro.command("LEAVE_MATCH_CONFIRM", waiting, 34.0, pending))
        retry = self.intro.command("LEAVE_MATCH_CONFIRM", waiting, 38.0, pending)
        assert retry is not None
        self.assertEqual(retry.capability, CONFIRM_LEAVE)

    def test_a_locked_bot_card_tries_the_welcome_title_and_does_not_queue_an_unpinned_lobby(self) -> None:
        self.latch()
        self.intro.observe(level=1, leased=True, state="LOBBY_READY_OTHER", rounds_returned=0)
        self.assertEqual(self.intro.phase, "welcome_match")
        # No welcome title on screen: the normal dispatcher may try the bot card.
        self.assertIsNone(self.intro.command("LOBBY_READY_OTHER", fire("lobby-change-mode"), 0, None))

        exhausted = Decision(
            "pause",
            capability=fire("mode-panel-focus-target", 3).capability,
            attempt=3,
            reason="ATTEMPTS_EXHAUSTED",
        )
        clicked = self.intro.command("MODE_PANEL_TARGET_VISIBLE", exhausted, 5, None)
        assert clicked is not None
        self.assertEqual(clicked.capability.action, "introWelcomeModeClick")

        # OTHER is not pinned to a mode. 准备 would queue whatever the card shows.
        self.assertIsNone(self.intro.command("LOBBY_READY_OTHER", fire("lobby-change-mode"), 8, None))

    def test_a_recognised_welcome_screen_is_not_overridden(self) -> None:
        self.latch()
        self.intro.observe(level=1, leased=True, state="LOBBY_READY_WELCOME", rounds_returned=0)
        self.assertEqual(self.intro.phase, "welcome_match")
        self.assertIsNone(
            self.intro.command("LOBBY_READY_WELCOME", fire("lobby-start-welcome-match"), 1, None)
        )
        self.assertIsNone(
            self.intro.command("MODE_PANEL_WELCOME_VISIBLE", fire("mode-panel-select-welcome"), 2, None)
        )
        # Welcome was already on screen, so a later training frame must not
        # be forced into the firing range.
        self.assertIsNone(
            self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 2), 3, None)
        )
        self.assertEqual(self.intro.phase, "welcome_match")

    def test_another_welcome_match_is_allowed_until_bots_are_actually_selected(self) -> None:
        self.latch()
        self.intro.observe(level=1, leased=True, state="LOBBY_READY_OTHER", rounds_returned=0)
        self.intro._bots_failed = True
        self.intro.command("LOBBY_READY_OTHER", fire("lobby-change-mode"), 0, None)
        self.intro.observe(level=2, leased=True, state="LOBBY_QUEUEING", rounds_returned=0)
        self.intro.observe(level=2, leased=True, state="LOBBY_READY_OTHER", rounds_returned=1)

        self.assertEqual(self.intro.phase, "welcome_match")
        self.assertIsNone(self.intro.command("LOBBY_READY_OTHER", fire("lobby-change-mode"), 10, None))
        self.intro.observe(level=2, leased=True, state="LOBBY_READY_TARGET", rounds_returned=1)
        self.assertEqual(self.intro.phase, "done")

    def test_an_already_selected_bot_lobby_skips_the_welcome_match(self) -> None:
        self.latch()
        self.intro.observe(level=1, leased=True, state="LOBBY_READY_TARGET", rounds_returned=0)
        self.assertEqual(self.intro.phase, "done")

    def test_level_eight_and_an_unleased_session_do_not_enter(self) -> None:
        self.intro.observe(level=8, leased=True, state="LOBBY_READY_TRAINING", rounds_returned=0)
        self.intro.observe(level=1, leased=False, state="LOBBY_READY_TRAINING", rounds_returned=0)
        self.assertEqual(self.intro.phase, "inactive")
        self.assertIsNone(
            self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 3), 5, None)
        )

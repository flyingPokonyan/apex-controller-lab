from __future__ import annotations

from pathlib import Path
import sys
import unittest

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capabilities import Capability, Decision
from apex_automation.intro_unlock import (
    CONFIRM_LEAVE,
    ENTER_RANGE,
    IntroUnlock,
    LEAVE_RANGE_ESC,
    RETURN_LOBBY,
    START_WELCOME,
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

    def test_the_leave_menu_is_left_to_its_own_capability(self) -> None:
        self.latch()
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode"), 0, None)
        self.intro.command("LOBBY_READY_TRAINING", fire("lobby-change-mode", 2), 1, None)

        self.assertIsNone(self.intro.command("LEAVE_MATCH_MENU", fire(RETURN_LOBBY.id), 30, None))
        forced = self.intro.command("LEAVE_MATCH_CONFIRM", Decision("wait", reason="NO_CAPABILITY"), 30, None)
        assert forced is not None
        self.assertEqual(forced.capability, CONFIRM_LEAVE)

    def test_a_locked_bot_card_closes_the_panel_and_queues_the_current_mode(self) -> None:
        self.latch()
        self.intro.observe(level=1, leased=True, state="LOBBY_READY_OTHER", rounds_returned=0)
        self.assertEqual(self.intro.phase, "welcome_match")
        # The first visit still tries the bot card. 准备 is only the fallback.
        self.assertIsNone(self.intro.command("LOBBY_READY_OTHER", fire("lobby-change-mode"), 0, None))

        exhausted = Decision(
            "pause",
            capability=fire("mode-panel-focus-target", 3).capability,
            attempt=3,
            reason="ATTEMPTS_EXHAUSTED",
        )
        closed = self.intro.command("MODE_PANEL_TARGET_VISIBLE", exhausted, 5, None)
        assert closed is not None
        self.assertEqual(closed.capability.action, "escapeScanCode")

        queued = self.intro.command("LOBBY_READY_OTHER", fire("lobby-change-mode"), 8, None)
        assert queued is not None
        self.assertEqual(queued.capability, START_WELCOME)

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

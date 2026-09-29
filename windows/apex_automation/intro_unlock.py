"""Level-1 leases still clear the firing range when the mode card will not open.

迎新赛 is not a level gate and this module does not choose it. While the lobby
card or the mode-panel title reads 迎新赛, the normal capabilities queue that
playlist. Once the title changes, those same capabilities select 机器人.
A managed lease whose lobby reads level 1 still gets one chance to open the
mode card. If that click does not leave the training lobby, press 准备, and
once the firing range is up press Esc, then 返回大厅, then 是. Seeing 迎新赛
leaves that path: do not spend bot-card clicks, and do not press 准备 on a
lobby that is only "some other mode".
"""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import Capability, Decision, PendingAction


TRAINING_LOBBY = "LOBBY_READY_TRAINING"
QUEUEING = "LOBBY_QUEUEING"
WELCOME_LOBBY = "LOBBY_READY_OTHER"
WELCOME_READY = "LOBBY_READY_WELCOME"
WELCOME_PANEL = "MODE_PANEL_WELCOME_VISIBLE"
WELCOME_STATES = frozenset({WELCOME_READY, WELCOME_PANEL})
SELECT_LOBBY = "LOBBY_SELECT_REQUIRED"
LEAVE_MENU = "LEAVE_MATCH_MENU"
LEAVE_CONFIRM = "LEAVE_MATCH_CONFIRM"
TARGET_LOBBIES = frozenset({"LOBBY_READY_TARGET", "LOBBY_READY_TARGET_FILL_ON"})
PANEL_STATES = frozenset({"MODE_PANEL_TARGET_VISIBLE", "MODE_PANEL_TARGET_HOVERED"})
MODE_CARD = "lobby-change-mode"

# The firing range does not appear the frame 准备 is pressed. Esc before the
# load finishes only hits the lobby or the short training queue.
ENTER_RANGE_ESC_DELAY_S = 8.0
# Overlay OCR on an unnamed screen retries slowly. A second Esc inside that
# window closes the leave menu the first Esc just opened.
ESC_RETRY_S = 20.0
ESC_ATTEMPTS = 3


def _capability(
    capability_id: str,
    action: str,
    kind: str,
    action_class: str,
    *,
    confirm_ms: int,
    max_attempts: int,
    allowed_next_states: tuple[str, ...],
    states: tuple[str, ...],
) -> Capability:
    return Capability(
        id=capability_id,
        priority=1,
        states=states,
        action=action,
        kind=kind,
        action_class=action_class,  # type: ignore[arg-type]
        confirm_ms=confirm_ms,
        max_attempts=max_attempts,
        allowed_next_states=allowed_next_states,
    )


ENTER_RANGE = _capability(
    "intro-enter-firing-range",
    "startMatchClick",
    "click",
    "commit",
    confirm_ms=8000,
    max_attempts=1,
    allowed_next_states=(QUEUEING,),
    states=(TRAINING_LOBBY,),
)
LEAVE_RANGE_ESC = _capability(
    "intro-leave-firing-range",
    "escapeScanCode",
    "key",
    "idempotent",
    confirm_ms=int(ESC_RETRY_S * 1000),
    max_attempts=ESC_ATTEMPTS,
    allowed_next_states=(LEAVE_MENU, LEAVE_CONFIRM, WELCOME_LOBBY, WELCOME_READY, SELECT_LOBBY, *TARGET_LOBBIES),
    states=("INTRO_RANGE",),
)
RETURN_LOBBY = _capability(
    "intro-return-to-lobby",
    "introReturnLobbyClick",
    "clickText",
    "idempotent",
    confirm_ms=2500,
    max_attempts=2,
    allowed_next_states=(LEAVE_CONFIRM, WELCOME_LOBBY, WELCOME_READY, SELECT_LOBBY, TRAINING_LOBBY, *TARGET_LOBBIES),
    states=(LEAVE_MENU,),
)
CONFIRM_LEAVE = _capability(
    "intro-confirm-leave",
    "introConfirmLeaveClick",
    "clickText",
    "idempotent",
    confirm_ms=8000,
    max_attempts=2,
    allowed_next_states=(WELCOME_LOBBY, WELCOME_READY, SELECT_LOBBY, TRAINING_LOBBY, *TARGET_LOBBIES),
    states=(LEAVE_CONFIRM,),
)
SELECT_WELCOME = _capability(
    "intro-select-welcome-mode",
    "introWelcomeModeClick",
    "clickText",
    "commit",
    confirm_ms=3000,
    max_attempts=2,
    allowed_next_states=(WELCOME_READY, QUEUEING),
    states=tuple(PANEL_STATES),
)
FOCUS_TARGET = "mode-panel-focus-target"


@dataclass(frozen=True)
class IntroCommand:
    """A click or key the intro sequence sends instead of the normal decision."""

    capability: Capability
    undo_id: str | None = None


class IntroUnlock:
    """Session memory for the firing-range gate and locked bot-mode fallback."""

    def __init__(self) -> None:
        self.phase = "inactive"
        self._latched = False
        self._probe_sent = False
        self._range_click_sent = False
        self._esc_not_before = 0.0
        self._esc_sent = 0
        self._menu_seen = False
        self._welcome_click_sent = False
        self._welcome_clicks = 0
        self._bots_failed = False
        self._panel_closes = 0
        self._rounds_seen = 0
        self._transitions: list[str] = []

    def consume_transitions(self) -> list[str]:
        found = self._transitions
        self._transitions = []
        return found

    def observe(
        self,
        *,
        level: int | None,
        leased: bool,
        state: str | None,
        rounds_returned: int,
    ) -> None:
        if self.phase == "done":
            return
        if not self._latched:
            if not leased or level is None:
                return
            if level == 1:
                self._latched = True
                self._set("clear_range")
            elif state in {WELCOME_LOBBY, SELECT_LOBBY} or state in PANEL_STATES:
                # Level 2 and up still land here while the bot card is locked.
                # A training lobby at those levels is a normal mode change.
                self._latched = True
                self._set("welcome_match")
            else:
                return

        if rounds_returned > self._rounds_seen:
            self._rounds_seen = rounds_returned
            if self.phase == "welcome_match":
                # The card may have unlocked during the match. The normal rules
                # pick 迎新赛 or 机器人 from the title; this only resets the
                # one exact-match recovery.
                self._bots_failed = False
                self._welcome_clicks = 0
                self._welcome_click_sent = False
                self._panel_closes = 0

        if self.phase == "clear_range":
            if state in TARGET_LOBBIES:
                self._set("done")
            elif state in WELCOME_STATES or state in PANEL_STATES or state in {WELCOME_LOBBY, SELECT_LOBBY}:
                self._set("welcome_match")
            return

        if self.phase == "leave_range":
            if state == QUEUEING or state is None or state in {LEAVE_MENU, LEAVE_CONFIRM}:
                return
            if state in TARGET_LOBBIES:
                self._set("done")
            elif state in WELCOME_STATES or state in {WELCOME_LOBBY, SELECT_LOBBY} or state in PANEL_STATES:
                self._set("welcome_match")
            return

        if self.phase == "welcome_match":
            if state == QUEUEING and self._welcome_click_sent:
                self._welcome_clicks = 0
                self._welcome_click_sent = False
                self._panel_closes = 0
            elif state in TARGET_LOBBIES:
                self._set("done")

    def command(
        self,
        state: str | None,
        decision: Decision,
        now: float,
        pending: PendingAction | None,
    ) -> IntroCommand | None:
        if self.phase == "clear_range" and state == TRAINING_LOBBY:
            return self._clear_range(decision, now)
        if self.phase == "leave_range":
            return self._leave_range(state, decision, now, pending)
        if self.phase == "welcome_match":
            return self._welcome(state, decision)
        return None

    def _clear_range(self, decision: Decision, now: float) -> IntroCommand | None:
        if (
            decision.kind == "fire"
            and decision.capability is not None
            and decision.capability.id == MODE_CARD
            and decision.attempt == 1
            and not self._probe_sent
        ):
            self._probe_sent = True
            return None
        if decision.reason == "AWAITING_POSTCONDITION":
            return None
        if not self._probe_sent or self._range_click_sent:
            return None
        self._range_click_sent = True
        self._esc_not_before = now + ENTER_RANGE_ESC_DELAY_S
        self._set("leave_range")
        undo = decision.capability.id if decision.kind == "fire" and decision.capability else None
        return IntroCommand(ENTER_RANGE, undo)

    def _leave_range(
        self,
        state: str | None,
        decision: Decision,
        now: float,
        pending: PendingAction | None,
    ) -> IntroCommand | None:
        if state in {LEAVE_CONFIRM, LEAVE_MENU}:
            # Esc on this menu is "返回", and on the next dialog it is "取消".
            # Another Esc while the page is changing closes the leave we just
            # opened. Hold it until the click has had time to land.
            self._menu_seen = True
            self._esc_not_before = max(self._esc_not_before, now + ESC_RETRY_S)
        if state == LEAVE_CONFIRM:
            if self._click_already_out(decision, pending, CONFIRM_LEAVE.id, now):
                return None
            return self._instead(CONFIRM_LEAVE, decision)
        if state == LEAVE_MENU:
            if self._click_already_out(decision, pending, RETURN_LOBBY.id, now):
                return None
            return self._instead(RETURN_LOBBY, decision)
        if state is not None:
            return None
        if pending is not None and pending.capability.id in {RETURN_LOBBY.id, CONFIRM_LEAVE.id, LEAVE_RANGE_ESC.id} and now < pending.retry_at:
            return None
        if now < self._esc_not_before or self._esc_sent >= ESC_ATTEMPTS:
            return None
        self._esc_sent += 1
        self._esc_not_before = now + ESC_RETRY_S
        return IntroCommand(LEAVE_RANGE_ESC)

    def _welcome(self, state: str | None, decision: Decision) -> IntroCommand | None:
        if decision.reason == "AWAITING_POSTCONDITION":
            return None
        # The title is still 迎新赛. The normal capability presses 准备 or
        # clicks that card. Do not open the bot card first.
        if state in WELCOME_STATES:
            return None
        if (
            state in PANEL_STATES
            and decision.reason == "ATTEMPTS_EXHAUSTED"
            and decision.capability is not None
            and decision.capability.id == FOCUS_TARGET
            and self._panel_closes < SELECT_WELCOME.max_attempts
        ):
            # The welcome-title rule missed, and the bot card would not select.
            # One exact-match click looks for the title itself. It does not
            # hit the lock hint, and it does not press 准备 on an unpinned lobby.
            self._bots_failed = True
            self._panel_closes += 1
            return self._instead(SELECT_WELCOME, decision)
        return None

    @staticmethod
    def _already(decision: Decision, capability_id: str) -> bool:
        return (
            decision.kind == "fire"
            and decision.capability is not None
            and decision.capability.id == capability_id
        )

    @staticmethod
    def _click_already_out(
        decision: Decision,
        pending: PendingAction | None,
        capability_id: str,
        now: float,
    ) -> bool:
        """True when this click is in flight and must not be sent again.

        The dispatcher waits out confirmMs before a retry. During that wait
        its decision is no longer "fire", and treating that as "never clicked"
        sends a second 是 while the first one is still landing. 20260929
        BrianBuckley did that 3.6s into an 8s confirm, then Apex left the
        foreground and the lease sat on the firing range for two hours.
        """
        if IntroUnlock._already(decision, capability_id):
            return True
        return (
            pending is not None
            and pending.capability.id == capability_id
            and now < pending.retry_at
        )

    def _instead(self, capability: Capability, decision: Decision) -> IntroCommand:
        undo = decision.capability.id if decision.kind == "fire" and decision.capability else None
        return IntroCommand(capability, undo)

    def _set(self, phase: str) -> None:
        if phase == self.phase:
            return
        self.phase = phase
        self._transitions.append(phase)

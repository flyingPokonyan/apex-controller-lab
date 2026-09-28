"""New leased accounts cannot pick 机器人匹配 until the game unlocks it.

The copy on the lobby changes between seasons, so this does not look for the
banner. A managed lease whose lobby reads level 1 gets one chance to open the
mode card. If that click does not leave the training lobby, the card is locked:
press 准备, and once the firing range is up press Esc, then 返回大厅, then 是.
Afterwards, and for any later level while the bot card still will not select,
try the card once per lobby visit. If it does not take, close the panel and
queue the mode already on the lobby card — on these accounts that is 迎新赛 —
instead of ending the session. Normal mode selection resumes only when the
lobby is actually the bot playlist.
"""

from __future__ import annotations

from dataclasses import dataclass

from .capabilities import Capability, Decision, PendingAction


TRAINING_LOBBY = "LOBBY_READY_TRAINING"
QUEUEING = "LOBBY_QUEUEING"
WELCOME_LOBBY = "LOBBY_READY_OTHER"
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
    allowed_next_states=(LEAVE_MENU, LEAVE_CONFIRM, WELCOME_LOBBY, SELECT_LOBBY, *TARGET_LOBBIES),
    states=("INTRO_RANGE",),
)
RETURN_LOBBY = _capability(
    "intro-return-to-lobby",
    "introReturnLobbyClick",
    "clickText",
    "idempotent",
    confirm_ms=2500,
    max_attempts=2,
    allowed_next_states=(LEAVE_CONFIRM, WELCOME_LOBBY, SELECT_LOBBY, TRAINING_LOBBY, *TARGET_LOBBIES),
    states=(LEAVE_MENU,),
)
CONFIRM_LEAVE = _capability(
    "intro-confirm-leave",
    "introConfirmLeaveClick",
    "clickText",
    "idempotent",
    confirm_ms=8000,
    max_attempts=2,
    allowed_next_states=(WELCOME_LOBBY, SELECT_LOBBY, TRAINING_LOBBY, *TARGET_LOBBIES),
    states=(LEAVE_CONFIRM,),
)
START_WELCOME = _capability(
    "intro-start-welcome-match",
    "startMatchClick",
    "click",
    "commit",
    confirm_ms=3000,
    max_attempts=2,
    allowed_next_states=(QUEUEING,),
    states=(WELCOME_LOBBY,),
)
SELECT_WELCOME = _capability(
    "intro-select-welcome-mode",
    "introWelcomeModeClick",
    "clickText",
    "idempotent",
    confirm_ms=3000,
    max_attempts=2,
    allowed_next_states=(WELCOME_LOBBY, QUEUEING, SELECT_LOBBY),
    states=tuple(PANEL_STATES),
)
CLOSE_LOCKED_PANEL = _capability(
    "intro-close-locked-mode-panel",
    "escapeScanCode",
    "key",
    "idempotent",
    confirm_ms=2500,
    max_attempts=2,
    allowed_next_states=(WELCOME_LOBBY, SELECT_LOBBY, *TARGET_LOBBIES),
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
                # The card may have unlocked during the match. Try it once
                # more on the next lobby visit, and queue 迎新赛 again if not.
                self._bots_failed = False
                self._welcome_clicks = 0
                self._welcome_click_sent = False
                self._panel_closes = 0

        if self.phase == "clear_range":
            if state in TARGET_LOBBIES:
                self._set("done")
            elif state in PANEL_STATES or state in {WELCOME_LOBBY, SELECT_LOBBY}:
                self._set("welcome_match")
            return

        if self.phase == "leave_range":
            if state == QUEUEING or state is None or state in {LEAVE_MENU, LEAVE_CONFIRM}:
                return
            if state in TARGET_LOBBIES:
                self._set("done")
            elif state in {WELCOME_LOBBY, SELECT_LOBBY} or state in PANEL_STATES:
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
            return None if self._already(decision, CONFIRM_LEAVE.id) else self._instead(CONFIRM_LEAVE, decision)
        if state == LEAVE_MENU:
            return None if self._already(decision, RETURN_LOBBY.id) else self._instead(RETURN_LOBBY, decision)
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
        if (
            state in PANEL_STATES
            and decision.reason == "ATTEMPTS_EXHAUSTED"
            and decision.capability is not None
            and decision.capability.id == FOCUS_TARGET
        ):
            # Three clicks on the bot card did nothing: it is still locked.
            # Close the panel and queue the mode already selected underneath.
            self._bots_failed = True
            if self._panel_closes < CLOSE_LOCKED_PANEL.max_attempts:
                self._panel_closes += 1
                return IntroCommand(CLOSE_LOCKED_PANEL)
            return None
        if state == WELCOME_LOBBY and self._bots_failed and self._welcome_clicks < START_WELCOME.max_attempts:
            self._welcome_clicks += 1
            self._welcome_click_sent = True
            return self._instead(START_WELCOME, decision)
        if state in PANEL_STATES and self._bots_failed and self._panel_closes < SELECT_WELCOME.max_attempts:
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

    def _instead(self, capability: Capability, decision: Decision) -> IntroCommand:
        undo = decision.capability.id if decision.kind == "fire" and decision.capability else None
        return IntroCommand(capability, undo)

    def _set(self, phase: str) -> None:
        if phase == self.phase:
            return
        self.phase = phase
        self._transitions.append(phase)

from __future__ import annotations

from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.capabilities import (
    Capability,
    CapabilityDispatcher,
    CapabilityEvidence,
    CapabilitySet,
)


def click(cap_id: str, state: str, **kwargs) -> Capability:
    payload = {
        "id": cap_id,
        "priority": 50,
        "states": (state,),
        "action": f"{cap_id}Click",
        "kind": "click",
        "action_class": "idempotent",
    }
    payload.update(kwargs)
    return Capability(**payload)  # type: ignore[arg-type]


class CapabilityValidationTest(unittest.TestCase):
    def test_toggle_may_never_be_retried(self) -> None:
        with self.assertRaisesRegex(ValueError, "toggle 动作只允许一次尝试"):
            click("detach", "DROPSHIP_FOLLOWING", action_class="toggle", max_attempts=2,
                  allowed_next_states=("LAUNCH_READY",))

    def test_toggle_requires_positive_postcondition(self) -> None:
        with self.assertRaisesRegex(ValueError, "toggle 动作必须声明正向后置画面"):
            click("detach", "DROPSHIP_FOLLOWING", action_class="toggle")

    def test_commit_requires_postcondition_and_a_tight_budget(self) -> None:
        with self.assertRaisesRegex(ValueError, "commit 动作必须声明正向后置画面"):
            click("ready", "LOBBY_READY", action_class="commit")
        with self.assertRaisesRegex(ValueError, "尝试上限必须为 1 或 2"):
            click("ready", "LOBBY_READY", action_class="commit", max_attempts=3,
                  allowed_next_states=("LEGEND_SELECT",))

    def test_periodic_must_be_idempotent(self) -> None:
        with self.assertRaisesRegex(ValueError, "周期触发只允许 idempotent 动作"):
            Capability(
                id="melee", priority=10, states=("IN_MATCH",), action="meleeKey",
                kind="key", action_class="commit", trigger="periodic",
                min_interval_ms=1000, max_interval_ms=5000,
                allowed_next_states=("IN_MATCH",),
            )

    def test_periodic_requires_a_sane_interval_range(self) -> None:
        with self.assertRaisesRegex(ValueError, "间隔区间非法"):
            Capability(
                id="melee", priority=10, states=("IN_MATCH",), action="meleeKey",
                kind="key", action_class="idempotent", trigger="periodic",
                min_interval_ms=5000, max_interval_ms=1000,
            )

    def test_on_state_capability_rejects_interval_fields(self) -> None:
        with self.assertRaisesRegex(ValueError, "状态触发不应声明周期间隔"):
            click("continue", "CONTINUE", min_interval_ms=1000, max_interval_ms=2000)

    def test_duplicate_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "能力 id 重复"):
            CapabilitySet([click("a", "CONTINUE"), click("a", "LOBBY_READY")])

    def test_a_fallback_must_name_a_real_higher_priority_parent(self) -> None:
        evidence = CapabilityEvidence(region="bottomLeft", all_terms=("esc", "返回"))
        fallback = Capability(
            id="back",
            priority=40,
            states=("PROMPT",),
            action="escapeScanCode",
            kind="key",
            action_class="idempotent",
            max_attempts=1,
            fallback_for="missing",
            evidence=evidence,
        )
        with self.assertRaisesRegex(ValueError, "不存在的能力"):
            CapabilitySet([fallback])

        parent = Capability(
            id="continue",
            priority=30,
            states=("PROMPT",),
            action="spaceScanCode",
            kind="key",
            action_class="idempotent",
        )
        fallback = Capability(
            id="back",
            priority=40,
            states=("PROMPT",),
            action="escapeScanCode",
            kind="key",
            action_class="idempotent",
            max_attempts=1,
            fallback_for="continue",
            evidence=evidence,
        )
        with self.assertRaisesRegex(ValueError, "优先级必须低于"):
            CapabilitySet([parent, fallback])

    def test_a_fallback_parent_must_be_on_state(self) -> None:
        parent = Capability(
            id="periodic",
            priority=50,
            states=("PROMPT",),
            action="spaceScanCode",
            kind="key",
            action_class="idempotent",
            trigger="periodic",
            min_interval_ms=1000,
            max_interval_ms=2000,
        )
        fallback = Capability(
            id="back",
            priority=40,
            states=("PROMPT",),
            action="escapeScanCode",
            kind="key",
            action_class="idempotent",
            max_attempts=1,
            fallback_for="periodic",
            evidence=CapabilityEvidence(
                region="bottomLeft",
                all_terms=("esc", "返回"),
            ),
        )

        with self.assertRaisesRegex(ValueError, "主能力必须是 onState"):
            CapabilitySet([parent, fallback])


class DispatcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0

    def _dispatcher(self, *capabilities: Capability, **kwargs) -> CapabilityDispatcher:
        kwargs.setdefault("jitter", lambda low, high: (low + high) // 2)
        return CapabilityDispatcher(CapabilitySet(capabilities), **kwargs)

    def test_highest_priority_capability_wins_within_one_frame(self) -> None:
        low = click("mode", "LOBBY", priority=10)
        high = click("popup", "LOBBY", priority=90)
        dispatcher = self._dispatcher(low, high)

        decision = dispatcher.decide("LOBBY", 1, self.now)
        self.assertEqual(decision.kind, "fire")
        self.assertEqual(decision.capability.id, "popup")

    def test_an_explicit_fallback_runs_once_after_the_parent_exhausts(self) -> None:
        primary = Capability(
            id="space",
            priority=80,
            states=("PROMPT",),
            action="spaceScanCode",
            kind="key",
            action_class="idempotent",
            confirm_ms=1000,
            max_attempts=2,
        )
        fallback = Capability(
            id="escape",
            priority=70,
            states=("PROMPT",),
            action="escapeScanCode",
            kind="key",
            action_class="idempotent",
            confirm_ms=1000,
            max_attempts=1,
            fallback_for="space",
            evidence=CapabilityEvidence(
                region="bottomLeft",
                all_terms=("esc", "返回"),
            ),
        )
        dispatcher = self._dispatcher(primary, fallback)
        dispatcher.note_state("PROMPT", 0.0)

        first = dispatcher.decide("PROMPT", 1, 0.0)
        second = dispatcher.decide("PROMPT", 1, 1.1)
        third = dispatcher.decide("PROMPT", 1, 2.2)
        exhausted = dispatcher.decide("PROMPT", 1, 3.3)

        self.assertEqual(first.capability.id, "space")
        self.assertEqual(second.capability.id, "space")
        self.assertEqual(third.capability.id, "escape")
        self.assertEqual(third.reason, "FALLBACK_AFTER_EXHAUSTED")
        self.assertEqual(exhausted.kind, "pause")
        self.assertEqual(exhausted.capability.id, "escape")
        self.assertEqual(exhausted.reason, "ATTEMPTS_EXHAUSTED")

    def test_unknown_state_never_fires_anything(self) -> None:
        dispatcher = self._dispatcher(click("continue", "CONTINUE"))
        self.assertEqual(dispatcher.decide(None, 1, self.now).kind, "wait")
        self.assertEqual(dispatcher.decide("SOMETHING_NEW", 1, self.now).reason, "NO_CAPABILITY")

    def test_pending_action_blocks_a_second_fire_until_the_window_expires(self) -> None:
        dispatcher = self._dispatcher(click("continue", "CONTINUE", confirm_ms=2000, max_attempts=2))

        self.assertEqual(dispatcher.decide("CONTINUE", 1, 0.0).kind, "fire")
        self.assertEqual(dispatcher.decide("CONTINUE", 1, 1.0).reason, "AWAITING_POSTCONDITION")
        self.assertEqual(dispatcher.decide("CONTINUE", 1, 2.5).kind, "fire")

    def test_a_pending_retry_is_dropped_once_the_screen_has_moved_on(self) -> None:
        ready = click("ready", "LOBBY", action_class="commit", confirm_ms=2000,
                      max_attempts=2, allowed_next_states=("QUEUEING",))
        melee = click("melee", "IN_MATCH")
        dispatcher = self._dispatcher(ready, melee)

        self.assertEqual(dispatcher.decide("LOBBY", 1, 0.0).kind, "fire")
        # The screens in between name no state, so the postcondition never got
        # settled either way and the window expires far from the lobby.
        self.assertEqual(dispatcher.decide(None, 1, 1.0).reason, "NO_STATE")
        decision = dispatcher.decide("IN_MATCH", 2, 90.0)
        self.assertEqual(decision.capability.id, "melee")
        self.assertEqual(dispatcher.pending.capability.id, "melee")

    def test_wrong_postcondition_is_rejected_not_counted_as_success(self) -> None:
        capability = click(
            "exit", "TUTORIAL_EXIT_MENU", action_class="commit",
            allowed_next_states=("TUTORIAL_EXIT_CONFIRM",),
        )
        dispatcher = self._dispatcher(capability)
        dispatcher.decide("TUTORIAL_EXIT_MENU", 1, 0.0)

        confirmed, pending = dispatcher.confirm_pending("LOBBY_READY")
        self.assertFalse(confirmed)
        self.assertEqual(pending.capability.id, "exit")

        dispatcher.decide("TUTORIAL_EXIT_MENU", 2, 1.0)
        confirmed, pending = dispatcher.confirm_pending("TUTORIAL_EXIT_CONFIRM")
        self.assertTrue(confirmed)

    def test_attempts_are_exhausted_then_the_frame_is_blocked(self) -> None:
        dispatcher = self._dispatcher(click("ready", "LOBBY", confirm_ms=1000, max_attempts=2))

        self.assertEqual(dispatcher.decide("LOBBY", 1, 0.0).kind, "fire")
        self.assertEqual(dispatcher.decide("LOBBY", 1, 1.5).kind, "fire")
        paused = dispatcher.decide("LOBBY", 1, 3.0)
        self.assertEqual(paused.kind, "pause")
        self.assertEqual(paused.reason, "ATTEMPTS_EXHAUSTED")
        self.assertEqual(dispatcher.decide("LOBBY", 1, 4.0).reason, "OBSERVATION_BLOCKED")

    def test_leaving_a_screen_restores_its_attempt_budget(self) -> None:
        dispatcher = self._dispatcher(click("news", "NEWS", confirm_ms=1000, max_attempts=1))

        dispatcher.note_state("NEWS", 0.0)
        self.assertEqual(dispatcher.decide("NEWS", 1, 0.0).kind, "fire")
        dispatcher.confirm_pending("LOBBY")
        dispatcher.note_state("LOBBY", 1.0)
        dispatcher.note_state("NEWS", 2.0)

        # A news page that comes back later is a fresh visit, not a retry. The
        # ordered model could not express this and got stuck here.
        self.assertEqual(dispatcher.decide("NEWS", 2, 2.0).kind, "fire")

    def test_a_four_step_loop_is_caught_even_though_each_step_succeeds(self) -> None:
        ready = click("ready", "LOBBY", action_class="commit",
                      allowed_next_states=("TUTORIAL",), max_attempts=2)
        escape = click("escape", "TUTORIAL", allowed_next_states=("LOBBY",))
        dispatcher = self._dispatcher(ready, escape, cycle_window_s=180.0, cycle_threshold=4)

        decisions = []
        for index in range(6):
            moment = index * 10.0
            dispatcher.note_state("LOBBY", moment)
            decisions.append(dispatcher.decide("LOBBY", index * 2, moment))
            dispatcher.confirm_pending("TUTORIAL")
            dispatcher.note_state("TUTORIAL", moment + 5)
            dispatcher.decide("TUTORIAL", index * 2 + 1, moment + 5)
            dispatcher.confirm_pending("LOBBY")

        self.assertEqual(decisions[0].kind, "fire")
        detected = [item for item in decisions if item.reason == "CYCLE_DETECTED"]
        # Reported once for the session, not once per alternation.
        self.assertEqual(len(detected), 1)
        self.assertEqual(detected[0].kind, "pause")
        self.assertEqual(decisions[-1].reason, "CYCLE_PAUSED")

    def test_a_screen_the_runner_never_acts_on_cannot_latch_the_cycle_pause(self) -> None:
        # A cycle means the runner's own actions are not making progress.
        # Spectating a squad that outlives you recurs constantly and is acted
        # on never; in 20260730-215512 it latched the pause and the match
        # summary that followed went unanswered for 78 seconds.
        summary = click("return", "SUMMARY", allowed_next_states=("LOBBY",))
        dispatcher = self._dispatcher(summary, cycle_window_s=180.0, cycle_threshold=4)

        for index in range(8):
            dispatcher.note_state("SPECTATING", index * 6.0)
            dispatcher.note_state(None, index * 6.0 + 3.0)

        dispatcher.note_state("SUMMARY", 50.0)
        decision = dispatcher.decide("SUMMARY", 1, 50.0)
        self.assertEqual(decision.kind, "fire")
        self.assertEqual(decision.capability.id, "return")

    def test_a_periodic_screen_recurring_is_rhythm_rather_than_a_cycle(self) -> None:
        # Melee is meant to repeat for as long as a match runs, so alternating
        # between the match screen and free fall must not read as a loop.
        melee = Capability(
            id="melee", priority=10, states=("IN_MATCH",), action="meleeKey",
            kind="key", action_class="idempotent", trigger="periodic",
            min_interval_ms=1000, max_interval_ms=5000,
        )
        summary = click("return", "SUMMARY", allowed_next_states=("LOBBY",))
        dispatcher = self._dispatcher(melee, summary, cycle_window_s=180.0, cycle_threshold=4)

        for index in range(8):
            dispatcher.note_state("IN_MATCH", index * 6.0)
            dispatcher.note_state("FREEFALL", index * 6.0 + 3.0)

        dispatcher.note_state("SUMMARY", 50.0)
        self.assertEqual(dispatcher.decide("SUMMARY", 1, 50.0).kind, "fire")

    def test_a_latched_pause_expires_even_if_the_screen_never_changes_again(self) -> None:
        # 20260730-225411 latched at 226.8s and then sat on an unrecognised
        # screen until the run ended. Ageing lived in note_state, which
        # returns early while the screen is unchanged, so the entries never
        # expired and the runner stayed inert for 704 seconds.
        ready = click("ready", "LOBBY", action_class="commit",
                      allowed_next_states=("PANEL",), max_attempts=2)
        card = click("card", "PANEL", allowed_next_states=("LOBBY",))
        dispatcher = self._dispatcher(ready, card, cycle_window_s=180.0, cycle_threshold=4)

        for index in range(6):
            dispatcher.note_state("LOBBY", index * 10.0)
            dispatcher.decide("LOBBY", index * 2, index * 10.0)
            dispatcher.confirm_pending("PANEL")
            dispatcher.note_state("PANEL", index * 10.0 + 5)
            dispatcher.decide("PANEL", index * 2 + 1, index * 10.0 + 5)
            dispatcher.confirm_pending("LOBBY")
        self.assertEqual(dispatcher.decide("LOBBY", 99, 60.0).reason, "CYCLE_PAUSED")

        # Nothing changes screen from here; only time passes.
        self.assertEqual(dispatcher.decide("LOBBY", 99, 100.0).reason, "CYCLE_PAUSED")
        self.assertEqual(dispatcher.decide("LOBBY", 100, 400.0).kind, "fire")

    def test_a_loop_still_latches_when_the_runner_is_the_one_going_in_circles(self) -> None:
        ready = click("ready", "LOBBY", action_class="commit",
                      allowed_next_states=("PANEL",), max_attempts=2)
        card = click("card", "PANEL", allowed_next_states=("LOBBY",))
        dispatcher = self._dispatcher(ready, card, cycle_window_s=180.0, cycle_threshold=4)

        reasons = []
        for index in range(6):
            dispatcher.note_state("LOBBY", index * 10.0)
            reasons.append(dispatcher.decide("LOBBY", index * 2, index * 10.0).reason)
            dispatcher.confirm_pending("PANEL")
            dispatcher.note_state("PANEL", index * 10.0 + 5)
            dispatcher.decide("PANEL", index * 2 + 1, index * 10.0 + 5)
            dispatcher.confirm_pending("LOBBY")

        self.assertIn("CYCLE_DETECTED", reasons)

    def test_periodic_capability_fires_repeatedly_on_an_unchanged_screen(self) -> None:
        melee = Capability(
            id="melee", priority=10, states=("IN_MATCH",), action="meleeKey",
            kind="key", action_class="idempotent", trigger="periodic",
            min_interval_ms=1000, max_interval_ms=5000,
        )
        dispatcher = self._dispatcher(melee)

        first = dispatcher.decide("IN_MATCH", 1, 0.0)
        self.assertEqual(first.kind, "fire")
        self.assertEqual(first.detail["intervalMs"], 3000)
        # Same observation version, same screen: an onState capability would be
        # done here, a periodic one is only waiting out its interval.
        self.assertEqual(dispatcher.decide("IN_MATCH", 1, 1.0).reason, "PERIODIC_COOLDOWN")
        self.assertEqual(dispatcher.decide("IN_MATCH", 1, 3.5).kind, "fire")

    def test_periodic_never_blocks_a_state_capability_on_the_same_screen(self) -> None:
        melee = Capability(
            id="melee", priority=10, states=("IN_MATCH",), action="meleeKey",
            kind="key", action_class="idempotent", trigger="periodic",
            min_interval_ms=1000, max_interval_ms=1000,
        )
        respawn = click("respawn", "IN_MATCH", priority=90, action_class="commit",
                        allowed_next_states=("IN_MATCH",))
        dispatcher = self._dispatcher(melee, respawn)

        self.assertEqual(dispatcher.decide("IN_MATCH", 1, 0.0).capability.id, "respawn")
        dispatcher.confirm_pending("IN_MATCH")
        self.assertEqual(dispatcher.decide("IN_MATCH", 1, 0.5).capability.id, "melee")

    def test_capability_set_loads_from_payload(self) -> None:
        payload = {
            "schemaVersion": 1,
            "capabilities": [
                {
                    "id": "continue", "priority": 80, "states": ["CONTINUE"],
                    "action": "continueClick", "kind": "click",
                    "actionClass": "idempotent", "confirmMs": 2000, "maxAttempts": 3,
                    "allowedNextStates": ["LOBBY_READY"],
                }
            ],
        }
        capabilities = CapabilitySet.from_payload(payload)
        self.assertEqual(capabilities.for_state("CONTINUE")[0].action, "continueClick")
        self.assertEqual(capabilities.for_state("LOBBY_READY"), ())

    def test_payload_schema_version_is_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "schemaVersion 必须为 1"):
            CapabilitySet.from_payload({"schemaVersion": 2, "capabilities": []})


class DelayedActionTest(unittest.TestCase):
    """Leaving a screen later is a different outcome, not a safer one.

    The dropship is the same picture for its whole flight; when the runner
    presses E decides where it lands, and pressing on sight put every landing
    on the same spot at the start of the path.
    """

    def dispatcher(self, **kwargs) -> CapabilityDispatcher:
        capability = click("jump", "DROPSHIP", delay_ms=35_000, **kwargs)
        return CapabilityDispatcher(
            CapabilitySet([capability]),
            jitter=lambda low, high: high,
        )

    def test_the_screen_has_to_persist_before_the_action_is_allowed(self) -> None:
        dispatcher = self.dispatcher()
        dispatcher.note_state("DROPSHIP", 0.0)
        self.assertEqual(dispatcher.decide("DROPSHIP", 1, 10.0).reason, "ACTION_DELAYED")
        self.assertEqual(dispatcher.decide("DROPSHIP", 1, 34.9).reason, "ACTION_DELAYED")
        self.assertEqual(dispatcher.decide("DROPSHIP", 1, 35.1).kind, "fire")

    def test_the_wait_is_measured_from_this_visit_not_the_first_one(self) -> None:
        dispatcher = self.dispatcher()
        dispatcher.note_state("DROPSHIP", 0.0)
        dispatcher.decide("DROPSHIP", 1, 40.0)
        dispatcher.note_state("FREEFALL", 41.0)
        dispatcher.note_state("DROPSHIP", 600.0)
        self.assertEqual(dispatcher.decide("DROPSHIP", 2, 610.0).reason, "ACTION_DELAYED")
        self.assertEqual(dispatcher.decide("DROPSHIP", 2, 636.0).kind, "fire")

    def test_a_frame_nobody_could_name_does_not_restart_the_wait(self) -> None:
        # Over 35 seconds of dropship the prompt will fail to read at least
        # once. Treating that as having left the screen would restart the
        # clock, and enough restarts would ride the flight to its forced drop
        # at the far edge — the exact landing this change exists to avoid.
        dispatcher = self.dispatcher()
        dispatcher.note_state("DROPSHIP", 0.0)
        dispatcher.decide("DROPSHIP", 1, 1.0)
        dispatcher.note_state(None, 20.0)
        dispatcher.note_state("DROPSHIP", 20.3)
        self.assertEqual(dispatcher.decide("DROPSHIP", 2, 36.0).kind, "fire")

    def test_actually_leaving_the_screen_does_restart_the_wait(self) -> None:
        dispatcher = self.dispatcher()
        dispatcher.note_state("DROPSHIP", 0.0)
        dispatcher.decide("DROPSHIP", 1, 1.0)
        dispatcher.note_state("IN_MATCH", 20.0)
        dispatcher.note_state("DROPSHIP", 30.0)
        self.assertEqual(dispatcher.decide("DROPSHIP", 2, 50.0).reason, "ACTION_DELAYED")
        self.assertEqual(dispatcher.decide("DROPSHIP", 2, 66.0).kind, "fire")

    def test_the_jitter_is_rolled_once_a_visit_rather_than_every_frame(self) -> None:
        # Re-rolling per frame would average the jitter away and put every
        # landing back on one spot, which is the whole reason it is here.
        dispatcher = self.dispatcher(delay_jitter_ms=8000)
        dispatcher.note_state("DROPSHIP", 0.0)
        for moment in (1.0, 20.0, 36.0, 42.0):
            self.assertEqual(dispatcher.decide("DROPSHIP", 1, moment).reason, "ACTION_DELAYED")
        self.assertEqual(dispatcher.decide("DROPSHIP", 1, 43.1).kind, "fire")

    def test_a_periodic_capability_may_not_also_declare_a_delay(self) -> None:
        with self.assertRaisesRegex(ValueError, "周期触发用间隔表达节奏"):
            Capability(
                id="melee", priority=10, states=("IN_MATCH",), action="meleeKey",
                kind="key", action_class="idempotent", trigger="periodic",
                min_interval_ms=1000, max_interval_ms=5000, delay_ms=1000,
            )

    def test_jitter_without_a_delay_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "声明了抖动却没有延迟"):
            click("jump", "DROPSHIP", delay_jitter_ms=5000)


if __name__ == "__main__":
    unittest.main()

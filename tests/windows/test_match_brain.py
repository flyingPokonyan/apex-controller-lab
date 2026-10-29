from __future__ import annotations

from pathlib import Path
import random
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.match_brain import (
    GROUND_KEYS_AFTER_MS,
    LEG_HOLD_MS,
    MAX_MOUSE_DX,
    MAX_MOUSE_DY,
    MENU_SCAN_CODES,
    HudReading,
    MatchBrain,
    MouseCalibration,
)


SCAN_CODES = {
    "forward": 17,
    "left": 30,
    "back": 31,
    "right": 32,
    "jump": 57,
    "melee": 47,
    "tactical": 16,
    "interact": 18,
    "sprint": 42,
    "crouch": 29,
    "ultimate": 44,
}
GROUND_ONLY = {SCAN_CODES["sprint"], SCAN_CODES["crouch"]}
CALIBRATED = MouseCalibration(counts_per_degree=10.0, pitch_floor_counts=2000, level_from_floor_counts=900)


def keys_in(steps: list[dict[str, object]]) -> set[int]:
    codes: set[int] = set()
    for step in steps:
        if step["type"] == "holdKeys":
            codes.update(int(code) for code in step["scanCodes"])
        elif step["type"] == "tapKey":
            codes.add(int(step["scanCode"]))
    return codes


def net_dx(steps: list[dict[str, object]]) -> int:
    return sum(int(step["dx"]) for step in steps if step["type"] == "moveMouse")


def net_dy(steps: list[dict[str, object]]) -> int:
    return sum(int(step["dy"]) for step in steps if step["type"] == "moveMouse")


class MatchBrainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rng = random.Random(11)

    def brain(self, mouse: MouseCalibration | None = None) -> MatchBrain:
        brain = MatchBrain(dict(SCAN_CODES), mouse or MouseCalibration())
        brain.begin_life(self.rng)
        return brain

    def test_search_holds_shift_and_ctrl_back_until_the_player_is_settled_on_the_ground(self) -> None:
        brain = self.brain()
        early: set[int] = set()
        late: set[int] = set()
        legs: set[str] = set()
        for index in range(400):
            alive_ms = index * 50 if index < 200 else GROUND_KEYS_AFTER_MS + index * 50
            mode, steps = brain.step(HudReading(), alive_ms=alive_ms, now_ms=index * 300, rng=self.rng)
            self.assertTrue(mode.startswith("search:"), mode)
            legs.add(mode)
            (early if alive_ms < GROUND_KEYS_AFTER_MS else late).update(keys_in(steps))
            for step in steps:
                self.assertNotIn(step.get("scanCode"), MENU_SCAN_CODES)
                if step["type"] == "holdKeys":
                    self.assertLessEqual(int(step["durationMs"]), LEG_HOLD_MS[1])
                    self.assertFalse(MENU_SCAN_CODES.intersection(step["scanCodes"]))
                elif step["type"] == "moveMouse":
                    self.assertLessEqual(abs(int(step["dx"])), MAX_MOUSE_DX)
                    self.assertLessEqual(abs(int(step["dy"])), MAX_MOUSE_DY)
        self.assertFalse(early & GROUND_ONLY)
        self.assertEqual(late & GROUND_ONLY, GROUND_ONLY)
        for leg in ("sprint", "walk", "crouch", "slide", "sprintJump", "scan", "pause", "ability"):
            self.assertIn(f"search:{leg}", legs)

    def test_no_leg_repeats_more_than_three_times_in_a_row(self) -> None:
        brain = self.brain()
        run = 0
        previous = None
        for index in range(600):
            mode, _ = brain.step(HudReading(), alive_ms=GROUND_KEYS_AFTER_MS + index, now_ms=index * 300, rng=self.rng)
            run = run + 1 if mode == previous else 1
            previous = mode
            self.assertLessEqual(run, 3)

    def test_a_pickup_prompt_gets_e(self) -> None:
        brain = self.brain()
        mode, steps = brain.step(HudReading(pickup_prompt=True), alive_ms=20_000, now_ms=0, rng=self.rng)
        self.assertEqual(mode, "pickup")
        self.assertEqual(keys_in(steps), {SCAN_CODES["interact"]})
        self.assertEqual(brain.summary()["pickupPresses"], 1)

    def test_a_shrinking_bar_with_a_weapon_answers_with_bursts(self) -> None:
        brain = self.brain()
        brain.step(HudReading(has_weapon=True, vitality=1.0), alive_ms=20_000, now_ms=0, rng=self.rng)
        mode, steps = brain.step(HudReading(has_weapon=True, vitality=0.8), alive_ms=20_300, now_ms=300, rng=self.rng)
        self.assertEqual(mode, "react")
        bursts = [step for step in steps if step["type"] == "holdMouse"]
        self.assertTrue(bursts)
        for step in bursts:
            self.assertEqual(step["button"], "left")
            self.assertTrue(150 <= int(step["durationMs"]) <= 400)
        # Uncalibrated: no turn by angle, only the small sweep between bursts.
        self.assertLessEqual(abs(net_dx(steps)), 40)
        summary = brain.summary()
        self.assertEqual(summary["reactions"], 1)
        self.assertGreaterEqual(summary["bursts"], 1)

    def test_without_a_weapon_it_still_fires_and_sometimes_runs_in_to_punch(self) -> None:
        ran_in = 0
        for seed in range(20):
            rng = random.Random(seed)
            brain = MatchBrain(dict(SCAN_CODES))
            brain.begin_life(rng)
            brain.step(HudReading(has_weapon=False, vitality=0.9), alive_ms=20_000, now_ms=0, rng=rng)
            mode, steps = brain.step(HudReading(has_weapon=False, vitality=0.5), alive_ms=20_300, now_ms=300, rng=rng)
            self.assertEqual(mode, "react")
            # Fire with fists out punches, so a hit is always answered on the trigger.
            self.assertTrue([step for step in steps if step["type"] == "holdMouse"])
            if SCAN_CODES["melee"] in keys_in(steps):
                ran_in += 1
                self.assertIn(SCAN_CODES["sprint"], keys_in(steps))
        self.assertTrue(0 < ran_in < 20)

    def test_the_reaction_lasts_a_few_seconds_then_search_resumes(self) -> None:
        brain = self.brain()
        brain.step(HudReading(vitality=1.0), alive_ms=20_000, now_ms=0, rng=self.rng)
        brain.step(HudReading(vitality=0.7), alive_ms=20_100, now_ms=100, rng=self.rng)
        mode, _ = brain.step(HudReading(vitality=0.7), alive_ms=22_000, now_ms=2_000, rng=self.rng)
        self.assertEqual(mode, "react")
        mode, _ = brain.step(HudReading(vitality=0.7), alive_ms=29_000, now_ms=9_000, rng=self.rng)
        self.assertTrue(mode.startswith("search:"), mode)

    def test_a_damage_arc_turns_toward_it_with_an_overshoot_that_settles_back(self) -> None:
        brain = self.brain(CALIBRATED)
        mode, steps = brain.step(HudReading(hit_bearing=90.0, hurt=True, has_weapon=True), alive_ms=20_000, now_ms=0, rng=self.rng)
        self.assertEqual(mode, "react")
        moves = [int(step["dx"]) for step in steps if step["type"] == "moveMouse"]
        burst_index = next(i for i, step in enumerate(steps) if step["type"] == "holdMouse")
        turn = sum(int(step["dx"]) for step in steps[:burst_index] if step["type"] == "moveMouse")
        self.assertEqual(turn, 900)
        self.assertGreater(max(moves), 0)
        self.assertLess(min(moves), 0)  # the settle-back after the overshoot
        # The arrow that caused the turn is still on screen next frame: no second turn,
        # only the small sweeps between bursts (2–6° each at 10 counts per degree).
        _, steps = brain.step(HudReading(hit_bearing=90.0, hurt=True, has_weapon=True), alive_ms=20_300, now_ms=300, rng=self.rng)
        self.assertLessEqual(abs(net_dx(steps)), 180)

    def test_an_arrow_alone_is_not_a_hit(self) -> None:
        brain = self.brain(CALIBRATED)
        brain.step(HudReading(vitality=1.0, shield=1.0), alive_ms=20_000, now_ms=0, rng=self.rng)
        mode, _ = brain.step(HudReading(vitality=1.0, shield=1.0, hit_bearing=-120.0), alive_ms=20_300, now_ms=300, rng=self.rng)
        self.assertTrue(mode.startswith("search:"), mode)

    def test_a_shield_drop_after_an_arrow_turns_toward_the_arrow(self) -> None:
        brain = self.brain(CALIBRATED)
        brain.step(HudReading(vitality=1.0, shield=1.0, has_weapon=False), alive_ms=20_000, now_ms=0, rng=self.rng)
        brain.step(HudReading(vitality=1.0, shield=1.0, hit_bearing=-90.0, has_weapon=False), alive_ms=20_250, now_ms=250, rng=self.rng)
        mode, steps = brain.step(HudReading(vitality=1.0, shield=0.6, has_weapon=False), alive_ms=20_500, now_ms=500, rng=self.rng)
        self.assertEqual(mode, "react")
        fire_index = next(i for i, step in enumerate(steps) if step["type"] == "holdMouse")
        self.assertEqual(sum(int(step["dx"]) for step in steps[:fire_index] if step["type"] == "moveMouse"), -900)

    def test_a_hit_without_an_arrow_looks_one_way_then_back_until_a_hit_is_confirmed(self) -> None:
        brain = self.brain(CALIBRATED)
        brain.step(HudReading(vitality=1.0, has_weapon=True), alive_ms=20_000, now_ms=0, rng=self.rng)
        _, first = brain.step(HudReading(vitality=0.8, has_weapon=True), alive_ms=20_300, now_ms=300, rng=self.rng)
        burst_index = next(i for i, step in enumerate(first) if step["type"] == "holdMouse")
        step_turn = sum(int(step["dx"]) for step in first[:burst_index] if step["type"] == "moveMouse")
        self.assertTrue(900 <= abs(step_turn) <= 1800)  # 90–180° first
        self.assertEqual(len(brain._turn_plan), 1)
        self.assertLess(brain._turn_plan[0] * step_turn, 0)  # then back the other way
        _, steps = brain.step(HudReading(vitality=0.8, has_weapon=True, hit_confirmed=True), alive_ms=20_600, now_ms=600, rng=self.rng)
        self.assertFalse(brain._turn_plan)
        self.assertLessEqual(abs(net_dx(steps)), 60)
        self.assertEqual(brain.summary()["hitConfirmed"], 1)

    def test_turns_are_many_small_eased_moves_not_jumps(self) -> None:
        brain = self.brain(CALIBRATED)
        _, steps = brain.step(HudReading(hit_bearing=120.0, hurt=True, has_weapon=True), alive_ms=20_000, now_ms=0, rng=self.rng)
        fire_index = next(i for i, step in enumerate(steps) if step["type"] == "holdMouse")
        moves = [int(step["dx"]) for step in steps[:fire_index] if step["type"] == "moveMouse"]
        self.assertGreaterEqual(len(moves), 15)
        self.assertLess(max(abs(move) for move in moves), 0.2 * 1200)
        # Slow start, fast middle: the first move is smaller than the largest.
        self.assertLess(abs(moves[0]), max(abs(move) for move in moves))

    def test_running_turns_the_view_along_the_way(self) -> None:
        brain = self.brain(CALIBRATED)
        brain._choose_leg = lambda picker: "sprint"
        brain._next_level_ms = 10**9
        looks = []
        for index in range(20):
            _, steps = brain.step(HudReading(), alive_ms=20_000 + index * 1000, now_ms=index * 1000, rng=self.rng)
            step = steps[0]
            self.assertEqual(step["type"], "holdKeys")
            self.assertLessEqual(len(step["look"]), 240)
            self.assertTrue(all(abs(dx) <= 300 and abs(dy) <= 300 for dx, dy in step["look"]))
            looks.append(sum(dx for dx, _ in step["look"]))
        self.assertGreater(max(abs(total) for total in looks), 100)  # some legs curve visibly

    def test_low_health_without_landing_hits_breaks_off_and_runs(self) -> None:
        brain = self.brain(CALIBRATED)
        brain.step(HudReading(vitality=0.9, has_weapon=True), alive_ms=20_000, now_ms=0, rng=self.rng)
        mode, steps = brain.step(HudReading(vitality=0.3, has_weapon=True), alive_ms=20_300, now_ms=300, rng=self.rng)
        self.assertEqual(mode, "react")
        self.assertFalse([step for step in steps if step["type"] == "holdMouse"])
        turn = abs(net_dx(steps))
        self.assertTrue(1500 <= turn <= 2100, turn)  # 150–210°: facing away
        run = steps[-1]
        self.assertEqual(run["type"], "holdKeys")
        self.assertEqual(set(run["scanCodes"]), {SCAN_CODES["forward"], SCAN_CODES["sprint"]})
        self.assertEqual(brain.summary()["retreats"], 1)
        # The reaction is over; the next frame without a new hit searches.
        mode, _ = brain.step(HudReading(vitality=0.3, has_weapon=True), alive_ms=20_600, now_ms=600, rng=self.rng)
        self.assertTrue(mode.startswith("search:"), mode)

    def test_low_health_while_landing_hits_keeps_fighting(self) -> None:
        brain = self.brain(CALIBRATED)
        brain.step(HudReading(vitality=0.9, has_weapon=True), alive_ms=20_000, now_ms=0, rng=self.rng)
        _, steps = brain.step(HudReading(vitality=0.3, has_weapon=True, hit_confirmed=True), alive_ms=20_300, now_ms=300, rng=self.rng)
        self.assertTrue([step for step in steps if step["type"] == "holdMouse"])
        self.assertNotIn("retreats", brain.summary())

    def test_two_movement_legs_into_a_wall_turn_away_and_step_back(self) -> None:
        brain = self.brain()
        brain._choose_leg = lambda picker: "sprint"
        modes = []
        for index in range(4):
            mode, steps = brain.step(HudReading(scene_change=1.0), alive_ms=20_000 + index * 1000, now_ms=index * 1000, rng=self.rng)
            modes.append(mode)
        self.assertEqual(modes[:3], ["search:sprint", "search:sprint", "unstick"])
        self.assertEqual(brain.summary()["unsticks"], 1)

    def test_a_moving_view_is_not_stuck(self) -> None:
        brain = self.brain()
        brain._choose_leg = lambda picker: "sprint"
        for index in range(6):
            mode, _ = brain.step(HudReading(scene_change=15.0), alive_ms=20_000 + index * 1000, now_ms=index * 1000, rng=self.rng)
            self.assertEqual(mode, "search:sprint")

    def test_toggle_crouch_always_taps_back_up(self) -> None:
        brain = MatchBrain(dict(SCAN_CODES), crouch_toggle=True)
        brain.begin_life(self.rng)
        for builder in (brain._crouch, brain._slide):
            steps = builder(self.rng)
            taps = [step for step in steps if step["type"] == "tapKey" and step["scanCode"] == SCAN_CODES["crouch"]]
            held = [step for step in steps if step["type"] == "holdKeys" and SCAN_CODES["crouch"] in step["scanCodes"]]
            self.assertEqual(len(taps) + len(held), 2)  # in and out
            for step in held:
                self.assertLessEqual(int(step["durationMs"]), 100)

    def test_weapon_reads_are_counted_for_the_go_no_go(self) -> None:
        brain = self.brain()
        brain.step(HudReading(has_weapon=False), alive_ms=20_000, now_ms=0, rng=self.rng)
        brain.step(HudReading(), alive_ms=21_000, now_ms=1_000, rng=self.rng)  # HUD hidden: not a read
        brain.step(HudReading(has_weapon=True), alive_ms=95_000, now_ms=75_000, rng=self.rng)
        brain.step(HudReading(has_weapon=True), alive_ms=96_000, now_ms=76_000, rng=self.rng)
        summary = brain.summary()
        self.assertEqual((summary["hudReads"], summary["weaponReads"], summary["weaponSeen"]), (3, 2, 1))
        self.assertEqual(summary["firstWeaponAliveMs"], 95_000)

    def test_the_view_is_leveled_after_landing_only_when_calibrated(self) -> None:
        brain = self.brain()
        modes = {brain.step(HudReading(), alive_ms=2_000 + i, now_ms=i, rng=self.rng)[0] for i in range(20)}
        self.assertNotIn("level", modes)

        brain = self.brain(CALIBRATED)
        mode, steps = brain.step(HudReading(), alive_ms=2_000, now_ms=0, rng=self.rng)
        self.assertEqual(mode, "level")
        downs = [int(step["dy"]) for step in steps if step["type"] == "moveMouse" and int(step["dy"]) > 0]
        self.assertEqual(sum(downs), 2000)
        # Down to the clamp, up 900 to the horizon, then 12° below it.
        self.assertEqual(net_dy(steps), 2000 - 900 + 120)
        mode, _ = brain.step(HudReading(), alive_ms=3_000, now_ms=1_000, rng=self.rng)
        self.assertNotEqual(mode, "level")
        mode, _ = brain.step(HudReading(), alive_ms=2_000 + 91_000, now_ms=91_000, rng=self.rng)
        self.assertEqual(mode, "level")

    def test_a_new_life_resets_the_counters_and_rolls_a_style(self) -> None:
        brain = self.brain()
        brain.step(HudReading(pickup_prompt=True), alive_ms=20_000, now_ms=0, rng=self.rng)
        brain.begin_life(self.rng)
        self.assertEqual(set(brain.summary()), {"style"})


class SelfCalibrationTests(unittest.TestCase):
    def run_world(self, true_counts_per_degree: float, readings: int = 12) -> MatchBrain:
        """A fake compass that turns by counts / true_counts_per_degree."""
        rng = random.Random(4)
        brain = MatchBrain(dict(SCAN_CODES))
        brain.begin_life(rng)
        heading = 100.0
        for index in range(readings):
            _, steps = brain.step(HudReading(heading=int(heading) % 360), alive_ms=20_000 + index * 300, now_ms=index * 300, rng=rng)
            heading += net_dx(steps) / true_counts_per_degree
        return brain

    def test_three_compass_samples_set_counts_per_degree_and_the_pitch_reset(self) -> None:
        brain = self.run_world(8.0)
        self.assertTrue(brain.mouse.can_turn)
        self.assertAlmostEqual(brain.mouse.counts_per_degree, 8.0, delta=0.3)
        self.assertEqual(brain.mouse.level_from_floor_counts, round(89 * brain.mouse.counts_per_degree))
        self.assertGreater(brain.mouse.pitch_floor_counts, brain.mouse.level_from_floor_counts)
        self.assertIn("countsPerDegree", brain.summary())

    def test_a_turn_too_small_to_read_is_doubled(self) -> None:
        brain = self.run_world(200.0, readings=4)  # 600 counts turn only 3°
        self.assertFalse(brain.mouse.can_turn)
        self.assertGreater(brain._calibration_counts, 600)

    def test_no_heading_means_no_calibration_turns(self) -> None:
        rng = random.Random(4)
        brain = MatchBrain(dict(SCAN_CODES))
        brain.begin_life(rng)
        for index in range(10):
            mode, _ = brain.step(HudReading(), alive_ms=20_000, now_ms=index * 300, rng=rng)
            self.assertNotEqual(mode, "calibrate")

    def test_the_measurement_survives_a_new_life(self) -> None:
        brain = self.run_world(8.0)
        brain.begin_life(random.Random(1))
        self.assertTrue(brain.mouse.can_turn)


class MouseCalibrationTests(unittest.TestCase):
    def test_empty_spec_is_uncalibrated(self) -> None:
        mouse = MouseCalibration.from_spec({})
        self.assertFalse(mouse.can_turn)
        self.assertFalse(mouse.can_level)

    def test_invalid_numbers_are_rejected(self) -> None:
        for spec in ({"countsPerDegree": 0}, {"countsPerDegree": "10"}, {"pitchFloorCounts": True}, []):
            with self.assertRaises(ValueError):
                MouseCalibration.from_spec(spec)


if __name__ == "__main__":
    unittest.main()

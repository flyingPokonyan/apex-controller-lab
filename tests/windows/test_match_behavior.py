from __future__ import annotations

from pathlib import Path
import random
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.match_behavior import (
    FORBIDDEN_SCAN_CODES,
    GESTURES,
    HOLD_MS,
    MOODS,
    MatchBehavior,
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
}


class MatchBehaviorTests(unittest.TestCase):
    def test_gestures_stay_put_and_off_the_menu_and_sprint_keys(self) -> None:
        behavior = MatchBehavior()
        rng = random.Random(7)
        names: set[str] = set()
        allowed = set(SCAN_CODES.values())
        previous = None
        for index in range(600):
            if index % 150 == 0:
                behavior.begin_life(rng)
                previous = None
            name, steps = behavior.sample(SCAN_CODES, rng)
            names.add(name)
            self.assertNotEqual(name, previous)
            previous = name
            self.assertTrue(steps)
            for step in steps:
                self.assertNotIn(step.get("scanCode"), FORBIDDEN_SCAN_CODES)
                if step["type"] == "moveMouse":
                    self.assertLessEqual(abs(int(step["dx"])), 64)
                    self.assertLessEqual(abs(int(step["dy"])), 24)
                elif step["type"] == "holdKeys":
                    codes = list(step["scanCodes"])
                    self.assertLessEqual(len(codes), 2)
                    self.assertTrue(
                        set(codes).issubset(
                            {
                                SCAN_CODES["forward"],
                                SCAN_CODES["left"],
                                SCAN_CODES["right"],
                                SCAN_CODES["back"],
                            }
                        )
                    )
                    if len(codes) == 2:
                        self.assertEqual(codes[0], SCAN_CODES["forward"])
                        self.assertIn(codes[1], {SCAN_CODES["left"], SCAN_CODES["right"]})
                    self.assertGreaterEqual(int(step["durationMs"]), HOLD_MS[0])
                    self.assertLessEqual(int(step["durationMs"]), HOLD_MS[1])
                elif step["type"] == "tapKey":
                    self.assertIn(step["scanCode"], allowed)
                    self.assertLessEqual(int(step["durationMs"]), 260)
                else:
                    self.assertEqual(step["type"], "wait")
                    self.assertLessEqual(int(step["durationMs"]), 4000)
        self.assertEqual(names, {item[0] for item in GESTURES})
        self.assertLess(abs(behavior.pitch), 200)
        self.assertEqual(sum(item[1] for item in GESTURES), 100)

    def test_steps_move_without_chaining_or_sprinting(self) -> None:
        behavior = MatchBehavior()
        behavior.mood = "wander"
        rng = random.Random(3)
        names = []
        saw_diagonal = False
        for _ in range(200):
            name, steps = behavior.sample(SCAN_CODES, rng)
            names.append(name)
            holds = [step for step in steps if step["type"] == "holdKeys"]
            self.assertLessEqual(len(holds), 1)
            for hold in holds:
                self.assertGreaterEqual(int(hold["durationMs"]), HOLD_MS[0])
                self.assertLessEqual(int(hold["durationMs"]), HOLD_MS[1])
                self.assertNotIn(42, hold["scanCodes"])
                if len(hold["scanCodes"]) == 2:
                    saw_diagonal = True
        self.assertGreater(names.count("forward"), 25)
        self.assertTrue(saw_diagonal)
        for index, name in enumerate(names):
            if index == 0:
                continue
            self.assertNotEqual(name, names[index - 1])

    def test_a_new_life_rolls_a_mood(self) -> None:
        behavior = MatchBehavior()
        behavior.last_gesture = "look"
        behavior.pitch = 40
        behavior.begin_life(random.Random(1))
        self.assertIn(behavior.mood, MOODS)
        self.assertIsNone(behavior.last_gesture)
        self.assertEqual(behavior.pitch, 0)

    def test_strafe_is_a_short_sidestep_and_jump_stays_put(self) -> None:
        behavior = MatchBehavior()
        rng = random.Random(11)
        seen = set()
        for _ in range(120):
            name, steps = behavior.sample(SCAN_CODES, rng)
            if name == "strafe":
                seen.add(name)
                self.assertEqual(len(steps), 1)
                hold = steps[0]
                self.assertEqual(hold["type"], "holdKeys")
                self.assertEqual(len(hold["scanCodes"]), 1)
                self.assertIn(hold["scanCodes"][0], {SCAN_CODES["left"], SCAN_CODES["right"]})
                self.assertGreaterEqual(int(hold["durationMs"]), HOLD_MS[0])
                self.assertLessEqual(int(hold["durationMs"]), HOLD_MS[1])
            elif name == "jump":
                seen.add(name)
                self.assertEqual(
                    [step["scanCode"] for step in steps],
                    [SCAN_CODES["jump"]],
                )
        self.assertEqual(seen, {"jump", "strafe"})


if __name__ == "__main__":
    unittest.main()

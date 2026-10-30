import random
import unittest

from windows.apex_automation.agent.controller import InputController, _Turn
from windows.apex_automation.safety import EmergencyStop


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Sender:
    def __init__(self) -> None:
        self.events: list[tuple] = []
        self.down: set = set()
        self.moved = [0, 0]

    def key_down(self, code):
        self.events.append(("down", code))
        self.down.add(code)

    def key_up(self, code):
        self.events.append(("up", code))
        self.down.discard(code)

    def mouse_down(self, button="left"):
        self.events.append(("mdown", button))
        self.down.add(button)

    def mouse_up(self, button="left"):
        self.events.append(("mup", button))
        self.down.discard(button)

    def move_mouse(self, dx, dy):
        self.events.append(("move", dx, dy))
        self.moved[0] += dx
        self.moved[1] += dy

    def release_all(self):
        self.events.append(("release",))
        self.down.clear()


class Guard:
    def __init__(self) -> None:
        self.front = True
        self.abort = False

    def target_is_foreground(self):
        if self.abort:
            raise EmergencyStop("F8")
        return self.front


def make():
    clock, sender, guard = Clock(), Sender(), Guard()
    controller = InputController(sender, guard, clock=clock, sleep=lambda _s: None, rng=random.Random(1))
    controller.resume()
    return controller, sender, guard, clock


def run(controller, clock, seconds, tick=0.008):
    for _ in range(round(seconds / tick)):
        clock.advance(tick)
        controller.tick()


class InputControllerTest(unittest.TestCase):
    def test_keys_stay_down_between_decisions(self) -> None:
        controller, sender, _guard, clock = make()
        controller.set_keys([17, 42])
        run(controller, clock, 1.0)
        self.assertEqual(sender.down, {17, 42})
        # Exactly one press each, never a release in between.
        self.assertEqual([e for e in sender.events if e[0] in ("down", "up")], [("down", 17), ("down", 42)])
        controller.set_keys([17])
        run(controller, clock, 0.1)
        self.assertEqual(sender.down, {17})

    def test_planned_turn_sends_exact_total_with_smooth_speed(self) -> None:
        controller, sender, _guard, clock = make()
        controller.turn(900, 0, 0.5)
        run(controller, clock, 0.7)
        self.assertFalse(controller.turning)
        # Tremor adds a little drift, the plan itself is exact.
        self.assertAlmostEqual(sender.moved[0], 900, delta=6)
        steps = [e[1] for e in sender.events if e[0] == "move"]
        # Speed ramps up: the first steps are smaller than the middle ones.
        self.assertLess(max(steps[:3]), max(steps[20:40]))

    def test_rate_change_is_limited_by_acceleration(self) -> None:
        controller, sender, _guard, clock = make()
        controller.set_rate(3000, 0)
        run(controller, clock, 0.016)
        first = sender.moved[0]
        # 12,000 counts/s² for 16 ms reaches at most ~190 counts/s → ~3 counts.
        self.assertLess(first, 6)
        run(controller, clock, 1.0)
        self.assertGreater(sender.moved[0], 2000)

    def test_tap_presses_and_releases_without_touching_held_keys(self) -> None:
        controller, sender, _guard, clock = make()
        controller.set_keys([17])
        controller.tap(18, 0.06)
        run(controller, clock, 0.2)
        self.assertIn(("down", 18), sender.events)
        self.assertIn(("up", 18), sender.events)
        self.assertEqual(sender.down, {17})
        self.assertFalse(controller.busy_tapping)

    def test_click_holds_button_for_its_duration(self) -> None:
        controller, sender, _guard, clock = make()
        controller.click("left", 0.2)
        run(controller, clock, 0.1)
        self.assertIn("left", sender.down)
        run(controller, clock, 0.2)
        self.assertNotIn("left", sender.down)

    def test_foreground_loss_releases_everything_and_pauses(self) -> None:
        controller, sender, guard, clock = make()
        controller.set_keys([17, 42])
        controller.set_buttons(["right"])
        run(controller, clock, 0.1)
        guard.front = False
        run(controller, clock, 0.05)
        self.assertEqual(sender.down, set())
        self.assertEqual(controller.fault, "FOREGROUND_LOST")
        self.assertFalse(controller.active)
        # Nothing is sent again until the decision side resumes.
        guard.front = True
        count = len(sender.events)
        run(controller, clock, 0.2)
        self.assertEqual(len(sender.events), count)

    def test_deadman_lets_go_when_decisions_stop(self) -> None:
        controller, sender, _guard, clock = make()
        controller.set_keys([17])
        run(controller, clock, 2.0)
        self.assertEqual(sender.down, {17})
        run(controller, clock, 1.5)
        self.assertEqual(sender.down, set())
        self.assertEqual(controller.fault, "DEADMAN")

    def test_emergency_stop_releases_and_stops_the_thread(self) -> None:
        controller, sender, guard, clock = make()
        controller.set_keys([17])
        run(controller, clock, 0.05)
        guard.abort = True
        run(controller, clock, 0.02)
        self.assertEqual(sender.down, set())
        self.assertEqual(controller.fault, "EMERGENCY_STOP")
        self.assertTrue(controller.stopped)

    def test_pause_releases_and_clears_wants(self) -> None:
        controller, sender, _guard, clock = make()
        controller.set_keys([17])
        controller.set_rate(500)
        run(controller, clock, 0.2)
        controller.pause("STATE_CHANGED")
        self.assertEqual(sender.down, set())
        controller.resume()
        moved = list(sender.moved)
        run(controller, clock, 0.2)
        self.assertEqual(sender.down, set())
        self.assertEqual(sender.moved, moved)

    def test_an_interrupted_turn_hands_what_it_had_left_to_the_next(self) -> None:
        controller, sender, _guard, clock = make()
        controller.turn(0, 900, 0.5)
        run(controller, clock, 0.2)
        controller.turn(300, 0, 0.3)
        run(controller, clock, 0.6)
        # Nothing of the first turn is lost: the view ends 900 down and 300 right.
        self.assertAlmostEqual(sender.moved[1], 900, delta=8)
        self.assertAlmostEqual(sender.moved[0], 300, delta=8)

    def test_turn_profile_covers_the_whole_path(self) -> None:
        turn = _Turn(100, 0, 0.0, 1.0)
        points = [turn.progress(t / 100) for t in range(101)]
        self.assertAlmostEqual(points[0], 0.0)
        self.assertAlmostEqual(points[-1], 1.0)
        self.assertTrue(all(b >= a for a, b in zip(points, points[1:])))
        # The middle runs at a steady speed.
        mids = [b - a for a, b in zip(points[30:70], points[31:71])]
        self.assertLess(max(mids) - min(mids), 1e-9)


if __name__ == "__main__":
    unittest.main()

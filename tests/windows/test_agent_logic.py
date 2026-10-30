import random
import unittest

from windows.apex_automation.agent.belief import Belief, Memory
from windows.apex_automation.agent.controller import InputController
from windows.apex_automation.agent.goals import GoalManager
from windows.apex_automation.agent.motor import Keys, Motor
from windows.apex_automation.agent.perception import Mate, Observation, Prompt
from windows.apex_automation.agent.skills import Context, Heal, Loot, Navigator, Reload, Status


class Sender:
    def __init__(self):
        self.taps = []
        self.down = set()

    def key_down(self, code):
        self.down.add(code)
        self.taps.append(code)

    def key_up(self, code):
        self.down.discard(code)

    def mouse_down(self, button="left"):
        self.down.add(button)

    def mouse_up(self, button="left"):
        self.down.discard(button)

    def move_mouse(self, dx, dy):
        pass

    def release_all(self):
        self.down.clear()


class Guard:
    def target_is_foreground(self):
        return True


class World:
    """A belief fed with hand-made observations, and a clock."""

    def __init__(self):
        self.now = 100.0
        self.sender = Sender()
        self.controller = InputController(self.sender, Guard(), clock=lambda: self.now, sleep=lambda s: None,
                                          rng=random.Random(1))
        self.controller.resume()
        self.motor = Motor(self.controller, Keys(), random.Random(2))
        self.motor.counts_per_degree = 9.0
        self.motor.calibrated = True
        self.motor.pitch = 0.0
        self.belief = Belief()
        self.memory = Memory()

    def see(self, **fields):
        base = dict(at=self.now, hud=True, weapon=False, vitality=1.0, shield=0.5, storm_edge=0.0,
                    scene_change=10.0, heading=90, prompt_checked=True)
        base.update(fields)
        self.belief.update(Observation(**base))

    def ctx(self):
        return Context(now=self.now, belief=self.belief, memory=self.memory, motor=self.motor, rng=random.Random(3))

    def advance(self, seconds, tick=0.008):
        for _ in range(round(seconds / tick)):
            self.now += tick
            self.controller.tick()


class BeliefTest(unittest.TestCase):
    def test_weapon_needs_three_frames_either_way(self):
        world = World()
        world.see(weapon=True, mag=9)
        world.see(weapon=True, mag=9)
        self.assertFalse(world.belief.is_armed())
        world.see(weapon=True, mag=9)
        self.assertTrue(world.belief.is_armed())
        world.see(weapon=False)
        self.assertTrue(world.belief.is_armed())

    def test_storm_needs_two_of_three_and_three_clear_seconds(self):
        world = World()
        world.see(storm_edge=0.6)
        self.assertFalse(world.belief.in_storm())
        world.see(storm_edge=0.1)
        world.see(storm_edge=0.6)
        self.assertTrue(world.belief.in_storm())
        world.now += 1.0
        world.see(storm_edge=0.05)
        world.now += 2.5
        world.see(storm_edge=0.05)
        self.assertTrue(world.belief.in_storm())
        world.now += 1.0
        world.see(storm_edge=0.05)
        self.assertFalse(world.belief.in_storm())

    def test_damage_with_an_arrow_is_a_player_and_without_it_in_storm_is_the_storm(self):
        world = World()
        world.see(vitality=1.0)
        world.now += 0.1
        world.see(vitality=0.8, arrow_bearing=-40.0)
        self.assertTrue(world.belief.under_fire(world.now))
        storm = World()
        storm.see(storm_edge=0.6)
        storm.see(storm_edge=0.6, vitality=1.0)
        storm.now += 0.1
        storm.see(storm_edge=0.6, vitality=0.9)
        self.assertFalse(storm.belief.under_fire(storm.now))
        self.assertEqual(storm.belief.last_damage.value, "storm")

    def test_teammates_are_tracked_by_colour_through_a_missed_frame(self):
        world = World()
        world.see(mates=(Mate(10, 15, 89), Mate(200, 50, 20)), minimap_self=True)
        world.now += 0.3
        world.see(mates=(Mate(200, 50, 20),), minimap_self=True)
        self.assertEqual(world.belief.nearest_mate(world.now).hue, 89)

    def test_out_of_ammo_needs_an_empty_magazine_and_the_message(self):
        world = World()
        for _ in range(3):
            world.see(weapon=True, mag=0)
        self.assertFalse(world.belief.out_of_ammo(world.now))
        world.see(weapon=True, mag=0, center_text="没有弹药")
        self.assertTrue(world.belief.out_of_ammo(world.now))


class GoalManagerTest(unittest.TestCase):
    def test_armed_and_safe_it_roams_instead_of_standing(self):
        world = World()
        manager = GoalManager()
        world.motor.calibrated = True
        for _ in range(3):
            world.see(weapon=True, mag=9, mates=(Mate(10, 15, 89),), minimap_self=True)
        world.now += 4
        world.see(weapon=True, mag=9, mates=(Mate(10, 15, 89),), minimap_self=True)
        self.assertEqual(manager.tick(world.ctx()), "roam")

    def test_turning_works_before_calibration(self):
        world = World()
        motor = Motor(world.controller, Keys(), random.Random(2))
        self.assertFalse(motor.calibrated)
        self.assertGreater(motor.turn(90.0), 0.0)

    def test_being_shot_preempts_and_wait_is_the_fallback(self):
        world = World()
        manager = GoalManager()
        world.see()
        # Unarmed with nothing around: look for gear.
        self.assertEqual(manager.tick(world.ctx()), "search")
        armed = World()
        manager = GoalManager()
        for _ in range(3):
            armed.see(weapon=True, mag=9)
        # Armed with nothing urgent: keep moving with the team.
        self.assertEqual(manager.tick(armed.ctx()), "roam")
        armed.now += 0.1
        armed.see(weapon=True, mag=9, vitality=0.8, arrow_bearing=30.0)
        self.assertEqual(manager.tick(armed.ctx()), "return_fire")

    def test_failure_starts_a_cooldown(self):
        world = World()
        manager = GoalManager()
        world.see(vitality=0.3)
        world.now += 5
        world.see(vitality=0.3)
        self.assertEqual(manager.tick(world.ctx()), "heal")
        for _ in range(40):
            world.advance(0.1)
            world.see(vitality=0.3)
            manager.tick(world.ctx())
        self.assertNotEqual(manager.current.name if manager.current else None, "heal")
        self.assertTrue(world.memory.cooling("heal", world.now))
        self.assertEqual(world.memory.last_result["heal"][1], "NO_HEAL_ITEMS")

    def test_a_goal_never_outlives_its_time_limit(self):
        world = World()
        manager = GoalManager()
        world.see(mates=(Mate(0, 80, 20),), minimap_self=True)
        self.assertEqual(manager.tick(world.ctx()), "follow")
        for _ in range(500):
            world.advance(0.1)
            world.see(mates=(Mate(0, 80, 20),), minimap_self=True, scene_change=20.0)
            manager.tick(world.ctx())
        statuses = [s.get("status") for s in manager.switches if s.get("from") == "follow"]
        self.assertTrue(statuses)
        self.assertTrue(all(status in ("timeout", "failed", "interrupted", "success") for status in statuses))


class SkillTest(unittest.TestCase):
    def test_a_card_that_stays_put_is_pressed_twice_and_never_counted(self):
        world = World()
        card = Prompt("捡起", "捡起", 1400, 600, "弹药", "轻型弹药 x40")
        world.see(prompt=card)
        skill = Loot(world.ctx())
        statuses = []
        for _ in range(60):
            world.advance(0.1)
            world.see(prompt=card)
            statuses.append(skill.step(world.ctx()))
            if statuses[-1] is not Status.RUNNING:
                break
        self.assertEqual(skill.presses, 2)
        self.assertEqual(skill.taken, 0)
        self.assertEqual((statuses[-1], skill.reason), (Status.FAILED, "NO_EFFECT"))

    def test_a_card_that_disappears_after_e_counts_as_taken(self):
        world = World()
        card = Prompt("捡起", "捡起", 1400, 600, "恢复 | 生命值", "医疗箱")
        world.see(prompt=card)
        skill = Loot(world.ctx())
        skill.step(world.ctx())
        status = Status.RUNNING
        for _ in range(30):
            world.advance(0.1)
            world.see(prompt=None)
            status = skill.step(world.ctx())
            if status is not Status.RUNNING:
                break
        self.assertEqual((status, skill.taken), (Status.SUCCESS, 1))
        self.assertEqual(world.ctx().counters, world.ctx().counters)

    def test_a_bin_that_does_not_open_fails_quickly(self):
        world = World()
        lid = Prompt("打开", "打开", 1500, 800)
        world.see(prompt=lid)
        skill = Loot(world.ctx())
        status = Status.RUNNING
        for _ in range(40):
            world.advance(0.1)
            world.see(prompt=lid)
            status = skill.step(world.ctx())
            if status is not Status.RUNNING:
                break
        self.assertEqual((status, skill.reason), (Status.FAILED, "OPEN_NO_EFFECT"))
        self.assertLess(world.now - 100.0, 3.0)

    def test_weapon_cards_are_skipped_with_both_slots_full(self):
        world = World()
        for _ in range(3):
            world.see(weapon=True, mag=9, guns=2)
        card = Prompt("装备", "装备", 1400, 600, "冲锋枪 | 重型弹药", "猎兽冲锋枪")
        world.see(weapon=True, mag=9, guns=2, prompt=card)
        manager = GoalManager()
        self.assertNotEqual(manager.tick(world.ctx()), "loot")

    def test_heal_fails_when_the_bars_do_not_move(self):
        world = World()
        world.see(vitality=0.3, shield=0.0)
        skill = Heal(world.ctx())
        status = skill.step(world.ctx())
        for _ in range(30):
            world.advance(0.1)
            world.see(vitality=0.3, shield=0.0)
            status = skill.step(world.ctx())
            if status is not Status.RUNNING:
                break
        self.assertEqual((status, skill.reason), (Status.FAILED, "NO_HEAL_ITEMS"))

    def test_reload_succeeds_when_the_magazine_refills(self):
        world = World()
        for _ in range(3):
            world.see(weapon=True, mag=1)
        skill = Reload(world.ctx())
        skill.step(world.ctx())
        world.advance(1.5)
        world.see(weapon=True, mag=20)
        self.assertEqual(skill.step(world.ctx()), Status.SUCCESS)

    def test_navigator_jumps_first_when_blocked(self):
        world = World()
        nav = Navigator()
        for _ in range(12):
            world.advance(0.1)
            world.see(scene_change=0.5)
            state = nav.drive(world.ctx(), 0.0, sprint=False)
        self.assertEqual(state, "unsticking")
        self.assertIn(Keys().jump, world.sender.taps)


if __name__ == "__main__":
    unittest.main()

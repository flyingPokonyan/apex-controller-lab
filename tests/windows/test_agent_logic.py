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


from windows.apex_automation.agent.detector import FRAME, Detection, decode
from windows.apex_automation.agent.skills import Approach, Engage


def seen(kind, yaw, pitch=0.0, height=0.1, x=1280.0):
    half_h = height * FRAME[1] / 2
    return Detection(kind, 0.9, x - 100, 720 - half_h, x + 100, 720 + half_h, yaw, pitch)


class DetectionGoalTest(unittest.TestCase):
    def test_unarmed_it_walks_to_a_bin_it_sees(self):
        world = World()
        manager = GoalManager()
        world.see(detections=(seen("bin_closed", 20.0),))
        self.assertNotEqual(manager.tick(world.ctx()), "approach")  # one read could be a stray box
        world.now += 0.5
        world.see(detections=(seen("bin_closed", 20.0),))
        self.assertEqual(GoalManager().tick(world.ctx()), "approach")

    def test_a_prompt_ends_the_approach_and_loot_takes_over(self):
        world = World()
        world.see(detections=(seen("bin_closed", 5.0, height=0.3),))
        world.see(detections=(seen("bin_closed", 5.0, height=0.3),))
        skill = Approach(world.ctx())
        self.assertEqual(skill.step(world.ctx()), Status.RUNNING)
        world.advance(0.2)
        world.see(detections=(seen("bin_closed", 2.0, height=0.45),), prompt=Prompt("打开", "打开", 1500, 800))
        self.assertEqual(skill.step(world.ctx()), Status.SUCCESS)

    def test_a_target_out_of_sight_is_given_up_and_remembered(self):
        world = World()
        world.see(detections=(seen("item", -10.0),))
        world.see(detections=(seen("item", -10.0),))
        skill = Approach(world.ctx())
        status = Status.RUNNING
        for _ in range(40):
            world.advance(0.1)
            world.see(detections=())
            status = skill.step(world.ctx())
            if status is not Status.RUNNING:
                break
        self.assertEqual((status, skill.reason), (Status.FAILED, "LOST"))
        self.assertTrue(world.memory.tried_near("target", 80.0, world.now, 60.0, spread=15.0))

    def test_bots_are_engaged_only_when_allowed(self):
        world = World()
        for _ in range(3):
            world.see(weapon=True, mag=12, detections=(seen("enemy", 8.0),))
        manager = GoalManager()
        self.assertNotEqual(manager.tick(world.ctx()), "engage")
        ctx = world.ctx()
        ctx.allow_engage = True
        self.assertEqual(GoalManager().tick(ctx), "engage")

    def test_no_shot_while_a_teammate_is_in_the_way(self):
        world = World()
        for _ in range(3):
            world.see(weapon=True, mag=12, detections=(seen("enemy", 1.0), seen("teammate", 0.0, height=0.3)))
        ctx = world.ctx()
        ctx.allow_engage = True
        skill = Engage(ctx)
        for _ in range(20):
            world.advance(0.1)
            world.see(weapon=True, mag=12, detections=(seen("enemy", 1.0), seen("teammate", 0.0, height=0.3)))
            ctx = world.ctx()
            ctx.allow_engage = True
            skill.step(ctx)
        self.assertNotIn("left", world.sender.down)
        self.assertGreater(ctx.counters["engage:hold_teammate"], 0)
        self.assertEqual(ctx.counters["engage:burst"], 0)


class DecodeTest(unittest.TestCase):
    def test_head_output_becomes_boxes_with_bearings(self):
        import numpy as np
        output = np.zeros((11, 2), dtype=np.float32)
        # One bin straight ahead at 416 scale (2560 -> 416 is 0.1625).
        output[:4, 0] = [208, 117, 40, 30]
        output[4, 0] = 0.9
        output[:4, 1] = [10, 10, 5, 5]
        output[9, 1] = 0.1
        found = decode(output, 416 / 2560, 0.4, 960.0)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].kind, "bin_closed")
        self.assertAlmostEqual(found[0].yaw, 0.0, delta=0.5)
        self.assertAlmostEqual(found[0].pitch, 0.0, delta=0.5)


from windows.apex_automation.agent.tracking import Tracker


class TrackerTest(unittest.TestCase):
    def test_the_same_bin_keeps_its_identity_while_we_turn(self):
        tracker = Tracker()
        tracker.update((seen("bin_closed", 20.0),), 90.0, 1.0)
        first = tracker.tracks[0].id
        # Turned 20 degrees right: the bin is now straight ahead, same bearing.
        tracker.update((seen("bin_closed", 0.0),), 110.0, 1.5)
        self.assertEqual([t.id for t in tracker.tracks], [first])
        self.assertTrue(tracker.tracks[0].confirmed)
        self.assertAlmostEqual(tracker.tracks[0].bearing, 110.0)

    def test_two_bins_are_two_tracks_and_old_ones_are_forgotten(self):
        tracker = Tracker()
        tracker.update((seen("bin_closed", -30.0), seen("bin_closed", 30.0)), 0.0, 1.0)
        self.assertEqual(len(tracker.tracks), 2)
        tracker.update((seen("bin_closed", 30.0),), 0.0, 4.0)
        self.assertEqual(len(tracker.tracks), 1)
        self.assertEqual(tracker.tracks[0].hits, 1)  # the old pair was forgotten

    def test_a_much_bigger_box_is_not_the_same_thing(self):
        tracker = Tracker()
        tracker.update((seen("bin_closed", 0.0, height=0.05),), 0.0, 1.0)
        tracker.update((seen("bin_closed", 0.0, height=0.3),), 0.0, 1.5)
        self.assertEqual(len(tracker.tracks), 2)

    def test_approach_stays_on_its_bin_when_a_closer_one_appears(self):
        world = World()
        for _ in range(2):
            world.see(detections=(seen("bin_closed", -20.0, height=0.1),))
        skill = Approach(world.ctx())
        target = skill.track_id
        for _ in range(3):
            world.advance(0.2)
            world.see(detections=(seen("bin_closed", -20.0, height=0.1), seen("bin_closed", 25.0, height=0.25)))
            self.assertEqual(skill.step(world.ctx()), Status.RUNNING)
        self.assertEqual(skill.track_id, target)


import math

import numpy as np

from windows.apex_automation.agent.space import SpaceView, free_from_disparity


def scene(wall_rows: dict[range, int], h=266, w=476, hfov=106.26, pitch=0.0):
    """Inverse depth of flat floor seen from eye height, with walls standing in some columns from a row up."""
    focal = (w / 2) / math.tan(math.radians(hfov) / 2)
    rows = np.arange(h)
    alpha = np.radians(-pitch) + np.arctan((rows - h / 2) / focal)
    floor = np.where(alpha > 0.005, np.tan(np.clip(alpha, 0.005, None)) / 1.6, 0.003)
    disp = np.repeat(floor[:, None], w, axis=1).astype(np.float32)
    for columns, foot in wall_rows.items():
        for x in columns:
            disp[:foot, x] = disp[foot, x]  # standing surface: same depth all the way up
    return disp


class FreeSpaceTest(unittest.TestCase):
    def test_open_floor_is_free_and_a_wall_ahead_is_close(self):
        open_ = free_from_disparity(scene({}), 0.0, 106.26)
        self.assertTrue(all(metres == 25.0 for _, metres in open_ if metres is not None))
        # A wall whose foot is at row 200 straight ahead: about 1.6 / tan(angle of row 200).
        focal = 238 / math.tan(math.radians(53.13))
        expected = 1.6 / math.tan(math.atan((200 - 133) / focal))
        sectors = free_from_disparity(scene({range(200, 276): 200}), 0.0, 106.26)
        ahead = [metres for yaw, metres in sectors if abs(yaw) < 3]
        self.assertTrue(ahead)
        self.assertAlmostEqual(min(ahead), expected, delta=0.6)
        left = [metres for yaw, metres in sectors if yaw < -30]
        self.assertTrue(all(metres == 25.0 for metres in left))

    def test_no_floor_at_the_bottom_of_the_view_is_right_in_front(self):
        sectors = free_from_disparity(scene({range(0, 476): 230}), 0.0, 106.26)
        self.assertTrue(all(metres == 0.0 for _, metres in sectors if metres is not None))

    def test_navigator_detours_around_something_close_ahead(self):
        from windows.apex_automation.agent.skills import Navigator

        world = World()
        sectors = tuple((float(yaw), 1.5 if -10 <= yaw <= 10 else 12.0) for yaw in range(-50, 51, 4))
        world.see(space=sectors)
        nav = Navigator()
        ctx = world.ctx()
        self.assertEqual(nav.drive(ctx, 0.0, sprint=False), "moving")
        self.assertEqual(ctx.counters["nav:detour"], 1)
        bearing, _until = nav.detour
        self.assertGreaterEqual(abs((bearing - 90 + 180) % 360 - 180), 10)
        # Held for a moment even when the next reading shows nothing.
        world.advance(0.3)
        ctx = world.ctx()
        nav.drive(ctx, 0.0, sprint=False)
        self.assertEqual(ctx.counters["nav:detour"], 0)
        self.assertIsNotNone(nav.detour)

    def test_open_ground_ahead_needs_no_detour(self):
        from windows.apex_automation.agent.skills import Navigator

        world = World()
        world.see(space=tuple((float(yaw), 12.0) for yaw in range(-50, 51, 4)))
        nav = Navigator()
        ctx = world.ctx()
        nav.drive(ctx, 5.0, sprint=False)
        self.assertIsNone(nav.detour)
        self.assertIsInstance(SpaceView(90.0, ()).sectors, tuple)

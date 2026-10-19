from pathlib import Path
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ocr_obstacles import OcrToken
from apex_automation.pack_opening import (
    ESC_SCAN, SPACE_SCAN, PackLimits, PackOpener, classify, grid_settled, heirloom_visible,
    open_point, select_count,
)


FIXTURE = Path(__file__).parent / "fixtures" / "pack-result-heirloom.png"


def load_bgr(path: Path) -> np.ndarray:
    # The CI job installs numpy and OpenCV only, so no Pillow here.
    return cv2.imread(str(path), cv2.IMREAD_COLOR)


def token(text, roi):
    return OcrToken(text, 0.99, roi)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


class Guard:
    def ensure_not_aborted(self):
        return None

    def target_is_foreground(self):
        return True


class World:
    def __init__(self, result, clock):
        self.result = result
        self.clock = clock
        self.phase = "lobby"
        self.maxed = False
        self.blank = np.zeros((100, 200, 3), np.uint8)
        self.sender = None

    def grab(self):
        return self.result if self.phase == "result" else self.blank

    def read_with_boxes(self, frame):
        return self.read(frame)

    def read(self, _frame):
        if (self.phase == "select" and self.maxed and self.sender.held
                and self.sender.hovering and self.clock() - self.sender.down_at >= 0.4):
            self.phase = "result"
        return {
            "lobby": (token("APEX", (160, 68, 180, 76)), token("组合包", (160, 78, 190, 90))),
            "intro": (
                token("火花奖励计数器", (40, 8, 140, 24)),
                token("开启组合包", (20, 30, 80, 46)),
                token("开启组合包", (70, 80, 130, 95)),
            ),
            "select": (
                token("开启组合包", (8, 4, 70, 18)),
                token("22", (10, 20, 40, 55)),
                token("升级22个" if self.maxed else "升级1个", (80, 50, 130, 68)),
                token("按住打开" if self.sender.hovering else ("开启 22 个" if self.maxed else "开启 1 个"),
                      (90, 72, 140, 88)),
                token("最高", (150, 70, 190, 90)),
            ) + ((token("11个超额组合包打开时不适用于火花奖励计数器", (60, 92, 160, 99)),) if self.maxed else ()),
            "result": (token("组合包奖励", (8, 8, 80, 24)),),
            "reward": (token("已获得奖励", (40, 10, 140, 30)),),
            "empty": (
                token("开启组合包", (8, 4, 70, 18)),
                token("0", (10, 20, 40, 55)),
                token("最高", (150, 70, 190, 90)),
            ),
        }[self.phase]


class Sender:
    def __init__(self, world):
        self.world = world
        self.events = []
        self.held = False
        self.hovering = False
        self.down_at = None
        self.rewarded = False
        world.sender = self

    def click(self, x, y):
        self.events.append(("click", self.world.phase, x, y))
        if self.world.phase == "lobby":
            self.world.phase = "intro"
        elif self.world.phase == "intro":
            self.world.phase = "select"
        elif self.world.phase == "select" and (x, y) == (170, 80):
            self.world.maxed = True
        # A click parks the pointer in the middle of the screen.
        self.hovering = False

    def move_to(self, x, y):
        self.events.append(("move", self.world.phase, x, y))
        self.hovering = self.world.phase == "select" and (x, y) == (115, 80)

    def mouse_down(self, button="left"):
        self.held = True
        self.down_at = self.world.clock()

    def mouse_up(self, button="left"):
        self.held = False

    def tap_scan_code(self, code, _duration):
        self.events.append(("key", code, self.world.phase))
        if code == ESC_SCAN and self.world.phase == "result":
            self.world.phase = "empty" if self.rewarded else "reward"
            self.rewarded = True
        elif code == SPACE_SCAN and self.world.phase == "reward":
            self.world.phase = "empty"

    def release_all(self):
        self.held = False


class PackOpeningTest(unittest.TestCase):
    def test_reference_result_is_heirloom_and_an_orange_border_is_not(self):
        self.assertTrue(heirloom_visible(load_bgr(FIXTURE)))
        frame = np.zeros((600, 1000, 3), np.uint8)
        frame[108:112, 40:98] = (40, 140, 220)
        self.assertFalse(heirloom_visible(frame))
        frame[130:170, 60:95] = (20, 30, 210)
        self.assertTrue(heirloom_visible(frame))

    def test_reference_without_its_red_cells_and_shard_icon_is_clear(self):
        frame = load_bgr(FIXTURE)
        height, width = frame.shape[:2]
        # Cover the three red shard cells with the blue cell at row 4, column 5,
        # and darken the red shard icon in the reward currency total.
        x0, y0, cell_w, cell_h = .04, .18, .29 / 5, .63 / 6
        def cell(row, column):
            top, left = int(height * (y0 + row * cell_h)), int(width * (x0 + column * cell_w))
            return top, top + int(height * cell_h), left, left + int(width * cell_w)
        bt, bb, bl, br = cell(3, 4)
        for column in range(3):
            top, bottom, left, right = cell(0, column)
            rows, columns = min(bottom - top, bb - bt), min(right - left, br - bl)
            frame[top:top + rows, left:left + columns] = frame[bt:bt + rows, bl:bl + columns]
        total = frame[int(height * .835):int(height * .90), int(width * .04):int(width * .33)]
        red = (total[:, :, 2] > 150) & (total[:, :, 2] > total[:, :, 1].astype(int) + 50)
        total[red] = (40, 30, 25)
        self.assertFalse(heirloom_visible(frame))

    def test_a_few_moving_sparks_still_count_as_a_settled_grid(self):
        before = load_bgr(FIXTURE)[100:500:8, 40:340:8].astype(np.int16)
        sparks = before.copy()
        sparks[:1, :10] += 120
        self.assertTrue(grid_settled(before, sparks))
        flipping = before.copy()
        flipping[: before.shape[0] // 3] = 0
        self.assertFalse(grid_settled(before, flipping))

    def test_flow_claims_the_spark_pack_and_stops_on_zero(self):
        clock = Clock()
        world = World(load_bgr(FIXTURE), clock)
        sender = Sender(world)
        saved = []
        result = PackOpener(
            world, world, sender, Guard(), save_frame=lambda frame, batch, heirloom: saved.append((batch, heirloom)) or True,
            log=lambda *args, **kwargs: None, clock=clock, sleep=clock.sleep,
            limits=PackLimits(page_s=5, hold_s=2, session_s=30),
        ).run()
        self.assertEqual(result.status, "DONE")
        self.assertTrue(result.heirloom)
        self.assertEqual(result.batches, 1)
        self.assertEqual(result.packs_before, 22)
        self.assertEqual(saved, [(1, True)])
        self.assertIn(("click", "lobby", 175, 79), sender.events)
        self.assertIn(("click", "intro", 100, 87), sender.events)
        self.assertIn(("click", "select", 170, 80), sender.events)
        self.assertIn(("move", "select", 115, 80), sender.events)
        # The open button is held, never clicked, and 升级22个 is never touched.
        self.assertFalse(any(event[0] == "click" and event[2:] in {(115, 80), (105, 59)} for event in sender.events))
        self.assertIn(("key", ESC_SCAN, "result"), sender.events)
        self.assertIn(("key", SPACE_SCAN, "reward"), sender.events)
        self.assertFalse(sender.held)

    def test_unreadable_count_does_not_click_open_or_upgrade(self):
        clock = Clock()
        world = World(np.zeros((100, 200, 3), np.uint8), clock)
        world.phase = "select"
        world.read = lambda _frame: (
            token("开启组合包", (8, 4, 70, 18)),
            token("升级22个", (80, 50, 130, 68)),
            token("最高", (150, 70, 190, 90)),
        )
        sender = Sender(world)
        result = PackOpener(
            world, world, sender, Guard(), save_frame=lambda *_args: True,
            log=lambda *args, **kwargs: None, clock=clock, sleep=clock.sleep,
            limits=PackLimits(page_s=1, hold_s=1, session_s=2),
        ).run()
        self.assertEqual((result.status, result.reason, result.batches), ("FAILED", "COUNT_UNREADABLE", 0))
        self.assertFalse(any(event[1] == "select" for event in sender.events if event[0] == "click"))

    def test_screenshot_failure_keeps_a_red_already_seen(self):
        clock = Clock()
        world = World(load_bgr(FIXTURE), clock)
        sender = Sender(world)
        result = PackOpener(
            world, world, sender, Guard(), save_frame=lambda *_args: False,
            log=lambda *args, **kwargs: None, clock=clock, sleep=clock.sleep,
            limits=PackLimits(page_s=5, hold_s=2, session_s=30),
        ).run()
        self.assertEqual(result.status, "FAILED")
        self.assertEqual(result.reason, "SCREENSHOT_FAILED")
        self.assertTrue(result.heirloom)
        self.assertEqual(result.batches, 1)
        self.assertFalse(sender.held)


class CaptureQuirkTest(unittest.TestCase):
    def test_a_capture_with_zero_alpha_is_read_without_its_alpha(self):
        seen = []

        class Ocr:
            def read_with_boxes(self, frame):
                seen.append(frame.shape)
                return (token("组合包奖励", (8, 8, 80, 24)),)

        class Source:
            def grab(self):
                return np.zeros((100, 200, 4), np.uint8)

        opener = PackOpener(Source(), Ocr(), None, Guard(), save_frame=None, log=None)
        self.assertEqual(opener._look(), "result")
        self.assertEqual(seen, [(100, 200, 3)])

    def test_lobby_card_falls_back_to_the_card_region(self):
        frame = np.zeros((1440, 2560, 3), np.uint8)

        class Ocr:
            def read_with_boxes(self, image):
                if image.shape[:2] == frame.shape[:2]:
                    return ()  # the whole frame read missed the small card text
                # Coordinates are relative to the card region crop.
                return (token("21", (60, 30, 180, 120)), token("apex", (170, 34, 270, 76)),
                        token("组合包", (168, 70, 278, 119)))

        opener = PackOpener(None, Ocr(), None, Guard(), save_frame=None, log=None)
        opener.frame, opener.tokens = frame, ()
        point = opener._lobby_card()
        self.assertIsNotNone(point)
        left, top = int(2560 * .77), int(1440 * .66)
        self.assertEqual(point, (left + (168 + 278) // 2, top + (34 + 119) // 2))
        self.assertEqual(opener.packs_before, 21)


class LobbyEntryTest(unittest.TestCase):
    """The 10-19 hands-on test clicked the card once, the game ignored it, and the run ended."""

    LOBBY = (token("21", (2092, 1054, 2210, 1143)), token("APEX", (2201, 1058, 2301, 1100)),
             token("组合包", (2198, 1094, 2308, 1143)), token("开启0/10", (2018, 1153, 2108, 1181)))

    def opener(self, sender, *, foreground=lambda: True, ocr=None, source=None):
        clock = Clock()

        class LiveGuard:
            def ensure_not_aborted(self):
                return None

            def target_is_foreground(self):
                return foreground()

        class Source:
            def grab(self):
                return np.zeros((1440, 2560, 3), np.uint8)

        lobby = self.LOBBY

        class Ocr:
            def read_with_boxes(self, frame):
                if sender.opened:
                    return (token("火花奖励计数器", (40, 8, 140, 24)),)
                if frame.shape[:2] == (1440, 2560):
                    return lobby
                # The card region crop, in crop coordinates.
                left, top = int(2560 * .77), int(1440 * .66)
                return tuple(OcrToken(t.text, t.confidence, (t.roi[0] - left, t.roi[1] - top,
                                                             t.roi[2] - left, t.roi[3] - top)) for t in lobby)

        events = []
        opener = PackOpener(source or Source(), ocr or Ocr(), sender, LiveGuard(), save_frame=lambda *_args: True,
                            log=lambda event, **payload: events.append((event, payload)),
                            clock=clock, sleep=clock.sleep, limits=PackLimits(page_s=3, hold_s=2, session_s=60))
        return opener, events

    def test_a_lost_card_click_is_retried_while_the_lobby_card_is_still_there(self):
        class Sender:
            clicks, opened = [], False

            def click(self, x, y):
                self.clicks.append((x, y))
                self.opened = len(self.clicks) >= 2  # the first click is swallowed

            def release_all(self):
                pass

        sender = Sender()
        opener, events = self.opener(sender)
        self.assertEqual(opener._look(), "unknown")
        self.assertEqual(opener._enter_from_lobby(), "intro")
        self.assertEqual(sender.clicks, [(2253, 1100), (2253, 1100)])
        self.assertEqual([payload["attempt"] for event, payload in events if event == "PACK_INPUT"], [1, 2])
        self.assertEqual(opener.packs_before, 21)

    def test_a_card_that_never_responds_stops_after_three_clicks(self):
        class Sender:
            clicks, opened = [], False

            def click(self, x, y):
                self.clicks.append((x, y))

            def release_all(self):
                pass

        sender = Sender()
        opener, _events = self.opener(sender)
        result = opener.run()
        self.assertEqual((result.status, result.reason), ("FAILED", "CARD_NO_EFFECT"))
        self.assertEqual(len(sender.clicks), 3)

    def test_losing_the_foreground_is_named_and_nothing_is_clicked(self):
        class Sender:
            clicks, opened = [], False

            def click(self, x, y):
                self.clicks.append((x, y))

            def release_all(self):
                pass

        sender = Sender()
        opener, _events = self.opener(sender, foreground=lambda: False)
        result = opener.run()
        self.assertEqual((result.status, result.reason), ("FAILED", "FOREGROUND_LOST"))
        self.assertEqual(sender.clicks, [])

    def test_a_fresh_frame_is_preferred_over_the_cached_one(self):
        stale, fresh = np.zeros((1440, 2560, 3), np.uint8), np.ones((1440, 2560, 3), np.uint8)

        class Source:
            calls = 0

            def grab(self):
                return stale

            def grab_fresh(self):
                self.calls += 1
                return fresh if self.calls >= 3 else None

        seen = []

        class Ocr:
            def read_with_boxes(self, frame):
                seen.append(int(frame[0, 0, 0]))
                return ()

        class Sender:
            opened = False

        opener, _events = self.opener(Sender(), ocr=Ocr(), source=Source())
        opener._look()
        self.assertEqual(seen, [1])

    def test_a_card_under_another_window_is_not_clicked(self):
        class Sender:
            clicks, opened = [], False

            def click(self, x, y):
                self.clicks.append((x, y))

            def release_all(self):
                pass

        sender = Sender()
        opener, events = self.opener(sender)
        opener.guard.expected = {"r5apex.exe"}
        opener.window_owner = lambda point: ("uuremote.exe", [1548, 866, 1906, 1022])
        result = opener.run()
        self.assertEqual((result.status, result.reason), ("FAILED", "CARD_COVERED"))
        self.assertEqual(sender.clicks, [])
        self.assertIn("PACK_CLICK_COVERED", [event for event, _payload in events])
        opener.window_owner = lambda point: ("r5apex.exe", [0, 0, 1920, 1080])
        opener.guard.expected = {"r5apex.exe"}
        self.assertTrue(opener._click((2253, 1100), "lobby-card"))
        self.assertEqual(sender.clicks, [(2253, 1100)])


class RealPageTokensTest(unittest.TestCase):
    """OCR of the 10-19 20:54 frames from Runner 1."""

    def test_the_open_page_after_max_is_still_the_open_page(self):
        tokens = (token("开启组合包", (95, 100, 514, 197)), token("21", (270, 285, 388, 373)),
                  token("升级21个", (1459, 1107, 1585, 1142)), token("开启21个", (1409, 1235, 1602, 1289)),
                  token("最高", (2041, 1233, 2139, 1293)),
                  token("11个超额组合包打开时不适用于火花奖励计数器", (1229, 1343, 1784, 1381)))
        self.assertEqual(classify(tokens), "select")
        self.assertEqual(select_count(tokens, 2560, 1440), 21)
        self.assertEqual(open_point(tokens), (1505, 1262))
        # Runner 1 read nothing of the bold 开启21个; the row still places it.
        without = tuple(item for item in tokens if item.normalized != "开启21个")
        self.assertEqual(open_point(without + (token("重置", (877, 1234, 968, 1289)),)), (1506, 1262))


class LateSparkRewardTest(unittest.TestCase):
    """10-19 21:13: the spark reward popped up after the count reached 0, and the run had already ended."""

    def test_the_late_reward_is_claimed_and_its_pack_opened(self):
        clock = Clock()
        state = {"page": "select", "count": 22, "maxed": False, "hover": False, "down": None,
                 "reward_at": None, "rewards": 1}

        def read(_frame):
            if state["page"] == "select" and state["maxed"] and state["hover"] and state["down"] is not None \
                    and clock() - state["down"] >= 0.4:
                state.update(page="result", count=0)
            if state["page"] == "select" and state["reward_at"] is not None and clock() >= state["reward_at"]:
                state.update(page="reward", reward_at=None)
            if state["page"] == "result":
                return (token("组合包奖励", (8, 8, 80, 24)),)
            if state["page"] == "reward":
                return (token("已获得奖励", (40, 10, 140, 30)), token("SPACE 继续", (80, 90, 120, 98)))
            return (token("开启组合包", (8, 4, 70, 18)), token(str(state["count"]), (10, 20, 40, 55)),
                    token("重置", (40, 72, 70, 88)),
                    token("按住打开" if state["hover"] else "开启 1 个", (90, 72, 140, 88)),
                    token("最高", (150, 70, 190, 90)))

        class Game:
            def grab(self):
                return np.zeros((100, 200, 3), np.uint8)

            def read_with_boxes(self, frame):
                return read(frame)

        keys = []

        class Input:
            def click(self, x, y):
                if (x, y) == (170, 80):
                    state["maxed"] = True
                state["hover"] = False

            def move_to(self, x, y):
                state["hover"] = (x, y) == (115, 80)

            def mouse_down(self, button="left"):
                state["down"] = clock()

            def mouse_up(self, button="left"):
                state["down"] = None

            def tap_scan_code(self, code, _duration):
                keys.append((code, state["page"]))
                if code == ESC_SCAN and state["page"] == "result":
                    state.update(page="select", maxed=False, hover=False)
                    if state["rewards"]:
                        state.update(reward_at=clock() + 2, rewards=0)
                elif code == SPACE_SCAN and state["page"] == "reward":
                    state.update(page="select", count=1)

            def release_all(self):
                state["down"] = None

        game = Game()
        saved = []
        result = PackOpener(game, game, Input(), Guard(), save_frame=lambda _f, batch, red: saved.append(batch) or True,
                            log=lambda *args, **kwargs: None, clock=clock, sleep=clock.sleep,
                            limits=PackLimits(page_s=5, hold_s=2, session_s=120)).run()
        self.assertEqual((result.status, result.batches, result.packs_before), ("DONE", 2, 22))
        self.assertEqual(saved, [1, 2])
        self.assertEqual(keys, [(ESC_SCAN, "result"), (SPACE_SCAN, "reward"), (ESC_SCAN, "result")])

    def test_packs_gone_without_a_result_page_are_not_reported_clean(self):
        clock = Clock()
        state = {"count": 22, "maxed": False, "hover": False, "down": None}

        def read(_frame):
            if state["maxed"] and state["hover"] and state["down"] is not None and clock() - state["down"] >= 0.4:
                state.update(count=0, maxed=False, hover=False, down=None)  # straight back, no rewards page
            return (token("开启组合包", (8, 4, 70, 18)), token(str(state["count"]), (10, 20, 40, 55)),
                    token("重置", (40, 72, 70, 88)), token("开启 1 个", (90, 72, 140, 88)),
                    token("最高", (150, 70, 190, 90)))

        class Game:
            def grab(self):
                return np.zeros((100, 200, 3), np.uint8)

            def read_with_boxes(self, frame):
                return read(frame)

        class Input:
            def click(self, x, y):
                state["maxed"] = state["maxed"] or (x, y) == (170, 80)

            def move_to(self, x, y):
                state["hover"] = (x, y) == (115, 80)

            def mouse_down(self, button="left"):
                state["down"] = clock()

            def mouse_up(self, button="left"):
                state["down"] = None

            def release_all(self):
                state["down"] = None

        game = Game()
        result = PackOpener(game, game, Input(), Guard(), save_frame=lambda *_args: True,
                            log=lambda *args, **kwargs: None, clock=clock, sleep=clock.sleep,
                            limits=PackLimits(page_s=5, hold_s=2, session_s=60)).run()
        self.assertEqual((result.status, result.reason, result.heirloom), ("FAILED", "RESULT_NOT_SEEN", False))

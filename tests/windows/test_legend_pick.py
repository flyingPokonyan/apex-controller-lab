from __future__ import annotations

from pathlib import Path
import random
import sys
import unittest

import cv2
import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.legend_pick import MAX_TRIES, LegendPicker, unlocked_tiles
from apex_automation.ocr_obstacles import OcrToken


FIXTURES = REPOSITORY_ROOT / "tests" / "windows" / "fixtures" / "legend"
# Bangalore, Pathfinder, Wraith, Octane, Bloodhound, Lifeline: the free roster.
FREE_ROSTER = {"突击1", "游击1", "游击2", "游击3", "侦察1", "支援2"}


def roster_frame(name: str) -> np.ndarray:
    """The roster band from a 2026-10-29 recording, back at (200, 930) on a 2560x1440 frame."""
    canvas = np.zeros((1440, 2560, 3), dtype=np.uint8)
    crop = cv2.imread(str(FIXTURES / f"{name}_roster.jpg"))
    canvas[930 : 930 + crop.shape[0], 200 : 200 + crop.shape[1]] = crop
    return canvas


class HintOcr:
    def __init__(self) -> None:
        self.text = ""

    def read(self, frame, region):
        return (OcrToken(self.text, 0.99),) if self.text else ()


class LegendPickTests(unittest.TestCase):
    def test_only_coloured_portraits_are_candidates(self) -> None:
        for name in ("bot_royale_our_turn", "trios_our_turn"):
            self.assertEqual({tile.name for tile in unlocked_tiles(roster_frame(name))}, FREE_ROSTER, name)

    def test_a_teammate_taken_legend_is_not_a_candidate(self) -> None:
        # 游击4 wears the sepia tint and the 「2」 badge of the teammate who took it.
        self.assertNotIn("游击4", {tile.name for tile in unlocked_tiles(roster_frame("bot_royale_our_turn"))})

    def test_waits_for_our_turn_then_clicks_one_unlocked_portrait_once(self) -> None:
        frame = roster_frame("trios_our_turn")
        ocr = HintOcr()
        picker = LegendPicker(random.Random(3))
        ocr.text = "F小队静音"  # a teammate's turn
        self.assertIsNone(picker.step(frame, 0.0, ocr))
        self.assertIsNone(picker.step(frame, 10.0, ocr))
        ocr.text = "选择传奇"
        self.assertIsNone(picker.step(frame, 11.0, ocr))  # a human pause first
        self.assertIsNone(picker.step(frame, 12.0, ocr))
        tile = picker.step(frame, 17.0, ocr)
        self.assertIsNotNone(tile)
        self.assertIn(tile.name, FREE_ROSTER)
        x, y = tile.center
        self.assertTrue(950 <= y <= 1250 and 200 <= x <= 2300)
        ocr.text = "ESC取消选择"
        self.assertIsNone(picker.step(frame, 18.0, ocr))
        self.assertTrue(picker.done)
        ocr.text = "选择传奇"
        self.assertIsNone(picker.step(frame, 30.0, ocr))

    def test_a_click_that_did_not_take_retries_another_portrait_then_gives_up(self) -> None:
        frame = roster_frame("bot_royale_our_turn")
        ocr = HintOcr()
        ocr.text = "选择传奇"
        picker = LegendPicker(random.Random(5))
        clicked = []
        for second in range(60):
            tile = picker.step(frame, float(second), ocr)
            if tile is not None:
                clicked.append(tile.name)
        self.assertEqual(len(clicked), MAX_TRIES)
        self.assertEqual(len(set(clicked)), MAX_TRIES)
        self.assertTrue(picker.done)

    def test_no_coloured_portrait_leaves_the_choice_to_the_game(self) -> None:
        ocr = HintOcr()
        ocr.text = "选择传奇"
        picker = LegendPicker(random.Random(1))
        blank = np.zeros((1440, 2560, 3), dtype=np.uint8)
        for second in range(20):
            self.assertIsNone(picker.step(blank, float(second), ocr))
        self.assertTrue(picker.done)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import cv2
import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.hud import FrameHud, keycaps
from apex_automation.match_brain import HudReading
from apex_automation.ocr_obstacles import OcrToken


FIXTURES = REPOSITORY_ROOT / "tests" / "windows" / "fixtures" / "hud"


def frame(name: str) -> np.ndarray:
    """Crops from the 2026-10-29 recordings pasted back where they were on a 2560x1440 frame."""
    manifest = json.loads((FIXTURES / "manifest.json").read_text(encoding="utf-8"))
    canvas = np.zeros((1440, 2560, 3), dtype=np.uint8)
    for piece in manifest[name]["pieces"]:
        crop = cv2.imread(str(FIXTURES / piece["file"]))
        y, x = int(piece["y"]), int(piece["x"])
        canvas[y : y + crop.shape[0], x : x + crop.shape[1]] = crop
    return canvas


class ScriptedOcr:
    def __init__(self, **texts: str) -> None:
        self.texts = texts
        self.regions: list[tuple[str, tuple[int, int, int, int]]] = []

    def read(self, frame: np.ndarray, region) -> tuple[OcrToken, ...]:
        self.regions.append((region.name, region.roi))
        text = self.texts.get(region.name)
        return () if text is None else (OcrToken(text, 0.99),)


class FrameHudTests(unittest.TestCase):
    def test_a_hit_from_behind_left_shows_the_arrow_and_the_red_trail(self) -> None:
        reading = FrameHud().read(frame("hurt_arrow"))
        self.assertTrue(reading.hurt)
        self.assertTrue(reading.has_weapon)
        self.assertIsNotNone(reading.hit_bearing)
        self.assertAlmostEqual(reading.hit_bearing, -104, delta=15)
        self.assertLess(reading.vitality, 0.98)

    def test_empty_slots_read_as_no_weapon_on_full_health(self) -> None:
        reading = FrameHud().read(frame("empty_handed"))
        self.assertIs(reading.has_weapon, False)
        self.assertGreater(reading.vitality, 0.95)
        self.assertGreater(reading.shield, 0.9)
        self.assertFalse(reading.hurt)
        self.assertIsNone(reading.hit_bearing)
        self.assertFalse(reading.hit_confirmed)

    def test_a_pistol_in_hand_reads_as_a_weapon(self) -> None:
        self.assertIs(FrameHud().read(frame("pistol")).has_weapon, True)

    def test_a_hidden_hud_reads_as_nothing_rather_than_zero_health(self) -> None:
        self.assertEqual(FrameHud(ScriptedOcr(compassHeading="65")).read(frame("hud_hidden")), HudReading())

    def test_the_pickup_card_is_read_beside_its_keycap(self) -> None:
        image = frame("pickup_card")
        caps = keycaps(image)
        self.assertTrue(caps)
        self.assertLess(abs(caps[0].x - 1356) + abs(caps[0].y - 391), 6)
        ocr = ScriptedOcr(pickupPrompt="捡起")
        reading = FrameHud(ocr).read(image)
        self.assertTrue(reading.pickup_prompt)
        roi = next(roi for name, roi in ocr.regions if name == "pickupPrompt")
        self.assertGreaterEqual(roi[0], caps[0].x + caps[0].w)
        # Without OCR nothing claims a prompt.
        self.assertFalse(FrameHud().read(image).pickup_prompt)

    def test_other_words_beside_a_keycap_are_not_a_prompt(self) -> None:
        self.assertFalse(FrameHud(ScriptedOcr(pickupPrompt="标记")).read(frame("pickup_card")).pickup_prompt)

    def test_a_shot_target_shows_its_bar_near_the_crosshair(self) -> None:
        self.assertTrue(FrameHud().read(frame("target_bar")).hit_confirmed)

    def test_scene_change_compares_consecutive_reads(self) -> None:
        hud = FrameHud()
        self.assertIsNone(hud.read(frame("empty_handed")).scene_change)
        self.assertLess(hud.read(frame("empty_handed")).scene_change, 0.5)
        self.assertGreater(hud.read(frame("target_bar")).scene_change, 0.5)

    def test_the_compass_heading_is_parsed(self) -> None:
        self.assertEqual(FrameHud(ScriptedOcr(compassHeading="202")).read(frame("empty_handed")).heading, 202)
        self.assertIsNone(FrameHud(ScriptedOcr(compassHeading="4O2")).read(frame("empty_handed")).heading)


if __name__ == "__main__":
    unittest.main()

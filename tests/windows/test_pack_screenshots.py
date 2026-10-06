"""Opt-in OCR regression on cropped, account-name-free production evidence."""
from pathlib import Path
import os
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.apex_packs import parse_pack_tokens, notification_marker
from apex_automation.ocr_obstacles import RapidOcrProvider
from apex_automation.notifications import find_close_cross


@unittest.skipUnless(os.environ.get("RUN_PACK_OCR_TESTS") == "1", "requires installed RapidOCR models")
class PackScreenshotTest(unittest.TestCase):
    def test_real_upper_lower_missing_and_obscured_cards(self):
        import cv2
        provider = RapidOcrProvider()
        fixtures = Path(__file__).parent / "fixtures" / "apex-packs"
        for name, count, status in (("upper-23",23,"OK"), ("lower-4",4,"OK"),
                                    ("missing",None,"NOT_VISIBLE"), ("hand-input",None,"OCCLUDED"),
                                    ("uu-remote",None,"OCCLUDED")):
            with self.subTest(name=name):
                frame = cv2.resize(cv2.imread(str(fixtures / (name+".png"))), None, fx=8/3, fy=8/3)
                tokens = provider.read_with_boxes(frame)
                reading = parse_pack_tokens(tokens)
                self.assertEqual((reading.count,reading.status), (count,status))
                if status == "OCCLUDED":
                    marker = notification_marker(tokens)
                    # HWND rects are supplied by Windows at runtime. These
                    # labelled crop-relative rects check glyph detection only.
                    rect = (37,45,220,159) if name == "hand-input" else (33,95,212,151)
                    rect = tuple(round(v*8/3) for v in rect)
                    self.assertIsNotNone(find_close_cross(frame,rect,marker[1].roi))

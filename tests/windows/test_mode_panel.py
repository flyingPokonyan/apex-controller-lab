from __future__ import annotations

from pathlib import Path
import sys
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.mode_panel import matching_text_token
from apex_automation.ocr_obstacles import OcrToken


def token(text: str, roi: tuple[int, int, int, int] | None) -> OcrToken:
    return OcrToken(text, 0.99, roi)


class ModePanelTextTest(unittest.TestCase):
    def test_the_full_title_matches_as_a_substring_and_not_exactly(self) -> None:
        title = token("进化版机器人大逃杀", (1700, 650, 2320, 720))
        found, missing = matching_text_token((title,), ("机器人",))
        self.assertIs(found, title)
        self.assertFalse(missing)
        exact, exact_missing = matching_text_token((title,), ("机器人",), exact=True)
        self.assertIsNone(exact)
        self.assertFalse(exact_missing)

    def test_a_split_title_uses_the_piece_that_contains_the_word(self) -> None:
        pieces = (
            token("进化版", (1700, 650, 1900, 720)),
            token("机器人大逃杀", (1900, 650, 2340, 720)),
        )
        found, missing = matching_text_token(pieces, ("机器人",))
        self.assertEqual(found.text, "机器人大逃杀")
        self.assertEqual(found.roi, (1900, 650, 2340, 720))
        self.assertFalse(missing)

    def test_confirm_must_sit_on_the_same_card_as_the_title(self) -> None:
        title = token("进化版机器人大逃杀", (1700, 650, 2320, 720))
        far = token("选择模式", (980, 780, 1240, 840))
        near = token("选择模式", (1900, 790, 2200, 850))
        tokens = (title, far, near)
        found, _missing = matching_text_token(
            tokens,
            ("选择模式",),
            near_anchors=("机器人",),
        )
        self.assertIs(found, near)
        only_far, _missing_far = matching_text_token(
            (title, far),
            ("选择模式",),
            near_anchors=("机器人",),
        )
        self.assertIsNone(only_far)

    def test_welcome_exact_match_skips_the_lock_card(self) -> None:
        locked = token("完成迎新赛即可解锁", (400, 700, 900, 780))
        title = token("迎新赛", (200, 500, 480, 560))
        missed, _missing = matching_text_token((locked,), ("迎新赛",), exact=True)
        self.assertIsNone(missed)
        found, _missing_found = matching_text_token((locked, title), ("迎新赛",), exact=True)
        self.assertIs(found, title)

    def test_a_hit_without_a_box_is_not_a_click(self) -> None:
        bare = token("进化版机器人大逃杀", None)
        found, missing = matching_text_token((bare,), ("机器人",))
        self.assertIsNone(found)
        self.assertTrue(missing)


if __name__ == "__main__":
    unittest.main()

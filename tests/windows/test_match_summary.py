import unittest

import numpy as np

from windows.apex_automation.match_summary import read_squad_summary
from windows.apex_automation.ocr_obstacles import OcrToken


class Provider:
    """What each summary region read on the 10-29 trial page (run 589, 00:26)."""

    def __init__(self, **texts):
        self.texts = texts

    def read(self, frame, region):
        # Only our (middle) column carries text in these tests.
        if region.roi[0] != 985 and region.roi[0] != 990:
            return ()
        text = self.texts.get(region.name.split(":", 1)[1])
        return () if text is None else (OcrToken(text, 0.95),)


FRAME = np.zeros((1440, 2560, 3), dtype=np.uint8)


class SquadSummaryTest(unittest.TestCase):
    def test_our_column_is_read_into_numbers(self):
        provider = Provider(name="EliasDalton", damageLabel="造成伤害", kad="0/0/0", damage="1,124", alive="10:24")
        column = {"name": "EliasDalton", "kills": 0, "assists": 0, "knocks": 0, "damage": 1124, "aliveS": 624}
        self.assertEqual(read_squad_summary(provider, FRAME), {**column, "nameMatched": False, "squad": [column]})
        self.assertTrue(read_squad_summary(provider, FRAME, expected_name="Elias_Dalton")["nameMatched"])

    def test_the_game_over_banner_is_not_a_summary(self):
        self.assertIsNone(read_squad_summary(Provider(name="游戏结束"), FRAME))

    def test_a_garbled_value_stays_unknown(self):
        provider = Provider(name="x", damageLabel="遗成伤害", kad="010/0a", damage="", alive="13-07")
        summary = read_squad_summary(provider, FRAME)
        self.assertIsNone(summary["kills"])
        self.assertIsNone(summary["damage"])
        self.assertEqual(summary["aliveS"], 787)


if __name__ == "__main__":
    unittest.main()

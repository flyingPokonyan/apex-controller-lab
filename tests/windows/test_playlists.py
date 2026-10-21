from __future__ import annotations

import json
from pathlib import Path
import sys
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation import cli
from apex_automation.account_provider import HttpAccountProvider, LeaseProviderError
from apex_automation.capabilities import CapabilitySet
from apex_automation.config import PLAY_CONFIG_PATH, load_config
from apex_automation.ocr_obstacles import OcrToken
from apex_automation.ocr_states import OcrStateDetector
from apex_automation.playlists import (
    FILL_OFF_STATE, trios_capabilities, trios_game_states, trios_guard_states, trios_overlays,
)


CONFIG = REPOSITORY_ROOT / "windows" / "config"
STATES = json.loads((CONFIG / "game-states.zh-CN.json").read_text(encoding="utf-8"))
OVERLAYS = json.loads((CONFIG / "play-overlays.zh-CN.json").read_text(encoding="utf-8"))
CAPABILITIES = json.loads((CONFIG / "enter-game.zh-CN.json").read_text(encoding="utf-8"))


def tokens(*items):
    return tuple(OcrToken(text, 0.99, roi) for text, roi in items)


# OCR of the operator's 10-21 frames (1280x720, scaled to the 2560x1440 canvas).
LOBBY_READY = tokens(
    ("补满", (95, 201, 173, 245)), ("derrickvasquezi16405", (190, 261, 399, 291)),
    ("关于", (273, 997, 345, 1042)), ("21", (2091, 1054, 2210, 1143)), ("APEX", (2200, 1058, 2302, 1104)),
    ("未上榜", (73, 1083, 179, 1129)), ("组合包", (2198, 1096, 2308, 1142)), ("准备", (1219, 1257, 1340, 1326)),
    ("三人赛", (78, 1289, 173, 1327)), ("奥林匹斯49分钟57秒", (78, 1323, 349, 1353)),
)
LOBBY_SELECT = tuple(OcrToken("选择", 0.99, (1218, 1254, 1342, 1327)) if t.text == "准备" else t for t in LOBBY_READY)
BOT_LOBBY = tokens(
    ("补满", (95, 201, 173, 245)), ("进化版机器人大逃杀", (73, 1083, 410, 1129)),
    ("准备", (1219, 1257, 1340, 1326)), ("三人赛", (78, 1289, 173, 1327)),
)
PANEL = tokens(
    ("模式", (1029, 28, 1128, 85)), ("私人游戏", (1389, 29, 1564, 83)), ("核心", (385, 150, 457, 196)),
    ("自由玩法", (994, 153, 1120, 193)), ("训练", (1596, 151, 1670, 195)), ("排位赛", (416, 666, 571, 726)),
    ("进化版机器人大逃杀", (1628, 666, 2070, 726)), ("三人赛", (419, 714, 530, 756)),
    ("残月2小时20分钟", (1629, 789, 1906, 828)), ("未上榜", (414, 1001, 573, 1069)),
    ("三人赛", (418, 1054, 530, 1100)), ("奥林匹斯50分钟20秒", (421, 1132, 739, 1168)),
    ("ESC返回", (80, 1319, 221, 1365)),
)
PANEL_HOVERED = tokens(
    ("核心", (385, 150, 457, 196)), ("自由玩法", (994, 153, 1120, 193)), ("排位赛", (416, 666, 571, 726)),
    ("进化版机器人大逃杀", (1628, 666, 2061, 726)),
    ("三人一队一起搜刮击杀并在安全区中生", (436, 1001, 914, 1039)), ("存下来", (618, 1037, 715, 1074)),
    ("选择模式", (458, 1125, 585, 1161)), ("更多信息", (651, 1123, 777, 1164)),
    ("马上开玩", (869, 1202, 984, 1241)), ("三人赛", (881, 1342, 963, 1374)), ("ESC返回", (80, 1319, 220, 1365)),
)


class Screen:
    """A fake OCR that answers each region with the tokens whose centre lies inside it."""

    def __init__(self, items):
        self.items = items

    def read(self, _frame, region):
        left, top, right, bottom = region.roi
        return tuple(t for t in self.items
                     if left <= (t.roi[0] + t.roi[2]) / 2 <= right and top <= (t.roi[1] + t.roi[3]) / 2 <= bottom)


def frame(*, filled: bool) -> np.ndarray:
    image = np.zeros((1440, 2560, 3), np.uint8)
    if filled:
        # The tick fills 0.28 of the checkbox crop on the reference frames.
        image[212:218, 75:97] = 255
    return image


def state(payload, items, *, filled=True):
    analysis = OcrStateDetector.from_payload(Screen(items), payload).analyze(frame(filled=filled))
    return analysis.decision.state if analysis.decision else None


class TriosStatesTest(unittest.TestCase):
    trios = trios_game_states(STATES)

    def test_the_trios_lobby_queues_only_with_fill_ticked(self):
        self.assertEqual(state(self.trios, LOBBY_READY, filled=True), "LOBBY_READY_TARGET")
        self.assertEqual(state(self.trios, LOBBY_READY, filled=False), FILL_OFF_STATE)

    def test_a_lobby_that_still_needs_a_mode_opens_the_panel(self):
        self.assertEqual(state(self.trios, LOBBY_SELECT), "LOBBY_SELECT_REQUIRED")

    def test_the_bot_lobby_is_another_mode_for_a_trios_lease(self):
        self.assertEqual(state(self.trios, BOT_LOBBY, filled=True), "LOBBY_READY_OTHER")
        self.assertEqual(state(self.trios, BOT_LOBBY, filled=False), "LOBBY_READY_OTHER")
        # The bot rules are untouched for every other lease.
        self.assertEqual(state(STATES, BOT_LOBBY, filled=False), "LOBBY_READY_TARGET")
        self.assertEqual(state(STATES, BOT_LOBBY, filled=True), "LOBBY_READY_TARGET_FILL_ON")

    def test_the_unranked_card_is_found_plain_and_hovered(self):
        self.assertEqual(state(self.trios, PANEL), "MODE_PANEL_TARGET_VISIBLE")
        self.assertEqual(state(self.trios, PANEL_HOVERED), "MODE_PANEL_TARGET_HOVERED")

    def test_the_panel_search_needs_the_title_and_a_column_header(self):
        overlays = trios_overlays(OVERLAYS)
        panel_rule = next(rule for rule in overlays["rules"] if rule["id"] == "mode-panel-target-visible")
        only = dict(overlays, rules=[panel_rule])
        search = Screen(PANEL)
        self.assertEqual(OcrStateDetector.from_payload(search, only).analyze(frame(filled=True)).decision.state,
                         "MODE_PANEL_TARGET_VISIBLE")
        # The lobby carries 未上榜 too, but no panel headers.
        self.assertIsNone(OcrStateDetector.from_payload(Screen(LOBBY_READY), only).analyze(frame(filled=True)).decision)


class TriosCapabilitiesTest(unittest.TestCase):
    def test_fill_is_ticked_instead_of_cleared_and_the_card_is_named_by_its_title(self):
        payload = trios_capabilities(CAPABILITIES)
        capabilities = {capability["id"]: capability for capability in payload["capabilities"]}
        self.assertNotIn("lobby-disable-fill", capabilities)
        self.assertEqual(capabilities["lobby-enable-fill"]["states"], [FILL_OFF_STATE])
        self.assertEqual(payload["actions"]["targetCardClick"]["any"], ["未上榜"])
        self.assertEqual(payload["actions"]["targetConfirmClick"]["region"], "modePanelUnrankedConfirm")
        self.assertNotIn("nearAnchor", payload["actions"]["targetConfirmClick"])
        for capability in payload["capabilities"]:
            self.assertNotIn("LOBBY_READY_TARGET_FILL_ON", capability.get("allowedNextStates", []))
        CapabilitySet.from_payload(payload)
        # The bot set is untouched.
        self.assertIn("lobby-disable-fill", {c["id"] for c in CAPABILITIES["capabilities"]})

    def test_both_rule_sets_pass_the_startup_checks(self):
        config = load_config(PLAY_CONFIG_PATH)
        bots = cli._play_rules(config, object(), CAPABILITIES, STATES, OVERLAYS)
        trios = cli._play_rules(config, object(), trios_capabilities(CAPABILITIES),
                                trios_game_states(STATES), trios_overlays(OVERLAYS),
                                trios_guard_states(config.overlay_ocr["guardStates"]))
        self.assertIn(FILL_OFF_STATE, trios[1].states)
        self.assertNotIn(FILL_OFF_STATE, bots[1].states)


class LeasePlaylistTest(unittest.TestCase):
    def test_only_trios_with_fill_is_accepted(self):
        self.assertIsNone(HttpAccountProvider._playlist({}))
        self.assertEqual(HttpAccountProvider._playlist({"playlist": "trios", "fill": True}), "trios")
        for payload in ({"playlist": "trios"}, {"playlist": "duos", "fill": True}, {"playlist": None}):
            with self.assertRaises(LeaseProviderError):
                HttpAccountProvider._playlist(payload)


if __name__ == "__main__":
    unittest.main()

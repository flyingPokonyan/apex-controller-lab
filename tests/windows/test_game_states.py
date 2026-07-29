from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.ocr_obstacles import OcrToken
from apex_automation.ocr_states import OcrStateDetector


STATES_PATH = REPOSITORY_ROOT / "windows" / "config" / "game-states.zh-CN.json"

# Verbatim readings taken from live DXGI frames on 2026-07-29, including the
# ones that only look like evidence: an alive frame reads a compass number in
# the spectate region, and a spectating frame reads a revive countdown where
# the alive frame carries the squad count.
LIVE_READINGS: dict[str, dict[str, tuple[str, float]]] = {
    "LOBBY_READY_UNRANKED": {
        "lobbyModeName": ("未上榜", 0.999),
        "lobbyPrimaryButton": ("准备", 1.000),
    },
    "LOBBY_READY_TRAINING": {
        "lobbyModeName": ("训练", 1.000),
        "lobbyPrimaryButton": ("准备", 1.000),
    },
    "LOBBY_SELECT_REQUIRED": {
        "lobbyModeName": ("训练", 1.000),
        "lobbyPrimaryButton": ("选择", 1.000),
    },
    # Queueing keeps showing the mode it queued into, so the mode name alone
    # can never tell this apart from a lobby that is merely ready. The button
    # is what changes, and it is the only thing that does so reliably.
    "LOBBY_QUEUEING": {
        "lobbyModeName": ("未上榜", 0.999),
        "lobbyPrimaryButton": ("取消", 1.000),
    },
    "POST_MATCH_SUMMARY": {
        "postMatchHints": ("SPACE继续 TAB返回大厅 ESC打开菜单", 0.973),
        "postMatchTabs": ("死亡回放总结经验值通行证", 1.000),
    },
    "SPECTATING": {
        "spectateTabs": ("观战小队", 0.989),
        "squadCountAlive": ("37", 0.993),
    },
    "IN_MATCH_ALIVE": {
        "squadCountAlive": ("20 剩余小队数量", 0.981),
        "spectateTabs": ("62", 0.996),
    },
    "CLIMB_SETTINGS_MODAL": {
        "modalTitle": ("攀爬加速设置", 1.000),
    },
    "CONTINUE": {
        "titleContinue": ("继续", 1.000),
    },
    "DROPSHIP_FOLLOWING": {
        "dropshipPrompt": ("LCTRL单独发射", 0.913),
    },
    "LAUNCH_READY": {
        "dropshipPrompt": ("LCTRL重新加入小队", 1.000),
    },
    # The squad count is on screen during freefall too, so this reading only
    # routes correctly if the more specific rule is evaluated first.
    "FREEFALL": {
        "freefallSpeedLabel": ("速度", 1.000),
        "squadCountAlive": ("18 剩余小队数量", 0.98),
    },
}


class ScriptedProvider:
    def __init__(self, readings: dict[str, tuple[str, float]]):
        self.readings = readings

    def read(self, frame: np.ndarray, region) -> tuple[OcrToken, ...]:
        item = self.readings.get(region.name)
        return () if item is None else (OcrToken(item[0], item[1]),)


class GameStateRoutingTest(unittest.TestCase):
    def _detector(self, readings: dict[str, tuple[str, float]]) -> OcrStateDetector:
        return OcrStateDetector.from_path(ScriptedProvider(readings), STATES_PATH)

    def test_every_live_reading_routes_to_its_own_state(self) -> None:
        for expected, readings in LIVE_READINGS.items():
            with self.subTest(state=expected):
                analysis = self._detector(readings).analyze(np.zeros((4, 4, 3), np.uint8))
                self.assertIsNotNone(analysis.decision, f"{expected} 未命中任何规则")
                self.assertEqual(analysis.decision.state, expected)

    def test_no_reading_routes_to_a_state_it_is_not(self) -> None:
        for owner, readings in LIVE_READINGS.items():
            decision = self._detector(readings).analyze(np.zeros((4, 4, 3), np.uint8)).decision
            for other in LIVE_READINGS:
                if other != owner:
                    with self.subTest(reading=owner, other=other):
                        self.assertNotEqual(decision.state, other)

    def test_screens_with_no_rule_stay_unknown(self) -> None:
        # Continue page, legend select, dropship and the mode panel all read
        # something in these regions; none of it may be mistaken for evidence.
        for label, readings in {
            "continue": {"lobbyPrimaryButton": ("点击继续", 0.99)},
            "dropship": {"squadCountAlive": ("140", 0.98), "spectateTabs": ("315", 0.97)},
            "mode-panel": {"lobbyModeName": ("核心", 0.99)},
            # The dropship prompt region overlaps the lobby button box, so an
            # in-flight frame reads something there on every single pass.
            "dropship-prompt": {"lobbyPrimaryButton": ("LCTRL单独发射", 0.91)},
            "control-mode-hud": {"lobbyPrimaryButton": ("与基地相连的区域", 0.95)},
        }.items():
            with self.subTest(screen=label):
                analysis = self._detector(readings).analyze(np.zeros((4, 4, 3), np.uint8))
                self.assertIsNone(analysis.decision)

    def test_low_confidence_readings_are_not_evidence(self) -> None:
        faint = {name: (text, 0.4) for name, (text, _) in LIVE_READINGS["POST_MATCH_SUMMARY"].items()}
        analysis = self._detector(faint).analyze(np.zeros((4, 4, 3), np.uint8))
        self.assertIsNone(analysis.decision)

    def test_a_modal_outranks_the_screen_it_covers(self) -> None:
        # The climb prompt appears over the lobby on the way back from a match;
        # whatever shows through it must not win the routing.
        covered = {
            **LIVE_READINGS["CLIMB_SETTINGS_MODAL"],
            **LIVE_READINGS["LOBBY_READY_UNRANKED"],
        }
        analysis = self._detector(covered).analyze(np.zeros((4, 4, 3), np.uint8))
        self.assertEqual(analysis.decision.state, "CLIMB_SETTINGS_MODAL")

    def test_a_lobby_on_any_other_mode_is_still_a_lobby(self) -> None:
        # Verbatim from the 2026-07-29 session at 592.3s, which this table did
        # not recognise at all: a whole lobby that reads as nothing means the
        # runner sits there forever once a match ends on that mode.
        analysis = self._detector(
            {"lobbyModeName": ("控制", 0.999), "lobbyPrimaryButton": ("准备", 1.000)}
        ).analyze(np.zeros((4, 4, 3), np.uint8))
        self.assertEqual(analysis.decision.state, "LOBBY_READY_OTHER")

    def test_the_catch_all_lobby_rule_never_shadows_a_named_mode(self) -> None:
        # It matches on the button alone, so it only means "some other mode"
        # while it stays last. Ordering is the only thing expressing that.
        detector = self._detector({})
        enabled = [rule.state for rule in detector.rules if rule.enabled]
        self.assertEqual(enabled[-1], "LOBBY_READY_OTHER")
        for named in ("LOBBY_READY_TRAINING", "LOBBY_READY_UNRANKED"):
            with self.subTest(state=named):
                self.assertLess(enabled.index(named), enabled.index("LOBBY_READY_OTHER"))

    def test_queueing_outranks_the_mode_it_queued_into(self) -> None:
        # Whichever mode is on the card, and whether matchmaking is still
        # searching or already connecting, the screen means "wait".
        for label, mode in {"training": "训练", "unranked": "未上榜"}.items():
            with self.subTest(mode=label):
                analysis = self._detector(
                    {"lobbyModeName": (mode, 1.000), "lobbyPrimaryButton": ("取消", 1.000)}
                ).analyze(np.zeros((4, 4, 3), np.uint8))
                self.assertEqual(analysis.decision.state, "LOBBY_QUEUEING")

    def test_every_region_is_declared_single_line(self) -> None:
        detector = self._detector({})
        for name, region in detector.regions.items():
            with self.subTest(region=name):
                # Reading without detection is what makes these regions cost
                # 4-16ms instead of seconds, and it only works on one line.
                self.assertTrue(region.single_line, f"{name} 未声明 singleLine")


if __name__ == "__main__":
    unittest.main()

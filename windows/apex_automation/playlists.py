"""Unranked trios as the target playlist instead of Bot Royale Evolved.

Forge sends `playlist: "trios"` on a lease once an account bound past level 20
has reached it: accounts that went on to 23 on the bot playlist were closed by
EA's nightly sweep (10-10..10-20: 9/9), so the levels past 20 are played in
unranked trios with fill on.

The lobby and mode-panel rules are the bot ones with the target swapped, so
everything else (queueing, the match, leaving it) stays exactly as it is. The
operator's 10-21 reference frames, measured on the 2560x1440 canvas:

- Panel: the 未上榜 card sits in the 核心 column, title at x414-573 y1001-1069.
  Hovered, the title is replaced by its description and 「选择模式」 shows at
  x458-585 y1125-1161; nothing names the card any more, so the confirm click
  is placed by that row alone, not near the title as for the bot card.
- Lobby: the mode card reads 未上榜 at the top (inside lobbyModeName) and
  三人赛 at the bottom, y1289-1327. That bottom line moved below the old
  lobbyModeSquad box, so the trios rules read the whole lower card.
- 补满 checked reads 0.279 bright in lobbyFillCheckbox, the same as the bot
  lobby's 0.284. Unchecked reads 0.
"""

from __future__ import annotations

import copy
from typing import Any


BOTS = "bots"
TRIOS = "trios"
TRIOS_TITLE = "未上榜"
FILL_OFF_STATE = "LOBBY_READY_TARGET_FILL_OFF"
# Words only the mode panel prints: its column headers. A map loading screen
# shows the playlist title too (10-14..10-21 incidents read 「进化版机器人大逃杀」
# there) but none of these.
PANEL_HEADERS = ["核心", "自由玩法"]

_STATE_REGIONS = {
    "modePanelUnrankedTitle": {
        "roi": [370, 960, 800, 1095],
        "_note": "核心栏下面那张未上榜卡片的标题行，2026-10-21 操作者截图实测 x414-573 y1001-1069。悬停后这一行变成说明文字。",
    },
    "modePanelUnrankedConfirm": {
        "roi": [380, 1100, 820, 1190],
        "singleLine": True,
        "_note": "未上榜卡片被悬停时底部的「选择模式  更多信息」，实测 x458-585 y1125-1161。未悬停时同一行是「地图｜倒计时」。",
    },
    "lobbyModeCardLower": {
        "roi": [60, 1140, 480, 1340],
        "_note": "大厅模式卡片标题以下整块。2026-10-21 改版后三人赛在 y1289-1327，旧的 lobbyModeSquad 框不再包住它。",
    },
}
_OVERLAY_REGIONS = {
    "modePanelUnrankedConfirm": {
        "roi": [380, 1100, 820, 1190],
        "_note": "同快速识别里的同名区域，供「选择模式」文字点击使用。",
    },
}


def _replace_rule(rules: list[dict[str, Any]], rule_id: str, replacement: dict[str, Any]) -> None:
    for index, rule in enumerate(rules):
        if rule.get("id") == rule_id:
            rules[index] = replacement
            return
    raise ValueError(f"三人赛规则找不到要替换的 {rule_id}")


def trios_game_states(payload: dict[str, Any]) -> dict[str, Any]:
    states = copy.deepcopy(payload)
    states["regions"].update(copy.deepcopy(_STATE_REGIONS))
    rules = states["rules"]
    _replace_rule(rules, "mode-panel-target-hovered", {
        "id": "mode-panel-trios-hovered", "state": "MODE_PANEL_TARGET_HOVERED",
        "requirements": [{"region": "modePanelUnrankedConfirm", "all": ["选择模式"]}],
    })
    _replace_rule(rules, "mode-panel-target-visible", {
        "id": "mode-panel-trios-visible", "state": "MODE_PANEL_TARGET_VISIBLE",
        "requirements": [{"region": "modePanelUnrankedTitle", "all": [TRIOS_TITLE]}],
    })
    lobby = [
        {"region": "lobbyPrimaryButton", "all": ["准备"]},
        {"region": "lobbyModeName", "all": [TRIOS_TITLE]},
        {"region": "lobbyModeCardLower", "all": ["三人赛"]},
        {"region": "lobbyFillLabel", "all": ["补满"]},
    ]
    # Fill on is the target lobby; the unchecked one must be ticked first. A
    # reading between the two matches neither and falls through to the other-
    # mode lobby, which reopens the panel rather than queueing alone.
    _replace_rule(rules, "lobby-ready-target-fill-on", {
        "id": "lobby-ready-trios-filled", "state": "LOBBY_READY_TARGET",
        "requirements": copy.deepcopy(lobby),
        "pixelRequirements": [{"region": "lobbyFillCheckbox", "brightAbove": 0.75, "minRatio": 0.15}],
    })
    _replace_rule(rules, "lobby-ready-target", {
        "id": "lobby-ready-trios-unfilled", "state": FILL_OFF_STATE,
        "requirements": copy.deepcopy(lobby),
        "pixelRequirements": [{"region": "lobbyFillCheckbox", "brightAbove": 0.75, "maxRatio": 0.05}],
    })
    return states


def trios_overlays(payload: dict[str, Any]) -> dict[str, Any]:
    overlays = copy.deepcopy(payload)
    overlays["regions"].update(copy.deepcopy(_OVERLAY_REGIONS))
    _replace_rule(overlays["rules"], "mode-panel-target-visible", {
        "id": "mode-panel-target-visible", "state": "MODE_PANEL_TARGET_VISIBLE",
        "requirements": [
            {"region": "modePanelSearch", "all": [TRIOS_TITLE], "minConfidence": 0.7},
            {"region": "modePanelHeaders", "any": list(PANEL_HEADERS), "minConfidence": 0.7},
        ],
    })
    return overlays


def trios_guard_states(states: list[str]) -> list[str]:
    """The overlay guard list with the unticked trios lobby where the bot fill-on lobby was."""
    return [FILL_OFF_STATE if state == "LOBBY_READY_TARGET_FILL_ON" else state for state in states]


def trios_capabilities(payload: dict[str, Any]) -> dict[str, Any]:
    capabilities = copy.deepcopy(payload)
    actions = capabilities["actions"]
    actions["targetCardClick"] = {"region": "modePanelSearch", "any": [TRIOS_TITLE], "minConfidence": 0.7}
    actions["targetConfirmClick"] = {"region": "modePanelUnrankedConfirm", "any": ["选择模式"], "minConfidence": 0.7}
    for capability in capabilities["capabilities"]:
        if capability.get("id") == "lobby-disable-fill":
            capability.update(id="lobby-enable-fill", states=[FILL_OFF_STATE], allowedNextStates=["LOBBY_READY_TARGET"])
            capability["_note"] = "三人赛要开补满：勾没打上时点一下复选框。toggle 不重试，点空了停下等人。"
        next_states = capability.get("allowedNextStates")
        if isinstance(next_states, list):
            capability["allowedNextStates"] = [
                FILL_OFF_STATE if state == "LOBBY_READY_TARGET_FILL_ON" else state for state in next_states
            ]
    return capabilities

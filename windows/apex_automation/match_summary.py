"""The squad summary at the end of a match: our own kills, damage and time alive.

The post-match 「总结」 page lays the squad out in three columns, ours always
in the middle: name, 击杀/助攻/击倒, 造成伤害, 生存时间. Reading it needs no
input — the page is already on screen while the pilot heads back to the
lobby — and it is the only place the game states the damage we dealt.
Regions measured on the 10-29 trial frames (2560x1440).
"""

from __future__ import annotations

import re

from .ocr_obstacles import OcrProvider, Region, normalize_ocr_text


REGIONS = {
    "name": (985, 392, 1330, 432),
    "damageLabel": (985, 596, 1200, 630),
    "kad": (990, 540, 1180, 574),
    "damage": (990, 636, 1180, 670),
    "alive": (985, 718, 1200, 754),
}


def _text(ocr: OcrProvider, frame, name: str) -> str:
    tokens = ocr.read(frame, Region("summary:" + name, REGIONS[name], single_line=True))
    return "".join(token.text for token in tokens if token.confidence >= 0.5).strip()


def read_squad_summary(ocr: OcrProvider, frame) -> dict[str, object] | None:
    """Our column, or None when the page on screen is not the squad summary."""
    if frame.shape[1] != 2560 or frame.shape[0] != 1440:
        return None
    if "伤害" not in normalize_ocr_text(_text(ocr, frame, "damageLabel")):
        return None
    kad = re.findall(r"\d+", _text(ocr, frame, "kad"))
    damage = re.sub(r"[^0-9]", "", _text(ocr, frame, "damage"))
    alive = re.search(r"(\d{1,2})\D(\d{2})", _text(ocr, frame, "alive"))
    return {
        "name": _text(ocr, frame, "name"),
        "kills": int(kad[0]) if len(kad) == 3 else None,
        "assists": int(kad[1]) if len(kad) == 3 else None,
        "knocks": int(kad[2]) if len(kad) == 3 else None,
        "damage": int(damage) if damage else None,
        "aliveS": int(alive.group(1)) * 60 + int(alive.group(2)) if alive else None,
    }

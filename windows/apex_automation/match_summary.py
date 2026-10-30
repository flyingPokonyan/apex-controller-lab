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


# Our column, as measured.
REGIONS = {
    "name": (985, 392, 1330, 432),
    "damageLabel": (985, 596, 1200, 630),
    "kad": (990, 540, 1180, 574),
    "damage": (990, 636, 1180, 670),
    "alive": (985, 718, 1200, 754),
}
# The neighbours' offsets from ours: the columns are not evenly spaced
# (x 110 / 378 / 641 on the 960-wide trial frames).
LEFT_OFFSET, RIGHT_OFFSET = -714, 700


def _region(name: str, offset: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = REGIONS[name]
    return (x1 + offset, y1, x2 + offset, y2)


def _text(ocr: OcrProvider, frame, name: str, offset: int = 0) -> str:
    tokens = ocr.read(frame, Region("summary:" + name, _region(name, offset), single_line=True))
    return "".join(token.text for token in tokens if token.confidence >= 0.5).strip()


def _column(ocr: OcrProvider, frame, offset: int) -> dict[str, object] | None:
    if "伤害" not in normalize_ocr_text(_text(ocr, frame, "damageLabel", offset)):
        return None
    kad = re.findall(r"\d+", _text(ocr, frame, "kad", offset))
    damage = re.sub(r"[^0-9]", "", _text(ocr, frame, "damage", offset))
    alive = re.search(r"(\d{1,2})\D(\d{2})", _text(ocr, frame, "alive", offset))
    return {
        "name": _text(ocr, frame, "name", offset),
        "kills": int(kad[0]) if len(kad) == 3 else None,
        "assists": int(kad[1]) if len(kad) == 3 else None,
        "knocks": int(kad[2]) if len(kad) == 3 else None,
        "damage": int(damage) if damage else None,
        "aliveS": int(alive.group(1)) * 60 + int(alive.group(2)) if alive else None,
    }


def read_squad_summary(ocr: OcrProvider, frame, expected_name: str | None = None) -> dict[str, object] | None:
    """Our column (the middle one, or the one carrying ``expected_name``), or
    None when the page on screen is not the squad summary.

    Every column read rides along under ``squad`` so a report can be checked
    against the account's name later; ``nameMatched`` says whether ours was
    found by name or assumed to be the middle one.
    """
    if frame.shape[1] != 2560 or frame.shape[0] != 1440:
        return None
    middle = _column(ocr, frame, 0)
    if middle is None:
        return None
    squad = [column for column in (_column(ocr, frame, LEFT_OFFSET), middle, _column(ocr, frame, RIGHT_OFFSET)) if column]
    ours, matched = middle, False
    if expected_name:
        wanted = normalize_ocr_text(expected_name)
        for column in squad:
            if wanted and normalize_ocr_text(str(column["name"])) == wanted:
                ours, matched = column, True
                break
    return {**ours, "nameMatched": matched, "squad": squad}

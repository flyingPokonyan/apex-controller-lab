"""Pick a random unlocked legend on the legend-select screen.

Until now the game chose the legend at its timeout, so every match was played
with whatever it assigned. Here, once it is our turn, the picker waits a
moment, clicks one unlocked portrait at random and checks that the pick took;
up to three tries, then it leaves the choice to the game as before.

Whose turn it is: the bottom bar names whoever picks now, and we are not
always first (trios put us second on 2026-10-29). The key hint at the top
left is ours alone: 「选择传奇」 shows only on our turn, and after a click the
same line reads 「ESC 取消选择」.

The roster is the 2026-10-29 season layout on the 2560x1440 canvas, measured
from a recording: 突击 5 and 游击 7 on the top row, 侦察 6, 支援 6 and 控制 4
on the bottom row, 110 px apart. Unlocked portraits are in colour; locked
ones, and ones a teammate took (sepia), measure a saturation of 0–30, so only
tiles above UNLOCKED_SATURATION are candidates. A new season that moves the
grid leaves no colourful tile at these spots, and the game picks as before.
"""

from __future__ import annotations

from dataclasses import dataclass
import random

import cv2
import numpy as np

from .ocr_obstacles import OcrProvider, Region, normalize_ocr_text


# (first tile left x, tiles, row) per class group.
GROUPS = {
    "突击": (510, 5, 0),
    "游击": (1180, 7, 0),
    "侦察": (236, 6, 1),
    "支援": (1010, 6, 1),
    "控制": (1786, 4, 1),
}
ROWS = ((952, 1060), (1138, 1250))
PITCH = 110
TILE_WIDTH = 100
UNLOCKED_SATURATION = 40

TURN_HINT = Region("legendTurnHint", (90, 515, 340, 565), single_line=True)
OUR_TURN_WORD = "选择传奇"
PICKED_WORD = "取消选择"

FIRST_CLICK_DELAY_S = (1.5, 5.0)
RETRY_DELAY_S = (1.5, 3.0)
MAX_TRIES = 3


@dataclass(frozen=True)
class Tile:
    group: str
    index: int
    x1: int
    y1: int
    x2: int
    y2: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x1 + self.x2) // 2, (self.y1 + self.y2) // 2

    @property
    def name(self) -> str:
        return f"{self.group}{self.index + 1}"


TILES = tuple(
    Tile(group, index, x0 + index * PITCH, ROWS[row][0], x0 + index * PITCH + TILE_WIDTH, ROWS[row][1])
    for group, (x0, count, row) in GROUPS.items()
    for index in range(count)
)


def unlocked_tiles(frame: np.ndarray) -> list[Tile]:
    hsv = cv2.cvtColor(np.ascontiguousarray(frame[..., :3]), cv2.COLOR_BGR2HSV)
    found = []
    for tile in TILES:
        # Skip the lock badge at the bottom and the border.
        saturation = float(hsv[tile.y1 + 12 : tile.y2 - 30, tile.x1 + 10 : tile.x2 - 10, 1].mean())
        if saturation > UNLOCKED_SATURATION:
            found.append(tile)
    return found


def turn_hint(frame: np.ndarray, ocr: OcrProvider) -> str:
    """'ours', 'picked', or '' for anything else (a teammate's turn, loading)."""
    text = normalize_ocr_text("".join(token.text for token in ocr.read(frame, TURN_HINT)))
    if PICKED_WORD in text:
        return "picked"
    if OUR_TURN_WORD in text:
        return "ours"
    return ""


class LegendPicker:
    """One pick per round. ``step`` returns the tile to click now, if any."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng or random.Random()
        self.reset()

    def reset(self) -> None:
        self.tries = 0
        self.done = False
        self.next_at: float | None = None
        self.tried: set[str] = set()

    def step(self, frame: np.ndarray, now: float, ocr: OcrProvider) -> Tile | None:
        if self.done:
            return None
        hint = turn_hint(frame, ocr)
        if hint == "picked" or (hint == "" and self.tries):
            # The pick took (or the turn passed after a click).
            self.done = True
            return None
        if hint != "ours":
            return None
        if self.tries >= MAX_TRIES:
            self.done = True
            return None
        if self.next_at is None:
            delay = FIRST_CLICK_DELAY_S if not self.tries else RETRY_DELAY_S
            self.next_at = now + self.rng.uniform(*delay)
            return None
        if now < self.next_at:
            return None
        candidates = [tile for tile in unlocked_tiles(frame) if tile.name not in self.tried] or unlocked_tiles(frame)
        self.next_at = None
        if not candidates:
            self.done = True
            return None
        tile = self.rng.choice(candidates)
        self.tries += 1
        self.tried.add(tile.name)
        return tile

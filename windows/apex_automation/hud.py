"""Reads the player's own HUD for the active profile.

Calibrated on 2560x1440 recordings from 2026-10-29 (Bot Royale and trios,
Simplified Chinese, colorblind mode off). Every rule reads something the game
prints about the player themself:

- weapon: the big ammo count in the weapon panel is pure white with a weapon
  in hand and dim grey ``00`` when both slots are empty, and the slot number
  box under it (「1」 before the weapon name) only exists with a weapon;
- health and shield: the white bar under the player's name, and the shield
  cells above it; a fresh hit leaves a red trail at the health bar's end;
- damage direction: a red arrowhead appears around the crosshair on the side
  the damage came from (a filled chevron, 50–300 px, 150–650 px out). Red
  decals on loot bins pass the same test, so the brain only uses an arrow to
  aim a reaction the own bars started;
- hit confirmation: a damaged target's thin red health bar shows up close to
  the crosshair;
- heading: the bearing under the compass at the top (OCR, single line), used
  to measure how far a mouse move turns the view;
- pickup: a white 「E」 keycap with 捡起 (ground item card, which follows the
  item), 装备 (a weapon card) or 打开 (loot bin, fixed spot under the
  crosshair) beside it.

The game sometimes hides the whole bottom HUD for a moment (zone banners,
skydiving). The keycaps for the health item (「4」) and the grenade (「G」)
are always drawn while it shows, so without them the frame reads as nothing
rather than as an empty-handed player on zero health.

None of this locates other players for aiming. The arrow gives a direction to
turn, and the target bar only says "this direction lands".
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import cv2
import numpy as np

from .match_brain import HudReading
from .ocr_obstacles import OcrProvider, Region, normalize_ocr_text


FRAME_SIZE = (2560, 1440)
CROSSHAIR = (1280, 720)

# Always-drawn keycaps of the bottom HUD: 610–650 white pixels each when shown.
HUD_KEYCAPS = ((670, 1375, 705, 1410), (1885, 1375, 1920, 1410))
HUD_KEYCAP_WHITE_MIN = 300

# The big ammo digits of the weapon in hand.
AMMO_DIGITS = (2290, 1275, 2390, 1335)
AMMO_WHITE_MIN = 120
# The 「1」/「2」 box in front of the weapon name. Empty slots: none at all.
SLOT_BOX = (2025, 1378, 2075, 1414)
SLOT_WHITE_MIN = 250

# One row through the player's own health bar, and its full extent.
HEALTH_ROW = 1352
HEALTH_X = (236, 556)
# The shield cells above it. Two full evo cells light about 1,290 pixels.
SHIELD_ROWS = (1329, 1340)
SHIELD_FULL_PIXELS = 1290

# Arrowheads: filled to about a third of their box.
ARROW_AREA = (600, 25_000)
ARROW_SIDE = 40
ARROW_FILL = (0.22, 0.62)
ARROW_RADIUS = (140, 660)
# The bottom HUD band and the top bar carry red icons of their own.
ARROW_Y = (120, 1200)

# A damaged target's bar: thin, nearly solid, close to the crosshair.
TARGET_BAR_HEIGHT = (5, 18)
TARGET_BAR_WIDTH = (18, 160)
TARGET_BAR_FILL = 0.72
TARGET_BAR_RADIUS = 420

# Where E keycaps can show: item cards above and right of the crosshair, the
# loot-bin prompt just below and right of it.
PROMPT_SEARCH = (900, 100, 2300, 1000)
KEYCAP_SIDE = (26, 48)
KEYCAP_FILL = 0.65
PROMPT_WORDS = ("捡起", "装备", "打开")
MAX_KEYCAPS = 3

# The heading under the compass, e.g. 「65」.
COMPASS = (1245, 118, 1315, 160)


def _bgr(frame: np.ndarray) -> np.ndarray:
    return frame[..., :3]


def _red_mask(bgr: np.ndarray) -> np.ndarray:
    b = bgr[..., 0].astype(np.int16)
    g = bgr[..., 1].astype(np.int16)
    r = bgr[..., 2].astype(np.int16)
    return ((r > 180) & (g < 70) & (b < 80)).astype(np.uint8)


@dataclass(frozen=True)
class Blob:
    x: int
    y: int
    w: int
    h: int
    area: int
    cx: float
    cy: float

    @property
    def fill(self) -> float:
        return self.area / max(1, self.w * self.h)

    @property
    def radius(self) -> float:
        return math.hypot(self.cx - CROSSHAIR[0], self.cy - CROSSHAIR[1])

    @property
    def bearing(self) -> float:
        """0 straight ahead (above the crosshair), positive to the right."""
        return math.degrees(math.atan2(self.cx - CROSSHAIR[0], -(self.cy - CROSSHAIR[1])))


def _blobs(mask: np.ndarray, offset: tuple[int, int] = (0, 0), min_area: int = 40) -> list[Blob]:
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    found: list[Blob] = []
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        if area < min_area:
            continue
        cx, cy = centroids[index]
        found.append(Blob(x + offset[0], y + offset[1], w, h, area, float(cx) + offset[0], float(cy) + offset[1]))
    return found


def _white(frame: np.ndarray, box: tuple[int, int, int, int], level: int) -> int:
    x1, y1, x2, y2 = box
    return int((_bgr(frame)[y1:y2, x1:x2].min(axis=2) > level).sum())


def hud_shown(frame: np.ndarray) -> bool:
    return all(_white(frame, box, 200) >= HUD_KEYCAP_WHITE_MIN for box in HUD_KEYCAPS)


def has_weapon(frame: np.ndarray) -> bool:
    # Either alone misreads: snow behind an empty panel lights the digits, and
    # the slot box is half drawn while skydiving.
    return _white(frame, AMMO_DIGITS, 225) >= AMMO_WHITE_MIN and _white(frame, SLOT_BOX, 215) >= SLOT_WHITE_MIN


def shield(frame: np.ndarray) -> float:
    cells = _bgr(frame)[SHIELD_ROWS[0]:SHIELD_ROWS[1], HEALTH_X[0]:HEALTH_X[1]]
    return float((cells.max(axis=2) > 175).sum()) / SHIELD_FULL_PIXELS


def health(frame: np.ndarray) -> tuple[float, bool]:
    """Share of the bar still white, and whether a fresh red trail shows."""
    row = _bgr(frame)[HEALTH_ROW, HEALTH_X[0]:HEALTH_X[1]].astype(np.int16)
    white = row.min(axis=1) > 200
    red = (row[:, 2] > 170) & (row[:, 1] < 90) & (row[:, 0] < 90)
    return float(white.sum()) / (HEALTH_X[1] - HEALTH_X[0]), int(red.sum()) >= 12


def damage_arrows(frame: np.ndarray, red: np.ndarray | None = None) -> list[Blob]:
    mask = _red_mask(_bgr(frame)) if red is None else red
    arrows = []
    for blob in _blobs(mask, min_area=ARROW_AREA[0]):
        if not ARROW_AREA[0] <= blob.area <= ARROW_AREA[1]:
            continue
        if blob.w < ARROW_SIDE or blob.h < ARROW_SIDE:
            continue
        if not ARROW_FILL[0] <= blob.fill <= ARROW_FILL[1]:
            continue
        if not ARROW_RADIUS[0] <= blob.radius <= ARROW_RADIUS[1]:
            continue
        if not ARROW_Y[0] <= blob.cy <= ARROW_Y[1]:
            continue
        arrows.append(blob)
    return sorted(arrows, key=lambda blob: -blob.area)


def target_bar_near_crosshair(frame: np.ndarray, red: np.ndarray | None = None) -> bool:
    x1, y1 = CROSSHAIR[0] - TARGET_BAR_RADIUS, CROSSHAIR[1] - TARGET_BAR_RADIUS
    x2, y2 = CROSSHAIR[0] + TARGET_BAR_RADIUS, CROSSHAIR[1] + TARGET_BAR_RADIUS
    mask = (_red_mask(_bgr(frame)) if red is None else red)[y1:y2, x1:x2]
    for blob in _blobs(mask, (x1, y1), min_area=60):
        if (
            TARGET_BAR_HEIGHT[0] <= blob.h <= TARGET_BAR_HEIGHT[1]
            and TARGET_BAR_WIDTH[0] <= blob.w <= TARGET_BAR_WIDTH[1]
            and blob.w >= 2 * blob.h
            and blob.fill >= TARGET_BAR_FILL
            and blob.radius <= TARGET_BAR_RADIUS
        ):
            return True
    return False


def keycaps(frame: np.ndarray) -> list[Blob]:
    x1, y1, x2, y2 = PROMPT_SEARCH
    crop = _bgr(frame)[y1:y2, x1:x2]
    mask = (crop.min(axis=2) > 215).astype(np.uint8)
    found = []
    for blob in _blobs(mask, (x1, y1), min_area=KEYCAP_SIDE[0] ** 2 // 2):
        if not (KEYCAP_SIDE[0] <= blob.w <= KEYCAP_SIDE[1] and KEYCAP_SIDE[0] <= blob.h <= KEYCAP_SIDE[1]):
            continue
        if abs(blob.w - blob.h) > 4 or blob.fill < KEYCAP_FILL:
            continue
        found.append(blob)
    return sorted(found, key=lambda blob: blob.radius)[:MAX_KEYCAPS]


# The middle of the view, shrunk, to tell a player walking into a wall.
SCENE_BOX = (640, 200, 1920, 1100)
SCENE_THUMB = (64, 36)


def scene_thumbnail(frame: np.ndarray) -> np.ndarray:
    x1, y1, x2, y2 = SCENE_BOX
    small = cv2.resize(_bgr(frame)[y1:y2, x1:x2], SCENE_THUMB, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)


class FrameHud:
    """The calibrated reader. OCR is only spent on keycap candidates and the compass."""

    def __init__(self, ocr: OcrProvider | None = None) -> None:
        self.ocr = ocr
        self._last_scene: np.ndarray | None = None
        # The brain turns this off once it has measured the mouse scale.
        self.want_heading = True

    def read(self, frame: np.ndarray) -> HudReading:
        if frame.shape[1] != FRAME_SIZE[0] or frame.shape[0] != FRAME_SIZE[1]:
            return HudReading()
        scene = scene_thumbnail(frame)
        change = None if self._last_scene is None else float(np.abs(scene - self._last_scene).mean())
        self._last_scene = scene
        if not hud_shown(frame):
            return HudReading()
        red = _red_mask(_bgr(frame))
        arrows = damage_arrows(frame, red)
        share, trail = health(frame)
        return HudReading(
            has_weapon=has_weapon(frame),
            vitality=share,
            shield=shield(frame),
            hit_bearing=arrows[0].bearing if arrows else None,
            hurt=trail,
            pickup_prompt=self._prompt(frame),
            hit_confirmed=target_bar_near_crosshair(frame, red),
            heading=self.heading(frame) if self.want_heading else None,
            scene_change=change,
        )

    def heading(self, frame: np.ndarray) -> int | None:
        if self.ocr is None:
            return None
        tokens = self.ocr.read(frame, Region("compassHeading", COMPASS, single_line=True))
        text = "".join(token.text for token in tokens if token.confidence >= 0.8)
        if not text.isdigit() or not 0 <= int(text) < 360:
            return None
        return int(text)

    def prompt_text(self, frame: np.ndarray) -> str | None:
        if self.ocr is None:
            return None
        for cap in keycaps(frame):
            roi = (cap.x + cap.w + 2, cap.y - 4, min(FRAME_SIZE[0], cap.x + cap.w + 150), cap.y + cap.h + 4)
            tokens = self.ocr.read(frame, Region("pickupPrompt", roi, single_line=True))
            text = normalize_ocr_text("".join(token.text for token in tokens))
            for word in PROMPT_WORDS:
                if word in text:
                    return word
        return None

    def _prompt(self, frame: np.ndarray) -> bool:
        return self.prompt_text(frame) is not None

"""What one frame shows the player about themself and their surroundings.

Everything here reads pixels or OCRs a small fixed region of a 2560x1440
frame; nothing reads game memory and nothing looks for enemies. Cheap pixel
reads run on every frame. OCR runs only where something changed or a skill
asked for it, on tight single-line regions (≈5 ms each).

Positions were measured on the 2026-10-29 recordings (Simplified Chinese,
colorblind off) and on the 10-29 night trial frames.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import math
import re

import cv2
import numpy as np

from .. import hud
from ..ocr_obstacles import OcrProvider, Region, normalize_ocr_text


FRAME_SIZE = hud.FRAME_SIZE
CROSSHAIR = hud.CROSSHAIR

# Weapon panel (bottom right). The big digits are the magazine, the small
# ones under them the reserve; the hexagon right of them takes the colour of
# the ammo type in hand.
MAG_DIGITS = (2290, 1275, 2392, 1335)
RESERVE_DIGITS = (2290, 1334, 2385, 1372)
AMMO_ICON = (2395, 1290, 2465, 1365)
WEAPON_SLOTS = ((2035, 1378, 2215, 1410), (2225, 1378, 2405, 1410))
# A weapon name (white text) in the second slot means two guns are carried.
SLOT_NAME_WHITE = 300

# 「击倒护盾」 sits under the crosshair only while the player is down.
KNOCKED_LABEL = (1140, 1140, 1420, 1200)
# 「回到安全区内！」 banner shown while outside the ring.
RING_BANNER = (1060, 440, 1520, 505)
# One-line messages just under the crosshair: 没有弹药, 弹药不足, 正在装填.
CENTER_MESSAGE = (1150, 800, 1420, 860)

# Storm: a red tint creeps in from every edge. Border strips of a 160x90
# thumbnail (the bottom carries the HUD and is left out).
THUMB = (160, 90)
EDGE = 10

# Ground band for motion: below the crosshair, above the HUD, middle third.
FLOW_BOX = (853, 820, 1707, 1180)
FLOW_THUMB = (128, 54)

# Minimap (top left). North-up, centred on the player, whose own arrow sits at
# MINIMAP_CENTRE. Squad arrows take one of three colours; whichever is ours
# is the one at the centre. Loot icons share the green but are smaller and a
# shade yellower (hue 42–43 against 38–40).
MINIMAP = (62, 62, 362, 362)
MINIMAP_CENTRE = (164, 164)
SQUAD_HUES = ((15, 24, 70), (84, 94, 45), (34, 41, 75))  # (low, high, minimum area)
SQUAD_SIDE = (8, 22)

PROMPT_WORDS = ("捡起", "装备", "打开", "关闭", "复活", "交换", "使用")
# Item categories as the card's first line names them. Order matters: an
# ammo card says 「弹药」, a weapon card says 「冲锋枪 | 重型弹药」.
ITEM_KINDS = (
    ("weapon", ("枪", "弓", "机枪", "喷子")),
    ("ammo", ("弹药",)),
    ("heal", ("生命值", "医疗", "注射器", "凤凰")),
    ("shield", ("护盾", "电池", "电容")),
    ("armor", ("护甲", "头盔", "背包", "击倒护盾")),
    ("grenade", ("投掷", "手雷", "电弧星", "铝热剂")),
    ("attachment", ("配件", "瞄准镜", "弹匣", "枪管", "枪托")),
)
MAG_LIT = 185
MAG_LIT_MIN = 40
RESERVE_LIT_MIN = 150


@dataclass(frozen=True)
class Prompt:
    word: str
    text: str
    # Centre of the 「E」 keycap, so a skill can keep the item card near the
    # crosshair while it walks up to it.
    x: float
    y: float
    # Item cards only: 「冲锋枪 | 重型弹药」 and 「猎兽冲锋枪」.
    category: str = ""
    name: str = ""

    @property
    def kind(self) -> str:
        """weapon, ammo, heal, shield, armor, attachment, grenade, bin, door or other."""
        if self.word == "打开" or self.word == "关闭":
            return "open"
        label = self.category + self.name
        for kind, words in ITEM_KINDS:
            if any(word in label for word in words):
                return kind
        return "other"

    @property
    def bearing(self) -> float:
        return math.degrees(math.atan2(self.x - CROSSHAIR[0], -(self.y - CROSSHAIR[1])))


@dataclass(frozen=True)
class Mate:
    """A teammate arrow on the minimap."""

    # Compass bearing from the player, degrees clockwise from north.
    bearing: float
    # Minimap pixels from the player; the map shows roughly 150 m across.
    distance: float
    hue: int


def minimap_mates(frame: np.ndarray) -> tuple[bool, tuple[Mate, ...]]:
    """Whether our own arrow shows at the centre, and every other squad arrow."""
    x1, y1, x2, y2 = MINIMAP
    hsv = cv2.cvtColor(frame[y1:y2, x1:x2, :3], cv2.COLOR_BGR2HSV)
    hue, sat, val = (hsv[..., index].astype(np.int16) for index in range(3))
    mask = (((sat > 110) & (val > 150)) | ((sat > 200) & (val > 115))).astype(np.uint8)
    count, labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    found_self = False
    mates: list[Mate] = []
    for index in range(1, count):
        _x, _y, width, height, area = (int(value) for value in stats[index])
        cx, cy = centroids[index]
        dx, dy = cx - MINIMAP_CENTRE[0], cy - MINIMAP_CENTRE[1]
        distance = math.hypot(dx, dy)
        if distance <= 10 and area >= 60:
            # Our arrow; the route line or an icon can merge into it, so no size test.
            found_self = True
            continue
        if not SQUAD_SIDE[0] <= max(width, height):
            continue
        colour = int(np.median(hue[labels == index]))
        if max(width, height) > SQUAD_SIDE[1]:
            continue
        if not any(low <= colour <= high and area >= minimum for low, high, minimum in SQUAD_HUES):
            continue
        mates.append(Mate(bearing=math.degrees(math.atan2(dx, -dy)) % 360, distance=distance, hue=colour))
    return found_self, tuple(sorted(mates, key=lambda mate: mate.distance))


@dataclass(frozen=True)
class Observation:
    """One frame. ``None`` means "not read this frame", never "absent"."""

    at: float
    hud: bool
    weapon: bool | None = None
    mag: int | None = None
    reserve_left: bool | None = None
    # How many weapon slots hold a gun (1 or 2), read with the weapon.
    guns: int | None = None
    vitality: float | None = None
    shield: float | None = None
    hurt_trail: bool = False
    arrow_bearing: float | None = None
    hit_confirmed: bool = False
    knocked: bool | None = None
    ring_banner: bool | None = None
    storm_edge: float | None = None
    center_text: str | None = None
    prompt: Prompt | None = None
    prompt_checked: bool = False
    flow_forward: float | None = None
    flow_side: float | None = None
    scene_change: float | None = None
    heading: int | None = None
    # None when the minimap was not read this frame.
    mates: tuple[Mate, ...] | None = None
    minimap_self: bool | None = None
    # Loot, doors and bots the detector found; None when it did not run.
    detections: tuple | None = None
    # Metres free per direction in view (agent.space); None when not measured.
    space: tuple | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)


def _digits(text: str) -> int | None:
    found = re.sub(r"[^0-9]", "", text.replace("O", "0").replace("o", "0"))
    return int(found) if found else None


def storm_edge(frame: np.ndarray) -> float:
    """Share of the left, right and top border that carries the storm's red.

    Every 16th pixel of a 2560x1440 frame is a 160x90 grid: no resize needed.
    """
    step = max(1, frame.shape[1] // THUMB[0])
    small = frame[::step, ::step, :3].astype(np.int16)
    strips = np.concatenate([
        small[:, :EDGE].reshape(-1, 3),
        small[:, -EDGE:].reshape(-1, 3),
        small[:EDGE, EDGE:-EDGE].reshape(-1, 3),
    ])
    b, g, r = strips[:, 0], strips[:, 1], strips[:, 2]
    red = (r > 90) & (r - np.maximum(g, b) > 35)
    return float(red.mean())


def scene_thumbnail(frame: np.ndarray) -> np.ndarray:
    """``hud.scene_thumbnail`` from a quarter of the pixels: same 64x36 grey."""
    x1, y1, x2, y2 = hud.SCENE_BOX
    small = cv2.resize(frame[y1:y2:4, x1:x2:4, :3], hud.SCENE_THUMB, interpolation=cv2.INTER_AREA)
    return cv2.cvtColor(small, cv2.COLOR_BGR2GRAY).astype(np.float32)


def keycaps(frame: np.ndarray) -> list[hud.Blob]:
    """``hud.keycaps`` on every other pixel: the white 「E」 boxes are 26–48 px wide."""
    x1, y1, x2, y2 = hud.PROMPT_SEARCH
    crop = frame[y1:y2:2, x1:x2:2, :3]
    mask = (crop.min(axis=2) > 215).astype(np.uint8)
    if int(mask.sum()) < (hud.KEYCAP_SIDE[0] // 2) ** 2 // 2:
        return []
    low, high = hud.KEYCAP_SIDE[0] // 2, hud.KEYCAP_SIDE[1] // 2 + 1
    found = []
    count, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, 8)
    for index in range(1, count):
        x, y, w, h, area = (int(value) for value in stats[index])
        if not (low <= w <= high and low <= h <= high) or abs(w - h) > 3:
            continue
        if area / max(1, w * h) < hud.KEYCAP_FILL:
            continue
        cx, cy = centroids[index]
        found.append(hud.Blob(x1 + 2 * x, y1 + 2 * y, 2 * w, 2 * h, 4 * area, x1 + 2 * float(cx), y1 + 2 * float(cy)))
    return sorted(found, key=lambda blob: blob.radius)[:hud.MAX_KEYCAPS]


def ground_flow(previous: np.ndarray | None, frame: np.ndarray) -> tuple[float | None, float | None, np.ndarray]:
    """Median vertical and horizontal flow of the ground band, in thumbnail pixels.

    Walking forward pulls the ground toward the bottom of the screen
    (positive vertical flow); turning slides it sideways. Against a wall
    both stay near zero although the keys are held.
    """
    x1, y1, x2, y2 = FLOW_BOX
    grey = cv2.cvtColor(cv2.resize(frame[y1:y2, x1:x2, :3], FLOW_THUMB, interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
    if previous is None or previous.shape != grey.shape:
        return None, None, grey
    flow = cv2.calcOpticalFlowFarneback(previous, grey, None, 0.5, 2, 9, 2, 5, 1.1, 0)
    return float(np.median(flow[..., 1])), float(np.median(flow[..., 0])), grey


class Perceiver:
    """Reads frames into Observations. Keeps what consecutive frames need."""

    def __init__(self, ocr: OcrProvider | None, detector=None, space=None) -> None:
        self.ocr = ocr
        self.detector = detector
        self.space = space
        self._last_scene: np.ndarray | None = None
        self._last_ground: np.ndarray | None = None
        self._last_mag_pixels: np.ndarray | None = None
        self._mag: int | None = None
        self._last_bars: tuple[float, float] | None = None
        # The last card read, keyed by a thumbnail of the card: a card that has
        # not changed is not OCR'd again on every decision.
        self._card_key: np.ndarray | None = None
        self._card: Prompt | None = None

    # ------------------------------------------------------------------ OCR helpers

    def _text(self, frame: np.ndarray, name: str, roi: tuple[int, int, int, int], min_confidence: float = 0.6) -> str:
        if self.ocr is None:
            return ""
        tokens = self.ocr.read(frame, Region(name, roi, single_line=True))
        return "".join(token.text for token in tokens if token.confidence >= min_confidence)

    def prompt(self, frame: np.ndarray) -> Prompt | None:
        if self.ocr is None:
            return None
        caps = keycaps(frame)
        if not caps:
            self._card_key = self._card = None
            return None
        first = caps[0]
        key = self._card_thumbnail(frame, first)
        if self._card_key is not None and key.shape == self._card_key.shape and float(np.abs(key - self._card_key).mean()) < 4.0:
            if self._card is not None:
                return Prompt(self._card.word, self._card.text, first.cx, first.cy, self._card.category, self._card.name)
            return None
        self._card_key = key
        self._card = None
        for cap in caps:
            roi = (cap.x + cap.w + 2, cap.y - 4, min(FRAME_SIZE[0], cap.x + cap.w + 420), cap.y + cap.h + 4)
            raw = self._text(frame, "agentPrompt", roi, 0.5)
            text = normalize_ocr_text(raw)
            for word in PROMPT_WORDS:
                if word in text:
                    category = name = ""
                    if word in ("捡起", "装备"):
                        category, name = self._item(frame, cap)
                    self._card = Prompt(word, raw.strip(), cap.cx, cap.cy, category, name)
                    return self._card
        return None

    @staticmethod
    def _card_thumbnail(frame: np.ndarray, cap: hud.Blob) -> np.ndarray:
        scale = cap.h / 33
        x1, y1 = max(0, cap.x - 4), max(0, cap.y - 6)
        x2 = min(FRAME_SIZE[0], int(cap.x + 620 * scale))
        y2 = min(FRAME_SIZE[1], int(cap.y + 125 * scale))
        crop = frame[y1:y2, x1:x2, :3]
        if crop.size == 0:
            return np.zeros((12, 48), dtype=np.float32)
        grey = cv2.cvtColor(cv2.resize(crop, (48, 12), interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        return grey.astype(np.float32)

    def _ammo(self, frame: np.ndarray) -> tuple[int | None, bool | None]:
        """Magazine count and whether any reserve shows.

        Unused digit places are drawn as dim grey zeros (「09」, 「040」) that
        OCR reads as 8s, so only the lit pixels are kept before reading. An
        empty magazine has no lit digit at all and needs no OCR. The reserve
        comes as a count or, on charge weapons, a percentage; only "anything
        left" matters, and that is a pixel count.
        """
        x1, y1, x2, y2 = MAG_DIGITS
        crop = frame[y1:y2, x1:x2, :3]
        lit = crop.min(axis=2) > MAG_LIT
        x1, y1, x2, y2 = RESERVE_DIGITS
        reserve = frame[y1:y2, x1:x2, :3].astype(np.int16)
        high, low = reserve.max(axis=2), reserve.min(axis=2)
        reserve_left = int(((high > 150) & (high - low > 50)).sum()) >= RESERVE_LIT_MIN
        if int(lit.sum()) < MAG_LIT_MIN:
            self._last_mag_pixels = None
            self._mag = 0
            return 0, reserve_left
        pixels = cv2.resize(lit.astype(np.uint8) * 255, (34, 20), interpolation=cv2.INTER_AREA).astype(np.int16)
        if self._last_mag_pixels is None or float(np.abs(pixels - self._last_mag_pixels).mean()) > 6.0:
            self._last_mag_pixels = pixels
            self._mag = None
            if self.ocr is not None:
                clean = np.zeros_like(crop)
                clean[lit] = 255
                clean = cv2.copyMakeBorder(clean, 12, 12, 12, 12, cv2.BORDER_CONSTANT)
                tokens = self.ocr.read(clean, Region("agentMag", (0, 0, clean.shape[1], clean.shape[0]), single_line=True))
                self._mag = _digits("".join(token.text for token in tokens if token.confidence >= 0.5))
        return self._mag, reserve_left

    def _item(self, frame: np.ndarray, cap: hud.Blob) -> tuple[str, str]:
        """The category and name lines of a ground item card, under its header."""
        scale = cap.h / 33
        x1 = int(cap.x + 180 * scale)
        x2 = min(FRAME_SIZE[0], int(cap.x + 620 * scale))
        category = self._text(frame, "agentItemKind", (x1, int(cap.y + 42 * scale), x2, int(cap.y + 82 * scale)), 0.5)
        name = self._text(frame, "agentItemName", (x1, int(cap.y + 80 * scale), x2, int(cap.y + 125 * scale)), 0.5)
        return category.strip(), name.strip()

    # ------------------------------------------------------------------ one frame

    def read(
        self,
        frame: np.ndarray,
        now: float,
        *,
        want_prompt: bool = True,
        want_knocked: bool = False,
        want_ring: bool = False,
        want_center: bool = False,
        want_heading: bool = False,
        want_minimap: bool = False,
        want_combat: bool = False,
        want_detect: bool = False,
        want_space: bool = False,
        pitch: float | None = None,
    ) -> Observation:
        if frame.shape[1] != FRAME_SIZE[0] or frame.shape[0] != FRAME_SIZE[1]:
            return Observation(at=now, hud=False)
        timings: dict[str, float] = {}
        tick = cv2.getTickCount()

        def lap(name: str) -> None:
            nonlocal tick
            later = cv2.getTickCount()
            timings[name] = round((later - tick) * 1000 / cv2.getTickFrequency(), 2)
            tick = later

        scene = scene_thumbnail(frame)
        change = None if self._last_scene is None else float(np.abs(scene - self._last_scene).mean())
        self._last_scene = scene
        flow_v = flow_h = None
        edge = storm_edge(frame)
        lap("motion")
        knocked = None
        if want_knocked:
            knocked = "击倒护盾" in normalize_ocr_text(self._text(frame, "agentKnocked", KNOCKED_LABEL))
        ring = None
        if want_ring:
            ring = "安全区" in normalize_ocr_text(self._text(frame, "agentRing", RING_BANNER))
        center = None
        if want_center:
            center = normalize_ocr_text(self._text(frame, "agentCenter", CENTER_MESSAGE)) or None
        lap("ocrFlags")
        if not hud.hud_shown(frame):
            return Observation(at=now, hud=False, knocked=knocked, ring_banner=ring, storm_edge=edge,
                               center_text=center, flow_forward=flow_v, flow_side=flow_h,
                               scene_change=change, timings_ms=timings)
        share, trail = hud.health(frame)
        shield_share = hud.shield(frame)
        weapon = hud.has_weapon(frame)
        # The red mask is the costly read. Arrows only matter for a hit and the
        # target bar only in a fight, so look for them when the own bars just
        # dropped (or show the red trail), or when the decision side asks.
        dropped = trail or (
            self._last_bars is not None
            and (self._last_bars[0] - share >= 0.02 or self._last_bars[1] - shield_share >= 0.04)
        )
        self._last_bars = (share, shield_share)
        arrows: list[hud.Blob] = []
        confirmed = False
        if dropped or want_combat:
            red = hud._red_mask(hud._bgr(frame))
            arrows = hud.damage_arrows(frame, red)
            confirmed = want_combat and hud.target_bar_near_crosshair(frame, red)
        lap("hud")
        mag = reserve_left = guns = None
        if weapon:
            mag, reserve_left = self._ammo(frame)
            x1, y1, x2, y2 = WEAPON_SLOTS[1]
            guns = 2 if int((frame[y1:y2, x1:x2, :3].min(axis=2) > 200).sum()) >= SLOT_NAME_WHITE else 1
        else:
            self._last_mag_pixels = None
        lap("ammo")
        prompt = self.prompt(frame) if want_prompt else None
        lap("prompt")
        heading = None
        if want_heading and self.ocr is not None:
            text = self._text(frame, "agentHeading", hud.COMPASS, 0.8)
            heading = int(text) if text.isdigit() and 0 <= int(text) < 360 else None
        lap("heading")
        mates = minimap_self = None
        if want_minimap:
            minimap_self, mates = minimap_mates(frame)
        lap("minimap")
        detections = None
        if want_detect and self.detector is not None:
            detections = tuple(self.detector.detect(frame))
            lap("detect")
        space = None
        if want_space and self.space is not None:
            # Unknown pitch: the agent walks looking a little down.
            space = self.space.measure(frame, -3.0 if pitch is None else pitch, None).sectors
            lap("space")
        return Observation(
            at=now,
            hud=True,
            weapon=weapon,
            mag=mag,
            reserve_left=reserve_left,
            guns=guns,
            vitality=share,
            shield=shield_share,
            hurt_trail=trail,
            arrow_bearing=arrows[0].bearing if arrows else None,
            hit_confirmed=confirmed,
            knocked=knocked,
            ring_banner=ring,
            storm_edge=edge,
            center_text=center,
            prompt=prompt,
            prompt_checked=want_prompt,
            flow_forward=flow_v,
            flow_side=flow_h,
            scene_change=change,
            heading=heading,
            mates=mates,
            minimap_self=minimap_self,
            detections=detections,
            space=space,
            timings_ms=timings,
        )

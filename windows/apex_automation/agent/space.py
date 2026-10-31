"""How far can I walk in each direction in view?

A small monocular depth model (Depth Anything V2 Small, Apache-2.0, int8
ONNX) gives relative depth for the frame: no metres, and a scale that changes
from frame to frame, so its numbers alone cannot say "a wall 1 m ahead".
Geometry can. Seen from eye height, open floor gets nearer quickly toward the
bottom of the view (inverse depth grows like the tangent of the angle below
the horizon); a wall, a bin or a rock standing on it does not. Scanning each
column up from the bottom, the first rows where depth stops behaving like
floor are the foot of whatever stands there, and the foot's angle below the
horizon with the eye height gives its distance in metres, whatever the
model's scale. Facing a wall, the floor test fails right at the bottom of the
view: right in front of us (reported as 0 m).

Checked on Runner frames and recordings (2026-10-30): walls, rock faces and
containers read as blocked every time; open ground reads 5-25 m, shorter
than it is on dark or snowy ground (the safe direction to be wrong in).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import cv2
import numpy as np

from .detector import horizontal_fov


MEAN = np.array([0.485, 0.456, 0.406], np.float32)
STD = np.array([0.229, 0.224, 0.225], np.float32)
EYE_M = 1.6
CAP_M = 25.0
# Below this share of the height is HUD; the held weapon fills this box.
HUD_BOTTOM = 0.84
WEAPON = (0.55, 0.40, 0.84)  # x1, y1, x2 as shares of the frame
# A row is floor while inverse depth grows at least this share of what flat
# floor would give; this many non-floor rows in a row mark a foot.
FLOOR_SHARE = 0.2
FOOT_ROWS = 4
SECTORS = 24


@dataclass(frozen=True)
class SpaceView:
    """Free distance per direction, as seen on one frame."""

    # Compass heading when the frame was taken (None when unknown).
    heading: float | None
    # (yaw from the view centre in degrees, metres free or None when unknown), left to right.
    sectors: tuple[tuple[float, float | None], ...]


def free_from_disparity(disp: np.ndarray, pitch_deg: float, hfov_deg: float, *,
                        eye_m: float = EYE_M, sectors: int = SECTORS) -> tuple[tuple[float, float | None], ...]:
    """Metres free per sector of the view from a relative inverse-depth map."""
    h, w = disp.shape
    focal = (w / 2) / math.tan(math.radians(hfov_deg) / 2)
    rows = np.arange(h)
    # Angle below the horizon of each row (pitch positive = looking up).
    alpha = np.radians(-pitch_deg) + np.arctan((rows - h / 2) / focal)
    growth = np.gradient(np.log(np.maximum(disp, 1e-3)), axis=0)
    step = max(1, w // (sectors * 2))
    columns: list[tuple[float, float | None]] = []
    for x in range(0, w - step + 1, step):
        col = np.median(growth[:, x:x + step], axis=1)
        bottom = int(h * HUD_BOTTOM)
        if WEAPON[0] * w <= x + step / 2 <= WEAPON[2] * w:
            bottom = min(bottom, int(h * WEAPON[1]))
        yaw = math.degrees(math.atan((x + step / 2 - w / 2) / focal))
        if alpha[bottom - 1] <= math.radians(1.5):
            columns.append((yaw, None))  # no floor in view in this column
            continue
        foot, run = None, 0
        for v in range(bottom - 1, 0, -1):
            a = alpha[v]
            if a <= math.radians(1.5):
                break  # floor all the way to the horizon
            if col[v] > FLOOR_SHARE / (focal * math.sin(a) * math.cos(a)):
                run = 0
                continue
            run += 1
            if run >= FOOT_ROWS:
                foot = v + FOOT_ROWS - 1
                break
        if foot is None:
            columns.append((yaw, CAP_M))
        elif foot >= bottom - FOOT_ROWS - 1:
            # Not floor even at the bottom of the view: closer than any floor we can see.
            columns.append((yaw, 0.0))
        else:
            columns.append((yaw, min(CAP_M, eye_m / math.tan(alpha[foot]))))
    out = []
    per = max(1, len(columns) // sectors)
    for start in range(0, len(columns) - per + 1, per):
        group = columns[start:start + per]
        known = [metres for _, metres in group if metres is not None]
        out.append((sum(yaw for yaw, _ in group) / len(group), min(known) if known else None))
    return tuple(out)


class FreeSpace:
    def __init__(self, model_path: str | Path, *, size: tuple[int, int] = (266, 476), fov_slider: float = 90.0,
                 threads: int = 2) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        # Apex owns this CPU; two threads keep the model from starving it.
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.size = size  # height, width; multiples of 14
        self.hfov = horizontal_fov(fov_slider)

    def disparity(self, frame: np.ndarray) -> np.ndarray:
        height, width = self.size
        # Every few pixels first: an area resize of the whole 2560x1440 frame
        # costs more than the model on a Runner CPU, and the model sees ~1/8 of it anyway.
        step = max(1, frame.shape[1] // (width * 2))
        img = cv2.resize(frame[::step, ::step, :3], (width, height), interpolation=cv2.INTER_AREA)[..., ::-1]
        blob = ((img.astype(np.float32) / 255.0 - MEAN) / STD).transpose(2, 0, 1)[None]
        return self.session.run(None, {self.input: np.ascontiguousarray(blob)})[0][0]

    def measure(self, frame: np.ndarray, pitch_deg: float, heading: float | None) -> SpaceView:
        return SpaceView(heading, free_from_disparity(self.disparity(frame), pitch_deg, self.hfov))

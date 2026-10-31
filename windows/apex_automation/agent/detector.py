"""Find loot bins, items, doors, death boxes and bots on the screen.

A small YOLO model exported to ONNX (trained in the lab from the operator's
recordings) run with onnxruntime on the CPU, which the Runner already has for
OCR. Nothing here aims: it only says what is where, as a bearing the motor
can turn by. Without a model file the agent simply has no detections and
works as before.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path

import cv2
import numpy as np


CLASSES = ("bin_closed", "bin_open", "item", "door", "deathbox", "enemy", "teammate")
FRAME = (2560, 1440)


def horizontal_fov(slider_degrees: float) -> float:
    """Apex's FOV slider is the 4:3 horizontal angle; the 16:9 view is wider."""
    half = math.radians(slider_degrees) / 2
    return math.degrees(2 * math.atan(math.tan(half) * (16 / 9) / (4 / 3)))


@dataclass(frozen=True)
class Detection:
    kind: str
    confidence: float
    x1: float
    y1: float
    x2: float
    y2: float
    # Degrees to turn to centre it: positive right / up.
    yaw: float
    pitch: float

    @property
    def height_share(self) -> float:
        """How much of the screen height it fills: a rough distance."""
        return (self.y2 - self.y1) / FRAME[1]

    @property
    def centre(self) -> tuple[float, float]:
        return (self.x1 + self.x2) / 2, (self.y1 + self.y2) / 2

    def covers_crosshair(self, margin: float = 0.0) -> bool:
        cx, cy = FRAME[0] / 2, FRAME[1] / 2
        return self.x1 - margin <= cx <= self.x2 + margin and self.y1 - margin <= cy <= self.y2 + margin


class Detector:
    def __init__(self, model_path: str | Path, *, imgsz: int = 416, confidence: float = 0.4,
                 fov_slider: float = 90.0, threads: int = 2) -> None:
        import onnxruntime as ort

        options = ort.SessionOptions()
        # Apex owns this CPU; two threads keep the detector from starving it.
        options.intra_op_num_threads = threads
        self.session = ort.InferenceSession(str(model_path), options, providers=["CPUExecutionProvider"])
        self.input = self.session.get_inputs()[0].name
        self.imgsz = imgsz
        self.confidence = confidence
        hfov = horizontal_fov(fov_slider)
        self.focal = (FRAME[0] / 2) / math.tan(math.radians(hfov) / 2)

    def _blob(self, frame: np.ndarray) -> tuple[np.ndarray, float]:
        scale = self.imgsz / max(frame.shape[1], frame.shape[0])
        width, height = round(frame.shape[1] * scale), round(frame.shape[0] * scale)
        small = cv2.resize(frame[..., :3], (width, height), interpolation=cv2.INTER_AREA)
        canvas = np.full((self.imgsz, self.imgsz, 3), 114, dtype=np.uint8)
        canvas[:height, :width] = small
        blob = canvas[..., ::-1].transpose(2, 0, 1)[None].astype(np.float32) / 255.0
        return np.ascontiguousarray(blob), scale

    def detect(self, frame: np.ndarray) -> list[Detection]:
        blob, scale = self._blob(frame)
        output = self.session.run(None, {self.input: blob})[0][0]  # (4 + classes, candidates)
        return decode(output, scale, self.confidence, self.focal)


def decode(output: np.ndarray, scale: float, confidence: float, focal: float, iou: float = 0.5) -> list[Detection]:
    """YOLO (Ultralytics) head output -> detections in 2560x1440 pixels."""
    boxes, scores = output[:4].T, output[4:].T
    classes = scores.argmax(axis=1)
    best = scores[np.arange(len(scores)), classes]
    keep = best >= confidence
    boxes, classes, best = boxes[keep], classes[keep], best[keep]
    if not len(best):
        return []
    xyxy = np.stack([boxes[:, 0] - boxes[:, 2] / 2, boxes[:, 1] - boxes[:, 3] / 2,
                     boxes[:, 0] + boxes[:, 2] / 2, boxes[:, 1] + boxes[:, 3] / 2], axis=1) / scale
    found: list[Detection] = []
    for kind in set(classes.tolist()):
        mask = classes == kind
        rects = [[float(x1), float(y1), float(x2 - x1), float(y2 - y1)] for x1, y1, x2, y2 in xyxy[mask]]
        kept = cv2.dnn.NMSBoxes(rects, best[mask].tolist(), confidence, iou)
        for index in np.array(kept).reshape(-1):
            x1, y1, x2, y2 = np.clip(xyxy[mask][index], 0, [FRAME[0], FRAME[1], FRAME[0], FRAME[1]])
            cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
            found.append(Detection(
                kind=CLASSES[int(kind)] if int(kind) < len(CLASSES) else str(int(kind)),
                confidence=float(best[mask][index]),
                x1=float(x1), y1=float(y1), x2=float(x2), y2=float(y2),
                yaw=math.degrees(math.atan((cx - FRAME[0] / 2) / focal)),
                pitch=-math.degrees(math.atan((cy - FRAME[1] / 2) / focal)),
            ))
    return sorted(found, key=lambda d: -d.confidence)

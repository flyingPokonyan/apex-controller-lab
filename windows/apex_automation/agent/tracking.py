"""Keep one identity per bin, item or bot across detector frames.

The detector answers "what is where" for a single frame. A skill walking to a
bin needs "that bin": the same one while the view sways, while we turn, and
after another bin comes into view. Each detection is placed by its compass
bearing (heading + yaw), which does not change when we turn, and matched to a
track of the same kind with a similar bearing and size. A track counts only
once it was seen on two reads, so one stray box never starts a walk or a shot.
"""

from __future__ import annotations

from dataclasses import dataclass

from .detector import FRAME, Detection, horizontal_fov


HFOV = horizontal_fov(90.0)
# Gone this long: forgotten (the skill gives up after 2 s unseen anyway).
MAX_AGE_S = 2.5
CONFIRM_HITS = 2


def _signed(degrees: float) -> float:
    return (degrees + 180.0) % 360.0 - 180.0


def _width_deg(detection: Detection) -> float:
    return (detection.x2 - detection.x1) / FRAME[0] * HFOV


@dataclass
class Track:
    id: int
    kind: str
    detection: Detection
    # Compass bearing of the target (None while the heading is unknown).
    bearing: float | None
    first_seen: float
    last_seen: float
    hits: int = 1

    @property
    def confirmed(self) -> bool:
        return self.hits >= CONFIRM_HITS

    @property
    def height_share(self) -> float:
        return self.detection.height_share


class Tracker:
    def __init__(self) -> None:
        self.tracks: list[Track] = []
        self._next_id = 1

    def update(self, detections, heading: float | None, now: float) -> None:
        self.tracks = [track for track in self.tracks if now - track.last_seen <= MAX_AGE_S]
        pairs = []
        for d_index, detection in enumerate(detections):
            bearing = None if heading is None else (heading + detection.yaw) % 360
            for t_index, track in enumerate(self.tracks):
                cost = self._cost(track, detection, bearing)
                if cost is not None:
                    pairs.append((cost, d_index, t_index, bearing))
        used_d, used_t = set(), set()
        for cost, d_index, t_index, bearing in sorted(pairs, key=lambda pair: pair[0]):
            if d_index in used_d or t_index in used_t:
                continue
            used_d.add(d_index)
            used_t.add(t_index)
            track = self.tracks[t_index]
            track.detection, track.last_seen, track.hits = detections[d_index], now, track.hits + 1
            if bearing is not None:
                track.bearing = bearing
        for d_index, detection in enumerate(detections):
            if d_index in used_d:
                continue
            bearing = None if heading is None else (heading + detection.yaw) % 360
            self.tracks.append(Track(self._next_id, detection.kind, detection, bearing, now, now))
            self._next_id += 1

    @staticmethod
    def _cost(track: Track, detection: Detection, bearing: float | None) -> float | None:
        """Angular distance when this detection can be that track, else None."""
        if track.kind != detection.kind:
            return None
        ratio = detection.height_share / max(1e-6, track.detection.height_share)
        if not 0.5 <= ratio <= 2.0:
            return None
        if bearing is not None and track.bearing is not None:
            distance = abs(_signed(bearing - track.bearing))
        else:
            # No compass: only the position in the view (fine while not turning).
            distance = abs(detection.yaw - track.detection.yaw)
        gate = max(5.0, 1.5 * max(_width_deg(detection), _width_deg(track.detection)))
        return distance if distance <= gate else None

    def get(self, track_id: int | None) -> Track | None:
        return next((track for track in self.tracks if track.id == track_id), None)

    def visible(self, now: float, kinds: tuple[str, ...], within: float = 0.8) -> list[Track]:
        """Confirmed tracks of these kinds seen in the last ``within`` seconds."""
        return [track for track in self.tracks
                if track.kind in kinds and track.confirmed and now - track.last_seen <= within]

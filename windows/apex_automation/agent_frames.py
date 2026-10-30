"""In-match frames for training the loot detector, queued and uploaded.

The agent drops a downscaled frame here every few seconds while the player
is on the ground; the diagnostics thread sends them to Forge a few at a time.
Nothing waits on the upload and a full outbox simply drops the oldest frames:
these are training material, not evidence.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from urllib.parse import urlsplit, urlunsplit

from .atomic_files import replace_with_retry


SIZE = (1280, 720)
MAX_IMAGE_BYTES = 480 * 1024
MAX_PENDING = 300
SEND_PER_PASS = 4


def _pending(root: Path) -> list[Path]:
    found = []
    for path in root.glob("*.json"):
        try:
            found.append((path.stat().st_mtime_ns, path))
        except FileNotFoundError:
            continue
    return [path for _, path in sorted(found)]


def outbox(runs_root: Path) -> Path:
    return Path(runs_root) / "diagnostics" / "agent-frames"


def store_frame(runs_root: Path, frame, label: str, lease_id: str | None = None) -> bool:
    import cv2

    small = cv2.resize(frame[..., :3], SIZE, interpolation=cv2.INTER_AREA)
    image = None
    for quality in (85, 75, 65):
        ok, encoded = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok and len(encoded) <= MAX_IMAGE_BYTES:
            image = encoded.tobytes()
            break
    if image is None:
        return False
    folder = outbox(runs_root)
    folder.mkdir(parents=True, exist_ok=True)
    event_id = secrets.token_hex(16)
    payload = {
        "schemaVersion": 1,
        "eventId": event_id,
        "leaseId": lease_id,
        "label": "".join(ch for ch in label if ch.isalnum() or ch in "-_")[:40],
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256": hashlib.sha256(image).hexdigest(),
        "width": SIZE[0],
        "height": SIZE[1],
        "imageBase64": base64.b64encode(image).decode("ascii"),
    }
    path = folder / (event_id + ".json")
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
    replace_with_retry(temporary, path)
    for stale in _pending(folder)[:-MAX_PENDING]:
        stale.unlink(missing_ok=True)
    return True


class AgentFrameUploader:
    def __init__(self, settings, runs_root, transport, *, timeout_s=30):
        self.settings, self.transport = settings, transport
        self.root = outbox(runs_root)
        parsed = urlsplit(settings.report_url or "")
        self.url = urlunsplit(parsed._replace(
            path=parsed.path.rsplit("/", 1)[0] + "/agent-frames", query="", fragment=""))
        self.timeout_s = timeout_s
        self.next_send_at = 0.0
        self.backoff = 5.0

    def process_once(self, *, should_stop=lambda: False) -> None:
        if time.monotonic() < self.next_send_at or not self.root.exists():
            return
        for path in _pending(self.root)[:SEND_PER_PASS]:
            if should_stop():
                return
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                continue
            except (ValueError, TypeError):
                path.unlink(missing_ok=True)
                continue
            try:
                payload["deviceId"] = self.settings.device_id
                status, response, _headers = self.transport.send(self.url, self.settings.report_token, payload, self.timeout_s)
                if status == 200 and response.get("eventId") == payload.get("eventId"):
                    path.unlink(missing_ok=True)
                    self.backoff = 5.0
                    continue
                code = (response.get("error") or {}).get("code")
                if code in {"INVALID_FRAME", "FRAME_TOO_LARGE", "AGENT_FRAMES_FULL", "AGENT_FRAMES_DISABLED"}:
                    # Not worth retrying: drop the frame, slow down a lot.
                    path.unlink(missing_ok=True)
                    self.backoff = 600.0
            except FileNotFoundError:
                continue
            except Exception:  # noqa: BLE001 - a flaky network must not touch play
                pass
            self.next_send_at = time.monotonic() + self.backoff
            self.backoff = min(600.0, self.backoff * 2)
            return

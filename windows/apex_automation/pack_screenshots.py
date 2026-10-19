"""Local result frames plus a retrying upload. Upload failure does not reopen packs."""
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


MAX_IMAGE_BYTES = 512 * 1024
MAX_PENDING = 40


def _pending(root: Path):
    found = []
    for path in root.glob("*.json"):
        try:
            found.append((path.stat().st_mtime_ns, path))
        except FileNotFoundError:
            continue
    return [path for _, path in sorted(found)]


def encode_jpeg(frame) -> bytes | None:
    import cv2
    image = None
    for quality in (85, 70, 55, 40, 30):
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok and len(encoded) <= MAX_IMAGE_BYTES:
            image = encoded.tobytes()
            break
    return image


def store_result(run_dir: Path, runs_root: Path, frame, batch: int, heirloom: bool, lease_id: str) -> bool:
    image = encode_jpeg(frame)
    if image is None:
        return False
    folder = Path(run_dir) / "pack-results"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{int(batch):02d}.jpg").write_bytes(image)
    outbox = Path(runs_root) / "diagnostics" / "pack-evidence"
    outbox.mkdir(parents=True, exist_ok=True)
    event_id = secrets.token_hex(32)
    payload = {
        "schemaVersion": 1,
        "eventId": event_id,
        "leaseId": lease_id,
        "batch": int(batch),
        "heirloom": bool(heirloom),
        "capturedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "sha256": hashlib.sha256(image).hexdigest(),
        "width": int(frame.shape[1]),
        "height": int(frame.shape[0]),
        "imageBase64": base64.b64encode(image).decode("ascii"),
    }
    path = outbox / (event_id + ".json")
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    replace_with_retry(temporary, path)
    for stale in _pending(outbox)[:-MAX_PENDING]:
        stale.unlink(missing_ok=True)
    return True


class PackScreenshotUploader:
    def __init__(self, settings, runs_root, transport, *, timeout_s=30, notify=lambda _message: None):
        self.settings, self.transport = settings, transport
        self.root = Path(runs_root) / "diagnostics" / "pack-evidence"
        parsed = urlsplit(settings.report_url or "")
        self.url = urlunsplit(parsed._replace(
            path=parsed.path.rsplit("/", 1)[0] + "/pack-opening-evidence", query="", fragment=""))
        self.timeout_s, self.notify = timeout_s, notify
        self.next_send_at = 0.0
        self.backoff = 2.0

    def process_once(self, *, should_stop=lambda: False):
        if time.monotonic() < self.next_send_at or not self.root.exists():
            return
        for path in _pending(self.root)[:2]:
            if should_stop():
                return
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or "eventId" not in payload:
                    raise ValueError("invalid pack screenshot")
            except FileNotFoundError:
                continue
            except (ValueError, TypeError, json.JSONDecodeError):
                path.unlink(missing_ok=True)
                continue
            try:
                payload["deviceId"] = self.settings.device_id
                status, response, _headers = self.transport.send(
                    self.url, self.settings.report_token, payload, self.timeout_s)
                if (status == 200 and response.get("schemaVersion") == 1
                        and response.get("deviceId") == self.settings.device_id
                        and response.get("eventId") == payload["eventId"]):
                    path.unlink(missing_ok=True)
                    self.backoff = 2.0
                    continue
                code = (response.get("error") or {}).get("code")
                if code in {"INVALID_EVIDENCE", "EVIDENCE_TOO_LARGE", "EVIDENCE_CONFLICT", "LEASE_NOT_ALLOWED", "DEVICE_NOT_ALLOWED"}:
                    path.unlink(missing_ok=True)
                    self.notify(f"开包截图上报被拒绝：{code}；本机原图仍保留")
                    continue
            except FileNotFoundError:
                continue
            except Exception:
                pass
            self.next_send_at = time.monotonic() + self.backoff
            self.backoff = min(300.0, self.backoff * 2)
            return

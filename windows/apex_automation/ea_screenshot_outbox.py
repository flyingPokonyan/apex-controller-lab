"""Bounded, durable EA desktop screenshots, independent of game runs."""
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
MAX_PENDING = 64


def _pending_files(root):
    # Producer pruning and uploader acknowledgements can remove files together.
    pending = []
    for path in root.glob("*.json"):
        try:
            pending.append((path.stat().st_mtime_ns, path))
        except FileNotFoundError:
            continue
    return [path for _, path in sorted(pending)]


def enqueue(root: Path, frame, record, lease_id):
    import cv2
    # Keep the original desktop dimensions and pixels visible. Only JPEG
    # compression changes; account fields and overlapping windows are retained.
    image = None
    for quality in (85, 70, 55, 40):
        ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        if ok and len(encoded) <= MAX_IMAGE_BYTES:
            image = encoded.tobytes()
            break
    if image is None:
        return
    root.mkdir(parents=True, exist_ok=True)
    event_id = secrets.token_hex(32)
    details = {key: record[key] for key in (
        "identifierVerified", "identifierEchoed", "fieldTarget", "submitTarget", "trigger", "attempt"
    ) if key in record}
    payload = {"schemaVersion": 1, "eventId": event_id, "leaseId": lease_id,
               "step": record["step"], "page": record["page"], "details": details,
               "capturedAt": record["at"], "sha256": hashlib.sha256(image).hexdigest(),
               "width": int(frame.shape[1]), "height": int(frame.shape[0]),
               "imageBase64": base64.b64encode(image).decode("ascii")}
    path = root / (event_id + ".json")
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(payload, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    replace_with_retry(temporary, path)
    files = _pending_files(root)
    for path in files[:-MAX_PENDING]:
        path.unlink(missing_ok=True)


class EaScreenshotUploader:
    def __init__(self, settings, runs_root, transport, *, timeout_s=10, notify=lambda _message: None):
        self.settings, self.transport = settings, transport
        self.root = Path(runs_root) / "diagnostics" / "ea-evidence"
        parsed = urlsplit(settings.report_url or "")
        self.url = urlunsplit(parsed._replace(path=parsed.path.rsplit("/", 1)[0] + "/ea-evidence", query="", fragment=""))
        self.timeout_s, self.notify = timeout_s, notify
        self.next_send_at = 0.0
        self.backoff = 2.0

    def process_once(self):
        if time.monotonic() < self.next_send_at:
            return
        for path in _pending_files(self.root)[:4]:
            try:
                if time.time() - path.stat().st_mtime > 86400:
                    path.unlink(missing_ok=True)
                    continue
                payload = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(payload, dict) or "eventId" not in payload:
                    raise ValueError("invalid screenshot outbox")
            except FileNotFoundError:
                continue
            except (ValueError, TypeError):
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
                    # Retain the original PNG in the attempt directory; one
                    # rejected image must not block all subsequent screenshots.
                    path.unlink(missing_ok=True)
                    self.notify(f"EA 截图上报被拒绝：{code}；本机原图仍保留")
                    continue
            except FileNotFoundError:
                continue
            except Exception:
                pass
            self.next_send_at = time.monotonic() + self.backoff
            self.backoff = min(300.0, self.backoff * 2)
            return

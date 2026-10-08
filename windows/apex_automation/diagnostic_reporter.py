"""Background delivery of allowlisted local diagnostics, including pre-run EA.

Cursor and outbox are committed together. A crash after server acceptance only
replays immutable event IDs. This worker never gates lease cleanup/report drain.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from . import __version__
from .diagnostic_schema import EA_STEPS, validate_event, timestamp
from .reporter import UrllibReportTransport, _read_json
from .atomic_files import replace_with_retry


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class DiagnosticReporter:
    def __init__(self, settings, runs_root, *, transport=None, notify=lambda _: None,
                 poll_interval_s=30, request_timeout_s=5):
        self.settings = settings
        self.root = Path(runs_root)
        self.transport = transport or UrllibReportTransport()
        self.notify = notify
        self.poll_interval_s = poll_interval_s
        self.request_timeout_s = request_timeout_s
        self.path = self.root / "diagnostics" / (_digest(settings.device_id)[:16] + ".json")
        self.state = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {"cursors": {}, "pending": [], "rejected": 0}
        if not isinstance(self.state.get("cursors"), dict) or not isinstance(self.state.get("pending"), list):
            raise ValueError("invalid diagnostic outbox")
        for event in self.state["pending"]:
            validate_event(event)
        self._stop = threading.Event()
        self._thread = None
        self._next_send_at = 0.0
        self._backoff = 2.0
        self._last_notice = None
        self._batch_size = 100
        parsed = urlsplit(settings.report_url or "")
        self.url = urlunsplit(parsed._replace(path=parsed.path.rsplit("/", 1)[0] + "/diagnostics", query="", fragment=""))

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.state, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        replace_with_retry(temporary, self.path)

    def _notice(self, code):
        if code != self._last_notice:
            self.notify(f"诊断上报：{code}；不影响账号运行，待上传摘要保留在本机")
            self._last_notice = code

    def _sources(self):
        sources = []
        # Backfill at most 24 hours of surviving local files on initial install.
        # No screenshot/steps/credential/config content enters this reader.
        cutoff = time.time() - 24 * 3600
        for path in (self.root / "ea-login" / "timings").glob("*.jsonl"):
            if path.stat().st_mtime >= cutoff:
                sources.append((path, "ea", None, None))
        if self.root.exists():
            for directory in self.root.iterdir():
                if not directory.is_dir() or directory.name in {"ea-login", "diagnostics"}:
                    continue
                paths = [directory / name for name in ("events.jsonl", "report-timings.previous.jsonl", "report-timings.jsonl")]
                paths = [p for p in paths if p.exists() and p.stat().st_mtime >= cutoff]
                if not paths:
                    continue
                binding = _read_json(directory / "manifest.json").get("reporting", {})
                if not isinstance(binding, dict) or binding.get("deviceId") != self.settings.device_id:
                    continue
                for path in paths:
                    sources.append((path, "notification" if path.name == "events.jsonl" else "http", binding.get("leaseId"), directory.name))
        # Bounded discovery and read work; pending delivery is independent of
        # source retention. Old game telemetry is never re-uploaded here.
        return sorted(sources, key=lambda s: s[0].stat().st_mtime, reverse=True)[:128]

    @staticmethod
    def _event(kind, at, lease_id, run_id, payload, source_ids):
        event = {"type": kind, "occurredAt": at, "leaseId": lease_id,
                 "runId": run_id, "payload": payload, "eventId": _digest(source_ids)}
        return validate_event(event)

    def _translate(self, record, source, lease_id, run_id, source_id):
        if source == "ea":
            kind = record.get("type")
            lease_id = record.get("leaseId")
            payload = {}
            if kind == "EA_STEP":
                payload = {k: record[k] for k in ("step", "page", "previousStep", "previousStepElapsedMs")}
                for key in ("step", "previousStep"):
                    if payload[key] is not None and payload[key] not in EA_STEPS:
                        payload[key] = "OTHER"
            elif kind == "WORKFLOW_PHASE":
                payload = {k: record[k] for k in ("previousPhase", "phase", "durationMs")}
            elif kind == "PERFORMANCE_SUMMARY":
                kind = "EA_PERFORMANCE"
                payload = {"windowMs": record["windowMs"], "metrics": {
                    name: sample for name, sample in record["metrics"].items() if name in {"eaCapture", "eaOcr"}}}
                if "page" in record:
                    payload["page"] = record["page"]
                if "step" in record:
                    payload["step"] = record["step"] if record["step"] in EA_STEPS else "OTHER"
            else:
                return None
            return self._event(kind, record["at"], lease_id, None, payload, [source_id])
        if source == "notification":
            kind = record.get("type")
            allowed = {
                "NOTIFICATION_CANDIDATE": ("kind", "recognisedOwner", "closeFound"),
                "NOTIFICATION_CLOSED": ("kind", "disappeared", "apexForeground", "attempt"),
            }
            if kind in allowed:
                payload = {key: record["payload"][key] for key in allowed[kind]}
            elif kind == "NOTIFICATION_CLOSE_DISABLED":
                payload = {"reason": "capture-coordinate-mapping" if record["payload"].get("reason") == "capture-coordinate-mapping" else "backend-error"}
            else:
                return None
            return self._event(kind, record["occurredAt"], lease_id, run_id, payload, [source_id])
        return None

    def collect(self):
        if len(self.state["pending"]) >= 2000:
            return
        groups = {}
        budget = 3000
        for path, source, lease_id, run_id in self._sources():
            if budget <= 0:
                break
            with path.open("rb") as stream:
                first = stream.readline(65537)
                # Rotation preserves this identity; truncation/rewrite gets a
                # new cursor. The logical source excludes '.previous'.
                identity = _digest([str(path.relative_to(self.root)).replace(".previous", ""), first.hex()])
                cursor = self.state["cursors"].get(identity, {"offset": 0})
                stream.seek(cursor["offset"] if cursor["offset"] <= path.stat().st_size else 0)
                for _ in range(min(budget, 1000)):
                    start = stream.tell()
                    line = stream.readline(65537)
                    if len(line) == 65537 and not line.endswith(b"\n"):
                        # Oversize lines cannot carry supported summaries. Drain
                        # them in bounded chunks rather than wedging this source.
                        while line and not line.endswith(b"\n"):
                            line = stream.readline(65537)
                        self.state["rejected"] = self.state.get("rejected", 0) + 1
                        budget -= 1
                        continue
                    if not line or not line.endswith(b"\n"):
                        stream.seek(start)
                        break
                    budget -= 1
                    source_id = _digest([identity, start, line.hex()])
                    try:
                        record = json.loads(line)
                        if not isinstance(record, dict):
                            raise ValueError("invalid local diagnostic")
                        if source == "http":
                            at = timestamp(record["at"])
                            kind = "report" if record["kind"] == "events" else record["kind"]
                            key = (run_id, lease_id, kind, record["status"], at[:16])
                            group = groups.setdefault(key, {"records": [], "ids": []})
                            # Validate each input before it can poison a group.
                            payload = {"kind": kind, "status": record["status"], "count": 1,
                                       "totalMs": record["durationMs"], "maxMs": record["durationMs"],
                                       "pendingMax": record["pendingEvents"], "eventCount": record["eventCount"],
                                       "imageBytesApproxTotal": record["imageBytesApprox"], "windowStart": at, "windowEnd": at}
                            self._event("HTTP_UPLOAD_SUMMARY", at, lease_id, run_id, payload, [source_id])
                            group["records"].append(payload)
                            group["ids"].append(source_id)
                        else:
                            event = self._translate(record, source, lease_id, run_id, source_id)
                            if event:
                                self.state["pending"].append(event)
                    except (ValueError, TypeError, KeyError, OverflowError, AttributeError):
                        self.state["rejected"] = self.state.get("rejected", 0) + 1
                self.state["cursors"][identity] = {"offset": stream.tell(), "seen": time.time()}
        for (run_id, lease_id, kind, status, _), group in groups.items():
            samples = group["records"]
            if not samples:
                continue
            payload = {"kind": kind, "status": status, "count": len(samples),
                       "totalMs": sum(s["totalMs"] for s in samples), "maxMs": max(s["maxMs"] for s in samples),
                       "pendingMax": max(s["pendingMax"] for s in samples),
                       "eventCount": sum(s["eventCount"] for s in samples),
                       "imageBytesApproxTotal": sum(s["imageBytesApproxTotal"] for s in samples),
                       "windowStart": min(s["windowStart"] for s in samples), "windowEnd": max(s["windowEnd"] for s in samples)}
            self.state["pending"].append(self._event("HTTP_UPLOAD_SUMMARY", payload["windowEnd"], lease_id, run_id, payload, group["ids"]))
        cutoff = time.time() - 14 * 86400
        self.state["cursors"] = {key: value for key, value in self.state["cursors"].items() if value.get("seen", 0) >= cutoff}
        self._save()

    def process_once(self, *, send=True):
        self.collect()
        pending = self.state["pending"]
        if not send or not pending or time.monotonic() < self._next_send_at:
            return len(pending)
        batch = pending[:self._batch_size]
        payload = {"schemaVersion": 1, "deviceId": self.settings.device_id,
                   "clientVersion": __version__, "events": batch}
        try:
            status, response, _headers = self.transport.send(self.url, self.settings.report_token, payload, self.request_timeout_s)
            expected = [e["eventId"] for e in batch]
            if status == 200 and response.get("schemaVersion") == 1 and response.get("deviceId") == self.settings.device_id and response.get("acceptedEventIds") == expected:
                del pending[:len(batch)]
                self._save()
                self._backoff = 2.0
                self._next_send_at = 0
                self._last_notice = None
                self._batch_size = 100
                return len(pending)
            code = (response.get("error") or {}).get("code")
            if code in {"INVALID_DIAGNOSTICS", "LEASE_NOT_ALLOWED", "DIAGNOSTIC_CONFLICT"}:
                # Isolate only the permanently rejected item. Auth outages and
                # temporary/old-server failures always retain the whole outbox.
                if len(batch) > 1:
                    self._batch_size = 1
                else:
                    quarantine = self.state.setdefault("quarantine", [])
                    quarantine.append({"event": batch[0], "code": code})
                    del quarantine[:-100]
                    del pending[:1]
                    self._save()
            self._notice(f"HTTP_{status}")
        except Exception:
            # Never interpolate transport exceptions: they can contain URL/token.
            self._notice("UPLOAD_RETRY")
        self._next_send_at = time.monotonic() + self._backoff
        self._backoff = min(300.0, self._backoff * 2)
        return len(pending)

    def _run(self):
        while not self._stop.is_set():
            try:
                self.process_once()
            except Exception:
                self._notice("LOCAL_DIAGNOSTIC_ERROR")
            self._stop.wait(self.poll_interval_s)
        try:
            self.process_once(send=False)
        except Exception:
            self._notice("LOCAL_DIAGNOSTIC_ERROR")

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="apex-diagnostics", daemon=True)
            self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.request_timeout_s + 1)
            # A wedged OS transport must never hold account cleanup hostage.
            if self._thread.is_alive():
                self._notice("DIAGNOSTIC_STOP_PENDING")

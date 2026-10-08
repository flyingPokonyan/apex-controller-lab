"""Best-effort launcher version snapshots. Standard library only, no game imports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise HTTPError(req.full_url, code, "redirect refused", headers, fp)


def read(path):
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def safe_code(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9_ .:()-]{1,128}", value) else None


def snapshot(state, *, device_id, generation, sequence, now):
    def commit(name):
        value = state.get(name)
        return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value) else None

    heartbeat = state.get("workerHeartbeatAt")
    age = max(0, now - heartbeat) if isinstance(heartbeat, (int, float)) else None
    confirmed = bool(state.get("pid") and state.get("workerConfirmedAt"))
    return {"schemaVersion": 1, "deviceId": device_id, "generation": generation, "sequence": sequence,
        "stage": state.get("stage", "STARTING"), "runningCommit": commit("running") if confirmed else None,
        "installedCommit": commit("installed"), "targetCommit": commit("target"),
        "workerResponding": bool(confirmed and age is not None and age <= 90), "heartbeatAgeSeconds": age,
        "workerPhase": safe_code(state.get("workerPhase")), "installedAt": state.get("installedAt"),
        "lastCheckedAt": state.get("lastCheckedAt"), "error": safe_code(state.get("error")),
        "checkError": safe_code(state.get("checkError"))}


class VersionPublisher:
    def __init__(self, repo_root, state_root, *, write_json, send=None):
        self.repo_root, self.root = Path(repo_root), Path(state_root)
        self.write = write_json
        self.send = send or self._send
        self.path = self.root / "version-report.json"
        previous = read(self.path)
        self.generation = max(int(previous.get("generation", 0)) + 1, time.time_ns() // 1_000_000)
        self.sequence = 0
        self.stop_event = threading.Event()
        self.thread = None
        self.next_send = 0.0
        self.last_fingerprint = None
        self.failed = False
        self.last_attempt = -60.0

    def _configuration(self):
        config = read(self.repo_root / "windows/account-cycle.private.json")
        if str(os.environ.get("APEX_REPORT_ENABLED", config.get("enabled", True))).lower() in {"false", "0", "no", "off"}:
            raise ValueError("VERSION_REPORT_DISABLED")
        token = os.environ.get("APEX_REPORT_TOKEN") or config.get("reportToken")
        device = os.environ.get("APEX_DEVICE_ID") or config.get("deviceId")
        report_url = os.environ.get("APEX_REPORT_URL") or config.get("reportUrl") or ""
        parsed = urlsplit(report_url)
        if (not token or not device or not parsed.netloc or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path.endswith("/reports")
                or (parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}))):
            raise ValueError("VERSION_REPORT_CONFIGURATION_INVALID")
        url = urlunsplit(parsed._replace(path=parsed.path.rsplit("/", 1)[0] + "/version-status"))
        return url, token, device

    @staticmethod
    def _send(url, token, payload):
        req = Request(url, data=json.dumps(payload, allow_nan=False).encode(), method="POST",
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with build_opener(NoRedirect).open(req, timeout=5) as response:
            result = json.loads(response.read(8192))
            if response.status != 200 or type(result.get("accepted")) is not bool:
                raise ValueError("INVALID_VERSION_REPORT_REPLY")

    def process_once(self):
        now = time.monotonic()
        state = read(self.root / "launcher.json")
        if not state:
            return
        fingerprint = tuple(state.get(k) for k in ("stage", "running", "installed", "target", "workerConfirmedAt", "error", "checkError"))
        due_change = not self.failed and fingerprint != self.last_fingerprint and now - self.last_attempt >= 3
        if now < self.next_send and not due_change:
            return
        self.last_attempt = now
        self.sequence += 1
        metadata = {"generation": self.generation, "sequence": self.sequence}
        # Persist identity before sending; a restart cannot replay an older session.
        self.write(self.path, metadata)
        try:
            url, token, device = self._configuration()
            payload = snapshot(state, device_id=device, generation=self.generation, sequence=self.sequence, now=time.time())
            self.send(url, token, payload)
        except Exception as error:
            self.failed = True
            self.next_send = now + 15
            # Never persist URLs, tokens or HTTP response bodies.
            self.write(self.path, {**metadata, "error": type(error).__name__})
        else:
            self.failed = False
            self.next_send = now + 60
            self.last_fingerprint = fingerprint
            self.write(self.path, {**metadata, "lastReportedAt": time.time(), "error": None})

    def _run(self):
        while not self.stop_event.is_set():
            try:
                self.process_once()
            except Exception:
                # Even local telemetry state failure cannot stop an account or an update.
                pass
            self.stop_event.wait(1)

    def start(self):
        self.thread = threading.Thread(target=self._run, name="version-status", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=6)

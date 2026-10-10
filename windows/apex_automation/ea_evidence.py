"""EA attempt evidence: original desktop screenshots and bounded text summaries.

Screenshots intentionally preserve the operator's actual desktop. Text logs
continue to omit credentials; the screenshot outbox is associated with a lease.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import secrets
import shutil
import threading
import time
from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    import numpy as np

    from .ocr_obstacles import OcrToken


EVIDENCE_SCHEMA_VERSION = 1

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")
# Five digits keeps every OTP and account number out of the evidence while
# leaving the masked halves of an account id readable.
_LONG_DIGITS = re.compile(r"\d{5,}")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class SecretHints:
    """Text the recorder must never let reach disk, even partially."""

    values: tuple[str, ...] = ()

    @classmethod
    def for_login(cls, login_identifier: str) -> "SecretHints":
        parts = {login_identifier, login_identifier.split("@")[0]}
        return cls(tuple(sorted(part.lower() for part in parts if len(part) >= 3)))

    def hits(self, text: str) -> bool:
        lowered = text.lower()
        squashed = re.sub(r"[^a-z0-9]", "", lowered)
        return any(
            value in lowered or re.sub(r"[^a-z0-9]", "", value) in squashed
            for value in self.values
        )


def is_sensitive_text(text: str, hints: SecretHints | None = None) -> bool:
    if _EMAIL.search(text) or _LONG_DIGITS.search(text):
        return True
    return hints is not None and hints.hits(text)


class EaLoginEvidence:
    """One directory per login attempt, pruned to the recent history."""

    def __init__(
        self,
        root: Path,
        *,
        keep_attempts: int = 20,
        save_screenshots: bool = True,
    ) -> None:
        self.root = Path(root)
        self.keep_attempts = max(1, keep_attempts)
        self.save_screenshots = save_screenshots
        self.dir = self._create_dir()
        self.steps_path = self.dir / "steps.jsonl"
        self._seq = 0
        self._hints: SecretHints | None = None
        self._timing_lock = threading.Lock()
        self._last_step_at = time.monotonic()
        self._previous_step = None
        self._timing_lease_id = None

    def bind_lease(self, lease_id):
        if lease_id is not None and not re.fullmatch(r"lease_[a-zA-Z0-9_-]{1,128}", str(lease_id)):
            raise ValueError("invalid evidence lease")
        self._timing_lease_id = lease_id

    def timing(self, event: str, **payload) -> None:
        """Compact diagnostics survive screenshot-attempt pruning for 14 days."""
        lease = payload.pop("leaseId", None)
        # This is the public opaque lease ID, never an EA login identifier.
        if lease and re.fullmatch(r"lease_[a-zA-Z0-9_-]{1,128}", str(lease)):
            self._timing_lease_id = lease
        self._reject_sensitive_values(payload)
        with self._timing_lock:
            root = self.root / "timings"
            root.mkdir(exist_ok=True)
            today = datetime.now()
            path = root / f"{today:%Y-%m-%d}.jsonl"
            # Two bounded files per day, keeping diagnostic disk use bounded.
            if path.exists() and path.stat().st_size > 10 * 1024 * 1024:
                path.replace(path.with_suffix(".previous.jsonl"))
            record = {"at": _now(), "type": event, "leaseId": self._timing_lease_id,
                      "attempt": self.dir.name, **payload}
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            cutoff = (today - timedelta(days=14)).strftime("%Y-%m-%d")
            for old in root.glob("*.jsonl"):
                if old.name[:10] < cutoff:
                    old.unlink(missing_ok=True)

    def _create_dir(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for _ in range(100):
            candidate = self.root / stamp
            try:
                candidate.mkdir()
            except FileExistsError:
                stamp = f"{stamp}-{secrets.token_hex(2)}"
                continue
            return candidate
        raise OSError("无法为 EA 登录证据创建目录")

    def rotate(self) -> Path:
        """Start a fresh attempt directory.

        The managed loop signs in many times per process. One directory per
        attempt is what keeps a failure readable next to the run that caused
        it, instead of one growing pile of frames.
        """

        self.dir = self._create_dir()
        self.steps_path = self.dir / "steps.jsonl"
        self._seq = 0
        self._hints = None
        self._last_step_at = time.monotonic()
        self._previous_step = None
        self.prune()
        return self.dir

    def protect(self, *values: str) -> None:
        """Register run-specific secrets before any frame is written."""

        collected: list[str] = list(self._hints.values if self._hints else ())
        for value in values:
            collected.extend(SecretHints.for_login(value).values)
        self._hints = SecretHints(tuple(sorted(set(collected))))

    def prune(self) -> None:
        attempts = sorted(
            (path for path in self.root.iterdir()
             if path.is_dir() and path.name != "timings" and path != self.dir),
            key=lambda path: (path.stat().st_mtime_ns, path.name),
        )
        # The active directory always occupies one slot. Same-second random
        # suffixes do not sort by age and must not make pruning skip a slot.
        for stale in attempts[: max(0, len(attempts) - (self.keep_attempts - 1))]:
            shutil.rmtree(stale, ignore_errors=True)

    def step(
        self,
        name: str,
        *,
        page: str,
        markers: Sequence[str] = (),
        frame: "np.ndarray | None" = None,
        tokens: "Sequence[OcrToken] | None" = None,
        rect: tuple[int, int, int, int] | None = None,
        **detail: Any,
    ) -> None:
        self._seq += 1
        record: dict[str, Any] = {
            "schemaVersion": EVIDENCE_SCHEMA_VERSION,
            "seq": self._seq,
            "step": name,
            "page": page,
            "markers": list(markers),
            "at": _now(),
        }
        if tokens:
            record["tokenCount"] = len(tokens)
            record["meanConfidence"] = round(
                sum(token.confidence for token in tokens) / len(tokens), 3
            )
        record.update({key: value for key, value in detail.items() if value is not None})
        self._reject_sensitive_values(record)
        now = time.monotonic()
        try:
            self.timing("EA_STEP", step=name, page=page, previousStep=self._previous_step,
                        previousStepElapsedMs=round((now - self._last_step_at) * 1000))
        except OSError:
            pass
        self._last_step_at, self._previous_step = now, name
        with self.steps_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")))
            handle.write("\n")
        if frame is not None and self.save_screenshots:
            try:
                self._save_frame(f"{self._seq:02d}-{name}", frame, tokens, rect)
                from .ea_screenshot_outbox import enqueue
                enqueue(self.root.parent / "diagnostics" / "ea-evidence", frame, record, self._timing_lease_id)
            except Exception:
                # Screenshot I/O must never turn a successful login or cleanup
                # into a business failure. The text event is already durable.
                pass

    def _save_frame(
        self,
        name: str,
        frame: "np.ndarray",
        tokens: "Sequence[OcrToken] | None",
        rect: tuple[int, int, int, int] | None,
    ) -> None:
        # Importing vision pulls in OpenCV, which the pure evidence tests and
        # any non-Windows caller have no reason to need.
        from .vision import save_frame

        if frame.size == 0:
            return
        save_frame(self.dir / f"{name}.png", frame)

    @staticmethod
    def _reject_sensitive_values(value: object, trail: str = "") -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                name = str(key)
                lowered = name.lower()
                # A sensitive name may still carry a bare fact — whether the
                # identifier was echoed back by the page is exactly the thing
                # this evidence exists to answer — so only values that could
                # hold the secret itself are refused.
                if any(
                    marker in lowered
                    for marker in ("password", "secret", "token", "otp", "identifier")
                ) and not isinstance(child, (bool, int)):
                    location = f"{trail}.{name}" if trail else name
                    raise ValueError(f"EA 登录证据不允许敏感字段：{location}")
                EaLoginEvidence._reject_sensitive_values(
                    child,
                    f"{trail}.{name}" if trail else name,
                )
        elif isinstance(value, (list, tuple)):
            for index, child in enumerate(value):
                EaLoginEvidence._reject_sensitive_values(child, f"{trail}[{index}]")
        elif isinstance(value, str) and is_sensitive_text(value):
            raise ValueError(f"EA 登录证据不允许敏感文本：{trail or 'value'}")


def default_evidence_root(runs_root: Path) -> Path:
    return Path(runs_root) / "ea-login"


def relative_to_repository(path: Path, repository_root: Path) -> str:
    try:
        return path.resolve().relative_to(Path(repository_root).resolve()).as_posix()
    except ValueError:
        return str(path)


__all__ = [
    "EaLoginEvidence",
    "SecretHints",
    "default_evidence_root",
    "is_sensitive_text",
    "relative_to_repository",
]

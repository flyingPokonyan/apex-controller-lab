"""Locate the installed EA desktop and keep it outside the input worker's job."""
from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from .atomic_files import replace_with_retry

EA_EXECUTABLE = "eadesktop.exe"
# Every process that can keep an EA window or respawn one. Child processes of
# these images are taken with taskkill /T; names outside this list are left
# alone.
EA_PROCESS_NAMES = ("EADesktop.exe", "EABackgroundService.exe")
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def listed_ea_pids(tasklist_csv: str) -> list[int]:
    """PIDs from `tasklist /FO CSV` whose image is an EA desktop process."""

    names = {name.casefold() for name in EA_PROCESS_NAMES}
    found: list[int] = []
    for row in csv.reader(tasklist_csv.splitlines()):
        if len(row) < 2 or not row[1].isdigit():
            continue
        if row[0].casefold() in names:
            pid = int(row[1])
            if pid not in found:
                found.append(pid)
    return found


def desktop_app_creationflags() -> int:
    # The managed launcher permits explicit breakaway. Ordinary input workers
    # remain in its kill-on-close job. Outside a job Windows ignores this flag.
    return CREATE_BREAKAWAY_FROM_JOB if os.name == "nt" else 0


def verified_ea_executable(value: str | Path) -> Path | None:
    path = Path(value)
    if path.is_absolute() and path.name.lower() == EA_EXECUTABLE and path.is_file():
        return path
    return None


def registered_ea_executables():
    if os.name != "nt":
        return
    import winreg
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\EADesktop.exe",
                                    0, winreg.KEY_READ | view) as key:
                    value, kind = winreg.QueryValueEx(key, "")
                    if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(value, str):
                        yield os.path.expandvars(value).strip('"')
            except OSError:
                continue


class EaDesktopInstallation:
    def __init__(self, cache_path: Path | None = None):
        root = Path(os.environ.get("APEX_MANAGED_ROOT") or Path(__file__).resolve().parents[1] / "runs/managed")
        self.cache_path = cache_path or root / "ea-app.json"
        self._remembered: Path | None = None

    def remember(self, executable: str | Path) -> Path | None:
        path = verified_ea_executable(executable)
        if path is None or path == self._remembered:
            return path
        self._remembered = path
        temporary = self.cache_path.with_name(f".{self.cache_path.name}.{os.getpid()}.tmp")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"executable": str(path)}), encoding="utf-8")
            replace_with_retry(temporary, self.cache_path)
        except OSError:
            # A cache write must not break a working EA window. Discovery also
            # has the registry and standard installation directories below.
            pass
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
        return path

    def resolve(self) -> Path | None:
        candidates = []
        if self._remembered is not None:
            candidates.append(self._remembered)
        try:
            cached = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and isinstance(cached.get("executable"), str):
                candidates.append(cached["executable"])
        except (OSError, ValueError):
            pass
        candidates.extend(registered_ea_executables())
        for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
            root = os.environ.get(variable)
            if root:
                candidates.append(Path(root) / "Electronic Arts/EA Desktop/EA Desktop/EADesktop.exe")
        for candidate in candidates:
            path = verified_ea_executable(candidate)
            if path is not None:
                return self.remember(path)
        return None

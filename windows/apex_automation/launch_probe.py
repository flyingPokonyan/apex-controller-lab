"""What the desktop holds when EA sits on "Launching game…": counts, ages and sizes only.

D791 left Play stalled about ten times in six days (D163 none), in streaks that a
restart of EA did not clear, and Forge only saw "no Apex process". This probe
answers the next question without a remote session: was a game or anti-cheat
process already (or still) there, did one crash, did EA's own services run.

Nothing leaves as text. Processes are counted under fixed labels; crash records
are read only for the program they name and counted per label, so the result
fits the diagnostic allowlist (no names, paths, command lines or messages).
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import subprocess
from typing import Callable

# Executable (lower case) -> label. Only these are counted.
PROCESS_LABELS = {
    "r5apex.exe": "game",
    "r5apex_dx12.exe": "game",
    "eaanticheat.gameservicelauncher.exe": "antiCheatLauncher",
    "eaanticheat.gameservice.exe": "antiCheatService",
    "eadesktop.exe": "eaDesktop",
    "eabackgroundservice.exe": "eaBackground",
}
LAUNCH_PROCESSES = ("game", "antiCheatLauncher", "antiCheatService", "eaDesktop", "eaBackground")
# Program-name prefix in a Windows Application error/hang record -> label.
CRASH_PREFIXES = (("r5apex", "game"), ("eaanticheat", "antiCheat"), ("eadesktop", "eaDesktop"))
LAUNCH_CRASHES = ("game", "antiCheat", "eaDesktop")

_PROCESS_SCRIPT = (
    "$names = @('" + "','".join(PROCESS_LABELS) + "');"
    "@(Get-CimInstance Win32_Process | Where-Object { $names -contains $_.Name.ToLower() } | ForEach-Object {"
    " [pscustomobject]@{ n = $_.Name.ToLower(); s = $_.CreationDate.ToUniversalTime().ToString('o'); m = $_.WorkingSetSize } })"
    " | ConvertTo-Json -Compress"
)
# Application Error (1000) and Application Hang (1002) name the program first;
# Windows Error Reporting (1001) names it as its sixth field. Last 10 minutes only.
_CRASH_SCRIPT = (
    "@(Get-WinEvent -FilterHashtable @{LogName='Application'; Id=1000,1001,1002;"
    " StartTime=(Get-Date).AddMinutes(-10)} -ErrorAction SilentlyContinue | ForEach-Object {"
    " if ($_.Id -eq 1001) { if ($_.Properties.Count -gt 5) { [string]$_.Properties[5].Value } }"
    " elseif ($_.Properties.Count -gt 0) { [string]$_.Properties[0].Value } })"
    " | ConvertTo-Json -Compress"
)


def summarize_processes(rows: list[dict], now: datetime) -> dict[str, dict]:
    """Per label: how many, how long the oldest has run (s), their memory (MB)."""
    summary = {label: {"count": 0, "ageS": None, "memMb": None} for label in LAUNCH_PROCESSES}
    for row in rows:
        label = PROCESS_LABELS.get(str(row.get("n", "")).lower())
        if label is None:
            continue
        sample = summary[label]
        sample["count"] = min(sample["count"] + 1, 64)
        try:
            started = datetime.fromisoformat(str(row.get("s", "")).replace("Z", "+00:00"))
            age = max(0.0, round((now - started).total_seconds(), 1))
            sample["ageS"] = age if sample["ageS"] is None else max(sample["ageS"], age)
        except ValueError:
            pass
        memory = row.get("m")
        if isinstance(memory, (int, float)) and not isinstance(memory, bool) and memory >= 0:
            sample["memMb"] = round((sample["memMb"] or 0.0) + memory / 2**20, 1)
    return summary


def summarize_crashes(programs: list[str]) -> dict[str, int]:
    """How many error/hang records in the window name each watched program."""
    counts = {label: 0 for label in LAUNCH_CRASHES}
    for program in programs:
        name = str(program).strip().lower()
        for prefix, label in CRASH_PREFIXES:
            if name.startswith(prefix):
                counts[label] = min(counts[label] + 1, 1000)
                break
    return counts


def _powershell(script: str, run: Callable) -> list:
    result = run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, errors="replace", timeout=20, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if result.returncode != 0:
        raise RuntimeError("probe command failed")
    text = (result.stdout or "").strip()
    if not text:
        return []
    value = json.loads(text)
    # ConvertTo-Json writes a single element as the element itself.
    return value if isinstance(value, list) else [value]


def probe(run: Callable = subprocess.run, now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> dict:
    """The allowlisted facts; probeOk is false when either query could not run."""
    ok = True
    try:
        processes = summarize_processes(_powershell(_PROCESS_SCRIPT, run), now())
    except Exception:
        ok = False
        processes = summarize_processes([], now())
    try:
        crashes = summarize_crashes(_powershell(_CRASH_SCRIPT, run))
    except Exception:
        ok = False
        crashes = summarize_crashes([])
    return {"processes": processes, "crashes": crashes, "probeOk": ok}

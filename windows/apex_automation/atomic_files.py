from __future__ import annotations

import os
from pathlib import Path
import time


def replace_with_retry(
    source: Path,
    target: Path,
    *,
    attempts: int = 12,
    delay_s: float = 0.05,
) -> None:
    """Rename over `target`, waiting out the transient Windows lock.

    Real-time virus scanning opens a file the moment it is written, and while
    that handle is alive `os.replace` fails with `WinError 5`. Every file this
    is used for is rewritten continuously — `status.json` once per frame — so
    the collision is unlikely per write and certain over a session. One lost
    race used to end the run: a status write raised mid-match, the play
    session unwound, and the account was left leased at CLEANUP_UNCONFIRMED.

    Retrying is safe because the temporary file is private to this attempt.
    The final failure still propagates: a lock that outlives four seconds is
    not the scanner, and the caller writing a checkpoint or an outbox needs to
    hear about it.
    """

    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay_s * (attempt + 1))

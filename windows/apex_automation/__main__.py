import os
import sys
from pathlib import Path

from .instance_lock import AlreadyRunningError, acquire_entry_lock

if any(command in sys.argv[1:] for command in ("play", "account-cycle", "ea-login-check")):
    try:
        acquire_entry_lock(Path(__file__).resolve().parents[1] / "runs" / "play.lock")
    except AlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(73 if os.environ.get("APEX_MANAGED_SESSION") else 2)

from .cli import main
from .managed_runtime import ManagedUpdateRequested, UPDATE_EXIT


try:
    exit_code = main()
except ManagedUpdateRequested:
    exit_code = UPDATE_EXIT
if sys.platform == "win32" and any(
    command in sys.argv[1:]
    for command in ("live", "observe", "play", "account-cycle", "start")
):
    # DXcam 0.3.0 issue #144 can crash during COM finalization. The live runner
    # has already flushed every log record; bypass Python finalizers so Windows
    # can reclaim the process-scoped capture resources safely.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)

raise SystemExit(exit_code)

import os
import sys

from .cli import main


exit_code = main()
if sys.platform == "win32" and any(command in sys.argv[1:] for command in ("live", "start")):
    # DXcam 0.3.0 issue #144 can crash during COM finalization. The live runner
    # has already flushed every log record; bypass Python finalizers so Windows
    # can reclaim the process-scoped capture resources safely.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(exit_code)

raise SystemExit(exit_code)

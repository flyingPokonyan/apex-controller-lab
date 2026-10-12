import sys
from . import check_runtime

if sys.argv[1:] == ["--check-runtime"]:
    check_runtime()
    print("Registration runtime ready")
else:
    from .cli import main
    raise SystemExit(main())

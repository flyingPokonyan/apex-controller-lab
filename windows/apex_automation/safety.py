"""Stop conditions shared by the input layer and the runners.

These live apart from `input_win32` so the capability loop can name them
without importing a Windows-only module; the runners are unit-tested on
machines that have no `ctypes.wintypes`.
"""

from __future__ import annotations


class EmergencyStop(RuntimeError):
    """F8, or any other request to stop the session outright."""


class ForegroundLost(RuntimeError):
    """Apex stopped being the foreground window before an input was sent.

    This is a *pause*, not a failure. The window can be given back at any
    moment and the run is expected to carry on watching — `20260730-232551`
    ended as FAILED with no summary written because alt-tabbing between the
    foreground check and the send raised a plain RuntimeError instead.
    """

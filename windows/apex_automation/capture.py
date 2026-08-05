from __future__ import annotations

import ctypes
import importlib.util
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np


_original_unraisable_hook = sys.unraisablehook
_camera_keepalive: list[Any] = []
_dxcam_log_handler: "_NotifyHandler | None" = None
_dxcam_recovery_bounded = False

# DXGI enumerates attached outputs only, so the highest valid index is however
# many displays are on the desktop. Probing past that is what tells us we ran
# out of them.
_OUTPUT_PROBE_LIMIT = 8


class CaptureRecoveryTimeout(Exception):
    """DXcam's output recovery ran past its deadline and was cut short.

    Deliberately not a `RuntimeError`: DXcam's recovery loop catches `COMError`,
    `OSError` and `RuntimeError` and reads every one of them as a reason to try
    again, so those are the three types that cannot end it.
    """


def _ignore_known_dxcam_release_warning(unraisable: sys.UnraisableHookArgs) -> None:
    """Hide DXcam 0.3.0 issue #144 without hiding unrelated cleanup errors.

    Releasing a duplicator that DXGI has already invalidated faults inside
    comtypes' pointer teardown. It surfaces as an unraisable exception during
    garbage collection, with the faulting address — and the read/write
    direction — depending on what the driver left behind.
    """

    message = str(unraisable.exc_value)
    target = repr(unraisable.object)
    if (
        unraisable.exc_type is OSError
        and "_compointer_base.__del__" in target
        and "access violation" in message
    ):
        return
    _original_unraisable_hook(unraisable)


def report(message: str) -> None:
    """Say it on the console and keep a copy on disk.

    Everything capture knows used to live only in the console, and the console
    lives in a window an operator closes. Sign-in has no run directory to fall
    back on either, so a cycle that ends there leaves nothing behind at all —
    and what capture was doing when a run stopped is precisely the thing that
    cannot be reconstructed afterwards.
    """

    print(message)
    try:
        path = Path(__file__).resolve().parents[1] / "runs" / "capture.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except Exception:
        # Diagnostics must never be the reason a run dies.
        pass


class _NotifyHandler(logging.Handler):
    """Sends DXcam's own log through the runner's console."""

    def __init__(self, notify: Callable[[str], None]) -> None:
        super().__init__(level=logging.INFO)
        self.notify = notify

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.notify(f"[dxcam] {record.getMessage()}")
        except Exception:
            pass


def _route_dxcam_logs(notify: Callable[[str], None]) -> None:
    """Make DXcam's recovery audible.

    Everything DXcam says while it is putting a duplicator back together is
    `INFO`, and this project configures no logging at all, so only the two
    warnings that open a recovery reach the console. A recovery that never
    succeeds then looks exactly like a runner that stopped for no reason.
    """

    global _dxcam_log_handler
    if _dxcam_log_handler is not None:
        _dxcam_log_handler.notify = notify
        return
    logger = logging.getLogger("dxcam")
    logger.setLevel(logging.INFO)
    logger.propagate = False  # otherwise every warning prints twice
    _dxcam_log_handler = _NotifyHandler(notify)
    logger.addHandler(_dxcam_log_handler)


def _bound_dxcam_recovery(timeout: float, notify: Callable[[str], None]) -> None:
    """Give DXcam's output recovery an end.

    `DisplayRecoveryHandler.handle` retries forever, up to five seconds apart,
    and it only re-enumerates outputs when DXGI *raises*. An output that is
    simply gone from the desktop — a monitor switched off with the capture
    aimed at it — reports itself detached instead, which the loop reads as one
    more transient failure. It never moves to the dummy plug that is still
    there, and the `grab()` that started all this never returns: the runner
    stops mid-match with nothing after those two warnings.

    Cutting the loop short hands the decision back to this module, which can
    build a camera on an output that still exists.
    """

    global _dxcam_recovery_bounded
    if _dxcam_recovery_bounded:
        return
    try:
        from dxcam.core import display_recovery, output_recovery

        display_handler = display_recovery.DisplayRecoveryHandler
        output_handler = output_recovery.OutputRecoveryHandler
        original_display = display_handler.handle
        original_output = output_handler.handle
    except Exception as error:
        notify(f"没能给 dxcam 的恢复循环加上超时（{type(error).__name__}），它会一直重试")
        return

    def handle_display(self: Any, **kwargs: Any) -> Any:
        recovery = getattr(self, "_output_recovery", None)
        if recovery is None:
            return original_display(self, **kwargs)
        recovery._deadline = time.monotonic() + timeout
        try:
            return original_display(self, **kwargs)
        finally:
            recovery._deadline = None

    def handle_output(self: Any, **kwargs: Any) -> Any:
        deadline = getattr(self, "_deadline", None)
        if deadline is not None and time.monotonic() >= deadline:
            raise CaptureRecoveryTimeout(f"恢复重试超过 {timeout:.0f} 秒仍未成功")
        return original_output(self, **kwargs)

    display_handler.handle = handle_display
    output_handler.handle = handle_output
    _dxcam_recovery_bounded = True


def describe_displays() -> str:
    """Name every monitor Windows currently has on the desktop.

    Printed when capture goes wrong, because that is the moment the answer
    matters: whether the dummy plug is on the desktop at all, and at what
    resolution, decides whether the runner has anywhere to go.
    """

    if sys.platform != "win32":
        return "显示器列表：仅 Windows 可读"
    try:
        monitors = _enumerate_monitors()
    except Exception as error:
        return f"显示器列表读取失败：{type(error).__name__}"
    if not monitors:
        return "显示器列表：桌面上一个显示器都没有"
    return "显示器列表：" + "；".join(monitors)


def _enumerate_monitors() -> list[str]:
    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.c_ulong),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", ctypes.c_ulong),
            ("szDevice", ctypes.c_wchar * 32),
        ]

    monitor_enum_proc = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(RECT),
        ctypes.c_ssize_t,
    )
    user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    user32.GetMonitorInfoW.restype = ctypes.c_int
    user32.EnumDisplayMonitors.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        monitor_enum_proc,
        ctypes.c_ssize_t,
    ]
    user32.EnumDisplayMonitors.restype = ctypes.c_int

    found: list[str] = []

    def on_monitor(handle: int, _dc: int, _rect: Any, _data: int) -> int:
        info = MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(MONITORINFOEXW)
        if user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            width = info.rcMonitor.right - info.rcMonitor.left
            height = info.rcMonitor.bottom - info.rcMonitor.top
            primary = "，主显示器" if info.dwFlags & 1 else ""
            found.append(f"{info.szDevice} {width}x{height}{primary}")
        return 1

    user32.EnumDisplayMonitors(None, None, monitor_enum_proc(on_monitor), 0)
    return found


class DxcamFrameSource:
    """Screen capture that survives DXGI taking the duplicator away.

    `DXGI_ERROR_ACCESS_LOST` is routine on a machine left running: a mode
    change, a driver update, the lock screen, a monitor powering off a
    DisplayPort link. DXcam rebuilds internally and sometimes comes back
    empty, so the source has to be prepared to build a fresh one.
    """

    def __init__(
        self,
        backend: str = "dxgi",
        output_index: int = 0,
        *,
        camera_factory: Callable[..., Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] = report,
        recovery_timeout: float = 20.0,
        rebuild_delays: tuple[float, ...] = (1.0, 2.0, 4.0, 8.0, 15.0),
    ):
        self.backend = backend
        self.output_index = output_index
        self.camera_factory = camera_factory
        self.sleep = sleep
        self.notify = notify
        self.recovery_timeout = recovery_timeout
        self.rebuild_delays = rebuild_delays
        self._camera: Any = None
        self._started = False
        self._camera_lost = False
        self._recovery_gave_up = False

    def _factory(self) -> Callable[..., Any]:
        if self.camera_factory is not None:
            return self.camera_factory
        try:
            import dxcam
        except ImportError as error:
            raise RuntimeError("缺少 dxcam，请先运行 windows\\setup.ps1") from error
        self._settle_backend()
        _route_dxcam_logs(self.notify)
        _bound_dxcam_recovery(self.recovery_timeout, self.notify)
        return dxcam.create

    def _settle_backend(self) -> None:
        """Choose the backend this machine can actually run.

        Windows.Graphics.Capture rides out the display transitions that end a
        desktop duplication — a mode change, a fullscreen switch, a
        configuration Windows re-applies — but it needs packages the DXGI path
        does not. A runner that refuses to start is worse than one on the older
        backend, so a missing install falls back and says so.
        """

        if self.backend != "winrt":
            return
        try:
            available = importlib.util.find_spec("winrt.windows.graphics.capture")
        except Exception:
            available = None
        if available is None:
            self.notify(
                "配置要求 winrt 捕获后端，但依赖没装，先用 dxgi。"
                '装法：windows\\.venv\\Scripts\\python -m pip install "dxcam[winrt]==0.3.0"'
            )
            self.backend = "dxgi"
            return
        # WGC composites the cursor and draws a capture border by default, and
        # both land inside the frames the regions are matched against.
        os.environ.setdefault("DXCAM_WINRT_BORDER_REQUIRED", "0")
        os.environ.setdefault("DXCAM_WINRT_CURSOR_CAPTURE", "0")

    def _create_at(self, output_index: int) -> Any:
        return self._factory()(
            backend=self.backend,
            output_idx=output_index,
            output_color="BGRA",
            processor_backend="numpy",
        )

    def _reenumerate_outputs(self) -> None:
        """Make DXcam look at the displays that exist now.

        `DXFactory` enumerates adapters and outputs once, when the module is
        imported, and every later `create()` hands back an `Output` from that
        first look. So after a monitor is switched off or the desktop is moved
        to another one, rebuilding aims at pointers into a topology that is
        gone — including the probe across every index, which is why a dummy
        plug that was there the whole time never got picked up. A new factory
        is the only thing short of a new process that sees the current one.
        """

        if self.camera_factory is not None:
            return
        try:
            import dxcam

            factory_type = dxcam.DXFactory
            type(factory_type)._instances.pop(factory_type, None)
            # Keyed by output index, which a topology change has just
            # renumbered; every camera in there belongs to the old look.
            factory_type._camera_instances.clear()
            setattr(dxcam, "__factory", factory_type())
        except Exception as error:
            self.notify(f"重新枚举显示输出失败：{type(error).__name__}：{error}")
            return
        self.notify("已按当前显示器重新枚举 dxcam 的输出")

    def _candidate_indices(self) -> list[int]:
        others = [index for index in range(_OUTPUT_PROBE_LIMIT) if index != self.output_index]
        return [self.output_index, *others]

    def _create(self) -> Any:
        """Build a camera on an output that still exists.

        DXGI only enumerates *attached* outputs, so a display going away
        shifts every index behind it down by one. After a monitor powers off,
        the index this runner was configured with may point at nothing — or at
        a different display than the one the regions were calibrated against,
        which the resolution check downstream will catch.
        """

        preferred_error: Exception | None = None
        for index in self._candidate_indices():
            try:
                camera = self._create_at(index)
            except Exception as error:
                if index == self.output_index:
                    preferred_error = error
                continue
            if camera is None:
                continue
            if index != self.output_index:
                self.notify(
                    f"输出 {self.output_index} 不可用，改用输出 {index}{self._describe(camera)}"
                )
                self.output_index = index
            return camera
        if preferred_error is not None:
            raise preferred_error
        raise RuntimeError("DXcam 无法在任何输出上创建捕获源")

    @staticmethod
    def _describe(camera: Any) -> str:
        width = getattr(camera, "width", None)
        height = getattr(camera, "height", None)
        return f"（{width}x{height}）" if width and height else ""

    def __enter__(self) -> "DxcamFrameSource":
        sys.unraisablehook = _ignore_known_dxcam_release_warning
        self._camera = self._create()
        self._started = True
        # Worth one line: which display the run was actually reading is the
        # first thing anyone asks when the regions stop matching.
        self.notify(f"捕获输出 {self.output_index}{self._describe(self._camera)}")
        return self

    @staticmethod
    def _mark_released(camera: Any) -> None:
        # DXcam's factory hands back the camera an output already has unless
        # that camera says it has been released, so a rebuild that stays quiet
        # gets the dead one back. Setting the flag says it without calling
        # release(), which is the call that corrupts the process (#144).
        try:
            camera._is_released = True
        except Exception:
            pass

    def _rebuild(self) -> None:
        # Never release the old one: DXcam 0.3.0 issue #144 turns that into a
        # process-corrupting fault precisely when the duplicator is already
        # invalid, which is the only time this runs.
        camera, self._camera = self._camera, None
        # Either the display went away under this camera, or an earlier
        # attempt already failed to find one. Both mean the displays DXcam
        # knows about are not the displays that are there.
        stale_topology = self._camera_lost or camera is None
        if camera is not None:
            _camera_keepalive.append(camera)
            if self._recovery_gave_up:
                # Recovery gave up *after* DXcam released this camera's
                # duplicator and stage surface, so the output is free for a
                # replacement to duplicate. Any other time the old duplicator
                # still holds the output, and asking for a fresh camera would
                # only fail — getting this one back is the better answer.
                self._mark_released(camera)
        self._camera_lost = False
        self._recovery_gave_up = False
        if stale_topology:
            self._reenumerate_outputs()
        self._camera = self._create()

    def _rebuild_until_a_frame_arrives(self) -> np.ndarray | None:
        """Keep rebuilding while the desktop is still moving.

        A display coming back — a mode change finishing, a driver resetting,
        Windows re-applying a configuration — can take longer than one attempt,
        and until it does there is no output to build a camera on. Giving up on
        the first failure turns a transition the machine would have survived
        into a dead run and a lease nobody released.
        """

        self.notify("捕获失效，正在重建截图源…")
        self.notify(describe_displays())
        last_error: Exception | None = None
        for delay in self.rebuild_delays:
            self.sleep(delay)
            try:
                self._rebuild()
            except Exception as error:
                last_error = error
                self.notify(f"重建失败：{type(error).__name__}：{error}")
                continue
            frame = self._safe_grab()
            if frame is not None:
                return frame
        if last_error is not None:
            raise last_error
        return None

    def grab(self) -> np.ndarray:
        if not self._started:
            raise RuntimeError("截图服务尚未启动")
        frame = self._safe_grab()
        if frame is None and not self._camera_lost:
            # DXcam puts its own duplicator back together in place, and the
            # first frame after it does is empty because recovery drops the
            # cached one. An empty frame is that far more often than it is a
            # camera worth throwing away; a raised one is not.
            self.sleep(1.0)
            frame = self._safe_grab()
        if frame is None:
            frame = self._rebuild_until_a_frame_arrives()
        if frame is None:
            raise RuntimeError("DXcam 没有返回画面帧")
        return frame.copy()

    def _safe_grab(self) -> np.ndarray | None:
        if self._camera is None:
            return None
        try:
            return self._camera.grab(new_frame_only=False)
        except CaptureRecoveryTimeout as error:
            self._camera_lost = True
            self._recovery_gave_up = True
            self.notify(f"桌面复制恢复超时：{error}")
            return None
        except Exception as error:  # DXcam surfaces access loss as OSError
            self._camera_lost = True
            self.notify(f"截图失败：{type(error).__name__}")
            return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._started = False
        if self._camera is not None:
            # Same reason as _rebuild: keep the object alive and let Windows
            # reclaim this one-shot runner's resources at process exit.
            _camera_keepalive.append(self._camera)
            self._camera = None

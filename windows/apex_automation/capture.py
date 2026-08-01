from __future__ import annotations

import sys
import time
from typing import Any, Callable

import numpy as np


_original_unraisable_hook = sys.unraisablehook
_camera_keepalive: list[Any] = []


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
        notify: Callable[[str], None] = print,
    ):
        self.backend = backend
        self.output_index = output_index
        self.camera_factory = camera_factory
        self.sleep = sleep
        self.notify = notify
        self._camera: Any = None
        self._started = False

    def _factory(self) -> Callable[..., Any]:
        if self.camera_factory is not None:
            return self.camera_factory
        try:
            import dxcam
        except ImportError as error:
            raise RuntimeError("缺少 dxcam，请先运行 windows\\setup.ps1") from error
        return dxcam.create

    def _create_at(self, output_index: int) -> Any:
        return self._factory()(
            backend=self.backend,
            output_idx=output_index,
            output_color="BGRA",
            processor_backend="numpy",
        )

    def _create(self) -> Any:
        """Build a camera, tolerating an output index that has moved.

        DXGI only enumerates *attached* outputs, so a display going away
        shifts every index behind it down by one. After a monitor powers off,
        the index this runner was configured with may point at nothing.
        """

        try:
            camera = self._create_at(self.output_index)
        except Exception as error:
            if self.output_index == 0:
                raise
            self.notify(f"输出 {self.output_index} 不可用（{type(error).__name__}），改用输出 0")
            camera = None
        if camera is None and self.output_index != 0:
            self.notify(f"输出 {self.output_index} 已不存在，改用输出 0")
            camera = self._create_at(0)
            self.output_index = 0
        if camera is None:
            raise RuntimeError("DXcam 无法在任何输出上创建捕获源")
        return camera

    def __enter__(self) -> "DxcamFrameSource":
        sys.unraisablehook = _ignore_known_dxcam_release_warning
        self._camera = self._create()
        self._started = True
        return self

    def _rebuild(self) -> None:
        # Never release the old one: DXcam 0.3.0 issue #144 turns that into a
        # process-corrupting fault precisely when the duplicator is already
        # invalid, which is the only time this runs.
        if self._camera is not None:
            _camera_keepalive.append(self._camera)
            self._camera = None
        self.sleep(1.0)
        self._camera = self._create()

    def grab(self) -> np.ndarray:
        if not self._started:
            raise RuntimeError("截图服务尚未启动")
        frame = self._safe_grab()
        if frame is None:
            self.notify("捕获失效，正在重建截图源…")
            self._rebuild()
            frame = self._safe_grab()
        if frame is None:
            raise RuntimeError("DXcam 没有返回画面帧")
        return frame.copy()

    def _safe_grab(self) -> np.ndarray | None:
        if self._camera is None:
            return None
        try:
            return self._camera.grab(new_frame_only=False)
        except Exception as error:  # DXcam surfaces access loss as OSError
            self.notify(f"截图失败：{type(error).__name__}")
            return None

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._started = False
        if self._camera is not None:
            # Same reason as _rebuild: keep the object alive and let Windows
            # reclaim this one-shot runner's resources at process exit.
            _camera_keepalive.append(self._camera)
            self._camera = None

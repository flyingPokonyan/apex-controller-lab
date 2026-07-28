from __future__ import annotations

import sys
from typing import Any

import numpy as np


_original_unraisable_hook = sys.unraisablehook
_camera_keepalive: list[Any] = []


def _ignore_known_dxcam_release_warning(unraisable: sys.UnraisableHookArgs) -> None:
    """Hide DXcam 0.3.0 issue #144 without hiding unrelated cleanup errors."""
    message = str(unraisable.exc_value)
    target = repr(unraisable.object)
    if (
        unraisable.exc_type is OSError
        and "_compointer_base.__del__" in target
        and "access violation reading 0xFFFFFFFFFFFFFFFF" in message
    ):
        return
    _original_unraisable_hook(unraisable)


class DxcamFrameSource:
    def __init__(self, backend: str = "dxgi", output_index: int = 0):
        self.backend = backend
        self.output_index = output_index
        self._camera: Any = None

    def __enter__(self) -> "DxcamFrameSource":
        sys.unraisablehook = _ignore_known_dxcam_release_warning
        try:
            import dxcam
        except ImportError as error:
            raise RuntimeError("缺少 dxcam，请先运行 windows\\setup.ps1") from error

        self._camera = dxcam.create(
            backend=self.backend,
            output_idx=self.output_index,
            output_color="BGRA",
            processor_backend="numpy",
        )
        return self

    def grab(self) -> np.ndarray:
        if self._camera is None:
            raise RuntimeError("截图服务尚未启动")
        frame = self._camera.grab(new_frame_only=False)
        if frame is None:
            raise RuntimeError("DXcam 没有返回画面帧")
        return frame.copy()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._camera is not None:
            # DXcam 0.3.0 issue #144: release() can corrupt the process after a
            # successful capture. Keep the object alive and let Windows reclaim
            # this one-shot runner's resources when __main__ calls os._exit().
            _camera_keepalive.append(self._camera)
            self._camera = None

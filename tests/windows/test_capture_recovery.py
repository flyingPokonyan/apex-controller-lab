from __future__ import annotations

from pathlib import Path
from types import ModuleType
import sys
import unittest


import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation import capture as capture_module
from apex_automation.capture import CaptureRecoveryTimeout, DxcamFrameSource


class FakeCamera:
    """Stands in for a DXcam camera with a scripted sequence of results."""

    def __init__(self, results: list[object]) -> None:
        self.results = list(results)
        self.grabs = 0

    def grab(self, new_frame_only: bool = True) -> object:
        self.grabs += 1
        if not self.results:
            return np.zeros((2, 2, 4), dtype=np.uint8)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class CaptureRecoveryTest(unittest.TestCase):
    def source(self, *cameras: FakeCamera) -> tuple[DxcamFrameSource, list[str]]:
        built = list(cameras)
        messages: list[str] = []

        def factory(**_: object) -> FakeCamera:
            return built.pop(0)

        return (
            DxcamFrameSource(
                camera_factory=factory,
                sleep=lambda _: None,
                notify=messages.append,
            ),
            messages,
        )

    def test_a_frame_is_returned_without_rebuilding(self) -> None:
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        camera = FakeCamera([frame])
        source, messages = self.source(camera)
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))
        self.assertEqual(messages, ["捕获输出 0"])

    def test_access_loss_rebuilds_the_camera_and_keeps_going(self) -> None:
        # DXGI hands the duplicator back as an OSError on access loss, which
        # is routine on a machine left running overnight.
        broken = FakeCamera([OSError("access lost")])
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        replacement = FakeCamera([frame])
        source, messages = self.source(broken, replacement)

        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))

        self.assertEqual(replacement.grabs, 1)
        self.assertTrue(any("重建" in message for message in messages))

    def test_an_empty_frame_retries_the_same_camera_first(self) -> None:
        # DXcam rebuilds its own duplicator in place and drops the cached frame
        # doing it, so the frame right after a recovery is empty on a camera
        # that is perfectly alive.
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        camera = FakeCamera([None, frame])
        source, messages = self.source(camera)
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))
        self.assertEqual(camera.grabs, 2)
        self.assertFalse(any("重建" in message for message in messages))

    def test_a_second_empty_frame_triggers_the_rebuild(self) -> None:
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        stale = FakeCamera([None, None])
        source, messages = self.source(stale, FakeCamera([frame]))
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))
        self.assertTrue(any("重建" in message for message in messages))
        # Its duplicator was never released, so it still holds the output and
        # the replacement has to be the same camera back, not a second one.
        self.assertFalse(getattr(stale, "_is_released", False))

    def test_a_recovery_timeout_rebuilds_without_a_second_try(self) -> None:
        # Past this point DXcam has already released the duplicator and given
        # up, so asking the same camera again only wastes a second.
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        wedged = FakeCamera([CaptureRecoveryTimeout("恢复重试超过 20 秒仍未成功")])
        source, messages = self.source(wedged, FakeCamera([frame]))
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))
        self.assertEqual(wedged.grabs, 1)
        self.assertTrue(any("恢复超时" in message for message in messages))
        # DXcam's factory hands back the camera an output already has unless it
        # has been told that one is finished.
        self.assertTrue(wedged._is_released)

    def test_a_replacement_that_also_fails_is_reported(self) -> None:
        source, _ = self.source(FakeCamera([None, None]), FakeCamera([None]))
        with source as capture:
            with self.assertRaisesRegex(RuntimeError, "没有返回画面帧"):
                capture.grab()

    def test_a_vanished_output_index_falls_back_to_the_first_one(self) -> None:
        # DXGI enumerates attached outputs only, so a monitor powering off
        # shifts every index behind it down by one.
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        asked: list[int] = []
        messages: list[str] = []

        def factory(*, output_idx: int, **_: object) -> FakeCamera | None:
            asked.append(output_idx)
            return FakeCamera([frame]) if output_idx == 0 else None

        source = DxcamFrameSource(
            output_index=1,
            camera_factory=factory,
            sleep=lambda _: None,
            notify=messages.append,
        )
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))

        self.assertEqual(asked, [1, 0])
        self.assertEqual(source.output_index, 0)
        self.assertTrue(any("输出 0" in message for message in messages))

    def test_a_raising_output_index_also_falls_back(self) -> None:
        frame = np.ones((2, 2, 4), dtype=np.uint8)

        def factory(*, output_idx: int, **_: object) -> FakeCamera:
            if output_idx != 0:
                raise IndexError("no such output")
            return FakeCamera([frame])

        source = DxcamFrameSource(
            output_index=2,
            camera_factory=factory,
            sleep=lambda _: None,
            notify=lambda _: None,
        )
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))
        self.assertEqual(source.output_index, 0)

    def test_output_zero_failing_is_not_swallowed(self) -> None:
        def factory(**_: object) -> FakeCamera:
            raise IndexError("no outputs at all")

        source = DxcamFrameSource(
            output_index=0,
            camera_factory=factory,
            sleep=lambda _: None,
            notify=lambda _: None,
        )
        with self.assertRaises(IndexError):
            source.__enter__()

    def test_grabbing_before_start_is_refused(self) -> None:
        source, _ = self.source(FakeCamera([]))
        with self.assertRaisesRegex(RuntimeError, "尚未启动"):
            source.grab()

    def test_the_probe_looks_past_more_than_one_missing_output(self) -> None:
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        asked: list[int] = []

        def factory(*, output_idx: int, **_: object) -> FakeCamera | None:
            asked.append(output_idx)
            if output_idx == 2:
                return FakeCamera([frame])
            raise IndexError("no such output")

        source = DxcamFrameSource(
            output_index=1,
            camera_factory=factory,
            sleep=lambda _: None,
            notify=lambda _: None,
        )
        with source as capture:
            self.assertTrue(np.array_equal(capture.grab(), frame))
        self.assertEqual(asked, [1, 0, 2])
        self.assertEqual(source.output_index, 2)

    def test_the_old_camera_is_never_released(self) -> None:
        # Releasing an invalidated duplicator is what corrupts the process,
        # so the broken one has to be abandoned rather than closed.
        broken = FakeCamera([OSError("access lost")])
        broken.release = lambda: self.fail("release must not be called")
        frame = np.ones((2, 2, 4), dtype=np.uint8)
        source, _ = self.source(broken, FakeCamera([frame]))
        with source as capture:
            capture.grab()


class RecoveryDeadlineTest(unittest.TestCase):
    """The patch that ends DXcam's endless output recovery.

    Shaped after `dxcam.core.display_recovery.DisplayRecoveryHandler.handle`:
    an unbounded loop whose only exit is an exception it does not catch, and it
    catches `OSError` and `RuntimeError` because both are transient there.
    """

    def install(self, timeout: float) -> tuple[type, type]:
        class FakeOutputRecovery:
            def __init__(self) -> None:
                self.attempts = 0
                self.detached = True

            def handle(self, *, requested_region: object, region_set_by_user: bool):
                self.attempts += 1
                if self.detached:
                    raise RuntimeError("Output is not attached to desktop.")
                return "recovered"

        class FakeDisplayRecovery:
            def __init__(self) -> None:
                self._output_recovery = FakeOutputRecovery()

            def handle(self, *, region: object, region_set_by_user: bool, is_capturing: bool):
                while True:
                    try:
                        return self._output_recovery.handle(
                            requested_region=region,
                            region_set_by_user=region_set_by_user,
                        )
                    except (OSError, RuntimeError):
                        continue

        display_module = ModuleType("dxcam.core.display_recovery")
        display_module.DisplayRecoveryHandler = FakeDisplayRecovery
        output_module = ModuleType("dxcam.core.output_recovery")
        output_module.OutputRecoveryHandler = FakeOutputRecovery
        core_module = ModuleType("dxcam.core")
        core_module.display_recovery = display_module
        core_module.output_recovery = output_module
        package = ModuleType("dxcam")
        package.core = core_module

        modules = {
            "dxcam": package,
            "dxcam.core": core_module,
            "dxcam.core.display_recovery": display_module,
            "dxcam.core.output_recovery": output_module,
        }
        saved = {name: sys.modules.get(name) for name in modules}
        sys.modules.update(modules)
        capture_module._dxcam_recovery_bounded = False

        def restore() -> None:
            capture_module._dxcam_recovery_bounded = False
            for name, module in saved.items():
                if module is None:
                    sys.modules.pop(name, None)
                else:
                    sys.modules[name] = module

        self.addCleanup(restore)
        capture_module._bound_dxcam_recovery(timeout, notify=lambda _: None)
        return FakeDisplayRecovery, FakeOutputRecovery

    def test_recovery_that_cannot_succeed_ends_instead_of_looping(self) -> None:
        display, _ = self.install(timeout=0.0)
        handler = display()
        with self.assertRaises(CaptureRecoveryTimeout):
            handler.handle(region=(0, 0, 1, 1), region_set_by_user=False, is_capturing=False)
        self.assertEqual(handler._output_recovery.attempts, 0)

    def test_a_recovery_inside_the_deadline_is_left_alone(self) -> None:
        display, _ = self.install(timeout=60.0)
        handler = display()
        handler._output_recovery.detached = False
        result = handler.handle(
            region=(0, 0, 1, 1), region_set_by_user=False, is_capturing=False
        )
        self.assertEqual(result, "recovered")
        self.assertEqual(handler._output_recovery.attempts, 1)
        self.assertIsNone(handler._output_recovery._deadline)


if __name__ == "__main__":
    unittest.main()

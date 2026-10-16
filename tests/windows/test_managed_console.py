from __future__ import annotations

import ctypes
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
import managed_launcher as launcher


class FakeKernel32:
    def __init__(self, mode: int, handle=7) -> None:
        self.mode = mode
        self.handle = handle
        self.opened = None
        self.set_to = None
        self.closed: list[object] = []

    def CreateFileW(self, name, *_args):
        self.opened = name
        return self.handle

    def GetConsoleMode(self, _handle, ref) -> int:
        ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, _handle, mode: int) -> int:
        self.set_to = mode
        return 1

    def CloseHandle(self, handle) -> int:
        self.closed.append(handle)
        return 1


class ConsoleQuickEditTest(unittest.TestCase):
    def test_quick_edit_is_turned_off_and_other_input_modes_are_kept(self) -> None:
        kernel32 = FakeKernel32(0x1F7)  # conhost default: QuickEdit + insert + echo/line/processed

        self.assertTrue(launcher.disable_console_quick_edit(kernel32))

        self.assertEqual(kernel32.opened, "CONIN$")
        self.assertEqual(kernel32.set_to, (0x1F7 & ~0x40) | 0x80)
        self.assertEqual(kernel32.closed, [7])

    def test_a_console_without_quick_edit_is_left_alone(self) -> None:
        kernel32 = FakeKernel32(0x1B7)

        self.assertFalse(launcher.disable_console_quick_edit(kernel32))

        self.assertIsNone(kernel32.set_to)
        self.assertEqual(kernel32.closed, [7])

    def test_no_console_is_not_an_error(self) -> None:
        kernel32 = FakeKernel32(0x1F7, handle=ctypes.c_void_p(-1).value)

        self.assertFalse(launcher.disable_console_quick_edit(kernel32))

        self.assertIsNone(kernel32.set_to)

    def test_a_failing_console_call_never_stops_the_launcher(self) -> None:
        kernel32 = FakeKernel32(0x1F7)
        kernel32.GetConsoleMode = lambda *_args: (_ for _ in ()).throw(OSError("no console"))

        self.assertFalse(launcher.disable_console_quick_edit(kernel32))
        self.assertEqual(kernel32.closed, [7])


if __name__ == "__main__":
    unittest.main()

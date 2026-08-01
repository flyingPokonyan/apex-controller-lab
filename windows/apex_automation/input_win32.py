from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import sys
import time
from typing import Iterable

from .safety import EmergencyStop, ForegroundLost


VK_F8 = 0x77
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_ABSOLUTE = 0x8000
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


if sys.platform == "win32":
    ULONG_PTR = ctypes.c_size_t

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", wintypes.LONG),
            ("dy", wintypes.LONG),
            ("mouseData", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wintypes.WORD),
            ("wScan", wintypes.WORD),
            ("dwFlags", wintypes.DWORD),
            ("time", wintypes.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", wintypes.DWORD),
            ("wParamL", wintypes.WORD),
            ("wParamH", wintypes.WORD),
        ]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


# Re-exported so existing imports keep working; the definitions live in
# `safety` because the capability loop needs them off Windows too.
__all__ = ["EmergencyStop", "ForegroundLost", "Win32SafetyGuard", "Win32InputSender"]


class Win32SafetyGuard:
    def __init__(self, expected_executables: Iterable[str]):
        if sys.platform != "win32":
            raise RuntimeError("真实输入只能在 Windows 上运行")
        self.expected = {name.lower() for name in expected_executables}
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetAsyncKeyState.argtypes = [wintypes.INT]
        self.user32.GetAsyncKeyState.restype = wintypes.SHORT
        self.kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.kernel32.OpenProcess.restype = wintypes.HANDLE
        self.kernel32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
        self.kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel32.CloseHandle.restype = wintypes.BOOL
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            self.user32.SetProcessDPIAware()

    def abort_requested(self) -> bool:
        return bool(self.user32.GetAsyncKeyState(VK_F8) & 0x8000)

    def ensure_not_aborted(self) -> None:
        if self.abort_requested():
            raise EmergencyStop("检测到 F8，已紧急停止")

    def foreground_executable(self) -> str:
        window = self.user32.GetForegroundWindow()
        if not window:
            return ""
        process_id = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(window, ctypes.byref(process_id))
        process = self.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, process_id.value)
        if not process:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not self.kernel32.QueryFullProcessImageNameW(process, 0, buffer, ctypes.byref(size)):
                return ""
            return Path(buffer.value).name.lower()
        finally:
            self.kernel32.CloseHandle(process)

    def ensure_target_foreground(self) -> None:
        self.ensure_not_aborted()
        executable = self.foreground_executable()
        if executable not in self.expected:
            expected = ", ".join(sorted(self.expected))
            raise ForegroundLost(
                f"前台程序不是 Apex（实际 {executable or '未知'}，期望 {expected}），拒绝发送输入"
            )

    def target_is_foreground(self) -> bool:
        """Return foreground state without turning a recoverable pause into an error."""
        self.ensure_not_aborted()
        return self.foreground_executable() in self.expected


class Win32InputSender:
    def __init__(self, guard: Win32SafetyGuard, screen_width: int, screen_height: int):
        self.guard = guard
        self.screen_width = screen_width
        self.screen_height = screen_height
        self.user32 = ctypes.windll.user32
        self._pressed_scan_codes: set[int] = set()
        self._pressed_mouse_buttons: set[str] = set()
        self.user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
        self.user32.SendInput.restype = wintypes.UINT
        expected_size = 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28
        if ctypes.sizeof(INPUT) != expected_size:
            raise RuntimeError(f"Win32 INPUT 结构尺寸异常：{ctypes.sizeof(INPUT)} != {expected_size}")

    def _send(self, inputs: list["INPUT"]) -> None:
        array_type = INPUT * len(inputs)
        payload = array_type(*inputs)
        pointer = ctypes.cast(payload, ctypes.POINTER(INPUT))
        sent = self.user32.SendInput(len(payload), pointer, ctypes.sizeof(INPUT))
        if sent != len(payload):
            raise OSError(f"SendInput 只发送了 {sent}/{len(payload)} 个事件")

    def click(self, x: int, y: int) -> None:
        self.guard.ensure_target_foreground()
        absolute_x = round(x * 65535 / max(self.screen_width - 1, 1))
        absolute_y = round(y * 65535 / max(self.screen_height - 1, 1))
        self._send(
            [INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(absolute_x, absolute_y, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0))]
        )
        # Give the game one render tick to establish hover before pressing.
        # Sending move/down/up in one SendInput batch was accepted on the title
        # screen but intermittently ignored by the lobby Ready button.
        time.sleep(0.12)
        self._send([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0))])
        time.sleep(0.08)
        self._send([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0))])
        time.sleep(0.12)

        # Move away so post-click recognition sees the normal, non-hover UI.
        park_x = round((self.screen_width // 2) * 65535 / max(self.screen_width - 1, 1))
        park_y = round((self.screen_height // 2) * 65535 / max(self.screen_height - 1, 1))
        self._send(
            [INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(park_x, park_y, 0, MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE, 0, 0))]
        )

    def _key(self, scan_code: int, key_up: bool) -> None:
        flags = KEYEVENTF_SCANCODE | (KEYEVENTF_KEYUP if key_up else 0)
        self._send([INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, scan_code, flags, 0, 0))])

    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None:
        self.guard.ensure_target_foreground()
        self._key(scan_code, False)
        self._pressed_scan_codes.add(scan_code)
        try:
            time.sleep(duration_ms / 1000)
        finally:
            self._key(scan_code, True)
            self._pressed_scan_codes.discard(scan_code)

    def hold_scan_codes(self, scan_codes: Iterable[int], duration_ms: int) -> None:
        self.guard.ensure_target_foreground()
        codes = tuple(dict.fromkeys(int(value) for value in scan_codes))
        try:
            for scan_code in codes:
                self._key(scan_code, False)
                self._pressed_scan_codes.add(scan_code)
            time.sleep(duration_ms / 1000)
        finally:
            for scan_code in reversed(codes):
                if scan_code in self._pressed_scan_codes:
                    self._key(scan_code, True)
                    self._pressed_scan_codes.discard(scan_code)

    def move_mouse(self, dx: int, dy: int) -> None:
        self.guard.ensure_target_foreground()
        self._send(
            [INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(int(dx), int(dy), 0, MOUSEEVENTF_MOVE, 0, 0))]
        )

    @staticmethod
    def _mouse_flags(button: str) -> tuple[int, int]:
        try:
            return {
                "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
            }[button]
        except KeyError as error:
            raise ValueError(f"未知鼠标按键：{button}") from error

    def hold_mouse_button(self, button: str, duration_ms: int) -> None:
        self.guard.ensure_target_foreground()
        down_flag, up_flag = self._mouse_flags(button)
        self._send([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, down_flag, 0, 0))])
        self._pressed_mouse_buttons.add(button)
        try:
            time.sleep(duration_ms / 1000)
        finally:
            self._send([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, up_flag, 0, 0))])
            self._pressed_mouse_buttons.discard(button)

    def release_all(self) -> None:
        for scan_code in tuple(self._pressed_scan_codes):
            self._key(scan_code, True)
            self._pressed_scan_codes.discard(scan_code)
        for button in tuple(self._pressed_mouse_buttons):
            _, up_flag = self._mouse_flags(button)
            self._send([INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, up_flag, 0, 0))])
            self._pressed_mouse_buttons.discard(button)

"""Dismiss only recognised desktop notifications covering the packs card."""
from __future__ import annotations

from dataclasses import dataclass
import time
import sys

from .apex_packs import notification_marker


@dataclass(frozen=True)
class NotificationWindow:
    handle: int
    rect: tuple[int, int, int, int]
    executable: str


def find_close_cross(frame, rect, title_rect):
    """Locate an actual X glyph in the title bar, rather than click a fixed corner."""
    import cv2
    import numpy as np
    left, top, right, bottom = rect
    _, ty, _, tb = title_rect
    title_height = max(6, tb - ty)
    x1, x2 = max(left, right - title_height * 4), min(frame.shape[1], right)
    y1, y2 = max(top, ty - title_height), min(bottom, tb + title_height)
    crop = frame[y1:y2, x1:x2, :3]
    if crop.size == 0:
        return None
    grey = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    # Hand Input uses a coloured bar; UU uses white. Contrast-based thresholds
    # also handle antialiasing and the compressed diagnostic screenshots.
    background = float(np.median(grey))
    matches = []
    rows, cols = np.indices((9, 9))
    diagonal = (abs(rows-cols) <= 2) | (abs(rows+cols-8) <= 2)
    for contrast, dark in ((20, True), (20, False), (40, True), (40, False), (60, True), (60, False)):
        mask = ((grey < background-contrast) if dark
                else (grey > background+contrast)).astype(np.uint8)
        count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        for index in range(1, count):
            x, y, w, h, area = stats[index]
            if not (3 <= w <= title_height * 1.6 and 3 <= h <= title_height * 1.6
                    and .7 <= w / h <= 1.4 and .15 <= area / (w * h) <= .75):
                continue
            glyph = cv2.resize((labels[y:y+h, x:x+w] == index).astype(np.uint8),
                               (9, 9), interpolation=cv2.INTER_NEAREST)
            main_score = np.mean([glyph[i, max(0,i-1):min(9,i+2)].max() for i in range(9)])
            other_score = np.mean([glyph[i, max(0,7-i):min(9,10-i)].max() for i in range(9)])
            if (glyph[diagonal].mean() < .15 or glyph[~diagonal].mean() > .30
                or main_score < .75 or other_score < .75):
                continue
            point = x1 + x + w // 2, y1 + y + h // 2
            if not any(abs(point[0]-p[0]) <= 4 and abs(point[1]-p[1]) <= 4 for p in matches):
                matches.append(point)
    return matches[0] if len(matches) == 1 else None


class NotificationCloser:
    def __init__(self, backend, guard, log, *, clock=time.monotonic, sleep=time.sleep):
        self.backend, self.guard, self.log = backend, guard, log
        self.clock, self.sleep = clock, sleep
        self.next_attempt = 0
        self.attempts = 0

    def __call__(self, frame, tokens):
        if self.attempts >= 2 or self.clock() < self.next_attempt:
            return False
        marker = notification_marker(tokens)
        if marker is None or marker[1].roi is None:
            return False
        kind, token = marker
        self.guard.ensure_not_aborted()
        if not self.guard.target_is_foreground():
            return False
        height, width = frame.shape[:2]
        physical_width, physical_height = self.backend.size()
        sx, sy = physical_width / width, physical_height / height
        tx, ty, tr, tb = token.roi
        window = self.backend.window_at((int((tx + tr) / 2 * sx), int((ty + tb) / 2 * sy)))
        if window is None:
            return False
        exe = window.executable.lower()
        known_owner = (kind == "hand-input" and any(v in exe for v in ("palm", "handinput", "shouxin"))) or (
            kind == "uu-remote" and exe.startswith(("uu", "netease")))
        rect = tuple(round(v / (sx if i % 2 == 0 else sy)) for i, v in enumerate(window.rect))
        l, t, r, b = rect
        # Only the small bottom-right notification window seen in evidence.
        geometry_ok = (l >= width * .65 and t >= height * .55 and r <= width + 2
                       and b <= height + 2 and 20 < r-l < width*.35 and 15 < b-t < height*.4)
        self.next_attempt = self.clock() + 30
        point = find_close_cross(frame, rect, token.roi) if known_owner and geometry_ok else None
        self.log("NOTIFICATION_CANDIDATE", kind=kind, executable=exe,
                 rect=list(window.rect), recognisedOwner=known_owner, closeFound=point is not None)
        if point is None:
            return False
        physical = int(point[0] * sx), int(point[1] * sy)
        # The window may have moved/disappeared or another app gained focus
        # during OCR. Revalidate both HWND and rect immediately before input.
        self.guard.ensure_not_aborted()
        if not self.guard.target_is_foreground() or self.backend.window_at(physical) != window:
            return False
        foreground = self.backend.foreground()
        self.attempts += 1
        self.backend.click(physical)
        disappeared = False
        for _ in range(4):
            self.sleep(.15)
            if self.backend.window_at(physical) != window:
                disappeared = True
                break
        if disappeared and self.backend.foreground() in (foreground, window.handle):
            self.backend.restore(foreground)
        resumed = self.guard.target_is_foreground()
        self.log("NOTIFICATION_CLOSED", kind=kind, disappeared=disappeared,
                 apexForeground=resumed, attempt=self.attempts)
        return disappeared and resumed


class Win32NotificationBackend:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("通知窗口操作只支持 Windows")
        import ctypes
        from ctypes import wintypes as w
        self.ctypes, self.w = ctypes, w
        self.u = ctypes.WinDLL("user32", use_last_error=True)
        self.k = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, args, result in (
            ("WindowFromPoint", [w.POINT], w.HWND), ("GetAncestor", [w.HWND, w.UINT], w.HWND),
            ("GetWindowRect", [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            ("GetWindowThreadProcessId", [w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            ("GetForegroundWindow", [], w.HWND), ("SetForegroundWindow", [w.HWND], w.BOOL),
            ("SetCursorPos", [w.INT, w.INT], w.BOOL), ("GetSystemMetrics", [w.INT], w.INT),
        ):
            fn = getattr(self.u, name)
            fn.argtypes, fn.restype = args, result
        self.k.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        self.k.OpenProcess.restype = w.HANDLE
        self.k.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
        self.k.QueryFullProcessImageNameW.restype = w.BOOL
        self.k.CloseHandle.argtypes, self.k.CloseHandle.restype = [w.HANDLE], w.BOOL

    def size(self):
        return self.u.GetSystemMetrics(0), self.u.GetSystemMetrics(1)

    def supports_capture(self, capture_size):
        # Without an output-origin mapping a non-primary/multiple-monitor
        # capture cannot safely be converted into desktop mouse coordinates.
        return self.u.GetSystemMetrics(80) == 1 and self.size() == tuple(capture_size)

    def foreground(self):
        return self.u.GetForegroundWindow()

    def window_at(self, point):
        c, w = self.ctypes, self.w
        handle = self.u.GetAncestor(self.u.WindowFromPoint(w.POINT(*point)), 2)
        rect, pid = w.RECT(), w.DWORD()
        if not handle or not self.u.GetWindowRect(handle, c.byref(rect)):
            return None
        self.u.GetWindowThreadProcessId(handle, c.byref(pid))
        process = self.k.OpenProcess(0x1000, False, pid.value)
        if not process:
            return None
        try:
            buffer, length = c.create_unicode_buffer(32768), w.DWORD(32768)
            if not self.k.QueryFullProcessImageNameW(process, 0, buffer, c.byref(length)):
                return None
            exe = buffer.value.replace("\\", "/").rsplit("/", 1)[-1]
            return NotificationWindow(handle, (rect.left, rect.top, rect.right, rect.bottom), exe)
        finally:
            self.k.CloseHandle(process)

    def click(self, point):
        from .input_win32 import INPUT, MOUSEINPUT
        c, w = self.ctypes, self.w
        self.u.SendInput.argtypes = [w.UINT, c.POINTER(INPUT), c.c_int]
        self.u.SendInput.restype = w.UINT
        if not self.u.SetCursorPos(*point):
            raise OSError("无法定位通知关闭按钮")
        events = (INPUT * 2)()
        for event, flags in zip(events, (0x0002, 0x0004)):
            event.type = 0
            event.mi = MOUSEINPUT(0, 0, 0, flags, 0, 0)
        if self.u.SendInput(2, events, c.sizeof(INPUT)) != 2:
            # A partial SendInput must never leave the left button held.
            self.u.SendInput(1, c.byref(events[1]), c.sizeof(INPUT))
            raise OSError("通知关闭输入失败")

    def restore(self, handle):
        self.u.SetForegroundWindow(handle)

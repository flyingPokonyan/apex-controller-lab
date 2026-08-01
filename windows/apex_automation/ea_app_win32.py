from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import time
from typing import Callable
import uuid

import numpy as np

from .account_provider import OtpCode, SecretCredentials
from .ea_app import (
    ApexExitEvidence,
    EaAppAutomationError,
    EaCaptchaRequired,
    EaIdentityFact,
    EaIdentityMismatch,
    EaUiState,
    OtpChallenge,
)
from .ocr_obstacles import RapidOcrProvider, Region, normalize_ocr_text


EA_EXECUTABLE = "eadesktop.exe"
APEX_EXECUTABLES = ("r5apex.exe", "r5apex_dx12.exe")
SW_RESTORE = 9
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
VK_TAB = 0x09
VK_RETURN = 0x0D
VK_BACK = 0x08
VK_CONTROL = 0x11
VK_A = 0x41


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

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("union",)
        _fields_ = [("type", wintypes.DWORD), ("union", INPUT_UNION)]


class WindowsEaHybridDriver:
    """Win32/OCR fallback for the EA CEF surface that exposes no inner UIA tree."""

    def __init__(
        self,
        *,
        capture_source: object,
        ocr: RapidOcrProvider | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("EA 混合驱动只能在 Windows 上运行")
        self.capture_source = capture_source
        self.ocr = ocr or RapidOcrProvider()
        self.sleep = sleep
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
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
        self.user32.SendInput.argtypes = [
            wintypes.UINT,
            ctypes.POINTER(INPUT),
            ctypes.c_int,
        ]
        self.user32.SendInput.restype = wintypes.UINT
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            self.user32.SetProcessDPIAware()

    def _process_name(self, process_id: int) -> str:
        process = self.kernel32.OpenProcess(0x1000, False, process_id)
        if not process:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if not self.kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                return ""
            return Path(buffer.value).name.lower()
        finally:
            self.kernel32.CloseHandle(process)

    def _ea_window(self) -> int:
        deadline = time.monotonic() + 10.0
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        while True:
            matches: list[tuple[int, int]] = []

            @callback_type
            def collect(hwnd, _lparam):
                if not self.user32.IsWindowVisible(hwnd):
                    return True
                process_id = wintypes.DWORD()
                self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                if self._process_name(process_id.value) == EA_EXECUTABLE:
                    rect = wintypes.RECT()
                    if self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        width = rect.right - rect.left
                        height = rect.bottom - rect.top
                        if width >= 480 and height >= 640:
                            matches.append((width * height, int(hwnd)))
                return True

            self.user32.EnumWindows(collect, 0)
            if matches:
                return max(matches)[1]
            if time.monotonic() >= deadline:
                raise EaAppAutomationError("没有发现可见的 EA App 主窗口")
            self.sleep(0.5)

    def _rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise EaAppAutomationError("无法读取 EA App 窗口边界")
        return rect.left, rect.top, rect.right, rect.bottom

    def _focus(self, hwnd: int) -> None:
        self.user32.ShowWindow(hwnd, SW_RESTORE)
        self.user32.SetForegroundWindow(hwnd)
        self.sleep(0.4)
        if int(self.user32.GetForegroundWindow()) != hwnd:
            raise EaAppAutomationError("EA App 无法取得前台焦点")

    def _send(self, inputs: list["INPUT"]) -> None:
        array_type = INPUT * len(inputs)
        payload = array_type(*inputs)
        pointer = ctypes.cast(payload, ctypes.POINTER(INPUT))
        if self.user32.SendInput(len(payload), pointer, ctypes.sizeof(INPUT)) != len(payload):
            raise EaAppAutomationError("EA App 输入事件发送不完整")

    def _click(self, hwnd: int, x_ratio: float, y_ratio: float) -> None:
        left, top, right, bottom = self._rect(hwnd)
        x = round(left + (right - left) * x_ratio)
        y = round(top + (bottom - top) * y_ratio)
        self._focus(hwnd)
        if not self.user32.SetCursorPos(x, y):
            raise EaAppAutomationError("EA App 鼠标定位失败")
        self.sleep(0.15)
        self._send(
            [
                INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)),
                INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)),
            ]
        )
        self.sleep(0.5)

    def _click_ocr_text(
        self,
        hwnd: int,
        targets: set[str],
        *,
        x_range: tuple[float, float],
        y_range: tuple[float, float],
    ) -> bool:
        left, top, right, bottom = self._rect(hwnd)
        width = right - left
        height = bottom - top
        matches: list[tuple[float, float, float]] = []
        for token in self.ocr.read_with_boxes(self._frame()):
            if token.roi is None or token.normalized not in targets:
                continue
            x1, y1, x2, y2 = token.roi
            x_ratio = ((x1 + x2) / 2 - left) / width
            y_ratio = ((y1 + y2) / 2 - top) / height
            if (
                x_range[0] <= x_ratio <= x_range[1]
                and y_range[0] <= y_ratio <= y_range[1]
            ):
                matches.append((token.confidence, x_ratio, y_ratio))
        if not matches:
            return False
        _, x_ratio, y_ratio = max(matches)
        self._click(hwnd, x_ratio, y_ratio)
        return True

    def _type_secret(self, value: str) -> None:
        inputs: list[INPUT] = []
        for character in value:
            code = ord(character)
            inputs.append(
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE, 0, 0))
            )
            inputs.append(
                INPUT(
                    type=INPUT_KEYBOARD,
                    ki=KEYBDINPUT(0, code, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, 0),
                )
            )
        self._send(inputs)

    def _tap(self, virtual_key: int) -> None:
        self._send(
            [
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(virtual_key, 0, 0, 0, 0)),
                INPUT(
                    type=INPUT_KEYBOARD,
                    ki=KEYBDINPUT(virtual_key, 0, KEYEVENTF_KEYUP, 0, 0),
                ),
            ]
        )

    def _clear_focused_field(self) -> None:
        self._send(
            [
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_CONTROL, 0, 0, 0, 0)),
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_A, 0, 0, 0, 0)),
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_A, 0, KEYEVENTF_KEYUP, 0, 0)),
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0, 0)),
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_BACK, 0, 0, 0, 0)),
                INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(VK_BACK, 0, KEYEVENTF_KEYUP, 0, 0)),
            ]
        )

    def _frame(self) -> np.ndarray:
        grab = getattr(self.capture_source, "grab", None)
        if not callable(grab):
            raise EaAppAutomationError("EA App 驱动缺少截图源")
        frame = grab()
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            raise EaAppAutomationError("EA App 截图为空")
        return frame

    def _tokens(self, hwnd: int, *, identity_only: bool = False):
        frame = self._frame()
        left, top, right, bottom = self._rect(hwnd)
        height, width = frame.shape[:2]
        left, right = max(0, left), min(width, right)
        top, bottom = max(0, top), min(height, bottom)
        if identity_only:
            window_width = right - left
            window_height = bottom - top
            region = Region(
                "eaIdentity",
                (
                    left + round(window_width * 0.78),
                    top + round(window_height * 0.02),
                    right - round(window_width * 0.02),
                    top + round(window_height * 0.15),
                ),
            )
        else:
            region = Region("eaWindow", (left, top, right, bottom))
        return self.ocr.read(frame, region)

    def _identity(self, hwnd: int) -> EaIdentityFact | None:
        candidates: list[tuple[float, str]] = []
        for token in self._tokens(hwnd, identity_only=True):
            normalized = normalize_ocr_text(token.text)
            for candidate in re.findall(r"[a-z0-9]{8,20}", normalized):
                if any(character.isalpha() for character in candidate):
                    candidates.append((token.confidence, candidate))
        if not candidates:
            return None
        confidence, account_id = max(candidates)
        return EaIdentityFact(
            ea_account_id=account_id,
            source=f"ea-window-ocr:{confidence:.3f}",
            verified=confidence >= 0.75,
        )

    def _state(self, hwnd: int) -> EaUiState:
        if self._identity(hwnd) is not None:
            return EaUiState.SIGNED_IN
        text = " ".join(token.normalized for token in self._tokens(hwnd))
        if any(term in text for term in ("captcha", "hcaptcha", "验证码图片")):
            raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
        if any(term in text for term in ("verificationcode", "securitycode", "安全代码")):
            return EaUiState.OTP
        if any(term in text for term in ("signin", "eaaccount", "emailoreaid", "登录")):
            return EaUiState.LOGIN
        return EaUiState.UNKNOWN

    def _dismiss_expired_session(self, hwnd: int) -> bool:
        compact = "".join(token.normalized for token in self._tokens(hwnd))
        if not any(
            term in compact
            for term in ("sessionhasexpired", "backtosignin", "会话已过期")
        ):
            return False
        self._click(hwnd, 0.50, 0.35)
        self.sleep(2.0)
        return True

    def _wait_for_text(
        self,
        hwnd: int,
        terms: tuple[str, ...],
        *,
        timeout_s: float = 15.0,
    ) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            compact = "".join(token.normalized for token in self._tokens(hwnd))
            if any(term in compact for term in terms):
                return True
            self.sleep(1.0)
        return False

    def preflight(self) -> EaUiState:
        hwnd = self._ea_window()
        self._dismiss_expired_session(hwnd)
        for _ in range(8):
            state = self._state(hwnd)
            if state is not EaUiState.UNKNOWN:
                return state
            self.sleep(1.0)
        raise EaAppAutomationError("EA App 页面无法识别，领号前预检失败")

    def ensure_started(self) -> EaUiState:
        return self.preflight()

    def current_identity(self) -> EaIdentityFact | None:
        return self._identity(self._ea_window())

    def sign_in(
        self,
        credentials: SecretCredentials,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
    ) -> EaIdentityFact:
        hwnd = self._ea_window()
        self._dismiss_expired_session(hwnd)
        if self._state(hwnd) is not EaUiState.LOGIN:
            raise EaAppAutomationError("EA App 当前不是登录页")
        compact = "".join(token.normalized for token in self._tokens(hwnd))
        if not any(term in compact for term in ("password", "密码")):
            self._click(hwnd, 0.50, 0.50)
            self._clear_focused_field()
            self._type_secret(credentials.login_identifier)
            self._click(hwnd, 0.50, 0.69)
            if not self._wait_for_text(hwnd, ("password", "密码")):
                raise EaAppAutomationError("EA App 提交账号后未出现密码页")
        self._click(hwnd, 0.50, 0.50)
        self._clear_focused_field()
        self._type_secret(credentials.password)
        self._click(hwnd, 0.50, 0.58)

        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            self.sleep(2.0)
            identity = self._identity(hwnd)
            if identity is not None:
                return identity
            state = self._state(hwnd)
            if state is EaUiState.OTP:
                challenge = OtpChallenge(uuid.uuid4().hex, datetime.now(timezone.utc))
                otp = otp_supplier(challenge)
                self._click(hwnd, 0.50, 0.50)
                self._clear_focused_field()
                self._type_secret(otp.code)
                self._click(hwnd, 0.50, 0.69)
        raise EaAppAutomationError("EA App 登录后未在时限内出现可验证身份")

    def verify_identity(self, expected_ea_account_id: str) -> EaIdentityFact:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            identity = self._identity(self._ea_window())
            if identity is None:
                self.sleep(1.0)
                continue
            if identity.ea_account_id != expected_ea_account_id:
                raise EaIdentityMismatch("EA App 当前稳定 EA ID 与租约不一致")
            if not identity.verified:
                self.sleep(1.0)
                continue
            return identity
        raise EaIdentityMismatch("EA App 页面没有可验证的稳定 EA ID")

    @staticmethod
    def _process_running(executable: str) -> bool:
        result = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {executable}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return executable.lower() in result.stdout.lower()

    def start_apex(self) -> None:
        if any(self._process_running(name) for name in APEX_EXECUTABLES):
            return
        hwnd = self._ea_window()
        # The orchestrator verifies the stable EA identity immediately before
        # entering this method.  Do not repeat that check here: the CEF surface
        # occasionally yields an empty OCR frame while it is otherwise fully
        # interactive.  Wait for the actual launch control instead.
        for _ in range(15):
            if self._click_ocr_text(
                hwnd,
                {"apexlegends"},
                x_range=(0.0, 0.30),
                y_range=(0.15, 0.90),
            ):
                break
            self.sleep(1.0)
        else:
            raise EaAppAutomationError("EA App 未找到左侧 Apex Legends 游戏入口")
        self.sleep(2.0)
        for _ in range(8):
            if self._click_ocr_text(
                hwnd,
                {"play", "launch", "开始游戏"},
                x_range=(0.35, 0.75),
                y_range=(0.30, 0.70),
            ):
                break
            self.sleep(1.0)
        else:
            raise EaAppAutomationError("EA App Apex 页面未找到 Play 按钮")
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if any(self._process_running(name) for name in APEX_EXECUTABLES):
                return
            self.sleep(2.0)
        raise EaAppAutomationError("点击 EA Play 后未发现 Apex 进程")

    def stop_apex(self) -> ApexExitEvidence:
        requested = False
        for executable in APEX_EXECUTABLES:
            if self._process_running(executable):
                requested = True
                subprocess.run(
                    ["taskkill", "/IM", executable, "/T"],
                    capture_output=True,
                    check=False,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            if not any(self._process_running(name) for name in APEX_EXECUTABLES):
                return ApexExitEvidence(requested, True)
            self.sleep(1.0)
        return ApexExitEvidence(requested, False)

    def sign_out(self) -> bool:
        hwnd = self._ea_window()
        identity = None
        for _ in range(8):
            identity = self._identity(hwnd)
            if identity is not None:
                break
            if self._state(hwnd) is EaUiState.LOGIN:
                return True
            self.sleep(1.0)
        if identity is None:
            return False
        self._click(hwnd, 0.89, 0.075)
        self.sleep(1.0)
        frame = self._frame()
        targets = {"signout", "logout", "退出登录", "登出"}
        matches = [
            token
            for token in self.ocr.read_with_boxes(frame)
            if token.roi is not None and token.normalized in targets and token.confidence >= 0.70
        ]
        if len(matches) != 1:
            return False
        x1, y1, x2, y2 = matches[0].roi or (0, 0, 0, 0)
        self._focus(hwnd)
        self.user32.SetCursorPos((x1 + x2) // 2, (y1 + y2) // 2)
        self.sleep(0.15)
        self._send(
            [
                INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTDOWN, 0, 0)),
                INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(0, 0, 0, MOUSEEVENTF_LEFTUP, 0, 0)),
            ]
        )
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            self.sleep(1.0)
            if self._state(hwnd) is EaUiState.LOGIN:
                return True
        return False

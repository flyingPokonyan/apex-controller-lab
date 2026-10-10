from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time
import unicodedata
from typing import Callable, Sequence
import uuid

import numpy as np

from .account_provider import (
    MIN_USABLE_OTP_LIFETIME_S,
    OtpCode,
    OtpMethod,
    SecretCredentials,
)
from .diagnostics import PerformanceMetrics
from .ea_app import (
    ApexExitEvidence,
    EaAccountBanned,
    EaAppAutomationError,
    EaApexDownloadRequired,
    EaApexStartFailed,
    EaCaptchaRequired,
    EaCaptureUnavailable,
    EaCredentialsRejected,
    EaIdentityFact,
    EaIdentityMismatch,
    EaIdentityUnconfirmed,
    EaLoginRejected,
    EaOtpUnavailable,
    EaUiState,
    EaUiRecoveryExhausted,
    OtpChallenge,
)
from .ea_evidence import EaLoginEvidence
from .ea_password_recovery import EaPasswordRecoveryMixin
from .ea_onboarding import library_tour_close_point, library_tour_visible
from .ea_pages import (
    ACCOUNT_BANNED_CLOSE_TERMS,
    ACCOUNT_FIELD_TERMS,
    AUTHENTICATOR_TERMS,
    EMAIL_CODE_TERMS,
    EMAIL_METHOD_TERMS,
    OTP_FIELD_TERMS,
    PASSWORD_FIELD_TERMS,
    PASSWORD_LINK_TERMS,
    SEND_CODE_TERMS,
    SIGN_OUT_CONFIRM_TERMS,
    SIGN_OUT_TERMS,
    BACK_TO_SIGN_IN_PHRASES,
    SUBMIT_TERMS,
    EaPage,
    phrase_point,
    classify_page,
    has_any,
    identity_candidates,
    identity_matches,
    is_login_error,
    is_ui_chrome,
    mask_identity,
    page_markers,
    password_page_blocker,
)
from .ocr_obstacles import (
    OcrPositionUnavailable,
    OcrToken,
    RapidOcrProvider,
    Region,
    normalize_ocr_text,
)


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
VK_ESCAPE = 0x1B
VK_BACK = 0x08
VK_CONTROL = 0x11
VK_A = 0x41

# Ratios stay as the last resort behind the OCR anchors. They are the only
# targeting this driver ever had, and one earlier run did reach the signed-in
# surface with them, so they are kept rather than replaced outright.
ACCOUNT_FIELD_RATIO = (0.50, 0.50)
ACCOUNT_SUBMIT_RATIO = (0.50, 0.69)
PASSWORD_FIELD_RATIO = (0.50, 0.50)
PASSWORD_SUBMIT_RATIO = (0.50, 0.58)
# Measured off the real 520x867 login window: the code field sits just
# under half height, NEXT below it, and on the chooser the authenticator
# option is the second row with SEND CODE underneath.
OTP_FIELD_RATIO = (0.50, 0.49)
OTP_SUBMIT_RATIO = (0.50, 0.64)
SEND_CODE_RATIO = (0.50, 0.68)
# Measured on the session-expired window: the blue button sits a third of
# the way down the tall login frame. OCR is preferred; this is the fallback
# when the button text is not boxed.
EXPIRED_SESSION_BUTTON_RATIO = (0.50, 0.35)
LOGIN_BACK_TERMS = ("back", "返回", "上一步")
CREDENTIAL_REJECTION_TERMS = (
    "yourcredentialsareincorrectorhaveexpired", "invalidcredentials",
    "您的凭据不正确或已过期", "密码不正确", "密码错误", "账号或密码错误",
)
# EA currently renders the badge in either of two vertical positions. Keep
# the old tight band first so an expanded friends list cannot win over the
# account name, then try the lower band used by the newer home layout.
IDENTITY_BANDS = (
    (0.75, 0.00, 1.00, 0.12),
    (0.75, 0.10, 1.00, 0.20),
)
INPUT_SETTLE_S = 0.8
MAX_OTP_ATTEMPTS = 3
EA_RESTART_TIMEOUT_S = 60.0
APEX_INSTALL_REPAIR_TIMEOUT_S = 180.0
APEX_LAUNCH_TIMEOUT_S = 90.0
APEX_UPDATE_TIMEOUT_S = 2 * 60 * 60.0
APEX_INSTALL_DIR = Path(r"D:\Apex")
HELP_TERMS = ("help", "帮助")
RESTART_APP_TERMS = (
    "restartapp",
    "restartapplication",
    "重启应用",
    "重新启动应用",
)
DOWNLOAD_OPTIONS_TERMS = ("downloadoptions", "下载选项")
EA_UPDATE_RESTART_TERMS = (
    "theeaapprequiresanupdate", "restartrequired", "eaapp需要更新",
    "ea应用需要更新", "需要重启", "需要重新启动",
)
INSTALL_LOCATION_TERMS = ("installlocation", "安装位置")
TERMS_OF_PLAY_TERMS = ("termsofplay", "游戏条款")
INSTALL_COMPLETE_TERMS = ("installationcomplete", "安装完成")
DOWNLOAD_MANAGER_TERMS = ("downloadmanager", "下载管理器")
COMPLETED_TERMS = ("completed", "已完成")
APEX_PLAY_TERMS = ("play", "launch", "launchgame", "startgame", "开始游戏")
APEX_UPDATE_ACTION_TERMS = ("update", "updategame", "更新", "更新游戏")
APEX_UPDATE_REQUIRED_TERMS = (
    "anupdateisrequiredtolaunchthisgame",
    "updaterequired",
    "需要更新才能启动此游戏",
    "启动此游戏需要更新",
)
CLOUD_DATA_ERROR_TERMS = (
    "wecouldntloadyourclouddata",
    "unabletoloadyourclouddata",
    "savingyourprogresstothecloud",
    "无法加载您的云数据",
    "无法加载云数据",
    "ec10600",
    "ec10609",
)
CONTINUE_LOCAL_DATA_TERMS = (
    "continuewithlocaldata",
    "skipsyncclose",
    "使用本地数据继续",
    "继续使用本地数据",
)
CLOUD_UPLOAD_ERROR_TERMS = (
    "failedtouploadgamedatatothecloud",
    "无法将游戏数据上传到云端",
    "无法将游戏数据上传至云端",
)
CLOUD_UPLOAD_LOCAL_SAVE_TERMS = (
    "gameisstillsavedlocally",
    "游戏仍保存在本地",
)

# Pages that prove no session exists yet.
PRE_LOGIN_PAGES = (
    EaPage.EMAIL,
    EaPage.PASSWORD,
    EaPage.OTP_METHOD,
    EaPage.OTP,
    EaPage.EXPIRED_SESSION,
    EaPage.RECOVERY_ACCOUNT,
    EaPage.RESET_PASSWORD,
    EaPage.RESET_SUCCESS,
)
GW_OWNER = 4


if sys.platform == "win32":
    # These must be the *same* classes the play-session sender uses.
    # ctypes.windll.user32 is one process-wide object, so whichever module
    # constructs last owns SendInput.argtypes — and a second, structurally
    # identical INPUT class makes the other module's calls fail with
    # "expected LP_INPUT instance instead of LP_INPUT". Import, never redefine.
    from .input_win32 import INPUT, KEYBDINPUT, MOUSEINPUT


@dataclass(eq=False)
class EaObservation:
    """One frame, its window-clipped OCR tokens and the page they describe.

    Every gate in a login step reads the same observation. The transition bugs
    this driver kept hitting came from asking two questions about two different
    frames captured a second apart.
    """

    rect: tuple[int, int, int, int]
    frame: np.ndarray
    tokens: tuple[OcrToken, ...]
    page: EaPage

    @property
    def normalized(self) -> tuple[str, ...]:
        return tuple(token.normalized for token in self.tokens)

    @property
    def markers(self) -> tuple[str, ...]:
        return page_markers(self.normalized)

    def has_login_error(self) -> bool:
        return is_login_error("".join(self.normalized))


class _LoginRestart(Exception):
    """BACK TO SIGN-IN was used; the caller must type the login again."""


class WindowsEaHybridDriver(EaPasswordRecoveryMixin):
    """Win32/OCR fallback for the EA CEF surface that exposes no inner UIA tree."""

    def __init__(
        self,
        *,
        capture_source: object,
        ocr: RapidOcrProvider | None = None,
        sleep: Callable[[float], None] = time.sleep,
        evidence: EaLoginEvidence | None = None,
        notify: Callable[[str], None] = lambda message: None,
    ) -> None:
        if sys.platform != "win32":
            raise RuntimeError("EA 混合驱动只能在 Windows 上运行")
        self.capture_source = capture_source
        self.ocr = ocr or RapidOcrProvider()
        self.sleep = sleep
        self.evidence = evidence
        self.metrics = PerformanceMetrics(evidence.timing) if evidence is not None else None
        self.notify = notify
        self.user32 = ctypes.windll.user32
        self.kernel32 = ctypes.windll.kernel32
        self.user32.GetForegroundWindow.restype = wintypes.HWND
        self.user32.GetParent.argtypes = [wintypes.HWND]
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user32.GetWindow.restype = wintypes.HWND
        self.user32.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user32.BringWindowToTop.restype = wintypes.BOOL
        self.user32.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user32.AttachThreadInput.restype = wintypes.BOOL
        self.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
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
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self.user32.IsIconic.argtypes = [wintypes.HWND]
        self.user32.IsIconic.restype = wintypes.BOOL
        # Default ctypes integer arguments are c_int. A 64-bit HWND does not
        # fit, and GetWindowRect then raises OverflowError inside the
        # EnumWindows callback. That aborts the scan, so the visible EA
        # window — including the expired-session button — is never clicked.
        self.user32.GetWindowRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user32.SetForegroundWindow.restype = wintypes.BOOL
        self.user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user32.SetCursorPos.restype = wintypes.BOOL
        self._hwnd: int | None = None
        self.apex_install_dir = APEX_INSTALL_DIR
        try:
            self.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            self.user32.SetProcessDPIAware()

    def _process_name(self, process_id: int) -> str:
        return Path(self._process_path(process_id)).name.lower()

    def _process_path(self, process_id: int) -> str:
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
            return buffer.value
        finally:
            self.kernel32.CloseHandle(process)

    def _ea_window(self) -> int:
        deadline = time.monotonic() + 10.0
        restored: set[int] = set()
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        while True:
            matches: list[tuple[int, int]] = []
            recoverable: list[tuple[int, int]] = []

            @callback_type
            def collect(hwnd, _lparam):
                # An exception here is swallowed by ctypes and returned as
                # FALSE, which stops EnumWindows before the EA window is seen.
                try:
                    process_id = wintypes.DWORD()
                    self.user32.GetWindowThreadProcessId(
                        hwnd, ctypes.byref(process_id)
                    )
                    if self._process_name(process_id.value) == EA_EXECUTABLE:
                        rect = wintypes.RECT()
                        if self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                            width = rect.right - rect.left
                            height = rect.bottom - rect.top
                            visible = bool(self.user32.IsWindowVisible(hwnd))
                            iconic = bool(self.user32.IsIconic(hwnd))
                            main_sized = width >= 480 and height >= 640
                            if visible and not iconic and main_sized:
                                matches.append((width * height, int(hwnd)))
                            elif int(hwnd) not in restored and (iconic or main_sized):
                                # Minimized windows have icon-sized rectangles;
                                # hidden tray windows retain their full size.
                                # Restore before applying the main-window gate.
                                recoverable.append((width * height, int(hwnd)))
                except Exception:
                    return True
                return True

            self.user32.EnumWindows(collect, 0)
            if matches:
                self._hwnd = max(matches)[1]
                return self._hwnd
            if recoverable:
                hwnd = max(recoverable)[1]
                restored.add(hwnd)
                self.user32.ShowWindow(hwnd, SW_RESTORE)
                self.sleep(0.5)
                continue
            if time.monotonic() >= deadline:
                raise EaAppAutomationError("恢复窗口后仍没有发现可见的 EA App 主窗口")
            self.sleep(0.5)

    def _alive(self, hwnd: int | None) -> bool:
        return bool(
            hwnd
            and self.user32.IsWindow(hwnd)
            and self.user32.IsWindowVisible(hwnd)
        )

    def _live(self, hwnd: int) -> int:
        """A handle that is still a window.

        EA destroys its login window the instant the sign-in succeeds and
        raises the main window in its place, so the handle every step of the
        login flow is carrying dies exactly once, at the least convenient
        moment. Re-discovering beats propagating a dead handle.
        """

        if self._alive(hwnd):
            return hwnd
        if hwnd != self._hwnd and self._alive(self._hwnd):
            assert self._hwnd is not None
            return self._hwnd
        self._hwnd = None
        return self._ea_window()

    def _rect(self, hwnd: int) -> tuple[int, int, int, int]:
        rect = wintypes.RECT()
        if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            raise EaAppAutomationError("无法读取 EA App 窗口边界")
        return rect.left, rect.top, rect.right, rect.bottom

    def _window_belongs_to_ea(self, ea_hwnd: int, candidate: int) -> bool:
        """True when the foreground window is the EA frame or one of its dialogs."""

        if not candidate:
            return False
        ea_hwnd = int(ea_hwnd)
        current = int(candidate)
        seen: set[int] = set()
        for _ in range(8):
            if not current or current in seen:
                break
            if current == ea_hwnd:
                return True
            seen.add(current)
            parent = int(self.user32.GetParent(current) or 0)
            owner = int(self.user32.GetWindow(current, GW_OWNER) or 0)
            current = parent or owner
        pid_ea = wintypes.DWORD()
        pid_other = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(ea_hwnd, ctypes.byref(pid_ea))
        self.user32.GetWindowThreadProcessId(int(candidate), ctypes.byref(pid_other))
        return bool(pid_ea.value and pid_ea.value == pid_other.value)

    def _focus(self, hwnd: int) -> None:
        hwnd = self._live(hwnd)
        self.user32.ShowWindow(hwnd, SW_RESTORE)
        foreground = int(self.user32.GetForegroundWindow() or 0)
        if self._window_belongs_to_ea(hwnd, foreground):
            return
        current_thread = int(self.kernel32.GetCurrentThreadId())
        fg_pid = wintypes.DWORD()
        fg_thread = int(
            self.user32.GetWindowThreadProcessId(foreground, ctypes.byref(fg_pid))
            or 0
        )
        attached = False
        if foreground and fg_thread and fg_thread != current_thread:
            attached = bool(self.user32.AttachThreadInput(current_thread, fg_thread, True))
        try:
            self.user32.BringWindowToTop(hwnd)
            self.user32.SetForegroundWindow(hwnd)
            self.sleep(0.4)
            if self._window_belongs_to_ea(
                hwnd, int(self.user32.GetForegroundWindow() or 0)
            ):
                return
        finally:
            if attached:
                self.user32.AttachThreadInput(current_thread, fg_thread, False)
        if self._window_belongs_to_ea(hwnd, int(self.user32.GetForegroundWindow() or 0)):
            return
        raise EaAppAutomationError("EA App 无法取得前台焦点")

    def _send(self, inputs: list["INPUT"]) -> None:
        array_type = INPUT * len(inputs)
        payload = array_type(*inputs)
        pointer = ctypes.cast(payload, ctypes.POINTER(INPUT))
        if self.user32.SendInput(len(payload), pointer, ctypes.sizeof(INPUT)) != len(payload):
            raise EaAppAutomationError("EA App 输入事件发送不完整")

    def _click_point(self, hwnd: int, x: int, y: int) -> None:
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

    def _click(self, hwnd: int, x_ratio: float, y_ratio: float) -> None:
        hwnd = self._live(hwnd)
        left, top, right, bottom = self._rect(hwnd)
        self._click_point(
            hwnd,
            round(left + (right - left) * x_ratio),
            round(top + (bottom - top) * y_ratio),
        )

    @staticmethod
    def _anchor(
        observation: EaObservation,
        terms: Sequence[str],
        *,
        x_range: tuple[float, float] = (0.0, 1.0),
        y_range: tuple[float, float] = (0.0, 1.0),
        exclude: Sequence[str] = (),
        exact: bool = False,
    ) -> tuple[int, int] | None:
        """Centre of the best on-screen token that carries one of `terms`.

        Earlier terms win over later ones before confidence is considered, so
        a page holding both "Next" and "Sign in" gets the control the caller
        asked for first.
        """

        left, top, right, bottom = observation.rect
        width = max(1, right - left)
        height = max(1, bottom - top)
        matches: list[tuple[int, float, int, int]] = []
        for token in observation.tokens:
            if token.roi is None:
                continue
            text = token.normalized
            if any(term in text for term in exclude):
                continue
            rank = next(
                (
                    index
                    for index, term in enumerate(terms)
                    if text == term or (not exact and term in text)
                ),
                None,
            )
            if rank is None:
                continue
            x1, y1, x2, y2 = token.roi
            x = (x1 + x2) // 2
            y = (y1 + y2) // 2
            x_ratio = (x - left) / width
            y_ratio = (y - top) / height
            if (
                x_range[0] <= x_ratio <= x_range[1]
                and y_range[0] <= y_ratio <= y_range[1]
            ):
                matches.append((rank, -token.confidence, x, y))
        if not matches:
            return None
        _, _, x, y = min(matches)
        return x, y

    @staticmethod
    def _installed_library_play_point(
        observation: EaObservation,
    ) -> tuple[int, int] | None:
        """Locate the icon-only Play control on an installed Apex library card."""

        installed_terms = ("installed", "已安装")
        if not any(
            any(term in token.normalized for term in installed_terms)
            for token in observation.tokens
        ):
            return None
        label = WindowsEaHybridDriver._anchor(
            observation,
            ("apexlegends",),
            x_range=(0.10, 0.45),
            y_range=(0.40, 0.75),
            exact=True,
        )
        if label is None:
            return None
        left, top, right, bottom = observation.rect
        width = max(1, right - left)
        height = max(1, bottom - top)
        x = min(right - 1, label[0] + round(width * 0.12))
        y = max(top, label[1] - round(height * 0.04))
        return x, y

    @staticmethod
    def _contains_any(
        observation: EaObservation,
        terms: Sequence[str],
    ) -> bool:
        joined = "".join(observation.normalized)
        return any(term in joined for term in terms)

    @classmethod
    def _continue_local_data_point(
        cls,
        observation: EaObservation,
    ) -> tuple[int, int] | None:
        """Return the safe recovery action only on the cloud-data error."""

        if not cls._contains_any(observation, CLOUD_DATA_ERROR_TERMS):
            return None
        return cls._anchor(
            observation,
            CONTINUE_LOCAL_DATA_TERMS,
            x_range=(0.35, 0.80),
            y_range=(0.45, 0.90),
        )

    @classmethod
    def _cloud_upload_error_point(
        cls, observation: EaObservation,
    ) -> tuple[int, int] | None:
        # The upload notice has only OK. It acknowledges the error; it does
        # not skip sync, close EA, or prove the session has been signed out.
        placed = replace(observation, tokens=tuple(
            token for token in observation.tokens if token.confidence >= 0.80
        ))
        title = next((
            point for term in CLOUD_UPLOAD_ERROR_TERMS
            if (point := phrase_point(placed.tokens, placed.rect, term,
                                      x_range=(0.25, 0.80), y_range=(0.25, 0.70))) is not None
        ), None)
        if title is None or not cls._contains_any(placed, CLOUD_UPLOAD_LOCAL_SAVE_TERMS):
            return None
        button = cls._anchor(placed, ("ok", "确定"), exact=True,
                             x_range=(0.40, 0.85), y_range=(0.40, 0.90))
        if button is None or button[1] <= title[1]:
            return None
        return button

    def _dismiss_cloud_upload_error(
        self, hwnd: int, observation: EaObservation,
    ) -> EaObservation:
        if not self._contains_any(observation, CLOUD_UPLOAD_ERROR_TERMS):
            return observation
        clear_samples = 0
        for attempt in range(1, 3):
            point = self._cloud_upload_error_point(observation)
            if point is None:
                self._record("cloud-upload-error-action-missing", observation)
                raise EaAppAutomationError("EA 云端上传失败提示未找到可信 OK 按钮")
            self._record("cloud-upload-error-ack", observation, attempt=attempt)
            self._click_point(hwnd, *point)
            self.notify("EA 云端数据上传失败，已确认提示，正在核对弹窗是否关闭")
            for _ in range(4):
                self.sleep(1.0)
                observation = self._observe(hwnd)
                visible = self._contains_any(observation, CLOUD_UPLOAD_ERROR_TERMS)
                clear = not visible and observation.page in (
                    EaPage.SIGNED_IN, EaPage.BANNED, *PRE_LOGIN_PAGES,
                )
                clear_samples = clear_samples + 1 if clear else 0
                if clear_samples >= 2:
                    self._record("cloud-upload-error-dismissed", observation)
                    return observation
            # Retry only if the same known dialog is still visible. An empty
            # capture or a different page is never permission to click again.
            if not self._contains_any(observation, CLOUD_UPLOAD_ERROR_TERMS):
                break
        self._record("cloud-upload-error-stuck", observation)
        raise EaAppAutomationError("EA 云端上传失败提示未能确认关闭，已停止点击")

    @classmethod
    def _apex_update_point(
        cls,
        observation: EaObservation,
    ) -> tuple[int, int] | None:
        """Find Apex's Update action without clicking unrelated update copy."""

        apex_page = cls._contains_any(observation, ("apexlegends",))
        update_required = cls._contains_any(
            observation,
            APEX_UPDATE_REQUIRED_TERMS,
        )
        if not apex_page and not update_required:
            return None
        return cls._anchor(
            observation,
            APEX_UPDATE_ACTION_TERMS,
            x_range=(0.30, 0.85),
            y_range=(0.25, 0.85),
            exact=True,
        )

    def _click_target(
        self,
        hwnd: int,
        observation: EaObservation,
        terms: Sequence[str],
        ratio: tuple[float, float],
        *,
        x_range: tuple[float, float] = (0.0, 1.0),
        y_range: tuple[float, float] = (0.0, 1.0),
        exclude: Sequence[str] = (),
    ) -> str:
        """Click an OCR anchor when there is one, the ratio otherwise.

        The return value names which one was used so the evidence log can say
        why a click landed where it did.
        """

        point = self._anchor(
            observation,
            terms,
            x_range=x_range,
            y_range=y_range,
            exclude=exclude,
        )
        if point is None:
            self._click(hwnd, *ratio)
            return "ratio"
        self._click_point(hwnd, *point)
        return "anchor"

    @classmethod
    def _login_field_anchor(
        cls, observation: EaObservation, *, password: bool = False,
    ) -> tuple[tuple[int, int], str] | None:
        """Target the editable row, never its label or the page heading."""
        labels = ("password", "密码") if password else ACCOUNT_FIELD_TERMS
        placeholders = (("enteryourpassword", "请输入密码", "输入密码") if password else
                        ("enteryouremailoreaid", "enteryouremailaddressoreaid",
                         "请输入邮箱或eaid", "输入邮箱或eaid"))
        placed = replace(observation, tokens=tuple(
            token for token in observation.tokens if token.confidence >= 0.85
        ))
        label = cls._anchor(placed, labels, exact=True,
                            x_range=(0.05, 0.90), y_range=(0.20, 0.75))
        left, top, right, bottom = observation.rect
        height = bottom - top
        if label is not None:
            token = next(t for t in placed.tokens if t.roi is not None
                         and ((t.roi[0] + t.roi[2]) // 2,
                              (t.roi[1] + t.roi[3]) // 2) == label)
            # The field is directly below its label. EA's PASSWORD heading
            # has identical placeholder text, but is above this row.
            field_top = token.roi[3]
            point = cls._anchor(placed, placeholders, exact=True,
                                x_range=(0.05, 0.90),
                                y_range=((field_top - top) / height,
                                         min(0.85, (field_top - top) / height + 0.10)))
            if point is not None:
                return point, "placeholder"
            y = round(field_top + (token.roi[3] - token.roi[1]) * 1.6)
            if top < y < bottom:
                return ((left + right) // 2, y), "label-offset"
        # An OCR pass may omit the label. The editable row in the supported
        # login layouts is below 40% height; the PASSWORD heading is above it.
        point = cls._anchor(placed, placeholders, exact=True,
                            x_range=(0.05, 0.90), y_range=(0.40, 0.80))
        return None if point is None else (point, "placeholder")

    def _click_login_field(
        self, hwnd: int, observation: EaObservation, *, password: bool = False,
    ) -> str:
        expected = (EaPage.PASSWORD, EaPage.RESET_PASSWORD) if password else (
            EaPage.EMAIL, EaPage.RECOVERY_ACCOUNT,
        )
        if observation.page not in expected:
            raise EaAppAutomationError("EA 输入前页面发生变化，已停止输入")
        anchor = self._login_field_anchor(observation, password=password)
        if anchor is None:
            self._click(hwnd, *(PASSWORD_FIELD_RATIO if password else ACCOUNT_FIELD_RATIO))
            return "ratio"
        point, target = anchor
        self._click_point(hwnd, *point)
        return target

    @staticmethod
    def _exact_identifier(value: str) -> str:
        # Preserve punctuation: user.name and username are different logins.
        return "".join(unicodedata.normalize("NFKC", value).casefold().split())

    def _verify_login_input(self, observation: EaObservation, identifier: str) -> bool:
        if self._verified_identifier_echo(observation, identifier):
            return True
        # Long email values can be split or missed by whole-window detection.
        # Read only the editable row; never accept a masked mailbox elsewhere.
        anchor = self._login_field_anchor(observation)
        if anchor is None or not hasattr(getattr(self, "ocr", None), "read"):
            return False
        (_, y), _ = anchor
        left, top, right, bottom = observation.rect
        radius = max(8, round((bottom - top) * 0.023))
        region = Region("eaLoginIdentifier", (
            round(left + (right - left) * 0.12), max(top, y - radius),
            round(left + (right - left) * 0.88), min(bottom, y + radius),
        ), single_line=True)
        try:
            tokens = self.ocr.read(observation.frame, region)
        except Exception:
            return False
        expected = self._exact_identifier(identifier)
        return bool(expected) and any(
            token.confidence >= 0.85 and self._exact_identifier(token.text) == expected
            for token in tokens
        )

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
        try:
            frame = grab()
        except EaAppAutomationError:
            raise
        except Exception as error:
            # A capture that dies here used to leave the orchestrator's
            # catch-all to end the process, which strands the lease until it
            # expires — the account is unusable for the whole window and
            # nothing says why. It is the same kind of failure as a window that
            # went away: worth another look, and worth a clean close if it
            # keeps failing.
            raise EaCaptureUnavailable(
                f"读不到画面：{type(error).__name__}：{error}"
            ) from error
        if not isinstance(frame, np.ndarray) or frame.size == 0:
            raise EaCaptureUnavailable("EA App 截图为空")
        return frame

    def _clip_rect(self, hwnd: int, frame: np.ndarray) -> tuple[int, int, int, int]:
        """The window rectangle, clipped to what the capture source can see."""

        left, top, right, bottom = self._rect(hwnd)
        height, width = frame.shape[:2]
        left, right = max(0, left), min(width, right)
        top, bottom = max(0, top), min(height, bottom)
        if right - left < 2 or bottom - top < 2:
            raise EaAppAutomationError("EA App 窗口不在当前捕获画面内")
        return left, top, right, bottom

    def _observe(self, hwnd: int, *, retries: int = 3) -> EaObservation:
        """Read the window once and answer every page question from that read."""

        observation: EaObservation | None = None
        for attempt in range(max(1, retries)):
            hwnd = self._live(hwnd)
            capture_started = time.monotonic()
            try:
                # Capture is the desktop, so a console covering EA otherwise
                # hides BACK, the account badge and the sign-out menu.
                self._focus(hwnd)
                frame = self._frame()
                left, top, right, bottom = self._clip_rect(hwnd, frame)
            except EaAppAutomationError:
                # The window can die between the liveness check and the rect
                # read. Drop the handle and let the next attempt re-find it.
                if attempt + 1 >= max(1, retries):
                    raise
                self._hwnd = None
                self.sleep(1.0)
                continue
            crop = np.ascontiguousarray(frame[top:bottom, left:right])
            metrics = getattr(self, "metrics", None)
            if metrics:
                metrics.add("eaCapture", (time.monotonic() - capture_started) * 1000)
            ocr_started = time.monotonic()
            try:
                tokens = tuple(
                    OcrToken(
                        token.text,
                        token.confidence,
                        None
                        if token.roi is None
                        else (
                            token.roi[0] + left,
                            token.roi[1] + top,
                            token.roi[2] + left,
                            token.roi[3] + top,
                        ),
                    )
                    for token in self.ocr.read_with_boxes(crop)
                )
            except OcrPositionUnavailable:
                tokens = self.ocr.read(
                    frame,
                    Region("eaWindow", (left, top, right, bottom)),
                )
            if metrics:
                metrics.add("eaOcr", (time.monotonic() - ocr_started) * 1000)
                metrics.flush(page=classify_page(token.normalized for token in tokens).value)
            observation = EaObservation(
                rect=(left, top, right, bottom),
                frame=frame,
                tokens=tokens,
                page=classify_page(token.normalized for token in tokens),
            )
            self._last_observation = observation
            # The CEF surface hands back a fully blank OCR pass while it is
            # otherwise interactive. Retrying beats treating it as a page.
            if tokens:
                return observation
            if attempt + 1 < max(1, retries):
                self.sleep(1.0)
        assert observation is not None
        return observation

    def _record(
        self,
        step: str,
        observation: EaObservation | None = None,
        **detail: object,
    ) -> None:
        """Evidence is diagnostic only: never let it break a login."""

        if self.evidence is None:
            return
        try:
            metrics = getattr(self, "metrics", None)
            if metrics:
                metrics.flush(force=True, step=step)
            if observation is None:
                self.evidence.step(step, page="NONE", **detail)
            else:
                self.evidence.step(
                    step,
                    page=observation.page.value,
                    markers=observation.markers,
                    frame=observation.frame,
                    tokens=observation.tokens,
                    rect=observation.rect,
                    **detail,
                )
        except Exception as error:  # pragma: no cover - diagnostics only
            self.notify(f"EA 登录证据写入失败：{type(error).__name__}")

    def _dismiss_library_tour(
        self, hwnd: int, observation: EaObservation,
    ) -> EaObservation:
        if not library_tour_visible(observation.tokens):
            return observation
        for attempt in range(1, 3):
            point = library_tour_close_point(
                observation.frame, observation.tokens, observation.rect,
            )
            if point is not None:
                self._record("library-tour-close", observation, attempt=attempt)
                self.notify("EA App 检测到游戏库新手引导，正在关闭")
                self._click_point(hwnd, *point)
            self.sleep(1.0)
            # A blank frame or one missed OCR pass cannot confirm dismissal.
            clear_observations = 0
            for _ in range(3):
                observation = self._observe(hwnd)
                if library_tour_visible(observation.tokens):
                    break
                if observation.page is EaPage.SIGNED_IN:
                    clear_observations += 1
                    if clear_observations >= 2:
                        self._record("library-tour-dismissed", observation)
                        return observation
                else:
                    clear_observations = 0
                self.sleep(0.5)
        self._record("library-tour-stuck", observation)
        raise EaAppAutomationError("EA App 游戏库新手引导未能确认关闭，已停止点击")

    def _account_ban_close_point(
        self, observation: EaObservation,
    ) -> tuple[int, int] | None:
        if observation.page is not EaPage.BANNED:
            return None
        return self._anchor(
            observation,
            ACCOUNT_BANNED_CLOSE_TERMS,
            x_range=(0.35, 0.90),
            y_range=(0.40, 0.95),
            exact=True,
        )

    def _dismiss_account_ban(
        self, hwnd: int, observation: EaObservation,
    ) -> EaObservation:
        point = self._account_ban_close_point(observation)
        if point is None:
            return observation
        self._record("account-banned-close", observation)
        self._click_point(hwnd, *point)
        self.sleep(1.0)
        return self._observe(hwnd)

    def _raise_if_account_banned(
        self, hwnd: int, observation: EaObservation,
    ) -> EaObservation:
        if observation.page is not EaPage.BANNED:
            return observation
        self._record("account-banned", observation)
        self.notify("EA App 报告当前账号已封禁，正在退出并换号")
        point = self._account_ban_close_point(observation)
        if point is not None:
            try:
                self._record("account-banned-close", observation)
                self._click_point(hwnd, *point)
                self.sleep(1.0)
            except EaAppAutomationError:
                self._record("account-banned-close-failed", observation)
        raise EaAccountBanned("EA App 报告账号已封禁")

    def _identity(self, hwnd: int) -> EaIdentityFact | None:
        """Read the signed-in badge from its own tight crop.

        This stays a separate OCR pass on purpose: the badge text is small,
        and a whole-window pass regularly fails to recognise it at all.
        """

        hwnd = self._live(hwnd)
        frame = self._frame()
        left, top, right, bottom = self._clip_rect(hwnd, frame)
        window_width = right - left
        window_height = bottom - top
        for band_index, (x1, y1, x2, y2) in enumerate(IDENTITY_BANDS):
            region = Region(
                f"eaIdentity{band_index}",
                (
                    left + round(window_width * x1),
                    top + round(window_height * y1),
                    min(right, left + round(window_width * x2)),
                    top + round(window_height * y2),
                ),
            )
            candidates: list[tuple[float, str]] = []
            for token in self.ocr.read(frame, region):
                for candidate in identity_candidates(
                    [normalize_ocr_text(token.text)]
                ):
                    # Window chrome shares this corner. Reading "Friends 0/2"
                    # as the account sends the orchestrator off to sign out of
                    # a session that is already the right one.
                    if is_ui_chrome(candidate):
                        continue
                    candidates.append((token.confidence, candidate))
            if not candidates:
                continue
            confidence, account_id = max(candidates)
            if self.evidence is not None:
                # The badge is operational evidence, but the stable EA ID
                # should not remain readable in every diagnostic screenshot.
                self.evidence.protect(account_id)
            return EaIdentityFact(
                ea_account_id=account_id,
                source=f"ea-window-ocr:{confidence:.3f}",
                verified=confidence >= 0.75,
            )
        return None

    def _matching_identity(
        self,
        observation: EaObservation,
        expected: str,
    ) -> EaIdentityFact | None:
        """Find the expected account id anywhere in the window.

        The badge is only one place the signed-in id shows up, and requiring
        it to land inside one corner crop is what made a successful login look
        like a timeout. Whole tokens are compared too, so an id that is not
        eight to twenty alphanumerics can still be verified.
        """

        wanted = normalize_ocr_text(expected)
        for token in observation.tokens:
            text = token.normalized
            found = identity_matches(wanted, text) or any(
                identity_matches(wanted, candidate)
                for candidate in identity_candidates([text])
            )
            if found:
                if self.evidence is not None:
                    self.evidence.protect(expected)
                return EaIdentityFact(
                    ea_account_id=expected,
                    source=f"ea-window-ocr:{token.confidence:.3f}",
                    verified=token.confidence >= 0.70,
                )
        return None

    def _state(
        self,
        hwnd: int,
        observation: EaObservation | None = None,
    ) -> EaUiState:
        observation = observation or self._observe(hwnd)
        page = observation.page
        if page is EaPage.CAPTCHA:
            raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
        if page is EaPage.OTP:
            return EaUiState.OTP
        # Login evidence outranks a stray account-id shaped word: reading the
        # login page as "signed in as somebody else" sent the orchestrator off
        # to sign out of a session that was never there.
        if page in PRE_LOGIN_PAGES:
            return EaUiState.LOGIN
        if page is EaPage.SIGNED_IN or self._identity(hwnd) is not None:
            return EaUiState.SIGNED_IN
        return EaUiState.UNKNOWN

    def _expired_session_point(
        self, observation: EaObservation
    ) -> tuple[int, int] | None:
        for phrase in BACK_TO_SIGN_IN_PHRASES:
            point = phrase_point(
                observation.tokens,
                observation.rect,
                phrase,
                x_range=(0.15, 0.85),
                y_range=(0.15, 0.70),
            )
            if point is not None:
                return point
        return None

    def _dismiss_expired_session(self, hwnd: int) -> bool:
        """Click BACK TO SIGN-IN until the login form is back, or give up.

        EA raises this full-window gate after the app sits idle. Leaving it
        up blocks every later login step, and a single miss used to be
        treated as success.
        """

        dismissed = False
        for _ in range(3):
            hwnd = self._live(hwnd)
            observation = self._observe(hwnd)
            if observation.page is not EaPage.EXPIRED_SESSION:
                return dismissed
            self._record("expired-session", observation)
            self.notify("EA 会话已过期，点击返回登录")
            point = self._expired_session_point(observation)
            if point is None:
                self._click(hwnd, *EXPIRED_SESSION_BUTTON_RATIO)
            else:
                self._click_point(hwnd, *point)
            dismissed = True
            self.sleep(2.0)
        observation = self._observe(self._live(hwnd))
        if observation.page is EaPage.EXPIRED_SESSION:
            self._record("expired-session-stuck", observation)
            raise EaAppAutomationError("EA 会话过期页面在返回操作后仍未关闭")
        return dismissed

    def _wait_for_page(
        self,
        hwnd: int,
        pages: Sequence[EaPage],
        *,
        timeout_s: float,
    ) -> EaObservation:
        """Poll until the window shows one of `pages`, or the time runs out."""

        deadline = time.monotonic() + timeout_s
        observation = self._observe(hwnd)
        while True:
            if observation.page in pages:
                return observation
            if time.monotonic() >= deadline:
                return observation
            self.sleep(1.0)
            observation = self._observe(hwnd)

    def preflight(self) -> EaUiState:
        hwnd = self._ea_window()
        self._dismiss_expired_session(hwnd)
        observation: EaObservation | None = None
        for _ in range(8):
            observation = self._observe(hwnd)
            hwnd, observation = self._handle_app_update(hwnd, observation)
            observation = self._dismiss_cloud_upload_error(hwnd, observation)
            observation = self._dismiss_library_tour(hwnd, observation)
            state = self._state(hwnd, observation)
            if state is not EaUiState.UNKNOWN:
                self._record("preflight", observation, state=state.value)
                return state
            self.sleep(1.0)
        self._record("preflight-unknown", observation)
        raise EaAppAutomationError("EA App 页面无法识别，领号前预检失败")

    def ensure_started(self) -> EaUiState:
        return self._with_ui_recovery("preflight", self.preflight)

    def configure_ui_recovery(self, reserve):
        self._reserve_ui_recovery = reserve

    def _with_ui_recovery(self, operation, action):
        for attempt in range(2):
            try:
                result = action()
                if result is not False:
                    return result
                raise EaAppAutomationError("EA 未确认退出登录")
            except (EaUiRecoveryExhausted, EaLoginRejected, EaCaptchaRequired, EaAccountBanned, EaOtpUnavailable):
                raise
            except EaAppAutomationError as error:
                if attempt:
                    self._record("ui-recovery-exhausted", getattr(self, "_last_observation", None))
                    raise EaUiRecoveryExhausted("EA 页面恢复后仍无法完成当前操作，保留租约等待处理") from error
                try:
                    self._recover_ui_surface(operation)
                except (EaUiRecoveryExhausted, EaLoginRejected, EaCaptchaRequired, EaAccountBanned, EaOtpUnavailable):
                    raise
                except EaAppAutomationError as recovery_error:
                    self._record("ui-recovery-exhausted", getattr(self, "_last_observation", None))
                    raise EaUiRecoveryExhausted("EA 页面重启失败，保留租约等待处理") from recovery_error

    def _recover_ui_surface(self, operation):
        # A real app restart changes the stuck surface. A worker restart alone
        # merely retries the same page, so the allowance belongs to the lease.
        reserve = getattr(self, "_reserve_ui_recovery", None)
        if callable(reserve):
            allowed = reserve(operation)
        else:
            used = getattr(self, "_local_ui_recovery", set())
            allowed = operation not in used
            self._local_ui_recovery = used | {operation}
        if not allowed:
            self._record("ui-recovery-exhausted", getattr(self, "_last_observation", None))
            raise EaUiRecoveryExhausted("本租约的 EA 页面恢复次数已用完，停止重复尝试")
        if any(self._process_running(name) for name in APEX_EXECUTABLES):
            raise EaUiRecoveryExhausted("Apex 尚未退出，不能重启 EA 恢复页面")
        self._record("ui-recovery-start", getattr(self, "_last_observation", None))
        self.notify("EA 页面操作未生效，重启 EA 后验证当前流程")
        try:
            self.restart_app()
        except (EaCaptchaRequired, EaAccountBanned, EaUiRecoveryExhausted):
            raise
        except EaAppAutomationError:
            # Login/error pages do not expose Help -> Restart app. Reopen only
            # the verified EA desktop executable; never clear cached sessions.
            self._restart_ea_process()
        self._record("ui-recovery-ready", self._observe(self._ea_window()))

    def _restart_ea_process(self):
        if any(self._process_running(name) for name in APEX_EXECUTABLES):
            raise EaUiRecoveryExhausted("Apex 尚未退出，不能重启 EA")
        hwnd = self._ea_window()
        pid = wintypes.DWORD()
        self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        executable = self._process_path(pid.value)
        if not pid.value or Path(executable).name.lower() != EA_EXECUTABLE:
            raise EaUiRecoveryExhausted("无法确认 EA 主窗口进程，停止恢复")
        try:
            result = subprocess.run(["taskkill", "/PID", str(pid.value), "/T", "/F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20, check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise EaUiRecoveryExhausted("EA 主窗口进程未退出")
            subprocess.Popen([executable], cwd=str(Path(executable).parent),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise EaUiRecoveryExhausted("EA 进程重启未完成") from error
        self._hwnd = None
        deadline = time.monotonic() + EA_RESTART_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                return self.preflight()
            except EaAppAutomationError:
                self.sleep(1.0)
        raise EaUiRecoveryExhausted("EA 重启后仍没有可识别页面")

    def current_identity(self) -> EaIdentityFact | None:
        hwnd = self._ea_window()
        observation = self._observe(hwnd)
        # A login page never carries a current identity, whatever the corner
        # crop happens to recognise there. A ban overlay still belongs to the
        # signed-in session underneath it.
        if observation.page in PRE_LOGIN_PAGES or observation.page is EaPage.CAPTCHA:
            return None
        identity = self._identity(hwnd)
        if identity is not None:
            return identity
        candidates: list[tuple[float, str]] = []
        for token in observation.tokens:
            for candidate in identity_candidates([token.normalized]):
                if is_ui_chrome(candidate):
                    continue
                candidates.append((token.confidence, candidate))
        if not candidates:
            return None
        confidence, account_id = max(candidates)
        if self.evidence is not None:
            self.evidence.protect(account_id)
        return EaIdentityFact(
            ea_account_id=account_id,
            source=f"ea-window-ocr:{confidence:.3f}",
            verified=confidence >= 0.70,
        )

    def current_page_is_banned(self) -> bool:
        hwnd = self._ea_window()
        observation = self._observe(hwnd)
        observation = self._dismiss_library_tour(hwnd, observation)
        return observation.page is EaPage.BANNED

    def _submit(
        self,
        hwnd: int,
        observation: EaObservation,
        ratio: tuple[float, float],
    ) -> str:
        """Send the form.

        Enter goes first: it needs no geometry at all and cannot land on the
        wrong control. The OCR anchor and the ratio stay behind it for the
        pages that do not submit on Enter.
        """

        self._focus(hwnd)
        self._tap(VK_RETURN)
        self.sleep(3.0)
        after_enter = self._observe(hwnd)
        if after_enter.page is not observation.page:
            return "enter"
        return self._click_target(
            hwnd,
            after_enter,
            SUBMIT_TERMS,
            ratio,
            y_range=(0.35, 0.95),
        )

    def _submit_login_identifier(
        self,
        hwnd: int,
        observation: EaObservation,
        credentials: SecretCredentials,
        *,
        timeout_s: float = 25.0,
    ) -> EaObservation:
        self._login_identifier_verified = False
        target = "none"
        for attempt in range(2):
            target = self._click_login_field(hwnd, observation)
            self._clear_focused_field()
            self._type_secret(credentials.login_identifier)
            self.sleep(INPUT_SETTLE_S)
            typed = self._observe(hwnd)
            echoed = self._identifier_echoed(typed, credentials.login_identifier)
            self._login_identifier_verified = (
                typed.page is EaPage.EMAIL
                and self._verify_login_input(typed, credentials.login_identifier)
            )
            self._record("account-typed", typed, fieldTarget=target,
                         identifierEchoed=echoed,
                         identifierVerified=self._login_identifier_verified,
                         attempt=attempt + 1)
            if self._login_identifier_verified:
                break
            if typed.page is not EaPage.EMAIL:
                raise EaAppAutomationError("EA 账号输入时页面发生变化，已停止提交")
            observation = typed
        if not self._login_identifier_verified:
            raise EaAppAutomationError("EA 未确认账号已输入编辑框，已停止提交并跳过本次登录")
        submit = self._submit(hwnd, typed, ACCOUNT_SUBMIT_RATIO)
        transitioned = self._wait_for_page(
            hwnd,
            (EaPage.PASSWORD, EaPage.OTP, EaPage.SIGNED_IN, EaPage.CAPTCHA),
            timeout_s=timeout_s,
        )
        if (
            transitioned.page is EaPage.EMAIL
            and not transitioned.has_login_error()
            and self._identifier_echoed(transitioned, credentials.login_identifier)
        ):
            self._login_identifier_verified = self._verify_login_input(
                transitioned, credentials.login_identifier
            )
            if not self._login_identifier_verified:
                raise EaAppAutomationError("EA 重试前账号输入无法确认，已停止提交")
            retry_submit = self._submit(hwnd, transitioned, ACCOUNT_SUBMIT_RATIO)
            transitioned = self._wait_for_page(
                hwnd,
                (EaPage.PASSWORD, EaPage.OTP, EaPage.SIGNED_IN, EaPage.CAPTCHA),
                timeout_s=10.0,
            )
            self._record(
                "account-submit-retry",
                transitioned,
                submitTarget=retry_submit,
                identifierEchoed=True,
            )
            submit = f"{submit}+retry:{retry_submit}"
        self._record(
            "account-submitted",
            transitioned,
            submitTarget=submit,
            identifierEchoed=echoed,
            identifierVerified=self._login_identifier_verified,
        )
        if transitioned.page is EaPage.CAPTCHA:
            raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
        if transitioned.has_login_error():
            raise EaLoginRejected("EA App 拒绝了登录标识")
        if transitioned.page in (EaPage.PASSWORD, EaPage.OTP, EaPage.SIGNED_IN):
            return transitioned
        blocker = password_page_blocker(transitioned.normalized)
        raise EaAppAutomationError(
            "EA App 提交账号后未出现密码页"
            f"（{blocker}，账号已回显={echoed}，提交方式={submit}，"
            f"输入定位={target}）"
        )

    @staticmethod
    def _identifier_echoed(observation: EaObservation, identifier: str) -> bool:
        expected = normalize_ocr_text(identifier)
        if not expected:
            return False
        return any(expected in token.normalized for token in observation.tokens)

    @classmethod
    def _verified_identifier_echo(cls, observation: EaObservation, identifier: str) -> bool:
        expected = cls._exact_identifier(identifier)
        if not expected:
            return False
        left, top, right, bottom = observation.rect
        placed = []
        for token in observation.tokens:
            if token.roi is None or token.confidence < 0.85:
                continue
            x1, y1, x2, y2 = token.roi
            if (left + (right - left) * 0.02 <= (x1 + x2) / 2 <= right
                    and top + (bottom - top) * 0.20 <= (y1 + y2) / 2
                    <= top + (bottom - top) * 0.80):
                placed.append(token)
        if any(cls._exact_identifier(t.text) == expected for t in placed):
            return True
        # Join adjacent fragments on the same row, retaining exact characters.
        for first in placed:
            prefix = cls._exact_identifier(first.text)
            if not prefix or not expected.startswith(prefix):
                continue
            row = sorted((t for t in placed if t is not first
                          and t.roi[0] >= first.roi[2]
                          and abs((first.roi[1] + first.roi[3]) - (t.roi[1] + t.roi[3]))
                          <= max(first.roi[3] - first.roi[1], t.roi[3] - t.roi[1])),
                         key=lambda t: t.roi[0])
            joined, previous = first.text, first
            for token in row:
                gap = token.roi[0] - previous.roi[2]
                row_height = max(first.roi[3] - first.roi[1], token.roi[3] - token.roi[1])
                if gap > row_height * 1.5:
                    break
                joined += token.text
                if cls._exact_identifier(joined) == expected:
                    return True
                previous = token
        return False

    def _submit_password(
        self,
        hwnd: int,
        observation: EaObservation,
        credentials: SecretCredentials,
    ) -> str:
        target = self._click_login_field(hwnd, observation, password=True)
        self._clear_focused_field()
        self._type_secret(credentials.password)
        self.sleep(INPUT_SETTLE_S)
        typed = self._observe(hwnd)
        self._record("password-typed", typed, fieldTarget=target)
        if typed.page is not EaPage.PASSWORD:
            raise EaAppAutomationError("EA 密码输入时页面发生变化，已停止提交")
        return self._submit(hwnd, typed, PASSWORD_SUBMIT_RATIO)

    @classmethod
    def _login_back_point(
        cls, observation: EaObservation,
    ) -> tuple[int, int] | None:
        placed = replace(observation, tokens=tuple(
            token for token in observation.tokens if token.confidence >= 0.80
        ))
        return cls._anchor(placed, LOGIN_BACK_TERMS, exact=True,
                           x_range=(0.02, 0.40), y_range=(0.04, 0.35))

    def _return_to_account_page(
        self, hwnd: int, observation: EaObservation,
    ) -> EaObservation:
        # A password/OTP page belongs to the identifier submitted earlier,
        # potentially by another lease. Masked email copy cannot bind it to
        # these credentials. Start each new login with its own identifier.
        if observation.page is EaPage.RESET_SUCCESS:
            point = self._recovery_control(observation, ("signin",), y_range=(0.25, 0.80))
            self._click_point(hwnd, *point)
            observation = self._wait_for_page(hwnd, (EaPage.EMAIL,), timeout_s=20.0)
        intermediate = (EaPage.PASSWORD, EaPage.OTP_METHOD, EaPage.OTP,
                        EaPage.RECOVERY_ACCOUNT, EaPage.RESET_PASSWORD)
        if observation.page not in intermediate:
            return observation
        self._record("signin-reset-start", observation)
        for attempt in range(1, 5):
            if observation.page not in intermediate:
                break
            back = self._login_back_point(observation)
            # A transition frame can identify OTP before its BACK control is
            # painted. Re-observe briefly; never guess where to click or type.
            for _ in range(3):
                if back is not None:
                    break
                self.sleep(1.0)
                observation = self._observe(hwnd)
                if (observation.page is EaPage.EMAIL
                        and password_page_blocker(observation.normalized) == "STILL_ON_ACCOUNT_PAGE"):
                    self._record("signin-account-page-ready", observation)
                    return observation
                if observation.page is EaPage.CAPTCHA:
                    raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
                if observation.page in intermediate:
                    back = self._login_back_point(observation)
            if back is None:
                self._record("signin-back-missing", observation)
                raise EaAppAutomationError("EA 登录流程未找到可信 BACK 按钮，未输入新密码")
            self._record("signin-back-to-account", observation, attempt=attempt)
            self._click_point(hwnd, *back)
            previous_page = observation.page
            for _ in range(4):
                self.sleep(1.0)
                observation = self._observe(hwnd)
                if (
                    observation.page is EaPage.EMAIL
                    and password_page_blocker(observation.normalized) == "STILL_ON_ACCOUNT_PAGE"
                ):
                    self._record("signin-account-page-ready", observation)
                    return observation
                if observation.page is EaPage.CAPTCHA:
                    self._record("captcha", observation)
                    raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
                if observation.page is EaPage.EXPIRED_SESSION:
                    self._dismiss_expired_session(hwnd)
                    observation = self._observe(hwnd)
                    continue
                if observation.page in intermediate and observation.page is not previous_page:
                    break
        self._record("signin-account-reset-failed", observation)
        raise EaAppAutomationError("EA 未能返回清晰账号输入页，已停止输入新密码")

    def sign_in(
        self,
        credentials: SecretCredentials,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
    ) -> EaIdentityFact:
        return self._with_ui_recovery("signin", lambda: self._sign_in_once(credentials, otp_supplier))

    def _sign_in_once(
        self,
        credentials: SecretCredentials,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
    ) -> EaIdentityFact:
        if self.evidence is not None:
            self.notify(f"EA 登录证据目录：{self.evidence.rotate()}")
            self.evidence.protect(credentials.login_identifier, credentials.password)
        recovery_attempted = False
        expired_restarts = 0
        # One password recovery and one expired-session restart, at most.
        for _attempt in range(3):
            self._login_identifier_verified = False
            hwnd = self._ea_window()
            self._dismiss_expired_session(hwnd)
            observation = self._observe(hwnd)
            self._record("signin-start", observation)
            if observation.page is EaPage.CAPTCHA:
                raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
            if observation.page is EaPage.BANNED:
                observation = self._dismiss_account_ban(hwnd, observation)
            if observation.page is EaPage.BANNED:
                self._raise_if_account_banned(hwnd, observation)
            observation = self._return_to_account_page(hwnd, observation)
            if observation.page is EaPage.EMAIL:
                observation = self._submit_login_identifier(
                    hwnd, observation, credentials
                )
            challenge_started_at = datetime.now(timezone.utc)
            if observation.page is EaPage.PASSWORD:
                # Only a page that shows a password field and no account field
                # gets the password typed into it. Guessing here once meant typing
                # the password into the account box.
                submit = self._submit_password(hwnd, observation, credentials)
                self.notify(f"EA 密码已提交（{submit}）")
            elif observation.page not in (EaPage.OTP, EaPage.SIGNED_IN):
                self._record("signin-wrong-page", observation)
                raise EaAppAutomationError(
                    f"EA App 当前不是可登录页面（{observation.page.value}）"
                )
            try:
                identity = self._await_identity(
                    hwnd,
                    otp_supplier,
                    otp_methods=credentials.otp_methods,
                    initial_challenge_started_at=challenge_started_at,
                )
                return identity
            except _LoginRestart:
                if expired_restarts >= 1:
                    break
                expired_restarts += 1
                self.notify("EA 会话已过期，已返回登录页，重新登录")
            except EaCredentialsRejected:
                if recovery_attempted:
                    raise
                recovery_attempted = True
                try:
                    credentials = self._recover_password(hwnd, credentials, otp_supplier) or credentials
                except EaCaptchaRequired as error:
                    try:
                        self._leave_recovery_captcha(hwnd)
                    except EaAppAutomationError:
                        pass
                    self._record("password-recovery-failed", getattr(self, "_password_recovery_observation", None))
                    raise EaCredentialsRejected("EA 密码恢复遇到 Captcha，跳过本次账号") from error
                except EaAccountBanned:
                    raise
                except EaAppAutomationError as error:
                    self._record("password-recovery-failed", getattr(self, "_password_recovery_observation", None))
                    self.notify(str(error))
                    if getattr(self, "_password_recovery_observation", None) is not None:
                        try:
                            self._leave_recovery_captcha(hwnd)
                        except EaAppAutomationError:
                            pass
                    raise EaCredentialsRejected("EA 密码恢复未完成，凭据仍需核对") from error
        raise EaAppAutomationError("EA 会话过期后重新登录仍未完成")

    def _choose_otp_method(
        self,
        hwnd: int,
        observation: EaObservation,
        otp_methods: tuple[OtpMethod, ...],
    ) -> tuple[OtpMethod, datetime]:
        """Prefer TOTP when both the account and EA's chooser offer it."""

        compact = "".join(observation.normalized)
        authenticator = self._anchor(observation, AUTHENTICATOR_TERMS)
        if OtpMethod.TOTP in otp_methods and authenticator is not None:
            self._click_point(hwnd, *authenticator)
            self.sleep(INPUT_SETTLE_S)
            chosen = self._observe(hwnd)
            method = OtpMethod.TOTP
            self._record("otp-method-authenticator", chosen)
            self.notify("EA 使用验证器验证码")
        elif OtpMethod.EMAIL in otp_methods and (
            # The email-only layout may show only the explanatory sentence
            # and a masked address, with no literal "Email" label. If there
            # is no TOTP source, the chooser itself is sufficient evidence
            # that its only available action is to send an email code.
            OtpMethod.TOTP not in otp_methods
            or has_any(compact, EMAIL_METHOD_TERMS)
        ):
            chosen = observation
            method = OtpMethod.EMAIL
            self._record("otp-method-email", chosen)
            self.notify("EA 未提供可用验证器，改用邮箱验证码")
        else:
            self._record(
                "otp-method-unavailable",
                observation,
                providerMethods=[item.value for item in otp_methods],
            )
            raise EaOtpUnavailable("EA 页面与账号可用的验证码方式不匹配")

        send = self._anchor(chosen, SEND_CODE_TERMS, y_range=(0.40, 0.95))
        challenge_started_at = datetime.now(timezone.utc)
        if send is None:
            self._click(hwnd, *SEND_CODE_RATIO)
        else:
            self._click_point(hwnd, *send)
        self._wait_for_page(
            hwnd,
            (EaPage.OTP, EaPage.SIGNED_IN),
            timeout_s=20.0,
        )
        return method, challenge_started_at

    @staticmethod
    def _otp_page_method(
        observation: EaObservation,
        otp_methods: tuple[OtpMethod, ...],
        selected_method: OtpMethod | None,
    ) -> OtpMethod:
        compact = "".join(observation.normalized)
        if has_any(compact, EMAIL_CODE_TERMS):
            method = OtpMethod.EMAIL
        elif has_any(compact, AUTHENTICATOR_TERMS):
            method = OtpMethod.TOTP
        elif selected_method is not None:
            method = selected_method
        elif len(otp_methods) == 1:
            method = otp_methods[0]
        else:
            method = OtpMethod.TOTP
        if method not in otp_methods:
            raise EaOtpUnavailable(
                f"EA 要求 {method.value}，但服务端没有对应验证码来源"
            )
        return method

    def _fresh_otp(
        self,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
        *,
        method: OtpMethod,
        challenge_started_at: datetime,
    ) -> OtpCode:
        """Get a code with enough life left to be typed.

        A TOTP dies at the end of its 30-second window, so one handed over
        with a second to go is useless — and asking again inside the same
        window returns the very same digits. Waiting the window out is the
        only thing that helps.
        """

        otp = otp_supplier(
            OtpChallenge(uuid.uuid4().hex, challenge_started_at, method)
        )
        if (
            method is OtpMethod.EMAIL
            or otp.lifetime_s >= MIN_USABLE_OTP_LIFETIME_S
        ):
            return otp
        self.notify(f"验证码只剩 {otp.lifetime_s:.0f} 秒，等下一个窗口再取")
        self.sleep(otp.lifetime_s + 1.0)
        return otp_supplier(
            OtpChallenge(
                uuid.uuid4().hex,
                datetime.now(timezone.utc),
                method,
            )
        )

    def _submit_otp(
        self,
        hwnd: int,
        observation: EaObservation,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
        *,
        method: OtpMethod,
        challenge_started_at: datetime,
    ) -> None:
        self._record(f"otp-{method.value.lower()}-code-page", observation)
        otp = self._fresh_otp(
            otp_supplier,
            method=method,
            challenge_started_at=challenge_started_at,
        )
        target = self._click_target(
            hwnd,
            observation,
            OTP_FIELD_TERMS,
            OTP_FIELD_RATIO,
            y_range=(0.20, 0.80),
        )
        self._clear_focused_field()
        self._type_secret(otp.code)
        self.sleep(INPUT_SETTLE_S)
        submit = self._submit(hwnd, self._observe(hwnd), OTP_SUBMIT_RATIO)
        self._record(
            "otp-submitted",
            self._observe(hwnd),
            fieldTarget=target,
            submitTarget=submit,
        )

    def _await_identity(
        self,
        hwnd: int,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
        *,
        otp_methods: tuple[OtpMethod, ...],
        initial_challenge_started_at: datetime,
        timeout_s: float = 90.0,
    ) -> EaIdentityFact:
        deadline = time.monotonic() + timeout_s
        observation: EaObservation | None = None
        seen_pages: set[EaPage] = set()
        otp_attempts = 0
        selected_method: OtpMethod | None = None
        pending_identity = None
        challenge_started_at = initial_challenge_started_at
        while time.monotonic() < deadline:
            self.sleep(2.0)
            observation = self._observe(hwnd)
            observation = self._dismiss_library_tour(hwnd, observation)
            self._raise_if_account_banned(hwnd, observation)
            # Whatever EA puts on screen here gets a frame the first time it
            # appears. Waiting for the timeout to explain itself meant an
            # unexpected page — a verification prompt, say — left no evidence
            # at all until 90 seconds later, and none of it from the moment
            # that mattered.
            if observation.page not in seen_pages:
                seen_pages.add(observation.page)
                self._record(
                    f"awaiting-{observation.page.value.lower()}",
                    observation,
                )
            if observation.page is EaPage.CAPTCHA:
                self._record("captcha", observation)
                raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
            if observation.has_login_error():
                self._record("login-rejected", observation,
                             identifierVerified=bool(getattr(self, "_login_identifier_verified", False)))
                if (
                    observation.page is EaPage.PASSWORD
                    and getattr(self, "_login_identifier_verified", False)
                    and has_any("".join(observation.normalized), CREDENTIAL_REJECTION_TERMS)
                ):
                    raise EaCredentialsRejected("EA 拒绝了本次已核对账号的登录凭据")
                raise EaLoginRejected("EA App 报告登录信息有误")
            if observation.page is EaPage.EXPIRED_SESSION:
                self._dismiss_expired_session(hwnd)
                raise _LoginRestart()
            if observation.page is EaPage.OTP_METHOD:
                selected_method, challenge_started_at = self._choose_otp_method(
                    hwnd,
                    observation,
                    otp_methods,
                )
                continue
            if observation.page is EaPage.OTP:
                otp_attempts += 1
                if otp_attempts > MAX_OTP_ATTEMPTS:
                    self._record("otp-exhausted", observation, attempts=otp_attempts)
                    raise EaOtpUnavailable(
                        f"EA 连续 {MAX_OTP_ATTEMPTS} 次没有接受验证码"
                    )
                method = self._otp_page_method(
                    observation,
                    otp_methods,
                    selected_method,
                )
                self._submit_otp(
                    hwnd,
                    observation,
                    otp_supplier,
                    method=method,
                    challenge_started_at=challenge_started_at,
                )
                continue
            # A login page still on screen is not a badge to read, whatever a
            # corner crop makes of the text sitting there.
            if observation.page in PRE_LOGIN_PAGES:
                pending_identity = None
                continue
            identity = self._identity(hwnd)
            if identity is not None and identity.verified:
                # Unknown/loading pages can contain account-shaped UI text.
                # Require a signed-in surface or a second consistent reading.
                if (observation.page is not EaPage.SIGNED_IN
                        and pending_identity != identity.ea_account_id):
                    pending_identity = identity.ea_account_id
                    continue
                self._record(
                    "signed-in",
                    observation,
                    identity=mask_identity(identity.ea_account_id),
                    identitySource=identity.source,
                )
                return identity
            pending_identity = None
        self._record("signin-timeout", observation)
        page = "NONE" if observation is None else observation.page.value
        raise EaAppAutomationError(
            f"EA App 登录后未在 {timeout_s:.0f} 秒内出现可验证身份（最后页面 {page}）"
        )

    def verify_identity(self, expected_ea_account_id: str) -> EaIdentityFact:
        deadline = time.monotonic() + 20.0
        seen: list[str] = []
        alternate = None
        alternate_count = 0
        observation: EaObservation | None = None
        while time.monotonic() < deadline:
            hwnd = self._ea_window()
            observation = self._observe(hwnd)
            observation = self._dismiss_library_tour(hwnd, observation)
            self._raise_if_account_banned(hwnd, observation)
            if observation.page is EaPage.CAPTCHA:
                raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
            if observation.page in PRE_LOGIN_PAGES:
                alternate, alternate_count = None, 0
                self.sleep(1.0)
                continue
            match = self._matching_identity(observation, expected_ea_account_id)
            if match is not None and match.verified:
                self._record(
                    "identity-verified",
                    observation,
                    identity=mask_identity(match.ea_account_id),
                )
                return match
            identity = self._identity(hwnd)
            if identity is not None:
                if identity_matches(expected_ea_account_id, identity.ea_account_id):
                    alternate, alternate_count = None, 0
                    if identity.verified:
                        self._record(
                            "identity-verified",
                            observation,
                            identity=mask_identity(expected_ea_account_id),
                        )
                        return EaIdentityFact(
                            ea_account_id=expected_ea_account_id,
                            source=identity.source,
                            verified=True,
                        )
                elif (match is None and identity.verified
                      and observation.page is EaPage.SIGNED_IN):
                    seen.append(mask_identity(identity.ea_account_id))
                    alternate_count = alternate_count + 1 if alternate == identity.ea_account_id else 1
                    alternate = identity.ea_account_id
                    # Three unrelated or low-confidence OCR guesses do not
                    # establish a stable alternate identity.
                    if alternate_count >= 3:
                        self._record("identity-mismatch", observation, observed=seen)
                        raise EaIdentityMismatch(
                            "EA App 当前稳定 EA ID 与租约不一致"
                            f"（观察到 {seen[-1]}）"
                        )
                else:
                    alternate, alternate_count = None, 0
            else:
                alternate, alternate_count = None, 0
            self.sleep(1.0)
        self._record("identity-timeout", observation, observed=seen or None)
        raise EaIdentityUnconfirmed(
            "EA App 页面没有可验证的稳定 EA ID"
            + (f"（观察到 {seen[-1]}）" if seen else "")
        )

    @staticmethod
    def _contains_compact_terms(
        observation: EaObservation,
        terms: Sequence[str],
    ) -> bool:
        compact = "".join(observation.normalized)
        return any(term in compact for term in terms)

    def _wait_for_observation(
        self,
        hwnd: int,
        predicate: Callable[[EaObservation], bool],
        *,
        timeout_s: float,
        missing_step: str,
        error_message: str,
    ) -> EaObservation:
        deadline = time.monotonic() + timeout_s
        observation: EaObservation | None = None
        while time.monotonic() < deadline:
            hwnd = self._live(hwnd)
            observation = self._observe(hwnd)
            if predicate(observation):
                return observation
            self.sleep(1.0)
        self._record(missing_step, observation)
        raise EaApexStartFailed(error_message)

    def _installed_apex_copy_exists(self) -> bool:
        return self.apex_install_dir.is_dir() and any(
            (self.apex_install_dir / executable).is_file()
            for executable in APEX_EXECUTABLES
        )

    def repair_apex_installation(self) -> None:
        """Re-register the existing D:\\Apex copy through EA's download flow."""

        if not self._installed_apex_copy_exists():
            raise EaApexStartFailed(
                f"未在 {self.apex_install_dir} 找到已安装的 Apex 程序，"
                "已拒绝启动下载流程"
            )

        hwnd = self._ea_window()
        download_page = self._observe(hwnd)
        download = self._anchor(
            download_page,
            ("download", "下载"),
            x_range=(0.35, 0.75),
            y_range=(0.30, 0.70),
            exact=True,
        )
        if download is None:
            self._record("apex-install-download-missing", download_page)
            raise EaApexStartFailed("EA App Apex 页面未找到 Download 按钮")
        self.notify("EA App 开始重新登记 D:\\Apex 已有游戏文件")
        self._record("apex-install-download", download_page)
        self._click_point(hwnd, *download)

        expected_location = normalize_ocr_text(str(self.apex_install_dir))

        def valid_options(observation: EaObservation) -> bool:
            compact = "".join(observation.normalized)
            return (
                any(term in compact for term in DOWNLOAD_OPTIONS_TERMS)
                and any(term in compact for term in INSTALL_LOCATION_TERMS)
                and expected_location in compact
            )

        def apex_play_ready(observation: EaObservation) -> bool:
            compact = "".join(observation.normalized)
            return "apexlegends" in compact and self._anchor(
                observation,
                ("play", "launch", "launchgame", "startgame", "开始游戏"),
                x_range=(0.35, 0.75),
                y_range=(0.30, 0.70),
                exact=True,
            ) is not None

        def installation_complete(observation: EaObservation) -> bool:
            compact = "".join(observation.normalized)
            return (
                any(term in compact for term in DOWNLOAD_MANAGER_TERMS)
                and "apexlegends" in compact
                and (
                    any(term in compact for term in INSTALL_COMPLETE_TERMS)
                    or any(term in compact for term in COMPLETED_TERMS)
                )
            )

        def installation_in_progress(observation: EaObservation) -> bool:
            compact = "".join(token.normalized for token in observation.tokens if token.confidence >= 0.8)
            return ("apexlegends" in compact
                    and any(term in compact for term in DOWNLOAD_MANAGER_TERMS)
                    and any(term in compact for term in (
                        "preparing", "verifying", "repairing", "installing", "downloading",
                        "正在准备", "正在验证", "正在校验", "正在修复", "正在安装", "正在下载",
                    )))

        def wait_for_completion() -> None:
            completed = self._wait_for_observation(
                hwnd, lambda observation: installation_complete(observation) or apex_play_ready(observation),
                timeout_s=APEX_INSTALL_REPAIR_TIMEOUT_S,
                missing_step="apex-install-complete-timeout",
                error_message="EA App 现有文件登记仍未完成，已保留失败证据",
            )
            self._record("apex-install-direct-play" if apex_play_ready(completed) else "apex-install-complete", completed)
            self.notify("EA App 已显示 Play 或 Installation complete，无需 Restart app")

        next_page = self._wait_for_observation(
            hwnd,
            lambda observation: (
                valid_options(observation)
                or apex_play_ready(observation)
                or installation_complete(observation)
                or installation_in_progress(observation)
            ),
            timeout_s=45.0,
            missing_step="apex-install-first-transition-timeout",
            error_message=(
                "EA App 点击 Download 后既未进入 Download options，"
                "也未显示 Play 或 Installation complete"
            ),
        )
        if apex_play_ready(next_page):
            self._record("apex-install-direct-play", next_page)
            self.notify(
                "EA App 点击 Download 后已直接识别 D:\\Apex 并显示 Play，"
                "无需 Restart app"
            )
            return
        if installation_complete(next_page):
            self._record("apex-install-complete", next_page)
            self.notify("EA App 已直接完成 D:\\Apex 登记，无需 Restart app")
            return
        if installation_in_progress(next_page):
            self._record("apex-install-progress", next_page)
            wait_for_completion()
            return

        options = next_page
        next_button = self._anchor(
            options,
            ("next", "下一步"),
            x_range=(0.45, 0.75),
            y_range=(0.65, 0.95),
            exact=True,
        )
        if next_button is None:
            self._record("apex-install-next-missing", options)
            raise EaApexStartFailed("EA App Download options 未找到 Next 按钮")
        self.notify(
            f"已确认安装路径 {self.apex_install_dir}；保留 EA 当前语言选择"
        )
        self._record("apex-install-options", options)
        self._click_point(hwnd, *next_button)

        next_page = self._wait_for_observation(
            hwnd,
            lambda observation: (
                self._contains_compact_terms(observation, TERMS_OF_PLAY_TERMS)
                or apex_play_ready(observation)
                or installation_complete(observation)
                or installation_in_progress(observation)
            ),
            timeout_s=45.0,
            missing_step="apex-install-second-transition-timeout",
            error_message=(
                "EA App 点击 Next 后未进入 Terms of play，"
                "也未显示 Play 或 Installation complete"
            ),
        )
        if apex_play_ready(next_page):
            self._record("apex-install-direct-play", next_page)
            self.notify("EA App 点击 Next 后已显示 Play，无需 Restart app")
            return
        if installation_complete(next_page):
            self._record("apex-install-complete", next_page)
            self.notify("EA App 已完成 D:\\Apex 登记，无需 Restart app")
            return
        if installation_in_progress(next_page):
            self._record("apex-install-progress", next_page)
            wait_for_completion()
            return

        terms = next_page
        confirm_download = self._anchor(
            terms,
            ("download", "下载"),
            x_range=(0.45, 0.75),
            y_range=(0.65, 0.95),
            exact=True,
        )
        if confirm_download is None:
            self._record("apex-install-confirm-missing", terms)
            raise EaApexStartFailed("EA App Terms of play 页面未找到 Download 按钮")
        self._record("apex-install-terms", terms)
        self._click_point(hwnd, *confirm_download)
        self.notify("EA App 已提交现有文件登记，等待 Installation complete")

        wait_for_completion()

    @staticmethod
    def _app_restart_required(observation: EaObservation) -> bool:
        _, top, _, bottom = observation.rect
        header = "".join(
            token.normalized for token in observation.tokens
            if token.roi is not None and token.confidence >= 0.70
            and top <= (token.roi[1] + token.roi[3]) / 2 <= top + (bottom - top) * 0.20
        )
        return has_any(header, EA_UPDATE_RESTART_TERMS)

    @staticmethod
    def _app_restart_point(observation: EaObservation) -> tuple[int, int] | None:
        if not WindowsEaHybridDriver._app_restart_required(observation):
            return None
        # A whole-banner OCR box cannot locate the underlined link. Require
        # the exact label, including split tokens, or use the Help menu.
        placed = tuple(t for t in observation.tokens if t.confidence >= 0.70)
        for term in RESTART_APP_TERMS:
            for start in range(len(placed)):
                for size in range(1, 4):
                    group = placed[start:start + size]
                    if "".join(t.normalized for t in group) == term:
                        point = phrase_point(group, observation.rect, term, y_range=(0.0, 0.20))
                        if point is not None:
                            return point
        return None

    def _handle_app_update(self, hwnd: int, observation: EaObservation) -> tuple[int, EaObservation]:
        if (not self._app_restart_required(observation)
                or getattr(self, "_update_restart_in_progress", False)
                or any(self._process_running(name) for name in APEX_EXECUTABLES)):
            return hwnd, observation
        self.notify("EA App 需要更新，Apex 已退出，正在重启 EA 后继续当前流程")
        self._update_restart_in_progress = True
        try:
            self.restart_app()
            hwnd = self._ea_window()
            updated = self._observe(hwnd)
            if self._app_restart_required(updated):
                raise EaAppAutomationError("EA App 重启后仍显示需要更新，保留租约等待处理")
            return hwnd, updated
        finally:
            self._update_restart_in_progress = False

    def _request_restart_app(self, hwnd: int) -> None:
        """Select the exact Help -> Restart app action from EA's main menu."""

        observation = self._observe(hwnd)
        restart_point = self._app_restart_point(observation)
        if restart_point is not None:
            self._record("restart-requested", observation)
            self._click_point(hwnd, *restart_point)
            return
        left, top, right, bottom = observation.rect
        width = max(1, right - left)
        height = max(1, bottom - top)
        hamburger = (
            left + round(width * 0.011),
            top + round(height * 0.018),
        )
        self._record("restart-menu-open", observation)
        self._click_point(hwnd, *hamburger)
        self.sleep(1.2)

        menu = self._observe(hwnd)
        help_item = self._anchor(
            menu,
            HELP_TERMS,
            x_range=(0.0, 0.35),
            exact=True,
        )
        if help_item is None:
            self._record("restart-help-missing", menu)
            raise EaApexStartFailed("EA App 主菜单未找到 Help")
        self._click_point(hwnd, *help_item)
        self.sleep(1.2)

        help_menu = self._observe(hwnd)
        restart_item = self._anchor(
            help_menu,
            RESTART_APP_TERMS,
            x_range=(0.0, 0.45),
            exact=True,
        )
        if restart_item is None:
            self._record("restart-item-missing", help_menu)
            raise EaApexStartFailed("EA App Help 菜单未找到 Restart app")
        self._record("restart-requested", help_menu)
        self._click_point(hwnd, *restart_item)

    def restart_app(self) -> EaUiState:
        """Restart EA through its own menu and wait for a readable session."""

        old_hwnd = self._ea_window()
        self._request_restart_app(old_hwnd)
        deadline = time.monotonic() + EA_RESTART_TIMEOUT_S

        # Normally Restart app destroys the window. Give that transition a
        # bounded head start, but also tolerate an in-process client reload.
        transition_deadline = min(deadline, time.monotonic() + 15.0)
        while self._alive(old_hwnd) and time.monotonic() < transition_deadline:
            self.sleep(0.5)

        self._hwnd = None
        last_error: EaAppAutomationError | None = None
        while time.monotonic() < deadline:
            try:
                state = self.preflight()
                observation = self._observe(self._ea_window())
                self._record("restart-complete", observation, state=state.value)
                return state
            except EaAppAutomationError as error:
                last_error = error
                self.sleep(1.0)
        raise EaApexStartFailed(
            "EA App 执行 Restart app 后未在 60 秒内恢复"
            + (f"：{last_error}" if last_error is not None else "")
        )

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
        hwnd = self._ea_window()
        # A leftover Apex process used to skip the Library overlay. The ban
        # dialog still sits on EA, and the play session then scans melee
        # against an account that cannot go online.
        if any(self._process_running(name) for name in APEX_EXECUTABLES):
            observation = self._observe(hwnd)
            observation = self._dismiss_library_tour(hwnd, observation)
            self._raise_if_account_banned(hwnd, observation)
            return
        # The orchestrator verifies the stable EA identity immediately before
        # entering this method.  Do not repeat that check here: the CEF surface
        # occasionally yields an empty OCR frame while it is otherwise fully
        # interactive.  Wait for the actual launch control instead.
        for _ in range(15):
            observation = self._observe(hwnd)
            observation = self._dismiss_cloud_upload_error(hwnd, observation)
            observation = self._dismiss_library_tour(hwnd, observation)
            self._raise_if_account_banned(hwnd, observation)
            point = self._anchor(
                observation,
                ("apexlegends",),
                x_range=(0.0, 0.30),
                y_range=(0.15, 0.90),
            )
            if point is not None:
                self._record("apex-entry", observation)
                self._click_point(hwnd, *point)
                break
            self.sleep(1.0)
        else:
            self._record("apex-entry-missing", self._observe(hwnd))
            raise EaApexStartFailed("EA App 未找到左侧 Apex Legends 游戏入口")
        self.sleep(2.0)
        action_deadline = time.monotonic() + 15.0
        launch_deadline: float | None = None
        update_deadline: float | None = None
        update_clicked_at = 0.0
        update_clicks = 0
        cloud_recovery_clicks = 0
        update_wait_notice_at = 0.0

        while True:
            if any(self._process_running(name) for name in APEX_EXECUTABLES):
                return
            now = time.monotonic()
            if update_deadline is not None and now >= update_deadline:
                raise EaApexStartFailed("EA App 中 Apex 更新等待超过 2 小时")
            if launch_deadline is not None and now >= launch_deadline:
                raise EaApexStartFailed("点击 EA Play 后未发现 Apex 进程")
            if (
                update_deadline is None
                and launch_deadline is None
                and now >= action_deadline
            ):
                observation = self._observe(hwnd)
                self._record("apex-play-missing", observation)
                raise EaApexStartFailed("EA App Apex 页面未找到 Play 或 Update 按钮")

            try:
                observation = self._observe(hwnd)
            except EaAppAutomationError:
                # EA may briefly hide or rebuild its CEF window after Play or
                # during an update. The process check above remains the source
                # of truth; keep the wait bounded by the active deadline.
                if launch_deadline is None and update_deadline is None:
                    raise
                self.sleep(2.0)
                continue

            observation = self._dismiss_cloud_upload_error(hwnd, observation)
            observation = self._dismiss_library_tour(hwnd, observation)
            self._raise_if_account_banned(hwnd, observation)
            local_data = self._continue_local_data_point(observation)
            if local_data is not None:
                if cloud_recovery_clicks >= 2:
                    self._record("apex-cloud-data-stuck", observation)
                    raise EaApexStartFailed(
                        "EA App 云存档错误在两次本地数据恢复后仍未关闭"
                    )
                cloud_recovery_clicks += 1
                self._record(
                    "apex-cloud-data-local",
                    observation,
                    attempt=cloud_recovery_clicks,
                )
                self.notify("EA 云存档不可用，选择本地数据继续启动 Apex")
                self._click_point(hwnd, *local_data)
                launch_deadline = time.monotonic() + APEX_LAUNCH_TIMEOUT_S
                self.sleep(2.0)
                continue

            update = self._apex_update_point(observation)
            if update is not None:
                should_click = update_deadline is None or (
                    update_clicks < 2 and now - update_clicked_at >= 15.0
                )
                if should_click:
                    update_clicks += 1
                    update_clicked_at = now
                    self._record(
                        "apex-update",
                        observation,
                        attempt=update_clicks,
                    )
                    self.notify("EA App 检测到 Apex 需要更新，已点击 Update 并等待完成")
                    self._click_point(hwnd, *update)
                    update_deadline = time.monotonic() + APEX_UPDATE_TIMEOUT_S
                    launch_deadline = None
                self.sleep(2.0)
                continue

            point = self._anchor(
                observation,
                APEX_PLAY_TERMS,
                x_range=(0.35, 0.75),
                y_range=(0.30, 0.70),
                exact=True,
            )
            if point is not None and launch_deadline is None:
                updated = update_deadline is not None
                step = (
                    "apex-play-after-update" if updated else "apex-play"
                )
                self._record(step, observation)
                if updated:
                    self.notify("Apex 更新已完成，正在启动游戏")
                self._click_point(hwnd, *point)
                update_deadline = None
                launch_deadline = time.monotonic() + APEX_LAUNCH_TIMEOUT_S
                self.sleep(2.0)
                continue

            download = self._anchor(
                observation,
                ("download", "下载"),
                x_range=(0.35, 0.75),
                y_range=(0.30, 0.70),
                exact=True,
            )
            if (
                download is not None
                and update_deadline is None
                and launch_deadline is None
            ):
                self._record("apex-download-required", observation)
                raise EaApexDownloadRequired(
                    "EA App 当前账号的 Apex 页面只提供 Download，不能启动已安装游戏"
                )

            library_play = self._installed_library_play_point(observation)
            if library_play is not None and launch_deadline is None:
                self._record("apex-library-play", observation)
                self._click_point(hwnd, *library_play)
                launch_deadline = time.monotonic() + APEX_LAUNCH_TIMEOUT_S
                self.sleep(2.0)
                continue

            if update_deadline is not None and now >= update_wait_notice_at:
                self.notify("Apex 仍在更新，Runner 会继续等待")
                update_wait_notice_at = now + 300.0
            self.sleep(2.0)

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

    def _account_menu_triggers(
        self,
        observation: EaObservation,
        identity: EaIdentityFact | None,
    ) -> list[tuple[str, tuple[int, int]]]:
        """Every control that opens the account menu, best first.

        Two of these are known to work on the real client: the chevron right
        of the signed-in name ("Log out"), and the hamburger at the top left
        ("Sign out", second from the bottom). The badge name itself is not one
        of them — clicking it left the menu shut and the account signed in.
        """

        left, top, right, bottom = observation.rect
        width = max(1, right - left)
        height = max(1, bottom - top)
        badge = (
            None
            if identity is None
            else self._anchor(
                observation,
                (identity.ea_account_id,),
                y_range=(0.0, 0.22),
            )
        )
        triggers: list[tuple[str, tuple[int, int]]] = []
        if badge is not None:
            triggers.append(("chevron", (right - round(width * 0.012), badge[1])))
        triggers.append(
            ("hamburger", (left + round(width * 0.011), top + round(height * 0.018)))
        )
        if badge is not None:
            badge_x, badge_y = badge
            triggers.append(("badge", (badge_x, badge_y)))
            triggers.append(
                ("avatar", (max(left, badge_x - round(width * 0.055)), badge_y))
            )
        triggers.append(
            ("ratio", (left + round(width * 0.89), top + round(height * 0.075)))
        )
        triggers.append(
            (
                "lower-ratio",
                (left + round(width * 0.89), top + round(height * 0.155)),
            )
        )
        return triggers

    def _open_account_menu(
        self,
        hwnd: int,
        identity: EaIdentityFact | None,
    ) -> tuple[EaObservation, tuple[int, int]] | None:
        for name, point in self._account_menu_triggers(self._observe(hwnd), identity):
            try:
                self._click_point(hwnd, *point)
            except EaAppAutomationError:
                self._record("signout-menu-focus-failed", trigger=name)
                continue
            self.sleep(1.2)
            menu = self._observe(hwnd)
            item = self._anchor(menu, SIGN_OUT_TERMS)
            if item is not None:
                self._record("signout-menu", menu, trigger=name)
                return menu, item
            self._record("signout-menu-missing", menu, trigger=name)
            # Close whatever that click did open before trying the next one.
            try:
                self._focus(hwnd)
            except EaAppAutomationError:
                continue
            self._tap(VK_ESCAPE)
            self.sleep(0.5)
        return None

    def sign_out(self) -> bool:
        return self._with_ui_recovery("signout", self._sign_out_once)

    def _sign_out_once(self) -> bool:
        hwnd = self._ea_window()
        identity = None
        signed_in_page_seen = False
        cloud_sync_close_deadline: float | None = None
        for _ in range(8):
            try:
                observation = self._observe(hwnd)
            except EaAppAutomationError:
                if cloud_sync_close_deadline is not None:
                    self._record("signout-cloud-sync-closed")
                    return True
                raise
            hwnd, observation = self._handle_app_update(hwnd, observation)
            # Sign-out is where a finished run sits after Apex closes. The
            # expired gate is not a session to log out of, but leaving the
            # button unclicked keeps the next login from starting.
            if observation.page is EaPage.EXPIRED_SESSION:
                self._dismiss_expired_session(hwnd)
                continue
            observation = self._dismiss_cloud_upload_error(hwnd, observation)
            local_data = self._continue_local_data_point(observation)
            if local_data is not None:
                if cloud_sync_close_deadline is not None:
                    if time.monotonic() >= cloud_sync_close_deadline:
                        self._record("signout-cloud-sync-timeout", observation)
                        return False
                    self.sleep(1.0)
                else:
                    cloud_sync_close_deadline = time.monotonic() + 15.0
                    self._record("signout-cloud-sync-skip", observation)
                    self.notify("EA 云同步失败，已选择跳过同步并关闭")
                    try:
                        self._click_point(hwnd, *local_data)
                    except EaAppAutomationError:
                        self._record("signout-cloud-sync-skip-failed", observation)
                        return False
                    self.sleep(1.0)
                continue
            observation = self._dismiss_library_tour(hwnd, observation)
            try:
                observation = self._dismiss_account_ban(hwnd, observation)
            except EaAppAutomationError:
                observation = self._observe(hwnd)
            # Anything still inside the login flow — the account page, the
            # password page, a verification prompt — means no session was ever
            # established, so there is nothing to sign out of. Failing here
            # blocked the lease from being handed back after a login that
            # stopped at EA's verification step.
            if observation.page in PRE_LOGIN_PAGES:
                self._record("signout-not-signed-in", observation)
                return True
            # Empty library and the EC:107 overlay are still a signed-in
            # session. They used to skip the account menu, so 换号 kept the
            # banned account on screen and the next lease looked banned too.
            signed_in_page_seen = signed_in_page_seen or observation.page in (
                EaPage.SIGNED_IN,
                EaPage.BANNED,
            )
            identity = self._identity(hwnd)
            if identity is not None:
                break
            self.sleep(1.0)
        if identity is None and not signed_in_page_seen:
            if cloud_sync_close_deadline is not None:
                self._record("signout-cloud-sync-timeout")
            return False
        opened = self._open_account_menu(hwnd, identity)
        if opened is None:
            return False
        _, item = opened
        try:
            self._click_point(hwnd, *item)
        except EaAppAutomationError:
            self._record("signout-item-failed")
            return False
        deadline = time.monotonic() + 25.0
        confirmed = False
        cloud_sync_skipped = False
        upload_signout_retries = 0
        while time.monotonic() < deadline:
            self.sleep(1.0)
            try:
                observation = self._observe(hwnd)
            except EaAppAutomationError:
                if cloud_sync_skipped:
                    self._record("signout-cloud-sync-closed")
                    return True
                raise
            upload_notice = self._contains_any(observation, CLOUD_UPLOAD_ERROR_TERMS)
            observation = self._dismiss_cloud_upload_error(hwnd, observation)
            if observation.page in (EaPage.EMAIL, EaPage.PASSWORD):
                self._record("signed-out", observation)
                return True
            if upload_notice and observation.page is EaPage.SIGNED_IN:
                if upload_signout_retries >= 1:
                    self._record("signout-cloud-upload-retry-exhausted", observation)
                    return False
                upload_signout_retries += 1
                self._record("signout-cloud-upload-retry", observation)
                opened = self._open_account_menu(hwnd, self._identity(hwnd))
                if opened is None:
                    return False
                _, item = opened
                self._click_point(hwnd, *item)
                confirmed = False
                continue
            local_data = self._continue_local_data_point(observation)
            if local_data is not None:
                if cloud_sync_skipped:
                    self._record("signout-cloud-sync-timeout", observation)
                    return False
                cloud_sync_skipped = True
                self._record("signout-cloud-sync-skip", observation)
                self.notify("EA 云同步失败，已选择跳过同步并关闭")
                try:
                    self._click_point(hwnd, *local_data)
                except EaAppAutomationError:
                    self._record("signout-cloud-sync-skip-failed", observation)
                    return False
                continue
            if confirmed:
                continue
            # EA can ask once more before it drops the session.
            confirm = self._anchor(observation, SIGN_OUT_CONFIRM_TERMS)
            if confirm is not None and confirm != item:
                self._record("signout-confirm", observation)
                try:
                    self._click_point(hwnd, *confirm)
                except EaAppAutomationError:
                    self._record("signout-confirm-failed", observation)
                    return False
                confirmed = True
        try:
            final_observation = self._observe(hwnd)
        except EaAppAutomationError:
            if cloud_sync_skipped:
                self._record("signout-cloud-sync-closed")
                return True
            raise
        self._record("signout-timeout", final_observation)
        return False

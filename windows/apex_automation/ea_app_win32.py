from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
import sys
import time
from typing import Callable, Sequence
import uuid

import numpy as np

from .account_provider import (
    MIN_USABLE_OTP_LIFETIME_S,
    OtpCode,
    OtpMethod,
    SecretCredentials,
)
from .ea_app import (
    ApexExitEvidence,
    EaAppAutomationError,
    EaApexStartFailed,
    EaCaptchaRequired,
    EaCaptureUnavailable,
    EaIdentityFact,
    EaIdentityMismatch,
    EaLoginRejected,
    EaOtpUnavailable,
    EaUiState,
    OtpChallenge,
)
from .ea_evidence import EaLoginEvidence
from .ea_pages import (
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
    SUBMIT_TERMS,
    EaPage,
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
# The badge sits at about 7.5% of the window height; the friends list
# starts just below 14%. Keep the crop above it.
IDENTITY_BAND = (0.75, 0.00, 1.00, 0.12)
INPUT_SETTLE_S = 0.8
MAX_OTP_ATTEMPTS = 3

# Pages that prove no session exists yet.
PRE_LOGIN_PAGES = (
    EaPage.EMAIL,
    EaPage.PASSWORD,
    EaPage.OTP_METHOD,
    EaPage.OTP,
    EaPage.EXPIRED_SESSION,
)


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


class WindowsEaHybridDriver:
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
        self.notify = notify
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
        self.user32.IsWindow.argtypes = [wintypes.HWND]
        self.user32.IsWindow.restype = wintypes.BOOL
        self.user32.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user32.IsWindowVisible.restype = wintypes.BOOL
        self._hwnd: int | None = None
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
                self._hwnd = max(matches)[1]
                return self._hwnd
            if time.monotonic() >= deadline:
                raise EaAppAutomationError("没有发现可见的 EA App 主窗口")
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

    def _focus(self, hwnd: int) -> None:
        hwnd = self._live(hwnd)
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
            try:
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
            observation = EaObservation(
                rect=(left, top, right, bottom),
                frame=frame,
                tokens=tokens,
                page=classify_page(token.normalized for token in tokens),
            )
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
        x1, y1, x2, y2 = IDENTITY_BAND
        region = Region(
            "eaIdentity",
            (
                left + round(window_width * x1),
                top + round(window_height * y1),
                min(right, left + round(window_width * x2)),
                top + round(window_height * y2),
            ),
        )
        candidates: list[tuple[float, str]] = []
        for token in self.ocr.read(frame, region):
            for candidate in identity_candidates([normalize_ocr_text(token.text)]):
                # Window chrome shares this corner. Reading "Friends 0/2" as
                # the signed-in account sends the orchestrator off to sign out
                # of a session that is already the right one.
                if is_ui_chrome(candidate):
                    continue
                candidates.append((token.confidence, candidate))
        if not candidates:
            return None
        confidence, account_id = max(candidates)
        if self.evidence is not None:
            # The badge is operational evidence, but the stable EA ID should
            # not remain readable in every later diagnostic screenshot.
            self.evidence.protect(account_id)
        return EaIdentityFact(
            ea_account_id=account_id,
            source=f"ea-window-ocr:{confidence:.3f}",
            verified=confidence >= 0.75,
        )

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
        if page in (EaPage.EMAIL, EaPage.PASSWORD, EaPage.EXPIRED_SESSION):
            return EaUiState.LOGIN
        if page is EaPage.SIGNED_IN or self._identity(hwnd) is not None:
            return EaUiState.SIGNED_IN
        return EaUiState.UNKNOWN

    def _dismiss_expired_session(self, hwnd: int) -> bool:
        observation = self._observe(hwnd)
        if observation.page is not EaPage.EXPIRED_SESSION:
            return False
        self._record("expired-session", observation)
        self._click(hwnd, 0.50, 0.35)
        self.sleep(2.0)
        return True

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
            state = self._state(hwnd, observation)
            if state is not EaUiState.UNKNOWN:
                self._record("preflight", observation, state=state.value)
                return state
            self.sleep(1.0)
        self._record("preflight-unknown", observation)
        raise EaAppAutomationError("EA App 页面无法识别，领号前预检失败")

    def ensure_started(self) -> EaUiState:
        return self.preflight()

    def current_identity(self) -> EaIdentityFact | None:
        hwnd = self._ea_window()
        # A login page never carries a current identity, whatever the corner
        # crop happens to recognise there.
        if self._observe(hwnd).page in (
            EaPage.EMAIL,
            EaPage.PASSWORD,
            EaPage.OTP,
            EaPage.CAPTCHA,
            EaPage.EXPIRED_SESSION,
        ):
            return None
        return self._identity(hwnd)

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
        target = self._click_target(
            hwnd,
            observation,
            ACCOUNT_FIELD_TERMS,
            ACCOUNT_FIELD_RATIO,
            y_range=(0.20, 0.80),
        )
        self._clear_focused_field()
        self._type_secret(credentials.login_identifier)
        # The capture source serves the last frame it has. Reading straight
        # after typing showed an empty field on a page that had in fact
        # accepted the text, which is a false negative on the one signal that
        # says the click found the input.
        self.sleep(INPUT_SETTLE_S)
        typed = self._observe(hwnd)
        # Whether the identifier actually landed in a field separates "the
        # click missed the input" from "the submit never fired". Only the fact
        # is kept; the identifier itself never reaches disk.
        echoed = self._identifier_echoed(typed, credentials.login_identifier)
        self._record(
            "account-typed",
            typed,
            fieldTarget=target,
            identifierEchoed=echoed,
        )
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

    def _submit_password(
        self,
        hwnd: int,
        observation: EaObservation,
        credentials: SecretCredentials,
    ) -> str:
        # Never let the anchor land on "Forgot your password": that link is on
        # the same page and it navigates away from the login entirely.
        target = self._click_target(
            hwnd,
            observation,
            PASSWORD_FIELD_TERMS,
            PASSWORD_FIELD_RATIO,
            y_range=(0.20, 0.80),
            exclude=PASSWORD_LINK_TERMS,
        )
        self._clear_focused_field()
        self._type_secret(credentials.password)
        self.sleep(INPUT_SETTLE_S)
        self._record("password-typed", self._observe(hwnd), fieldTarget=target)
        return self._submit(hwnd, observation, PASSWORD_SUBMIT_RATIO)

    def sign_in(
        self,
        credentials: SecretCredentials,
        otp_supplier: Callable[[OtpChallenge], OtpCode],
    ) -> EaIdentityFact:
        if self.evidence is not None:
            self.notify(f"EA 登录证据目录：{self.evidence.rotate()}")
            self.evidence.protect(credentials.login_identifier)
        hwnd = self._ea_window()
        self._dismiss_expired_session(hwnd)
        observation = self._observe(hwnd)
        self._record("signin-start", observation)
        if observation.page is EaPage.CAPTCHA:
            raise EaCaptchaRequired("EA App 出现 Captcha，已暂停")
        if observation.page is EaPage.EMAIL:
            observation = self._submit_login_identifier(hwnd, observation, credentials)
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
        return self._await_identity(
            hwnd,
            otp_supplier,
            otp_methods=credentials.otp_methods,
            initial_challenge_started_at=challenge_started_at,
        )

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
        elif OtpMethod.EMAIL in otp_methods and has_any(
            compact, EMAIL_METHOD_TERMS
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
        challenge_started_at = initial_challenge_started_at
        while time.monotonic() < deadline:
            self.sleep(2.0)
            observation = self._observe(hwnd)
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
                self._record("login-rejected", observation)
                raise EaLoginRejected("EA App 报告登录信息有误")
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
            if observation.page in (EaPage.EMAIL, EaPage.PASSWORD):
                continue
            identity = self._identity(hwnd)
            if identity is not None:
                self._record(
                    "signed-in",
                    observation,
                    identity=mask_identity(identity.ea_account_id),
                    identitySource=identity.source,
                )
                return identity
        self._record("signin-timeout", observation)
        page = "NONE" if observation is None else observation.page.value
        raise EaAppAutomationError(
            f"EA App 登录后未在 {timeout_s:.0f} 秒内出现可验证身份（最后页面 {page}）"
        )

    def verify_identity(self, expected_ea_account_id: str) -> EaIdentityFact:
        deadline = time.monotonic() + 20.0
        seen: list[str] = []
        observation: EaObservation | None = None
        while time.monotonic() < deadline:
            hwnd = self._ea_window()
            observation = self._observe(hwnd)
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
                elif match is None:
                    seen.append(mask_identity(identity.ea_account_id))
                    # A badge that reads as a different account is only a
                    # mismatch once the expected id is nowhere in the window.
                    if len(seen) >= 3:
                        self._record("identity-mismatch", observation, observed=seen)
                        raise EaIdentityMismatch(
                            "EA App 当前稳定 EA ID 与租约不一致"
                            f"（观察到 {seen[-1]}）"
                        )
            self.sleep(1.0)
        self._record("identity-timeout", observation, observed=seen or None)
        raise EaIdentityMismatch(
            "EA App 页面没有可验证的稳定 EA ID"
            + (f"（观察到 {seen[-1]}）" if seen else "")
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
        if any(self._process_running(name) for name in APEX_EXECUTABLES):
            return
        hwnd = self._ea_window()
        # The orchestrator verifies the stable EA identity immediately before
        # entering this method.  Do not repeat that check here: the CEF surface
        # occasionally yields an empty OCR frame while it is otherwise fully
        # interactive.  Wait for the actual launch control instead.
        for _ in range(15):
            observation = self._observe(hwnd)
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
        for _ in range(15):
            if any(self._process_running(name) for name in APEX_EXECUTABLES):
                return
            observation = self._observe(hwnd)
            point = self._anchor(
                observation,
                ("play", "launch", "launchgame", "startgame", "开始游戏"),
                x_range=(0.35, 0.75),
                y_range=(0.30, 0.70),
                exact=True,
            )
            if point is not None:
                self._record("apex-play", observation)
                self._click_point(hwnd, *point)
                break
            download = self._anchor(
                observation,
                ("download", "下载"),
                x_range=(0.35, 0.75),
                y_range=(0.30, 0.70),
                exact=True,
            )
            if download is not None:
                self._record("apex-download-required", observation)
                raise EaApexStartFailed(
                    "EA App 当前账号的 Apex 页面只提供 Download，不能启动已安装游戏"
                )
            library_play = self._installed_library_play_point(observation)
            if library_play is not None:
                self._record("apex-library-play", observation)
                self._click_point(hwnd, *library_play)
                break
            self.sleep(1.0)
        else:
            self._record("apex-play-missing", self._observe(hwnd))
            raise EaApexStartFailed("EA App Apex 页面未找到 Play 按钮")
        deadline = time.monotonic() + 90.0
        while time.monotonic() < deadline:
            if any(self._process_running(name) for name in APEX_EXECUTABLES):
                return
            self.sleep(2.0)
        raise EaApexStartFailed("点击 EA Play 后未发现 Apex 进程")

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
        identity: EaIdentityFact,
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
        badge = self._anchor(
            observation,
            (identity.ea_account_id,),
            y_range=(0.0, 0.20),
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
        return triggers

    def _open_account_menu(
        self,
        hwnd: int,
        identity: EaIdentityFact,
    ) -> tuple[EaObservation, tuple[int, int]] | None:
        for name, point in self._account_menu_triggers(self._observe(hwnd), identity):
            self._click_point(hwnd, *point)
            self.sleep(1.2)
            menu = self._observe(hwnd)
            item = self._anchor(menu, SIGN_OUT_TERMS)
            if item is not None:
                self._record("signout-menu", menu, trigger=name)
                return menu, item
            self._record("signout-menu-missing", menu, trigger=name)
            # Close whatever that click did open before trying the next one.
            self._focus(hwnd)
            self._tap(VK_ESCAPE)
            self.sleep(0.5)
        return None

    def sign_out(self) -> bool:
        hwnd = self._ea_window()
        identity = None
        for _ in range(8):
            observation = self._observe(hwnd)
            # Anything still inside the login flow — the account page, the
            # password page, a verification prompt — means no session was ever
            # established, so there is nothing to sign out of. Failing here
            # blocked the lease from being handed back after a login that
            # stopped at EA's verification step.
            if observation.page in PRE_LOGIN_PAGES:
                self._record("signout-not-signed-in", observation)
                return True
            identity = self._identity(hwnd)
            if identity is not None:
                break
            self.sleep(1.0)
        if identity is None:
            return False
        opened = self._open_account_menu(hwnd, identity)
        if opened is None:
            return False
        _, item = opened
        self._click_point(hwnd, *item)
        deadline = time.monotonic() + 25.0
        confirmed = False
        while time.monotonic() < deadline:
            self.sleep(1.0)
            observation = self._observe(hwnd)
            if observation.page in (EaPage.EMAIL, EaPage.PASSWORD):
                self._record("signed-out", observation)
                return True
            if confirmed:
                continue
            # EA can ask once more before it drops the session.
            confirm = self._anchor(observation, SIGN_OUT_CONFIRM_TERMS)
            if confirm is not None and confirm != item:
                self._record("signout-confirm", observation)
                self._click_point(hwnd, *confirm)
                confirmed = True
        self._record("signout-timeout", self._observe(hwnd))
        return False

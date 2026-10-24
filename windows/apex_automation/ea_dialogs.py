"""The EA App dialogs the Runner answers, in one table.

Each entry names a dialog, how to recognise it, the controls to press in
order, and where in the account cycle that answer is safe. The driver
answers every entry the same way (``WindowsEaHybridDriver._answer_dialog``):

* every control is located before the first press, or nothing is pressed;
* each press is recorded with its screenshot, and the dialog then has to be
  gone from two readable frames in a row before it counts as answered;
* it is pressed at most twice, and only while the same dialog is still on
  screen. A dialog that stays is recorded as stuck and raised.

A dialog EA adds later is one more entry here, not another method on the
driver. A launch dialog nobody has described yet is still read
(``unknown_dialog_text``) so its screenshot and the failure name it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Sequence

from .ea_app import EaApexStartFailed, EaAppAutomationError
from .ea_evidence import is_sensitive_text
from .ea_pages import phrase_point, token_point

Point = tuple[int, int]
# Controls are located only on text read at least this confidently.
TRUSTED = 0.80

# Where in the account cycle an answer is safe.
PREFLIGHT = "preflight"  # before a lease uses the window
LAUNCH = "launch"  # starting Apex for the leased account
SIGNOUT = "signout"  # leaving the account at the end of a lease


@dataclass(frozen=True)
class EaDialog:
    # Evidence steps: <key>-<press>, <key>-dismissed, <key>-stuck,
    # <key>-action-missing.
    key: str
    press: str
    contexts: frozenset[str]
    visible: Callable[..., bool]
    controls: Callable[..., "tuple[Point, ...] | None"]
    notice: str
    missing: str
    stuck: str
    # Its last control starts Apex; the launch wait takes over from there.
    launches: bool = False
    error: type[EaAppAutomationError] = EaAppAutomationError

    def step(self, outcome: str) -> str:
        return f"{self.key}-{outcome}"


def _joined(observation) -> str:
    return "".join(token.normalized for token in observation.tokens)


def _trusted(observation) -> tuple:
    return tuple(token for token in observation.tokens
                 if token.confidence >= TRUSTED and token.roi is not None)


def _contains(observation, terms: Sequence[str]) -> bool:
    joined = _joined(observation)
    return any(term in joined for term in terms)


# --- Failed to upload game data to the cloud (OK) ------------------------------
# Seen after a finished run and at the next start. It only acknowledges the
# error: it does not skip sync, close EA, or prove the session signed out.

CLOUD_UPLOAD_ERROR_TERMS = (
    "failedtouploadgamedatatothecloud",
    "无法将游戏数据上传到云端",
    "无法将游戏数据上传至云端",
)
CLOUD_UPLOAD_LOCAL_SAVE_TERMS = (
    "gameisstillsavedlocally",
    "游戏仍保存在本地",
)


def left_aligned_block(tokens: Sequence, head) -> str:
    """Normalized text of the block whose lines start at `head`'s left edge.

    A dialog wraps its heading and copy when the EA window is narrow, and
    OCR returns side-panel words between those lines. Reading the block by
    its left edge, line by line, keeps a wrapped sentence whole.
    """

    def middle(token):
        return (token.roi[1] + token.roi[3]) / 2

    starts = sorted((
        token for token in tokens
        if abs(token.roi[0] - head.roi[0]) <= 12 and token.roi[1] >= head.roi[1] - 4
    ), key=middle)
    parts: list[str] = []
    for start in starts:
        line, end = [start], start
        while True:
            # A line split into several boxes continues just right of the last one.
            following = [
                token for token in tokens
                if token not in line
                and abs(middle(token) - middle(start)) <= (start.roi[3] - start.roi[1]) * 0.6
                and end.roi[2] - 4 <= token.roi[0] <= end.roi[2] + 40
            ]
            if not following:
                break
            end = min(following, key=lambda token: token.roi[0])
            line.append(end)
        parts.extend(token.normalized for token in line)
    return "".join(parts)


def cloud_upload_notice(observation):
    """The heading token of the upload-failed notice, read even when wrapped."""

    placed = _trusted(observation)
    left, top, right, bottom = observation.rect
    width, height = max(1, right - left), max(1, bottom - top)
    for head in placed:
        if not any(head.normalized.startswith(term[:8]) for term in CLOUD_UPLOAD_ERROR_TERMS):
            continue
        x = ((head.roi[0] + head.roi[2]) / 2 - left) / width
        y = ((head.roi[1] + head.roi[3]) / 2 - top) / height
        if not (0.25 <= x <= 0.80 and 0.25 <= y <= 0.70):
            continue
        text = left_aligned_block(placed, head)
        if (any(term in text for term in CLOUD_UPLOAD_ERROR_TERMS)
                and any(term in text for term in CLOUD_UPLOAD_LOCAL_SAVE_TERMS)):
            return head
    return None


def cloud_upload_error_visible(observation) -> bool:
    return _contains(observation, CLOUD_UPLOAD_ERROR_TERMS) or cloud_upload_notice(observation) is not None


def cloud_upload_error_point(observation) -> Point | None:
    head = cloud_upload_notice(observation)
    if head is None:
        return None
    button = token_point(_trusted(observation), observation.rect, ("ok", "确定"), exact=True,
                         x_range=(0.40, 0.85), y_range=(0.40, 0.90))
    if button is None or button[1] <= (head.roi[1] + head.roi[3]) // 2:
        return None
    return button


# --- Your latest sync was interrupted (Launch game / Cancel) -------------------
# Shown while leaving an account whose launch never finished: cancel the
# pending launch, then still prove the sign-out.

CLOUD_LAUNCH_INTERRUPTED_TERMS = ("yourlatestsyncwasinterrupted",)


def interrupted_launch_cancel_point(observation) -> Point | None:
    placed, rect = _trusted(observation), observation.rect
    title = phrase_point(placed, rect, CLOUD_LAUNCH_INTERRUPTED_TERMS[0],
                         x_range=(0.25, 0.80), y_range=(0.20, 0.65))
    cancel = token_point(placed, rect, ("cancel", "取消"), exact=True,
                         x_range=(0.40, 0.85), y_range=(0.40, 0.85))
    launch = token_point(placed, rect, ("launchgame", "启动游戏", "开始游戏"), exact=True,
                         x_range=(0.35, 0.85), y_range=(0.40, 0.85))
    if title is None or cancel is None or launch is None or cancel[1] <= title[1]:
        return None
    return cancel


# --- We couldn't load your cloud data (Continue with local data) ---------------

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


def continue_local_data_point(observation) -> Point | None:
    """The safe recovery action, only on the cloud-data error."""

    if not _contains(observation, CLOUD_DATA_ERROR_TERMS):
        return None
    return token_point(observation.tokens, observation.rect, CONTINUE_LOCAL_DATA_TERMS,
                       x_range=(0.35, 0.80), y_range=(0.45, 0.90))


# --- Cloud sync failed: choose which save (local / cloud, Confirm) -------------
# 2026-10-23/24, leases 3231, 3235, 3238 and 3241: after Play, EA asks which
# Apex save to keep. Nothing answered, so the launch waited out its time,
# the account was signed out and the next login met the same question.
# The local save is this machine's, the settings the Runner plays with, and
# what "We couldn't load your cloud data" already continues with.

CLOUD_SYNC_FAILED_TERMS = ("cloudsyncfailed",)
CLOUD_SYNC_CHOICE_TERMS = ("choosewhichsave", "couldntbesynced")
CONFIRM_TERMS = ("confirm", "确认", "确定")


def cloud_sync_choice_points(observation) -> tuple[Point, Point] | None:
    # Single tokens, not phrase_point: the game art behind the dialog reads
    # "Apex Legends  APEX" on the option's row, and a joined line would put
    # the click on that art instead of the option.
    placed, rect = _trusted(observation), observation.rect
    title = token_point(placed, rect, CLOUD_SYNC_FAILED_TERMS,
                        x_range=(0.20, 0.80), y_range=(0.10, 0.70))
    local = token_point(placed, rect, ("continuewithlocalsave",),
                        x_range=(0.20, 0.85), y_range=(0.15, 0.85))
    confirm = token_point(placed, rect, CONFIRM_TERMS, exact=True,
                          x_range=(0.35, 0.90), y_range=(0.25, 0.95))
    if title is None or local is None or confirm is None:
        return None
    width = max(1, rect[2] - rect[0])
    # Stacked as EA draws it: title, the option, then Confirm, all one dialog.
    if not (title[1] < local[1] < confirm[1]
            and abs(local[0] - title[0]) <= width * 0.25
            and abs(confirm[0] - local[0]) <= width * 0.35):
        return None
    return local, confirm


def _single(point_of: Callable[..., Point | None]) -> Callable[..., "tuple[Point, ...] | None"]:
    def controls(observation):
        point = point_of(observation)
        return None if point is None else (point,)
    return controls


CLOUD_UPLOAD_FAILED = EaDialog(
    key="cloud-upload-error", press="ack",
    contexts=frozenset({PREFLIGHT, LAUNCH, SIGNOUT}),
    visible=cloud_upload_error_visible,
    controls=_single(cloud_upload_error_point),
    notice="EA 云端数据上传失败，已确认提示，正在核对弹窗是否关闭",
    missing="EA 云端上传失败提示未找到可信 OK 按钮",
    stuck="EA 云端上传失败提示未能确认关闭，已停止点击",
)
CLOUD_LAUNCH_INTERRUPTED = EaDialog(
    key="signout-cloud-launch", press="cancel",
    contexts=frozenset({SIGNOUT}),
    visible=lambda observation: _contains(observation, CLOUD_LAUNCH_INTERRUPTED_TERMS),
    controls=_single(interrupted_launch_cancel_point),
    notice="EA 上次启动的云同步被中断，已取消这次启动",
    missing="EA 云同步中断弹窗未找到可信取消按钮",
    stuck="EA 云同步中断弹窗未能确认关闭",
)
CLOUD_SYNC_CHOICE = EaDialog(
    key="apex-cloud-sync-choice", press="local",
    contexts=frozenset({LAUNCH}),
    visible=lambda observation: (_contains(observation, CLOUD_SYNC_FAILED_TERMS)
                                 and _contains(observation, CLOUD_SYNC_CHOICE_TERMS)),
    controls=cloud_sync_choice_points,
    notice="EA 云同步失败，选择本机存档并确认，继续启动 Apex",
    missing="EA 云同步失败弹窗未找到可信的本机存档和确认按钮",
    stuck="EA 云同步失败弹窗在两次选择本机存档后仍未关闭",
    launches=True,
    error=EaApexStartFailed,
)
# Without its Continue control this error used to be left to the launch wait,
# so it only counts as this dialog when the control is there.
CLOUD_DATA_ERROR = EaDialog(
    key="apex-cloud-data", press="local",
    contexts=frozenset({LAUNCH}),
    visible=lambda observation: continue_local_data_point(observation) is not None,
    controls=_single(continue_local_data_point),
    notice="EA 云存档不可用，选择本地数据继续启动 Apex",
    missing="EA 云存档错误未找到本地数据按钮",
    stuck="EA App 云存档错误在两次本地数据恢复后仍未关闭",
    launches=True,
    error=EaApexStartFailed,
)

DIALOGS: tuple[EaDialog, ...] = (
    CLOUD_UPLOAD_FAILED,
    CLOUD_LAUNCH_INTERRUPTED,
    CLOUD_SYNC_CHOICE,
    CLOUD_DATA_ERROR,
)


def dialogs_for(context: str) -> tuple[EaDialog, ...]:
    return tuple(dialog for dialog in DIALOGS if context in dialog.contexts)


def known_dialog(observation, context: str) -> EaDialog | None:
    return next((dialog for dialog in dialogs_for(context) if dialog.visible(observation)), None)


# --- Dialogs nobody has described yet -----------------------------------------

DIALOG_BUTTON_TERMS = frozenset({
    "confirm", "cancel", "ok", "close", "continue", "retry", "tryagain", "yes", "no",
    "确认", "确定", "取消", "关闭", "继续", "重试", "是", "否",
})


def unknown_dialog_text(observation, *, limit: int = 160) -> str | None:
    """The text of a modal dialog with buttons, read from above its buttons.

    Used only to name what EA showed in evidence and in the failure, never to
    press anything.
    """

    placed = _trusted(observation)
    left, top, right, bottom = observation.rect
    width, height = max(1, right - left), max(1, bottom - top)

    def centre(token):
        return ((token.roi[0] + token.roi[2]) / 2 - left) / width, ((token.roi[1] + token.roi[3]) / 2 - top) / height

    buttons = [token for token in placed if token.normalized in DIALOG_BUTTON_TERMS
               and 0.30 <= centre(token)[0] <= 0.95 and 0.30 <= centre(token)[1] <= 0.95]
    if not buttons:
        return None
    x1 = min(token.roi[0] for token in buttons) - width * 0.45
    x2 = max(token.roi[2] for token in buttons) + width * 0.05
    y1 = min(token.roi[1] for token in buttons) - height * 0.40
    y2 = max(token.roi[3] for token in buttons)
    lines = sorted((
        token for token in placed
        if token not in buttons and token.roi[0] >= x1 and token.roi[2] <= x2
        and token.roi[1] >= y1 and token.roi[3] <= y2 and not is_sensitive_text(token.text)
    ), key=lambda token: (token.roi[1], token.roi[0]))
    text = " / ".join(token.text for token in lines)
    return text[:limit] or None

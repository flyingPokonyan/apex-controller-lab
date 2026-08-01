"""Pure page facts for the EA App login surface.

The hybrid driver only ever sees the EA CEF surface as a bag of normalized OCR
words, and every login regression so far lived in how those words were read,
not in the ctypes around them. Keeping the rules here — importable on any
platform — is what makes them testable without a Windows box in front of EA.
"""

from __future__ import annotations

from enum import Enum
import re
from typing import Iterable, Sequence


class EaPage(str, Enum):
    EMAIL = "EMAIL"
    PASSWORD = "PASSWORD"
    OTP_METHOD = "OTP_METHOD"
    OTP = "OTP"
    CAPTCHA = "CAPTCHA"
    EXPIRED_SESSION = "EXPIRED_SESSION"
    SIGNED_IN = "SIGNED_IN"
    UNKNOWN = "UNKNOWN"


# The account field advertises itself with its placeholder. A bare "eaid" is
# not enough: the password page keeps talking about the EA account too.
ACCOUNT_FIELD_TERMS = (
    "emailoreaid",
    "emailaddress",
    "邮箱或eaid",
    "电子邮件或eaid",
    "电子邮件地址",
    "邮箱地址",
)

PASSWORD_FIELD_TERMS = ("password", "密码")

# Every page that offers a password reset link mentions the word "password"
# without holding a password field. Removing the link text first is what keeps
# the email page from being read as the password page.
PASSWORD_LINK_TERMS = (
    "forgotyourpassword",
    "forgotpassword",
    "resetyourpassword",
    "resetpassword",
    "忘记密码",
    "忘记了密码",
    "重置密码",
)

# "Verify your identity" — the method chooser EA raises on a new device. It
# offers the account's email first and the authenticator second, and the
# button underneath sends a code by whichever one is selected.
OTP_METHOD_TERMS = (
    "verifyyouridentity",
    "usemyappauthenticator",
    "sendcode",
    "验证你的身份",
    "使用验证器",
)

AUTHENTICATOR_TERMS = ("usemyappauthenticator", "appauthenticator", "验证器")

SEND_CODE_TERMS = ("sendcode", "continue", "next", "发送验证码", "继续")

OTP_TERMS = (
    "verificationcode",
    "securitycode",
    "logincode",
    "enterthecode",
    "enteryourcode",
    "enter6digitcode",
    "rememberthisdevice",
    "安全代码",
    "验证代码",
    "验证码",
)

OTP_FIELD_TERMS = ("enter6digitcode", "digitcode", "code", "验证码")

# The code page reached through the email option. A generated TOTP will never
# match it, so recognising it is the difference between a clear failure and
# burning attempts against a code that cannot be right.
EMAIL_CODE_TERMS = (
    "wesentacodeto",
    "checkyourspamfolder",
    "requestaresend",
    "promotionstab",
)

CAPTCHA_TERMS = (
    "captcha",
    "hcaptcha",
    "recaptcha",
    "imnotarobot",
    "验证码图片",
    "我不是机器人",
)

EXPIRED_SESSION_TERMS = (
    "sessionhasexpired",
    "backtosignin",
    "会话已过期",
    "登录已过期",
)

# Two independent markers are required, so a single stray word on the login
# page cannot promote it to the signed-in surface.
SIGNED_IN_TERMS = (
    "store",
    "library",
    "browse",
    "friends",
    "downloads",
    "achievements",
    "playlist",
    "商店",
    "游戏库",
    "浏览",
    "好友",
    "下载",
    "成就",
)

LOGIN_ERROR_TERMS = (
    "incorrect",
    "invalidcredentials",
    "cantfindanaccount",
    "couldntfindanaccount",
    "somethingwentwrong",
    "toomanyattempts",
    "账号或密码",
    "密码不正确",
    "密码错误",
    "无法找到",
    "出了点问题",
    "尝试次数过多",
)

SUBMIT_TERMS = ("next", "continue", "signin", "login", "下一步", "继续", "登录")

SIGN_OUT_TERMS = ("signout", "logout", "退出登录", "登出", "注销")

# The confirmation EA can raise between the menu item and the login page.
SIGN_OUT_CONFIRM_TERMS = ("signout", "logout", "confirm", "yes", "确认", "确定")

# RapidOCR reliably confuses these glyph pairs on the EA App's condensed font.
# Folding both sides of a comparison keeps a real account id from being
# rejected, and stays far away from the fuzzy matching that could accept a
# different account.
_IDENTITY_FOLD = str.maketrans({"0": "o", "1": "l", "5": "s", "8": "b"})

_IDENTITY_CANDIDATE = re.compile(r"[a-z0-9]{8,20}")


def compact_text(normalized_tokens: Iterable[str]) -> str:
    """Join normalized tokens without separators.

    OCR splits one label across token boundaries at unpredictable places, so
    the driver matches against the concatenation rather than per token.
    """

    return "".join(normalized_tokens)


def matched_terms(compact: str, terms: Sequence[str]) -> tuple[str, ...]:
    return tuple(term for term in terms if term in compact)


def has_any(compact: str, terms: Sequence[str]) -> bool:
    return any(term in compact for term in terms)


def strip_password_links(compact: str) -> str:
    for term in PASSWORD_LINK_TERMS:
        compact = compact.replace(term, "")
    return compact


def has_password_field(compact: str) -> bool:
    return has_any(strip_password_links(compact), PASSWORD_FIELD_TERMS)


def has_account_field(compact: str) -> bool:
    return has_any(compact, ACCOUNT_FIELD_TERMS)


def is_login_error(compact: str) -> bool:
    return has_any(compact, LOGIN_ERROR_TERMS)


def classify_page(normalized_tokens: Iterable[str]) -> EaPage:
    compact = compact_text(normalized_tokens)
    if has_any(compact, CAPTCHA_TERMS):
        return EaPage.CAPTCHA
    if has_any(compact, EXPIRED_SESSION_TERMS):
        return EaPage.EXPIRED_SESSION
    # The chooser talks about codes too, so it has to be settled before the
    # code-entry page: typing into it would go nowhere.
    if has_any(compact, OTP_METHOD_TERMS):
        return EaPage.OTP_METHOD
    if has_any(compact, OTP_TERMS):
        return EaPage.OTP
    # The account field wins a tie: during the page transition both fields can
    # be on screen for a frame or two, and the caller must keep waiting rather
    # than type a password into the field it can still see.
    if has_account_field(compact):
        return EaPage.EMAIL
    if has_password_field(compact):
        return EaPage.PASSWORD
    if len(set(matched_terms(compact, SIGNED_IN_TERMS))) >= 2:
        return EaPage.SIGNED_IN
    return EaPage.UNKNOWN


def password_page_blocker(normalized_tokens: Iterable[str]) -> str:
    """Why this frame is not accepted as the password page.

    The two answers need different fixes — a click that never submitted versus
    a password page that keeps the account label on screen — and the timeout
    message is the only place most runs will ever say which one happened.
    """

    compact = compact_text(normalized_tokens)
    account = has_account_field(compact)
    password = has_password_field(compact)
    if account and password:
        return "ACCOUNT_LABEL_STILL_VISIBLE"
    if account:
        return "STILL_ON_ACCOUNT_PAGE"
    if password:
        return ""
    return "NO_PASSWORD_FIELD"


def page_markers(normalized_tokens: Iterable[str]) -> tuple[str, ...]:
    """Known UI markers seen in this frame, for redaction-safe evidence."""

    compact = compact_text(normalized_tokens)
    seen: list[str] = []
    for name, terms in (
        ("account", ACCOUNT_FIELD_TERMS),
        ("otpMethod", OTP_METHOD_TERMS),
        ("authenticator", AUTHENTICATOR_TERMS),
        ("emailCode", EMAIL_CODE_TERMS),
        ("otp", OTP_TERMS),
        ("captcha", CAPTCHA_TERMS),
        ("expired", EXPIRED_SESSION_TERMS),
        ("submit", SUBMIT_TERMS),
        ("error", LOGIN_ERROR_TERMS),
        ("nav", SIGNED_IN_TERMS),
    ):
        if has_any(compact, terms):
            seen.append(name)
    if has_password_field(compact):
        seen.append("password")
    if has_any(compact, PASSWORD_LINK_TERMS):
        seen.append("passwordLink")
    return tuple(seen)


# Window chrome that happens to look like an account id once OCR strips the
# punctuation: "Friends 0/2" normalizes to "friends02", nine alphanumerics
# sitting in the same corner as the signed-in badge.
UI_CHROME_WORDS = (
    "friends",
    "library",
    "browse",
    "store",
    "search",
    "download",
    "notification",
    "achievement",
    "installed",
    "playlist",
)


def is_ui_chrome(candidate: str) -> bool:
    return any(word in candidate for word in UI_CHROME_WORDS)


def fold_identity(value: str) -> str:
    return value.strip().lower().translate(_IDENTITY_FOLD)


def identity_matches(expected: str, observed: str) -> bool:
    if not expected or not observed:
        return False
    return fold_identity(expected) == fold_identity(observed)


def identity_candidates(normalized_tokens: Iterable[str]) -> tuple[str, ...]:
    """Account-id shaped runs, longest first."""

    found: list[str] = []
    for token in normalized_tokens:
        for candidate in _IDENTITY_CANDIDATE.findall(token):
            if any(character.isalpha() for character in candidate):
                found.append(candidate)
    return tuple(sorted(set(found), key=len, reverse=True))


def mask_identity(value: str) -> str:
    """Enough of an account id to compare runs by eye, never the whole id."""

    if len(value) <= 8:
        return f"{value[:2]}...{value[-2:]}" if len(value) > 4 else "..."
    return f"{value[:4]}...{value[-4:]}"

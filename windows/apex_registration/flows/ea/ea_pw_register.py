#!/usr/bin/env python3
"""Playwright EA (juno) signup — alternative engine to try_ea_once protocol path.

Driven by the same backend env contract as ``try_ea_once.py``:
  - EA_EMAIL / ACCOUNT_FLOW_EMAIL_POOL
  - EA_PROXY / EA_REGISTER_COUNTRY
  - ACCOUNT_FLOW_OUTPUT_DIR

Writes ``protocol_register_result.json`` with the same success gates so
``ScriptFlowRunner.register_ea`` can swap engines via
``GAME_ACCOUNTS_EA_REGISTER_ENGINE=playwright`` without touching API/store.
"""

from __future__ import annotations

import json
import math
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright


from apex_registration.utils.bitbrowser import BitBrowserClient, uses_bitbrowser  # noqa: E402
from apex_registration.utils.roxybrowser import RoxyBrowserClient, roxy_host, uses_roxybrowser_engine  # noqa: E402
from apex_registration.utils.browser_profile import (  # noqa: E402
    PLAYWRIGHT_CHROME_ARGS,
    pick_register_country,
    playwright_context_options,
    playwright_proxy,
    rand_gamer_id,
    save_page_screenshot,
)

# Reuse protocol helpers so password/id/email/Graph semantics stay identical.
from . import try_ea_once as protocol  # noqa: E402

ROOT = Path(__file__).resolve().parent
ARTIFACT_ROOT = Path(os.environ.get("ACCOUNT_FLOW_OUTPUT_DIR") or ROOT)
OUT_LOG = ARTIFACT_ROOT / "last_run.log"
OUT_RESULT = ARTIFACT_ROOT / "protocol_register_result.json"
OUT_OK = ARTIFACT_ROOT / "accounts_succ.txt"
OUT_FAIL = ARTIFACT_ROOT / "protocol_register_failures.jsonl"
LOGIN_TARGET = "https://myaccount.ea.com/am/ui/security-privacy"

NEXT_RE = re.compile(
    r"^(Next|Sign In|Log In|下一步|继续|繼續|登录|登入|次へ|Create|创建|創建|Submit|提交)\b",
    re.I,
)
EA_HOME_URL = "https://www.ea.com/"
CREATE_ACCOUNT_RE = re.compile(
    r"^(Create an account|建立帳戶|建立帳號|創建帳戶|创建账户|アカウントを作成)$",
    re.I,
)
ACCOUNT_MENU_RE = re.compile(
    r"^(Account|Sign in|登入|登录|ログイン|帳號|账号|帳戶|账户|アカウント)$",
    re.I,
)
ACCOUNT_BUTTON_SELECTORS = (
    "nav#network-nav button[class*='NetworkNav_imageItem']",
    "button[class*='NetworkNav_imageItem']",
    "button[aria-label*='account' i]",
    "a[aria-label*='account' i]",
    "button[aria-label*='sign in' i]",
    "button[aria-label*='登入']",
    "button[aria-label*='ログイン']",
)
CAPTCHA_RE = re.compile(r"recaptcha|hcaptcha|data-sitekey|6L[0-9A-Za-z_-]{20,}", re.I)
NET_ERROR_RE = re.compile(r"net::(ERR_[A-Z_0-9]+)")
EMAIL_VERIFY_STEP_RE = re.compile(
    r"mandatoryemailverification|mevemailcollection|emailverify|email.?verify",
    re.I,
)
EMAIL_VERIFY_TEXT_RE = re.compile(
    r"驗證您的電子郵件|验证您的电子邮件|輸入您的代碼|输入您的代码|"
    r"verify your email|enter (your )?code|verification code",
    re.I,
)
EMAIL_VERIFY_READY_LOCATOR = (
    "#emailVerifyCode-1, input[name=emailVerifyCode], "
    "input[id^=emailVerifyCode], input[name^=emailVerifyCode], "
    "input.otp-input, input[maxlength='1']"
)


def log(msg: str) -> None:
    safe = re.sub(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        "[EMAIL]",
        str(msg),
        flags=re.I,
    )
    safe = re.sub(
        r"([?&](?:code|token|state|sid|sessionid)=)[^&\s]+",
        r"\1[REDACTED]",
        safe,
        flags=re.I,
    )
    line = f"[{time.strftime('%H:%M:%S')}] {safe}"
    print(line, flush=True)
    with OUT_LOG.open("a") as handle:
        handle.write(line + "\n")


def safe_url(value: str) -> str:
    try:
        parsed = urlparse(str(value))
        if parsed.scheme and parsed.netloc:
            return parsed._replace(params="", query="", fragment="").geturl()
    except Exception:
        pass
    return str(value).split("?", 1)[0]


def screenshot(page, name: str) -> None:
    dest = ARTIFACT_ROOT / name
    if not save_page_screenshot(page, dest, full_page=True, timeout=2500):
        log(f"screenshot {name} failed")


EA_COOKIE_URLS = (
    "https://accounts.ea.com/",
    "https://signin.ea.com/",
    "https://www.ea.com/",
    "https://myaccount.ea.com/",
    "https://www.origin.com/",
)


def _cookie_bags(context, page=None) -> list[dict]:
    """All cookie jars Playwright can see, including HttpOnly via CDP.

    ``document.cookie`` / F12 Application cookies include HttpOnly remid.
    Playwright's default ``context.cookies()`` should too, but Roxy CDP
    sometimes omits partitioned or other-origin EA cookies unless we also
    query explicit URLs and ``Network.getAllCookies``.
    """
    rows: list[dict] = []

    def add(bag) -> None:
        if isinstance(bag, dict):
            bag = bag.get("cookies")
        if not isinstance(bag, list):
            return
        rows.extend(item for item in bag if isinstance(item, dict))

    try:
        add(context.cookies())
    except Exception:
        pass
    try:
        add(context.cookies(list(EA_COOKIE_URLS)))
    except Exception:
        pass
    cdp_page = page
    if cdp_page is None:
        for candidate in getattr(context, "pages", None) or []:
            try:
                if not candidate.is_closed():
                    cdp_page = candidate
                    break
            except Exception:
                continue
    if cdp_page is not None:
        try:
            session = context.new_cdp_session(cdp_page)
            add(session.send("Network.getAllCookies"))
        except Exception:
            pass
    return rows


def capture_connect_cookies(context, page=None) -> dict | None:
    sid = remid = None
    remid_expires_at = None
    for cookie in _cookie_bags(context, page=page):
        name = str(cookie.get("name") or "").lower()
        value = cookie.get("value")
        if not value:
            continue
        if name == "sid":
            sid = value
        elif name == "remid":
            remid = value
            expires = cookie.get("expires")
            try:
                if expires and float(expires) > 0:
                    remid_expires_at = time.strftime(
                        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(expires))
                    )
            except (TypeError, ValueError):
                remid_expires_at = None
    if not sid and not remid:
        return None
    return {"sid": sid, "remid": remid, "remid_expires_at": remid_expires_at}


EA_AUTH_COOKIE_NAMES = frozenset({
    "sid",
    "remid",
    "osc",
    "signin-cookie",
    "jsessionid",
    "pim-session-id",
})
EA_KEEP_COOKIE_NAMES = frozenset({"ealocale"})


def _ea_cookie_host(domain: str) -> bool:
    host = str(domain or "").lstrip(".").lower()
    return (
        host == "ea.com"
        or host.endswith(".ea.com")
        or host == "origin.com"
        or host.endswith(".origin.com")
    )


def drop_ea_auth_cookies(context, page=None) -> int:
    """Forget the EA SPA session so the next login can issue remid.

    Dropping only ``sid``/``remid`` leaves ``PIM-SESSION-ID`` / ``osc`` /
    ``signin-cookie`` in place, so myaccount still returns 200 and the
    remember-me retry never actually logs in again.
    """
    dropped = 0
    seen: set[tuple[str, str, str]] = set()
    for cookie in _cookie_bags(context, page=page):
        name = str(cookie.get("name") or "")
        domain = str(cookie.get("domain") or "")
        path = str(cookie.get("path") or "/")
        key = (name, domain, path)
        if key in seen:
            continue
        seen.add(key)
        lowered = name.lower()
        if lowered in EA_KEEP_COOKIE_NAMES:
            continue
        if lowered not in EA_AUTH_COOKIE_NAMES and not _ea_cookie_host(domain):
            continue
        try:
            context.clear_cookies(name=name, domain=domain, path=path)
            dropped += 1
        except Exception:
            continue
    return dropped


def recapture_connect_cookies(context, *, page=None, attempts: int = 5) -> dict | None:
    """Wait briefly for EA to issue remid after a completed login."""
    payload = None
    for attempt in range(max(1, attempts)):
        payload = capture_connect_cookies(context, page=page)
        if payload and payload.get("remid"):
            persist_connect_cookies(payload)
            log("captured EA remid")
            return payload
        if page is not None:
            try:
                page.wait_for_timeout(1200)
            except Exception:
                time.sleep(1.2)
        else:
            time.sleep(1.2)
    if payload:
        names = sorted({str(c.get("name") or "") for c in _cookie_bags(context, page=page)})
        log(f"WARN remid missing after login cookies={names[:24]}")
        persist_connect_cookies(payload)
    return payload


def persist_connect_cookies(payload: dict | None) -> None:
    if not payload:
        return
    path = ARTIFACT_ROOT / "ea_connect_cookies.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    path.chmod(0o600)


def write_result(payload: dict) -> None:
    history = OUT_OK if payload.get("ok_guess") else OUT_FAIL
    with history.open("a") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    history.chmod(0o600)
    OUT_RESULT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    )
    OUT_RESULT.chmod(0o600)


def failure_result(email: str, reason: str, **details) -> dict:
    payload = {
        "email": email,
        "password": details.pop("password", None),
        "originId": details.pop("originId", None),
        "final_url": details.pop("final_url", None),
        "final_step": details.pop("final_step", None),
        "ok_guess": False,
        "account_login_verified": False,
        "account_profile_completed": False,
        "failure_reason": reason,
        "connect_cookies": None,
    }
    payload.update(details)
    write_result(payload)
    return payload


def page_blob(page) -> str:
    try:
        return page.content()
    except Exception:
        return ""


def visible_text(page) -> str:
    try:
        return re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=5000))[:800]
    except Exception:
        return ""


def net_error_reason(exc: BaseException | str) -> str | None:
    match = NET_ERROR_RE.search(str(exc))
    return match.group(1) if match else None


def human_delay(min_s: float = 0.6, max_s: float = 1.6) -> None:
    """Pause like a person: clustered near the low-middle, not a flat uniform."""
    lo, hi = (min_s, max_s) if min_s <= max_s else (max_s, min_s)
    mode = lo + (hi - lo) * 0.35
    time.sleep(random.triangular(lo, hi, mode))


def cubic_bezier(
    p0: tuple[float, float],
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    u = 1.0 - t
    return (
        (u**3) * p0[0] + 3 * (u**2) * t * p1[0] + 3 * u * (t**2) * p2[0] + (t**3) * p3[0],
        (u**3) * p0[1] + 3 * (u**2) * t * p1[1] + 3 * u * (t**2) * p2[1] + (t**3) * p3[1],
    )


def _mouse_pos(page) -> tuple[float, float]:
    pos = getattr(page, "_apex_mouse", None)
    if pos:
        return pos
    try:
        vp = page.viewport_size or {"width": 1280, "height": 720}
    except Exception:
        vp = {"width": 1280, "height": 720}
    return (float(vp.get("width") or 1280) * 0.42, float(vp.get("height") or 720) * 0.38)


def human_move_to(page, x: float, y: float) -> None:
    """Move the pointer along a short bezier curve instead of teleporting."""
    start = _mouse_pos(page)
    dist = math.hypot(x - start[0], y - start[1])
    if dist < 5:
        try:
            page.mouse.move(x, y)
        except Exception:
            pass
        page._apex_mouse = (x, y)
        return
    mid_x = (start[0] + x) / 2
    mid_y = (start[1] + y) / 2
    spread = max(18.0, dist * 0.22)
    c1 = (
        start[0] + (x - start[0]) * 0.28 + random.uniform(-spread, spread),
        start[1] + (y - start[1]) * 0.12 + random.uniform(-spread, spread),
    )
    c2 = (
        mid_x + random.uniform(-spread, spread),
        mid_y + random.uniform(-spread * 0.6, spread * 0.6),
    )
    steps = max(12, min(36, int(dist / 14)))
    try:
        for i in range(1, steps + 1):
            t = i / steps
            eased = t * t * (3.0 - 2.0 * t)
            px, py = cubic_bezier(start, c1, c2, (x, y), eased)
            page.mouse.move(px, py)
            time.sleep(random.uniform(0.003, 0.011))
    except Exception:
        try:
            page.mouse.move(x, y)
        except Exception:
            pass
    page._apex_mouse = (x, y)


def locator_is_blocked(loc) -> bool:
    """True when a control is visible but not a real user click target yet."""
    try:
        if loc.get_attribute("disabled") is not None:
            return True
        aria = (loc.get_attribute("aria-disabled") or "").strip().lower()
        if aria in {"true", "1"}:
            return True
        cls = (loc.get_attribute("class") or "").lower()
        tokens = set(cls.replace("_", " ").split())
        if tokens.intersection({"disabled", "is-disabled", "otkbtn-disabled"}):
            return True
        if hasattr(loc, "is_enabled") and not loc.is_enabled():
            return True
    except Exception:
        return False
    return False


def _as_locator(page, selector_or_locator):
    if isinstance(selector_or_locator, str):
        return page.locator(selector_or_locator).first
    return selector_or_locator


def human_click(page, selector_or_locator, timeout: int = 8000) -> bool:
    """Move to a visible, enabled control and click it without force=True."""
    loc = _as_locator(page, selector_or_locator)
    loc.wait_for(state="visible", timeout=timeout)
    deadline = time.monotonic() + max(0.4, timeout / 1000)
    while time.monotonic() < deadline and locator_is_blocked(loc):
        time.sleep(0.2)
    if locator_is_blocked(loc):
        return False
    try:
        box = loc.bounding_box()
    except Exception:
        box = None
    if box and box.get("width") and box.get("height"):
        x = box["x"] + box["width"] * random.uniform(0.28, 0.72)
        y = box["y"] + box["height"] * random.uniform(0.32, 0.68)
        human_move_to(page, x, y)
        human_delay(0.16, 0.45)
        try:
            page.mouse.down()
            human_delay(0.08, 0.22)
            page.mouse.up()
            return True
        except Exception:
            pass
    try:
        loc.click(timeout=min(timeout, 4000))
        return True
    except Exception:
        return False


LOGIN_PASSWORD_ERROR_RE = re.compile(
    r"憑證不正確|凭证不正确|不正确或已过期|incorrect or expired|"
    r"invalid credentials|wrong password|password is incorrect",
    re.I,
)


def login_password_rejected(page) -> bool:
    try:
        if LOGIN_PASSWORD_ERROR_RE.search(visible_text(page) or ""):
            return True
    except Exception:
        pass
    selectors = (
        ".otkform-group-haserror",
        "p.otkinput-errormsg",
        ".otkinput-errormsg",
        "[role=alert]",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                if LOGIN_PASSWORD_ERROR_RE.search(loc.inner_text() or ""):
                    return True
        except Exception:
            continue
    return False


def fill_secret(page, selector_or_locator, text: str) -> bool:
    """Set a password exactly.

    Sequential typing under the zh-HK Roxy IME drops or mutates ``!@#$%``,
    so the value stored at create time does not match the later login.
    """
    loc = _as_locator(page, selector_or_locator)
    loc.wait_for(state="visible", timeout=15000)
    try:
        loc.click(timeout=4000)
    except Exception:
        try:
            loc.click(timeout=2000, force=True)
        except Exception:
            pass
    human_delay(0.08, 0.2)
    payload = str(text)
    try:
        human_type(page, loc, payload)
    except Exception:
        pass
    try:
        if loc.input_value() == payload:
            return True
    except Exception:
        pass
    try:
        loc.fill("")
        loc.fill(payload)
    except Exception:
        pass
    try:
        loc.evaluate(
            """(el, value) => {
                const proto = window.HTMLInputElement && HTMLInputElement.prototype;
                const setter = proto && Object.getOwnPropertyDescriptor(proto, 'value') &&
                    Object.getOwnPropertyDescriptor(proto, 'value').set;
                if (setter) setter.call(el, value);
                else el.value = value;
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return el.value;
            }""",
            payload,
        )
    except Exception:
        pass
    try:
        current = loc.input_value()
    except Exception:
        current = None
    if current != payload:
        log(
            f"WARN password field len={len(current or '')} expected={len(payload)}"
        )
        return False
    return True


def human_type(
    page, selector_or_locator, text: str, min_delay: int = 110, max_delay: int = 280
) -> bool:
    """Click the field like a person, then type with uneven keystroke gaps."""
    loc = _as_locator(page, selector_or_locator)
    loc.wait_for(state="visible", timeout=15000)
    if not human_click(page, loc, timeout=8000):
        try:
            loc.click()
        except Exception:
            pass
    human_delay(0.2, 0.55)
    payload = str(text)
    index = 0
    while index < len(payload):
        step = 2 if random.random() < 0.05 and index + 1 < len(payload) else 1
        loc.press_sequentially(
            payload[index:index + step],
            delay=random.randint(min_delay, max_delay),
        )
        index += step
        if index < len(payload) and random.random() < 0.16:
            human_delay(0.35, 1.05)
    return True


def click_login_or_next(page, timeout: int = 8000) -> bool:
    """Prefer the juno login control (登入/下一步), then the generic Next/Submit."""
    try:
        from . import ea_pw_link_2fa as pw_link

        if pw_link.click_login_action(page, timeout=timeout):
            return True
    except Exception:
        pass
    return click_next(page, timeout=timeout)


def _login_field_visible(page, selector: str) -> bool:
    try:
        loc = page.locator(selector).first
        return bool(loc.count() and loc.is_visible())
    except Exception:
        return False


def login_credentials_visible(page) -> bool:
    return _login_field_visible(page, "#email, input[name=email]") or _login_field_visible(
        page, "#password, input[name=password], input[type=password]"
    )


def click_login_continue(page, timeout: int = 8000) -> bool:
    """Advance a credential-less juno/login interstitial (Steam callback).

    Avoid ``click_next`` here: that regex also matches Create Account.
    """
    try:
        from . import ea_pw_link_2fa as pw_link

        if pw_link.click_login_action(page, timeout=timeout):
            return True
    except Exception:
        pass
    for selector in ("#continue", "a#continue[role=button]", "a#continue"):
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible() and human_click(page, loc, timeout=timeout):
                return True
        except Exception as exc:
            log(f"login continue {selector} failed: {exc}")
            continue
    return False


class LoginFormError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def complete_ea_login_form(
    page, email: str, password: str, *, timeout_ms: int = 25000,
    require_transition: bool = False,
) -> bool:
    """Fill EA email then password as the juno steps appear.

    After 下一步 the password field can take well over 2s. A single snapshot
    miss used to leave the form on 輸入密碼 with an empty box.

    Steam's OAuth callback can also park on juno/login with only Continue,
    no email/password fields. Click that interstitial in place so we do
    not drop the linking state by navigating away.
    """
    deadline = time.monotonic() + max(1.0, timeout_ms / 1000)
    filled_email = False
    filled_password = False
    last_bare_continue = 0.0
    email_sel = "#email, input[name=email]"
    password_sel = "#password, input[name=password], input[type=password]"
    while time.monotonic() < deadline:
        if on_myaccount(page):
            return True
        if require_transition:
            from . import ea_pw_link_2fa as pw_link
            if pw_link.is_login_otp_page(page_blob(page), page.url, page.title()):
                return True
        email_visible = _login_field_visible(page, email_sel)
        password_visible = _login_field_visible(page, password_sel)
        if (
            not filled_email
            and not filled_password
            and not email_visible
            and not password_visible
            and (not last_bare_continue or time.monotonic() - last_bare_continue >= 2.5)
        ):
            if click_login_continue(page, timeout=1500):
                last_bare_continue = time.monotonic()
                log("EA login continue clicked without credential fields")
                page.wait_for_timeout(800)
                continue
        try:
            email_box = page.locator(email_sel).first
            if (
                not filled_email
                and email_box.count()
                and email_box.is_visible()
            ):
                human_type(page, email_box, email)
                if email_box.input_value().strip().casefold() != email.casefold():
                    email_box.fill(email)
                if email_box.input_value().strip().casefold() != email.casefold():
                    raise LoginFormError("ea_login_email_fill_failed", "EA 登录邮箱未正确填入")
                filled_email = True
                log("EA login email filled")
                try:
                    from . import ea_pw_link_2fa as pw_link

                    pw_link.ensure_remember_device(page)
                except Exception:
                    pass
                human_delay(0.4, 0.9)
                if not click_login_or_next(page):
                    raise LoginFormError("ea_login_submit_failed", "EA 登录邮箱提交未完成")
                page.wait_for_timeout(400)
        except LoginFormError:
            raise
        except Exception as exc:
            log(f"login email fill: {type(exc).__name__}")
        try:
            password_box = page.locator(password_sel).first
            if (
                not filled_password
                and password_box.count()
                and password_box.is_visible()
            ):
                if not fill_secret(page, password_box, password):
                    raise LoginFormError("ea_login_password_fill_failed", "EA 登录密码未正确填入")
                filled_password = True
                log("EA login password filled")
                try:
                    from . import ea_pw_link_2fa as pw_link

                    pw_link.ensure_remember_device(page)
                except Exception:
                    pass
                human_delay(0.4, 0.9)
                if not click_login_or_next(page):
                    raise LoginFormError("ea_login_submit_failed", "EA 登录密码提交未完成")
                page.wait_for_timeout(1200)
                if login_password_rejected(page):
                    log("WARN EA login rejected password; retrying exact fill")
                    try:
                        password_box = page.locator(password_sel).first
                        if not fill_secret(page, password_box, password):
                            raise LoginFormError("ea_login_password_fill_failed", "EA 登录密码未正确填入")
                    except LoginFormError:
                        raise
                    except Exception:
                        pass
                    try:
                        from . import ea_pw_link_2fa as pw_link

                        pw_link.ensure_remember_device(page)
                    except Exception:
                        pass
                    human_delay(0.3, 0.6)
                    if not click_login_or_next(page):
                        raise LoginFormError("ea_login_submit_failed", "EA 登录密码重试未提交")
                    page.wait_for_timeout(1200)
                    if login_password_rejected(page):
                        raise LoginFormError("ea_login_credentials_rejected", "EA 拒绝登录凭据，注册结果仍待确认")
                if not require_transition:
                    return True
        except LoginFormError:
            raise
        except Exception as exc:
            log(f"login password fill: {type(exc).__name__}")
        page.wait_for_timeout(400)
    try:
        log(
            "WARN login form stalled "
            f"url={safe_url(getattr(page, 'url', '') or '')} "
            f"filled_email={filled_email} filled_password={filled_password} "
            f"text={visible_text(page)[:180]!r}"
        )
    except Exception:
        pass
    return filled_password and not require_transition


def click_next(page, timeout: int = 8000) -> bool:
    """Click the primary Next/Create/Submit control on juno pages."""
    try:
        from . import ea_pw_link_2fa as pw_link
        pw_link.dismiss_cookies(page)
    except Exception:
        pass
    selectors = (
        "#countryDobNextBtn",
        "#basicInfoNextBtn",
        "#btnSendCode",
        "#logInBtn",
        "#nextBtn",
        "#btnNext",
        "#btnSubmit",
        "#submitBtn",
        "#createBtn",
        "button[type=submit]",
        "input[type=submit]",
        "a#next",
        "a#btnSubmit",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible() and human_click(page, loc, timeout=timeout):
                return True
        except Exception as exc:
            log(f"click_next {selector} failed: {exc}")
            continue
    for role in ("button", "link"):
        try:
            loc = page.get_by_role(role, name=NEXT_RE).first
            if loc.count() and loc.is_visible() and human_click(page, loc, timeout=timeout):
                return True
        except Exception:
            continue
    return False


def fill_first(page, selectors: str, value: str, timeout: int = 15000) -> bool:
    loc = page.locator(selectors).first
    loc.wait_for(state="visible", timeout=timeout)
    return human_type(page, loc, str(value))


_EN_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)
_ZH_MONTHS = (
    "一月", "二月", "三月", "四月", "五月", "六月",
    "七月", "八月", "九月", "十月", "十一月", "十二月",
)


def _dob_labels(kind: str, value: str) -> tuple[str, ...]:
    raw = str(value).strip()
    labels = [raw]
    if raw.isdigit():
        number = int(raw)
        labels.append(f"{number:02d}")
        if kind == "month" and 1 <= number <= 12:
            labels.extend((
                _EN_MONTHS[number - 1],
                _EN_MONTHS[number - 1][:3],
                f"{number}月",
                _ZH_MONTHS[number - 1],
            ))
        elif kind == "day":
            labels.append(f"{number}日")
        elif kind == "year":
            labels.append(f"{raw}年")
    seen: list[str] = []
    for item in labels:
        if item and item not in seen:
            seen.append(item)
    return tuple(seen)


def _selected_named(page, name: str) -> str:
    loc = page.locator(f"select[name='{name}'], select#{name}").first
    try:
        if loc.count():
            return str(loc.input_value() or "").strip()
    except Exception:
        return ""
    return ""


def _select_value_ok(actual: str, value: str, labels: tuple[str, ...] | list[str]) -> bool:
    got = (actual or "").strip().lower()
    if not got:
        return False
    accepted = {
        str(value).strip().lower(),
        *[str(item).strip().lower() for item in labels if item],
    }
    if got in accepted:
        return True
    if got.isdigit() and str(value).strip().isdigit():
        return int(got) == int(str(value).strip())
    return False


def _mark_select_trigger(page, name: str) -> bool:
    """Mark the visible Origin control for one named select.

    ``closest()`` includes the element itself. A ``select.otkselect-select``
    therefore used to become its own search scope, and the sibling
    ``span.otkselect-label`` was never found. The native ``<select>`` is also
    often painted on top of that label, so the click has to land on the label.
    """
    try:
        return bool(page.evaluate(
            """(name) => {
                const visible = (el) => {
                    if (!el || el.tagName === 'SELECT' || el.tagName === 'OPTION') return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const box = el.getBoundingClientRect();
                    return box.width > 8 && box.height > 8;
                };
                document.querySelectorAll('[data-apex-select-trigger]').forEach((el) => {
                    el.removeAttribute('data-apex-select-trigger');
                });
                const select = document.querySelector(`select[name="${name}"]`)
                    || document.getElementById(name);
                if (!select || select.tagName !== 'SELECT') return false;
                let scope = select.parentElement;
                const wrapped = select.closest('[class*="otkselect"]');
                if (wrapped && wrapped !== select && wrapped.tagName !== 'SELECT') scope = wrapped;
                if (!scope || scope === select) return false;
                const nodes = scope.querySelectorAll(
                    '.otkselect-label, .otkselect-selected, .otkselect-value, ' +
                    'button, [role="button"], [role="combobox"], ' +
                    '[class*="label"], [class*="selected"], [class*="display"]'
                );
                const mark = (el) => {
                    el.setAttribute('data-apex-select-trigger', name);
                    select.style.pointerEvents = 'none';
                    return true;
                };
                for (const el of nodes) {
                    if (el === select || el.contains(select) || select.contains(el) || !visible(el)) continue;
                    return mark(el);
                }
                if (visible(scope)) return mark(scope);
                return false;
            }""",
            name,
        ))
    except Exception:
        return False


def _custom_select_pending(page, name: str) -> bool:
    """True when the named control is still missing or looks like Origin chrome."""
    try:
        return bool(page.evaluate(
            """(name) => {
                const select = document.querySelector(`select[name="${name}"]`)
                    || document.getElementById(name);
                if (!select) return true;
                const blob = document.documentElement ? document.documentElement.outerHTML : '';
                return /otkselect|combobox|listbox/i.test(blob);
            }""",
            name,
        ))
    except Exception:
        return False


def _await_custom_trigger(page, name: str, timeout_s: float = 1.6) -> bool:
    if _mark_select_trigger(page, name):
        return True
    if not _custom_select_pending(page, name):
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            page.wait_for_timeout(200)
        except Exception:
            time.sleep(0.2)
        if _mark_select_trigger(page, name):
            return True
    return False


def _mark_select_option(page, name: str, value: str, labels: tuple[str, ...] | list[str]) -> bool:
    try:
        return bool(page.evaluate(
            """({name, value, labels}) => {
                const visible = (el) => {
                    if (!el) return false;
                    const style = window.getComputedStyle(el);
                    if (style.display === 'none' || style.visibility === 'hidden') return false;
                    const box = el.getBoundingClientRect();
                    return box.width > 2 && box.height > 2;
                };
                document.querySelectorAll('[data-apex-select-option]').forEach((el) => {
                    el.removeAttribute('data-apex-select-option');
                });
                const want = new Set(
                    [value, ...(labels || [])]
                        .map((item) => String(item || '').trim().toLowerCase())
                        .filter(Boolean)
                );
                const select = document.querySelector(`select[name="${name}"]`)
                    || document.getElementById(name);
                const roots = [];
                if (select && select.parentElement) roots.push(select.parentElement);
                if (select) {
                    const group = select.closest('[class*="otk"], [class*="field"], [class*="select"]');
                    if (group) roots.push(group);
                }
                roots.push(document);
                const query = '[role="option"], [role="listitem"], li, a, [data-value], [class*="option"]';
                for (const root of roots) {
                    for (const el of root.querySelectorAll(query)) {
                        if (!visible(el)) continue;
                        const data = (el.getAttribute('data-value') || el.getAttribute('value') || '').trim().toLowerCase();
                        const text = (el.innerText || '').trim().toLowerCase();
                        if (!want.has(data) && !want.has(text)) continue;
                        el.scrollIntoView({block: 'center', inline: 'nearest'});
                        el.setAttribute('data-apex-select-option', name);
                        return true;
                    }
                }
                return false;
            }""",
            {"name": name, "value": str(value), "labels": [str(item) for item in labels if item]},
        ))
    except Exception:
        return False


def _click_custom_select(page, name: str, value: str, labels: tuple[str, ...] | list[str]) -> bool:
    if not _await_custom_trigger(page, name):
        return False
    trigger = page.locator(f"[data-apex-select-trigger='{name}']").first
    if not human_click(page, trigger, timeout=2500):
        return False
    human_delay(0.35, 0.8)
    if not _mark_select_option(page, name, value, labels):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return False
    human_delay(0.2, 0.5)
    option = page.locator(f"[data-apex-select-option='{name}']").first
    if not human_click(page, option, timeout=2500):
        return False
    human_delay(0.15, 0.4)
    return _select_value_ok(_selected_named(page, name), value, labels)


def _option_typeahead(page, name: str, value: str, labels: tuple[str, ...] | list[str]) -> str:
    """Visible option text to type into an open native select menu.

    The menu is the browser's own popup, not a list inside the page. Typeahead
    only keeps about a second of keystrokes, so the text is capped.
    """
    try:
        text = page.evaluate(
            """({name, value, labels}) => {
                const select = document.querySelector(`select[name="${name}"]`)
                    || document.getElementById(name);
                if (!select || !select.options) return '';
                const wanted = new Set(
                    [value, ...(labels || [])]
                        .map((item) => String(item || '').trim().toLowerCase())
                        .filter(Boolean)
                );
                const wantedNumber = /^\\d+$/.test(String(value || '').trim())
                    ? String(parseInt(value, 10))
                    : '';
                for (const opt of select.options) {
                    const rawValue = String(opt.value || '').trim();
                    const rawText = String(opt.text || '').trim();
                    const val = rawValue.toLowerCase();
                    const text = rawText.toLowerCase();
                    const numeric = wantedNumber && /^\\d+$/.test(rawValue)
                        && String(parseInt(rawValue, 10)) === wantedNumber;
                    if (val === String(value || '').trim().toLowerCase() || numeric || wanted.has(text)) {
                        return rawText.slice(0, 16);
                    }
                }
                return '';
            }""",
            {
                "name": name,
                "value": str(value),
                "labels": [str(item) for item in labels if item],
            },
        )
    except Exception:
        return ""
    return str(text or "").strip()


def _select_via_picker(page, name: str, value: str, labels: tuple[str, ...] | list[str]) -> bool:
    """Open the browser's select popup and type the option.

    EA draws an invisible ``<select>`` over ``span.otkselect-label``. The option
    list is a window outside the page, so there is no item to click.
    """
    loc = page.locator(f"select[name='{name}'], select#{name}").first
    try:
        loc.wait_for(state="attached", timeout=8000)
        box = loc.bounding_box()
    except Exception:
        return False
    if not box or box.get("width", 0) < 8 or box.get("height", 0) < 8:
        return False
    typed = _option_typeahead(page, name, value, labels)
    if not typed:
        return False
    try:
        page.evaluate(
            """(name) => {
                const select = document.querySelector(`select[name="${name}"]`)
                    || document.getElementById(name);
                if (select) select.style.pointerEvents = 'auto';
            }""",
            name,
        )
    except Exception:
        pass
    try:
        page.mouse.click(
            box["x"] + box["width"] * random.uniform(0.35, 0.6),
            box["y"] + box["height"] * random.uniform(0.35, 0.65),
        )
    except Exception:
        return False
    human_delay(0.15, 0.35)
    try:
        page.evaluate(
            """(name) => {
                const select = document.querySelector(`select[name="${name}"]`)
                    || document.getElementById(name);
                if (!select) return;
                select.focus();
                try { if (select.showPicker) select.showPicker(); } catch (err) {}
            }""",
            name,
        )
    except Exception:
        pass
    try:
        page.keyboard.type(typed, delay=30)
        human_delay(0.12, 0.28)
        page.keyboard.press("Enter")
    except Exception:
        return False
    human_delay(0.2, 0.45)
    if _select_value_ok(_selected_named(page, name), value, labels):
        return True
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    return False


def choose_visible_select(
    page, name: str, value: str, labels: tuple[str, ...] | list[str] = ()
) -> bool:
    """Click a custom menu, otherwise open the browser's own select popup."""
    options = [str(value), *[str(item) for item in labels if item and str(item) != str(value)]]
    if _click_custom_select(page, name, str(value), options):
        log(f"select {name} via menu value={_selected_named(page, name) or value}")
        return True
    if _select_via_picker(page, name, str(value), options):
        log(f"select {name} via picker value={value}")
        return True
    if select_or_fill(page, name, str(value), labels=options):
        log(f"select {name} via native value={value}")
        return True
    return False


def select_or_fill(page, name: str, value: str, labels: tuple[str, ...] | list[str] = ()) -> bool:
    """Set a named select/input; EA DOB fields are sometimes inputs, sometimes selects.

    Juno's country control is often a hidden ``<select>`` behind Origin Toolkit
    chrome. ``is_visible()`` is false there, so we still call ``select_option``.
    """
    options = [str(value), *[str(item) for item in labels if item and str(item) != str(value)]]
    select = page.locator(f"select[name='{name}'], select#{name}").first
    try:
        if select.count():
            for option in options:
                try:
                    select.select_option(option, force=True, timeout=1500)
                    return True
                except Exception:
                    try:
                        select.select_option(label=option, force=True, timeout=1500)
                        return True
                    except Exception:
                        continue
    except Exception:
        pass
    inp = page.locator(f"input[name='{name}'], input#{name}").first
    try:
        if inp.count() and inp.is_visible():
            return human_type(page, inp, str(value))
    except Exception:
        return False
    return False


COUNTRY_OPTION_LABELS = {
    "HK": ("HK", "Hong Kong", "香港", "Hong Kong SAR", "中國香港特別行政區"),
    "US": ("US", "United States", "USA", "美國", "美国", "アメリカ合衆国"),
    "JP": ("JP", "Japan", "日本"),
}


def looks_technical_issue(html: str, text: str, url: str = "") -> bool:
    blob = f"{html or ''}\n{text or ''}"
    if protocol.classify_create_response(
        protocol.cur_step(html or ""), url, text or ""
    ) == "technical_issue":
        return True
    return bool(
        re.search(
            r"發生錯誤|发生错误|無法完成您的請求|无法完成您的请求|"
            r"目前發生技術問題|目前发生技术问题|"
            r"we ran into a problem|unable to complete your request",
            blob,
            re.I,
        )
    )


JUNO_CREATE_RE = re.compile(r"juno/create|junoweb/create", re.I)


def on_juno_create(url: str) -> bool:
    return bool(JUNO_CREATE_RE.search(url or ""))


def email_field_visible(page) -> bool:
    try:
        loc = page.locator("#email, input[name=email]").first
        return bool(loc.count() and loc.is_visible())
    except Exception:
        return False


def country_dob_visible(page) -> bool:
    selectors = (
        "select[name=country]",
        "select#country",
        "select[name=dobYear]",
        "#countryDobNextBtn",
        "#dobYear",
    )
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible():
                return True
        except Exception:
            continue
    try:
        if page.get_by_text(
            re.compile(r"國家/地區|出生日期|Country/Region|Date of birth", re.I)
        ).count():
            return True
    except Exception:
        pass
    # Native country/DOB selects stay attached but 0×0 on juno; do not
    # treat the marketing homepage as the form just because a select exists.
    if on_juno_create(getattr(page, "url", "") or ""):
        for selector in ("select[name=country]", "select#country", "select[name=dobYear]"):
            try:
                loc = page.locator(selector).first
                if loc.count():
                    return True
            except Exception:
                continue
    return False


def _dismiss_register_cookies(page) -> None:
    try:
        from . import ea_pw_link_2fa as pw_link

        pw_link.dismiss_cookies(page)
    except Exception:
        pass


def _settle_register_page(page, settle_ms: int) -> None:
    _dismiss_register_cookies(page)
    deadline = time.monotonic() + max(int(settle_ms), 0) / 1000.0
    while time.monotonic() < deadline:
        url = getattr(page, "url", "") or ""
        if country_dob_visible(page) or email_field_visible(page) or on_juno_create(url):
            return
        try:
            page.wait_for_timeout(400)
        except Exception:
            return


def ensure_live_page(context, page=None):
    """Return a page that can still be driven after Roxy replaces a tab."""
    try:
        if page is not None:
            closed = getattr(page, "is_closed", None)
            if closed is None or not closed():
                return page
    except Exception:
        pass
    try:
        pages = list(getattr(context, "pages", None) or [])
    except Exception:
        pages = []
    for candidate in pages:
        try:
            closed = getattr(candidate, "is_closed", None)
            if closed is not None and closed():
                continue
            try:
                candidate.set_default_timeout(60000)
            except Exception:
                pass
            return candidate
        except Exception:
            continue
    new_page = getattr(context, "new_page", None)
    if new_page is None:
        raise RuntimeError("fingerprint CDP has no live page")
    fresh = new_page()
    try:
        fresh.set_default_timeout(60000)
    except Exception:
        pass
    return fresh


def browser_still_connected(browser, context) -> bool:
    try:
        connected = getattr(browser, "is_connected", None)
        if connected is not None and not connected():
            return False
        _ = list(getattr(context, "pages", None) or [])
        return True
    except Exception:
        return False


def _first_visible(locator):
    try:
        count = locator.count()
    except Exception:
        return None
    for index in range(min(int(count or 0), 6)):
        try:
            item = locator.nth(index)
        except Exception:
            item = locator
        try:
            if item.is_visible():
                return item
        except Exception:
            continue
    return None


def _click_named(page, pattern, timeout: int = 5000) -> bool:
    for role in ("button", "link", "menuitem"):
        try:
            target = _first_visible(page.get_by_role(role, name=pattern))
        except Exception:
            target = None
        if target is not None and human_click(page, target, timeout=timeout):
            return True
    try:
        target = _first_visible(page.get_by_text(pattern))
    except Exception:
        target = None
    return target is not None and human_click(page, target, timeout=timeout)


def _close_home_cookie(page) -> None:
    try:
        banner = page.get_by_text(re.compile(r"Cookie", re.I)).first
        if not banner.count() or not banner.is_visible():
            return
    except Exception:
        return
    for label in ("Close", "关闭", "關閉", "閉じる"):
        if _click_named(page, re.compile(rf"^{label}$", re.I), timeout=2000):
            return


def _click_account_button(page) -> bool:
    # The header control is an unlabeled icon. Role text such as Account is
    # only inside the menu, so the icon selector has to go first.
    try:
        page.locator(ACCOUNT_BUTTON_SELECTORS[0]).first.wait_for(state="visible", timeout=8000)
    except Exception:
        pass
    for selector in ACCOUNT_BUTTON_SELECTORS:
        try:
            loc = page.locator(selector)
            count = loc.count()
            if not count:
                continue
            item = loc.nth(count - 1)
            if item.is_visible() and human_click(page, item, timeout=4000):
                return True
        except Exception:
            continue
    return _click_named(page, ACCOUNT_MENU_RE)


def _open_create_account(page) -> bool:
    """From the public EA home, open the account menu and choose create."""
    for _ in range(2):
        url = getattr(page, "url", "") or ""
        if country_dob_visible(page) or email_field_visible(page) or on_juno_create(url):
            return True
        _close_home_cookie(page)
        if not _click_account_button(page):
            continue
        human_delay(0.3, 0.8)
        if not _click_named(page, CREATE_ACCOUNT_RE):
            continue
        _settle_register_page(page, 15000)
        url = getattr(page, "url", "") or ""
        if country_dob_visible(page) or email_field_visible(page) or on_juno_create(url):
            return True
    return False


def goto_register(page, timeout_ms: int = 60000, settle_ms: int = 15000):
    """Open ea.com and click through to the create-account form.

    The site chooses the locale from the exit. The script does not open a
    ``/zh-hk`` path or a prebuilt ``locale=zh_HK`` create URL.
    """
    del settle_ms
    context = getattr(page, "context", None)
    last_exc = None
    for attempt in range(3):
        if context is not None:
            page = ensure_live_page(context, page)
        try:
            page.goto(EA_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            lowered = str(exc).lower()
            transient = any(
                token in lowered
                for token in (
                    "err_aborted",
                    "detached",
                    "has been closed",
                    "target closed",
                    "browser has been closed",
                )
            )
            log(f"WARN goto register attempt={attempt + 1}: {type(exc).__name__}")
            if not transient or attempt == 2:
                raise
            time.sleep(1.0)
    if last_exc:
        raise last_exc
    human_delay(0.7, 2.2)
    if not _open_create_account(page):
        log(f"WARN create-account click did not reach the form url={safe_url(getattr(page, 'url', '') or '')}")
    return page


def reload_page(page, reason: str, *, timeout_ms: int = 15000) -> None:
    """Reload a stuck step instead of waiting many minutes."""
    if page is None:
        return
    log(f"reload page ({reason})")
    try:
        page.reload(wait_until="domcontentloaded", timeout=timeout_ms)
    except Exception as exc:
        log(f"WARN reload {reason}: {type(exc).__name__}")
        try:
            page.reload(wait_until="commit", timeout=min(8000, timeout_ms))
        except Exception:
            pass


def recover_transient_register(page, reason: str):
    """Recover from EA's generic technical page or a dead SPA.

    A reload is enough when the form is still there. If EA swapped in the
    yellow-bang error, reopen juno instead of sitting on that screen.
    """
    context = getattr(page, "context", None)
    if context is not None:
        page = ensure_live_page(context, page)
    reload_page(page, reason)
    try:
        page.wait_for_timeout(800)
    except Exception:
        pass
    html = page_blob(page)
    text = visible_text(page)
    url = getattr(page, "url", "") or ""
    still_dead = looks_technical_issue(html, text, url) or not (
        country_dob_visible(page) or email_field_visible(page) or on_juno_create(url)
    )
    if not still_dead:
        return page
    log(f"reopening juno after {reason}")
    try:
        return goto_register(page)
    except Exception as exc:
        log(f"WARN reopen register after {reason}: {type(exc).__name__}")
        return page


def _click_matching_option(page, labels: tuple[str, ...] | list[str]) -> bool:
    names = [str(item) for item in labels if item]
    if not names:
        return False
    pattern = re.compile("|".join(re.escape(name) for name in names), re.I)
    locators = (
        page.get_by_role("option", name=pattern),
        page.get_by_role("listitem", name=pattern),
        page.get_by_text(pattern),
    )
    for loc in locators:
        try:
            candidate = loc.first
            if not candidate.count():
                continue
            try:
                if not candidate.is_visible():
                    continue
            except Exception:
                continue
            if human_click(page, candidate, timeout=2500):
                return True
        except Exception:
            continue
    return False


def selected_country_code(page) -> str:
    loc = page.locator("select[name=country], select#country").first
    try:
        if loc.count():
            return str(loc.input_value() or "").strip().upper()
    except Exception:
        return ""
    return ""


def select_country(page, country: str) -> bool:
    code = (country or "HK").strip().upper() or "HK"
    labels = COUNTRY_OPTION_LABELS.get(code, (code,))
    if choose_visible_select(page, "country", code, labels=labels):
        native = selected_country_code(page)
        return native in {"", code}
    try:
        trigger = page.locator(
            "select[name=country], #country, [aria-label*='國家'], "
            "[aria-label*='Country'], [class*='otkselect']"
        ).first
        if trigger.count():
            human_click(page, trigger, timeout=2500)
            page.wait_for_timeout(400)
    except Exception:
        pass
    if _click_matching_option(page, labels):
        native = selected_country_code(page)
        return native in {"", code}
    try:
        shown = page.get_by_text(
            re.compile(r"日本|Indonesia|United States|美國", re.I)
        ).first
        if shown.count() and shown.is_visible():
            human_click(page, shown, timeout=2500)
            page.wait_for_timeout(400)
            if _click_matching_option(page, labels):
                native = selected_country_code(page)
                return native in {"", code}
    except Exception:
        return False
    native = selected_country_code(page)
    return native == code


def fill_register_country_dob(
    page,
    country: str,
    dob_month: str,
    dob_day: str,
    dob_year: str,
    *,
    _recovered: bool = False,
) -> bool:
    """Fill the first juno step. False means we are still on country/DOB."""
    wanted = (country or "HK").strip().upper() or "HK"
    if email_field_visible(page) and not country_dob_visible(page):
        return True
    if not country_dob_visible(page) and not email_field_visible(page):
        return False
    human_delay(0.7, 1.4)
    if wanted == "AUTO":
        landed = selected_country_code(page)
        log(f"country left as page default {landed or 'unknown'}")
    elif not select_country(page, wanted):
        log(f"WARN country select did not land on {wanted}")
        return False
    landed = selected_country_code(page)
    if wanted != "AUTO" and landed and landed != wanted:
        log(f"WARN country select stayed {landed}, need {wanted}")
        return False
    human_delay(0.55, 1.15)
    choose_visible_select(page, "dobMonth", dob_month, labels=_dob_labels("month", dob_month))
    human_delay(0.45, 1.05)
    choose_visible_select(page, "dobDay", dob_day, labels=_dob_labels("day", dob_day))
    human_delay(0.45, 1.05)
    choose_visible_select(page, "dobYear", dob_year, labels=_dob_labels("year", dob_year))
    log(f"POST dob/country country={country} dob={dob_year}-{dob_month}-{dob_day}")
    human_delay(0.6, 1.2)
    if not click_next(page):
        log("WARN dob submit click failed")
        return False
    human_delay(2.5, 4.2)
    html = page_blob(page)
    text = visible_text(page)
    if looks_technical_issue(html, text, page.url):
        log("WARN dob submit landed on technical issue")
        if not _recovered:
            page = recover_transient_register(page, "dob technical issue")
            return fill_register_country_dob(
                page,
                country,
                dob_month,
                dob_day,
                dob_year,
                _recovered=True,
            )
        return False
    return email_field_visible(page) or not country_dob_visible(page)


def check_tos(page) -> None:
    """Check EA Terms of Service without force-clicking hidden controls."""
    for name in ("readAccept", "readAcceptUnderage"):
        loc = page.locator(f"input[name='{name}'], input#{name}").first
        try:
            if loc.count() and loc.is_visible() and not loc.is_checked():
                if human_click(page, loc, timeout=2500):
                    continue
                loc.check()
        except Exception:
            pass

    try:
        chk = page.locator("#readAccept, input[name=readAccept]").first
        if chk.count() and not chk.is_checked():
            label = page.locator("label[for=readAccept]").first
            if label.count() and label.is_visible():
                human_click(page, label, timeout=1500)
    except Exception:
        pass

    # Trigger change event via JS to ensure jQuery registers the check
    try:
        page.evaluate(
            """() => {
                const cb = document.querySelector('#readAccept, input[name="readAccept"]');
                if (cb && !cb.checked) {
                    cb.checked = true;
                    cb.dispatchEvent(new Event('change', { bubbles: true }));
                }
            }"""
        )
    except Exception:
        pass


def looks_like_captcha(html: str) -> bool:
    return bool(CAPTCHA_RE.search(html or ""))


def is_email_verify_step(
    step: str | None, text: str | None = None, html: str | None = None
) -> bool:
    """True when Juno has moved to the mailbox OTP screen.

    Live zh-HK copy uses ``juno/mevEmailCollection`` plus 「驗證您的電子郵件」.
    Recaptcha scripts stay in the page HTML, so this must win over captcha.
    """
    if EMAIL_VERIFY_STEP_RE.search(str(step or "")):
        return True
    return bool(EMAIL_VERIFY_TEXT_RE.search(f"{text or ''}\n{html or ''}"))


def should_abort_as_captcha(
    html: str, *, text: str | None = None, step: str | None = None
) -> bool:
    if is_email_verify_step(step, text, html):
        return False
    return looks_like_captcha(html)


def looks_rate_limited(html: str, text: str) -> bool:
    blob = f"{html}\n{text}".lower()
    return any(needle in blob for needle in (
        "您提出請求次數過多", "您提出请求次数过多", "too many requests",
        "request limit", "請稍候再試", "请稍后再试", "try again later"
    ))


def looks_already_registered(html: str, text: str) -> bool:
    if re.search(r"['\"]alreadyRegistered['\"]\s*:\s*true", html or "", re.I):
        return True
    if text:
        t = text.lower()
        if any(needle in t for needle in ("already registered", "already exists", "已註冊", "已注册", "該電郵已註冊", "此電子郵件已註冊", "此電子郵件地址已被使用", "此電子郵箱已註冊")):
            return True
    # Check server-injected error elements specifically, avoiding JS template strings like 'alreadyRegistered': false
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html or "", "html.parser")
    err_el = soup.select_one(".int-registration-error, #email-error, .field-error, .ot-error-message, [class*='error']")
    if err_el:
        t = err_el.get_text(strip=True).lower()
        if any(needle in t for needle in ("already registered", "已註冊", "已注册", "此電子郵件地址已被使用", "已被使用")):
            return True
    return False


def next_register_mailbox(
    pool,
    *,
    burned: set[str] | list[str] | tuple[str, ...] | None = None,
    current_email: str,
    skip_emails: set[str] | list[str] | tuple[str, ...] | None = None,
) -> dict | None:
    """Pick the next Graph mailbox after EA rejects the current address."""
    skip = {str(item).strip().lower() for item in (burned or ()) if item}
    skip.update(str(item).strip().lower() for item in (skip_emails or ()) if item)
    skip.add(str(current_email or "").strip().lower())
    items = pool.values() if isinstance(pool, dict) else (pool or [])
    for item in items:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip()
        if not email or email.lower() in skip:
            continue
        if not item.get("refresh_token"):
            continue
        return item
    return None


def replace_register_email(page, new_email: str) -> bool:
    """Type a replacement mailbox on the current create form. False => reload."""
    box = page.locator("#email, input[name=email]").first
    try:
        if not box.count() or not box.is_visible():
            return False
        box.click(timeout=5000)
        box.fill("")
        human_type(page, box, new_email)
        page.keyboard.press("Tab")
        page.wait_for_timeout(1200)
        return True
    except Exception:
        return False


def wait_graph_code(email_data: dict, after_ts: float, page=None) -> str | None:
    wait_seconds = protocol.mail_wait_seconds()
    wait_started = time.monotonic()
    attempts = 0
    resent = False
    reloaded = False
    rate_limited = False
    log("wait EA verification email via Graph")
    while time.monotonic() - wait_started < wait_seconds:
        attempts += 1
        if page:
            text = visible_text(page)
            if looks_rate_limited(page_blob(page), text):
                # Another resend or a verify-page reload counts as a new request
                # and keeps the「請求次數過多」banner up. Keep polling Graph.
                log("WARN EA rate-limit banner visible; keep waiting for Graph mail")
                rate_limited = True
                resent = True
        try:
            token = protocol.graph_access_token(email_data)
            code = protocol.graph_find_ea_code(token, after_ts=after_ts - 10)
        except Exception as exc:
            log(f"graph err: {exc}")
            code = None
        if code:
            log("got EA verification code")
            return code
        elapsed = int(time.monotonic() - wait_started)
        log(f"no code yet attempt={attempts} elapsed={elapsed}s")

        if page and not resent and elapsed >= 40:
            try:
                resend_btn = page.locator(
                    "#resend, a:text-matches('重新發送|重新发送|請求重新發送|Resend', 'i')"
                ).first
                if resend_btn.count() and resend_btn.is_visible():
                    log("Clicking resend verification email after 40s...")
                    resend_btn.click()
            except Exception as resend_exc:
                log(f"WARN resend click: {resend_exc}")
            resent = True

        if page and not reloaded and elapsed >= 55 and not rate_limited:
            try:
                html = page_blob(page)
                text = visible_text(page)
                if looks_rate_limited(html, text):
                    log("skip reload while EA rate-limit banner is up")
                    rate_limited = True
                else:
                    step = protocol.cur_step(html)
                    if is_email_verify_step(step, text, html):
                        reload_page(page, "email verify stuck")
            except Exception as reload_exc:
                log(f"WARN verify reload: {reload_exc}")
            reloaded = True

        remaining = wait_seconds - (time.monotonic() - wait_started)
        if remaining > 0:
            time.sleep(min(5, remaining))
    return None


def _type_otp_char(box, ch: str) -> None:
    delay = random.randint(140, 280)
    try:
        box.click(timeout=1500)
    except Exception:
        pass
    human_delay(0.12, 0.32)
    try:
        box.press_sequentially(ch, delay=delay)
    except Exception:
        box.fill(ch)
    if random.random() < 0.28:
        human_delay(0.2, 0.55)


def fill_verify_code(page, code: str) -> None:
    code = str(code).strip()
    # 1. Segmented numbered boxes: #emailVerifyCode-1 .. #emailVerifyCode-6 or #twoFactorCode-1 .. #twoFactorCode-6
    for prefix in ("#emailVerifyCode-", "#twoFactorCode-", "#securityCode-"):
        first_box = page.locator(f"{prefix}1").first
        try:
            if first_box.count() and first_box.is_visible():
                for idx, ch in enumerate(code, 1):
                    box = page.locator(f"{prefix}{idx}").first
                    if box.count() and box.is_visible():
                        _type_otp_char(box, ch)
                return
        except Exception:
            pass

    # 2. Split boxes by query selector
    boxes = page.locator(
        "input[name^=emailVerifyCode], input[id^=emailVerifyCode], "
        "input[name^=twoFactorCode], input[id^=twoFactorCode], "
        "input[name^=securityCode], input[id^=securityCode], "
        "input.otp-input, input[maxlength='1']"
    )
    try:
        count = boxes.count()
    except Exception:
        count = 0
    if count >= len(code):
        for idx, ch in enumerate(code):
            try:
                _type_otp_char(boxes.nth(idx), ch)
            except Exception:
                break
        return

    # 3. Single field
    for single_sel in (
        "#twoFactorCode", "input[name=twoFactorCode]",
        "#securityCode", "input[name=securityCode]",
        "#emailVerifyCode", "input[name=emailVerifyCode]",
        "#two-factor-code", "input[name='two-factor-code']"
    ):
        single = page.locator(single_sel).first
        try:
            if single.count() and single.is_visible():
                human_type(page, single, code, min_delay=140, max_delay=280)
                return
        except Exception:
            pass

    fill_first(page, "input[type=text], input[type=tel], input[type=number]", code)


def on_myaccount(page) -> bool:
    url = page.url or ""
    return "myaccount.ea.com" in url and "signin.ea.com" not in url


def handle_post_create_gates(page, origin_id: str) -> None:
    """Clear optional EA ID upgrade / privacy / TOS gates after create or login."""
    for _ in range(10):
        html = page_blob(page)
        text = visible_text(page)
        url = page.url or ""
        step = protocol.cur_step(html) or ""
        if on_myaccount(page):
            return
        if "dynamicchallenge" in step.lower():
            # Do not solve login OTP merely to prove creation.
            return
        if "upgradePrivacy" in step or "privacy" in text.lower():
            for name, value in (
                ("friendVisibility", "EVERYONE"),
                ("emailVisibility", "on"),
                ("_emailVisibility", "on"),
            ):
                try:
                    loc = page.locator(f"[name='{name}']").first
                    if loc.count():
                        tag = (loc.evaluate("el => el.tagName") or "").lower()
                        if tag == "select":
                            loc.select_option(value)
                        elif loc.get_attribute("type") == "checkbox":
                            if not loc.is_checked():
                                loc.check(force=True)
                except Exception:
                    pass
            click_next(page)
            page.wait_for_timeout(1500)
            continue
        if "tosUpdate" in step or "terms" in text.lower() or "服務條款" in text:
            check_tos(page)
            click_next(page) or page.locator(
                "button:has-text('Accept'), button:has-text('接受')"
            ).first.click(timeout=5000)
            page.wait_for_timeout(1500)
            continue
        if "upgrade" in step.lower() or page.locator("input[name=originId]").count():
            try:
                box = page.locator("input[name=originId], #originId").first
                if box.count() and box.is_visible():
                    box.fill(origin_id)
                click_next(page)
                page.wait_for_timeout(1500)
                continue
            except Exception:
                pass
        if "signin.ea.com" not in url and "juno" not in url.lower():
            return
        page.wait_for_timeout(1000)
        # Avoid infinite loops on an unrecognized gate.
        if _ == 9:
            return


def headed_requested() -> bool:
    """Open a visible window. Does not switch the engine off Roxy."""
    return any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}
        for name in (
            "GAME_ACCOUNTS_EA_REGISTER_HEADED",
            "ACCOUNT_FLOW_HEADED",
        )
    )


def keep_fingerprint_window() -> bool:
    return any(
        os.environ.get(name, "").strip().lower() in {"1", "true", "yes"}
        for name in (
            "GAME_ACCOUNTS_ROXY_KEEP",
            "GAME_ACCOUNTS_FINGERPRINT_KEEP",
        )
    )


def release_fingerprint_window(client, profile_id) -> None:
    if not client or not profile_id or keep_fingerprint_window():
        return
    releaser = getattr(client, "delete", None) or getattr(client, "close", None)
    if not releaser:
        return
    try:
        releaser(profile_id)
    except Exception as exc:
        log(f"fingerprint window release failed id={profile_id}: {type(exc).__name__}")
        return


def uses_chrome_incognito() -> bool:
    raw = os.environ.get("GAME_ACCOUNTS_DESKTOP_BROWSER", "").strip().lower()
    return raw in {"chrome", "chrome-incognito", "incognito"}


def _register_engine_name() -> str:
    if uses_chrome_incognito():
        return "chrome_incognito"
    if uses_bitbrowser():
        return "bitbrowser"
    if uses_roxybrowser_engine():
        if roxy_host() == "windows":
            return "roxybrowser_windows"
        return "roxybrowser"
    return "playwright"


def _windows_page_warmup(page) -> None:
    """Move the pointer once so the first paint is not a dead window."""
    try:
        human_delay(0.4, 1.1)
        page.mouse.move(random.randint(80, 320), random.randint(70, 220))
    except Exception:
        return


def attach_browser(playwright, *, proxy: str | None, seed: str, on_profile_opened=None):
    """Return (browser, context, page, fingerprint_client, profile_id).

    Fingerprint engines connect to an already-opened window via CDP.
    Playwright engine launches bundled Chromium as before.
    """
    fingerprint_client = None
    desktop_window = uses_bitbrowser() or roxy_host() == "windows"
    if uses_chrome_incognito():
        log("Chrome incognito, no proxy")
        browser = playwright.chromium.launch(
            channel="chrome",
            headless=False,
            args=["--incognito"],
            ignore_default_args=["--enable-automation"],
        )
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(60000)
        return browser, context, page, None, None
    if uses_bitbrowser():
        if not proxy:
            raise RuntimeError("BitBrowser EA register requires a proxy URL")
        fingerprint_client = BitBrowserClient()
        opened = fingerprint_client.ensure_open(
            seed=seed, proxy_url=proxy, on_profile=on_profile_opened
        )
        if on_profile_opened:
            on_profile_opened(fingerprint_client, opened["id"])
        log(
            f"BitBrowser opened id={opened['id']} name={opened.get('name')} "
            f"cdp={opened['cdp']}"
        )
    elif uses_roxybrowser_engine():
        if not proxy:
            raise RuntimeError("RoxyBrowser EA register requires a proxy URL")
        fingerprint_client = RoxyBrowserClient()
        opened = fingerprint_client.ensure_open(
            seed=seed, proxy_url=proxy, on_profile=on_profile_opened
        )
        if on_profile_opened:
            on_profile_opened(fingerprint_client, opened["id"])
        log(
            f"RoxyBrowser opened id={opened['id']} name={opened.get('name')} "
            f"cdp={opened['cdp']}"
        )
    if fingerprint_client is not None:
        try:
            browser = playwright.chromium.connect_over_cdp(opened["cdp"])
            if not browser.contexts:
                raise RuntimeError("fingerprint CDP connected but has no context")
            context = browser.contexts[0]
            if desktop_window:
                context.add_init_script(
                    "Object.defineProperty(Navigator.prototype, 'webdriver', "
                    "{get: () => undefined});"
                )
            page = context.pages[0] if context.pages else context.new_page()
            page = ensure_live_page(context, page)
            try:
                page.wait_for_timeout(800)
            except Exception:
                pass
            if desktop_window:
                _windows_page_warmup(page)
            page.set_default_timeout(60000)
            return browser, context, page, fingerprint_client, opened["id"]
        except Exception:
            if not keep_fingerprint_window():
                try:
                    fingerprint_client.delete(opened["id"])
                except Exception:
                    pass
            raise

    browser = playwright.chromium.launch(
        headless=not headed_requested(),
        args=list(PLAYWRIGHT_CHROME_ARGS),
    )
    context_options = playwright_context_options(seed)
    pw = playwright_proxy(proxy)
    if pw is not None:
        context_options["proxy"] = pw
    context = browser.new_context(**context_options)
    page = context.new_page()
    page.set_default_timeout(60000)
    return browser, context, page, None, None


def pw_login_verify(page, context, email: str, password: str, origin_id: str) -> bool:
    """Prefer Playwright login/myaccount verify after create."""
    log("registration credentials verify via Playwright login")
    try:
        page.goto(LOGIN_TARGET, wait_until="domcontentloaded", timeout=60000)
    except Exception as exc:
        log(f"login verify nav failed: {net_error_reason(exc) or type(exc).__name__}")
        return False
    page.wait_for_timeout(2000)

    if on_myaccount(page):
        handle_post_create_gates(page, origin_id)
        persist_connect_cookies(capture_connect_cookies(context))
        log("registration credentials and EA profile verified")
        return True

    try:
        if not complete_ea_login_form(page, email, password, timeout_ms=25000):
            log("login form did not reach a filled password step")
    except Exception as exc:
        log(f"login form fill failed: {type(exc).__name__}")
        screenshot(page, "pw_register_login_fail.png")
        return False

    handle_post_create_gates(page, origin_id)

    for _ in range(40):
        if on_myaccount(page):
            persist_connect_cookies(capture_connect_cookies(context))
            log("registration credentials and EA profile verified")
            return True
        html = page_blob(page)
        step = (protocol.cur_step(html) or "").lower()
        if "dynamicchallenge" in step:
            # Credentials accepted; skip OTP challenge for create proof.
            persist_connect_cookies(capture_connect_cookies(context))
            log("registration credentials verified (challenge gate skipped)")
            return True
        if "password" in step and "invalid" in visible_text(page).lower():
            log("login verify password rejected")
            return False
        page.wait_for_timeout(1000)

    screenshot(page, "pw_register_verify_fail.png")
    log(f"login verify incomplete url={safe_url(page.url)}")
    return False


def main() -> int:
    os.umask(0o077)
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT_LOG.write_text("")
    OUT_LOG.chmod(0o600)

    # Keep protocol module artifact paths aligned with this run's workspace.
    protocol.ARTIFACT_ROOT = ARTIFACT_ROOT
    protocol.OUT_LOG = OUT_LOG
    protocol.OUT_RESULT = OUT_RESULT
    protocol.OUT_OK = OUT_OK
    protocol.OUT_FAIL = OUT_FAIL

    proxy = protocol.load_proxy()
    email_data = protocol.load_email()
    email = email_data["email"]
    mailbox_pool = [email_data]
    try:
        if protocol.EMAIL_POOL.is_file():
            raw_pool = json.loads(protocol.EMAIL_POOL.read_text(encoding="utf-8"))
            if isinstance(raw_pool, list):
                seen = {str(email).lower()}
                for item in raw_pool:
                    extra = str((item or {}).get("email") or "").lower()
                    if extra and extra not in seen and isinstance(item, dict):
                        mailbox_pool.append(item)
                        seen.add(extra)
    except Exception:
        pass
    skipped_ea_emails: list[str] = []
    country = pick_register_country()
    dob_month, dob_day, dob_year = protocol.random_adult_dob()
    password = protocol.rand_password()
    origin_id = protocol.rand_id()

    engine_name = _register_engine_name()
    log(f"mailbox selected domain={email.rsplit('@', 1)[-1]}")
    log(
        "network=direct"
        if proxy is None
        else f"proxy={proxy.split('@')[-1] if '@' in proxy else proxy}"
    )
    log(
        f"GET ea.com/register country={country} "
        f"dob={dob_year}-{dob_month}-{dob_day} lang=zh-HK engine={engine_name}"
    )

    account_login_verified = False
    account_profile_completed = False
    failure_reason = None
    final_url = None
    final_step = None
    connect_cookies = None
    bit_client = None
    bit_profile_id = None

    try:
        with sync_playwright() as playwright:
            try:
                browser, context, page, bit_client, bit_profile_id = attach_browser(
                    playwright, proxy=proxy, seed=email
                )
                try:
                    page = goto_register(page)
                except Exception as exc:
                    reason = net_error_reason(exc)
                    failure_reason = "proxy_error" if reason else "network_error"
                    log(f"FAIL open register: {reason or type(exc).__name__}")
                    screenshot(page, "pw_register_open_fail.png")
                    failure_result(email, failure_reason, final_url=safe_url(page.url))
                    browser.close()
                    return 1
                final_url = page.url
                log(f"landed {safe_url(page.url)} len={len(page_blob(page))}")
                html = page_blob(page)
                text = visible_text(page)
                if looks_technical_issue(html, text, page.url):
                    log("WARN EA technical issue page on first load; recovering")
                    screenshot(page, "pw_register_technical.png")
                    page = recover_transient_register(page, "first load technical issue")
                    html = page_blob(page)
                    text = visible_text(page)
                    final_url = page.url
                    if looks_technical_issue(html, text, page.url):
                        log("FAIL: EA technical issue page on first load")
                        failure_result(
                            email, "technical_issue", final_url=safe_url(page.url)
                        )
                        browser.close()
                        return 4

                # 2) DOB + country. A stale juno execution can return EA's
                # generic technical page; one fresh navigation recovered it
                # on the local Roxy + 1024 smoke.
                human_delay(0.5, 1.2)
                dob_ok = False
                for dob_try in range(2):
                    if fill_register_country_dob(
                        page, country, dob_month, dob_day, dob_year
                    ):
                        dob_ok = True
                        break
                    html = page_blob(page)
                    text = visible_text(page)
                    if looks_technical_issue(html, text, page.url) and dob_try == 0:
                        log("WARN dob technical issue; reloading juno once")
                        screenshot(page, "pw_register_dob_technical.png")
                        try:
                            page = goto_register(page)
                        except Exception as exc:
                            reason = net_error_reason(exc)
                            failure_reason = "proxy_error" if reason else "network_error"
                            log(
                                f"FAIL reload register after dob: "
                                f"{reason or type(exc).__name__}"
                            )
                            screenshot(page, "pw_register_dob_fail.png")
                            failure_result(
                                email, failure_reason, final_url=safe_url(page.url)
                            )
                            browser.close()
                            return 1
                        continue
                    break
                if not dob_ok:
                    html = page_blob(page)
                    text = visible_text(page)
                    if looks_technical_issue(html, text, page.url):
                        failure_reason = "technical_issue"
                        screenshot(page, "pw_register_dob_technical.png")
                        failure_result(
                            email, failure_reason, final_url=safe_url(page.url)
                        )
                        browser.close()
                        return 4
                    failure_reason = "dob_submit_failed"
                    screenshot(page, "pw_register_dob_fail.png")
                    failure_result(email, failure_reason, final_url=safe_url(page.url))
                    browser.close()
                    return 2

                html = page_blob(page)
                text = visible_text(page)
                if looks_like_captcha(html) and "email" not in html.lower():
                    log("FAIL: captcha required at this step (not implemented yet)")
                    screenshot(page, "pw_register_captcha.png")
                    Path(ARTIFACT_ROOT / "captcha_page.html").write_text(html)
                    failure_result(email, "captcha_required", final_url=safe_url(page.url))
                    browser.close()
                    return 3

                # 3) email + ToS. Stay in this window if EA says the mailbox is taken.
                email_accepted = False
                started = time.time()
                for _mailbox_try in range(1, 5):
                    email_box = page.locator("#email, input[name=email]").first
                    try:
                        email_box.wait_for(state="visible", timeout=45000)
                    except PWTimeout:
                        if looks_like_captcha(page_blob(page)):
                            log("FAIL: captcha required at this step (not implemented yet)")
                            screenshot(page, "pw_register_captcha.png")
                            failure_result(
                                email, "captcha_required", final_url=safe_url(page.url)
                            )
                            browser.close()
                            return 3
                        log("FAIL: email field missing after DOB")
                        screenshot(page, "pw_register_email_missing.png")
                        failure_result(
                            email, "email_step_missing", final_url=safe_url(page.url)
                        )
                        browser.close()
                        return 2
                    try:
                        current_value = (email_box.input_value() or "").strip()
                    except Exception:
                        current_value = ""
                    if current_value.lower() != email.lower():
                        human_delay(1.0, 2.0)
                        try:
                            email_box.fill("")
                        except Exception:
                            pass
                        human_type(page, email_box, email)
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(1000)

                    html = page_blob(page)
                    text = visible_text(page)
                    if looks_already_registered(html, text):
                        nxt = next_register_mailbox(
                            mailbox_pool,
                            burned=set(skipped_ea_emails),
                            current_email=email,
                        )
                        if not nxt:
                            log("FAIL: email already registered with EA")
                            screenshot(page, "pw_register_already_registered.png")
                            failure_result(
                                email,
                                "email_already_registered",
                                final_url=safe_url(page.url),
                                final_step=protocol.cur_step(html),
                                skipped_ea_emails=skipped_ea_emails,
                            )
                            browser.close()
                            return 2
                        skipped_ea_emails.append(email)
                        log("EA mailbox already registered; switching mailbox in this window")
                        screenshot(page, "pw_register_already_registered.png")
                        email_data = nxt
                        email = nxt["email"]
                        log("reloading register page after mailbox switch")
                        try:
                            page = goto_register(page)
                        except Exception as exc:
                            log(f"WARN reload register after mailbox switch: {exc}")
                        fill_register_country_dob(
                            page, country, dob_month, dob_day, dob_year
                        )
                        continue

                    human_delay(0.6, 1.2)
                    started = time.time()
                    log("POST email")
                    try:
                        page.locator("#basicInfoNextBtn[aria-disabled='false'], #basicInfoNextBtn:not(.disabled)").first.wait_for(state="visible", timeout=30000)
                    except Exception:
                        pass
                    if not click_next(page):
                        failure_reason = "email_submit_failed"
                        screenshot(page, "pw_register_email_fail.png")
                        failure_result(email, failure_reason, final_url=safe_url(page.url))
                        browser.close()
                        return 2
                    verify_ready = False
                    for _ in range(20):
                        page.wait_for_timeout(1000)
                        html = page_blob(page)
                        text = visible_text(page)
                        final_url = page.url
                        final_step = protocol.cur_step(html)
                        if looks_already_registered(html, text):
                            break
                        otp_boxes = 0
                        try:
                            otp_boxes = page.locator(EMAIL_VERIFY_READY_LOCATOR).count()
                        except Exception:
                            otp_boxes = 0
                        if is_email_verify_step(final_step, text, html) or otp_boxes:
                            verify_ready = True
                            break
                        if looks_like_captcha(html):
                            break
                    log(
                        f"after email step={final_step} url={safe_url(page.url)} "
                        f"verify_ready={verify_ready}"
                    )

                    if looks_already_registered(html, text):
                        continue
                    email_accepted = True
                    break

                if not email_accepted:
                    log("FAIL: email already registered with EA")
                    screenshot(page, "pw_register_already_registered.png")
                    failure_result(
                        email,
                        "email_already_registered",
                        final_url=safe_url(page.url),
                        skipped_ea_emails=skipped_ea_emails,
                    )
                    browser.close()
                    return 2

                if not verify_ready and not is_email_verify_step(
                    final_step, text, html
                ):
                    if should_abort_as_captcha(html, text=text, step=final_step):
                        log("FAIL: captcha required at this step (not implemented yet)")
                        Path(ARTIFACT_ROOT / "captcha_page.html").write_text(html)
                        screenshot(page, "pw_register_captcha.png")
                        failure_result(
                            email,
                            "captcha_required",
                            final_url=safe_url(page.url),
                            final_step=final_step,
                        )
                        browser.close()
                        return 3
                    log(f"WARN unexpected step after email: {final_step}")

                # 4) Graph OTP
                code = wait_graph_code(email_data, started, page=page)
                if not code:
                    wait_seconds = protocol.mail_wait_seconds()
                    log(f"FAIL: no verification code received within {wait_seconds}s")
                    screenshot(page, "pw_register_otp_timeout.png")
                    failure_result(
                        email,
                        "email_verification_timeout",
                        final_url=safe_url(page.url),
                        final_step=protocol.cur_step(page_blob(page)),
                        mail_wait_seconds=wait_seconds,
                    )
                    browser.close()
                    return 4

                # 5) submit verification code
                human_delay(1.0, 2.0)
                fill_verify_code(page, code)
                human_delay(0.6, 1.2)
                log("POST email verify code")
                if not click_next(page):
                    failure_result(
                        email,
                        "email_verification_submit_failed",
                        final_url=safe_url(page.url),
                    )
                    browser.close()
                    return 5
                page.wait_for_timeout(2500)
                html = page_blob(page)
                text = visible_text(page)
                final_step = protocol.cur_step(html)
                log(f"after verify step={final_step} url={safe_url(page.url)}")
                if (
                    "invalid" in text.lower()
                    and "code" in text.lower()
                    and final_step
                    and "verify" in (final_step or "").lower()
                ):
                    log("FAIL: code rejected")
                    screenshot(page, "pw_register_otp_rejected.png")
                    failure_result(
                        email,
                        "email_verification_rejected",
                        final_url=safe_url(page.url),
                        final_step=final_step,
                    )
                    browser.close()
                    return 5

                # 6) originId + password
                created = False
                for attempt in range(1, 6):
                    try:
                        id_box = page.locator(
                            "input[name=originId], #originId, input[name=originID]"
                        ).first
                        pw_box = page.locator(
                            "input[name=password], #password, input[type=password]"
                        ).first
                        if not (id_box.count() and pw_box.count()):
                            # May already have left create after a prior attempt.
                            if on_myaccount(page) or "juno/create" not in (page.url or ""):
                                created = True
                                break
                            if attempt == 1:
                                log("WARN create form not visible yet")
                                page.wait_for_timeout(1500)
                                continue
                        # Verify Origin ID uniqueness
                        id_accepted = False
                        for id_try in range(1, 6):
                            human_delay(0.4, 0.8)
                            id_box.click()
                            id_box.fill("")
                            human_type(page, id_box, origin_id)
                            page.keyboard.press("Tab")
                            page.wait_for_timeout(1500)

                            eaid_err = page.locator(
                                "#eaid-form-errors p.otkform-group-haserror, "
                                "#form-error-eaid-existed:visible, #form-error-eaid-too-long:visible"
                            ).first
                            if eaid_err.count() and eaid_err.is_visible():
                                err_txt = eaid_err.inner_text().strip()
                                log(f"WARN EA ID '{origin_id}' rejected ({err_txt}), generating alternative...")
                                origin_id = rand_gamer_id()
                                continue
                            else:
                                log(f"EA ID '{origin_id}' valid and available!")
                                id_accepted = True
                                break

                        if not id_accepted:
                            log("WARN could not find acceptable EA ID after 5 tries")
                            failure_reason = "ea_id_rejected"
                            break

                        human_delay(0.4, 0.8)
                        fill_secret(page, pw_box, password)
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(500)
                        check_tos(page)
                        human_delay(0.8, 1.5)
                        log(f"POST create/password attempt={attempt} origin_id={origin_id}")
                        if not click_next(page):
                            screenshot(page, "pw_register_create_fail.png")
                            failure_reason = "create_submit_failed"
                            break
                        page.wait_for_timeout(3500)
                    except Exception as exc:
                        log(f"create form error: {type(exc).__name__}: {exc}")
                        screenshot(page, "pw_register_create_error.png")
                        failure_reason = "create_form_error"
                        break

                    html = page_blob(page)
                    text = visible_text(page)
                    final_url = page.url
                    final_step = protocol.cur_step(html)
                    log(
                        f"after password step={final_step} "
                        f"url={safe_url(page.url)} text={text[:200]}"
                    )
                    registration_error = protocol.registration_error_reason(html)
                    if registration_error == "email_already_registered":
                        # Prior attempt may have committed; verify before failing.
                        if attempt > 1 and pw_login_verify(
                            page, context, email, password, origin_id
                        ):
                            account_login_verified = True
                            account_profile_completed = True
                            created = True
                            break
                        failure_reason = "email_already_registered"
                        break
                    if registration_error:
                        failure_reason = registration_error
                        break

                    outcome = protocol.classify_create_response(
                        final_step, page.url, text
                    )
                    if outcome == "technical_issue":
                        log("EA returned technicalIssue after create/password")
                        if pw_login_verify(page, context, email, password, origin_id):
                            account_login_verified = True
                            account_profile_completed = True
                            created = True
                            break
                        if attempt < 5:
                            time.sleep(1)
                            continue
                        failure_reason = "technical_issue"
                        break
                    if outcome == "left_create" or on_myaccount(page) or "chrome-error" in (page.url or ""):
                        created = True
                        break
                    # Still on basicInfo — verify before retrying same identity.
                    if pw_login_verify(page, context, email, password, origin_id):
                        account_login_verified = True
                        account_profile_completed = True
                        created = True
                        break
                    time.sleep(0.5)

                if failure_reason:
                    failure_result(
                        email,
                        failure_reason,
                        password=password,
                        originId=origin_id,
                        final_url=safe_url(page.url),
                        final_step=final_step,
                    )
                    browser.close()
                    return 6

                # 7) Prefer PW login verify when create appeared to succeed.
                if not account_login_verified:
                    if pw_login_verify(page, context, email, password, origin_id):
                        account_login_verified = True
                        account_profile_completed = True
                    elif created:
                        # Fallback: protocol finish (same credentials).
                        log("PW login verify incomplete; falling back to protocol_finish")
                        finish = protocol.protocol_finish_registration(
                            email, password, origin_id, proxy
                        )
                        log(f"post-create login check={finish}")
                        if finish == "complete":
                            account_login_verified = True
                            account_profile_completed = True
                            log(
                                "registration credentials and EA profile verified "
                                "after redirect"
                            )

                cookie_path = ARTIFACT_ROOT / "ea_connect_cookies.json"
                if cookie_path.is_file():
                    try:
                        connect_cookies = json.loads(cookie_path.read_text())
                    except (OSError, json.JSONDecodeError):
                        connect_cookies = None
                if connect_cookies is None:
                    connect_cookies = capture_connect_cookies(context)
                    persist_connect_cookies(connect_cookies)

                ok = bool(account_login_verified and account_profile_completed)
                if not ok and not failure_reason:
                    failure_reason = protocol.unresolved_failure_reason(final_step)

                payload = {
                    "email": email,
                    "password": password,
                    "originId": origin_id,
                    "final_url": safe_url(page.url),
                    "final_step": final_step or protocol.cur_step(page_blob(page)),
                    "ok_guess": ok,
                    "account_login_verified": account_login_verified,
                    "account_profile_completed": account_profile_completed,
                    "failure_reason": None if ok else failure_reason,
                    "skipped_ea_emails": skipped_ea_emails,
                    "connect_cookies": connect_cookies if ok else None,
                    "register_engine": engine_name,
                }
                write_result(payload)
                screenshot(page, "pw_register_done.png")
                log(f"DONE ok_guess={ok}")
                log(
                    "result saved="
                    f"{OUT_RESULT} final_step={payload['final_step']!r} "
                    f"connect_cookies={bool(connect_cookies and connect_cookies.get('remid'))}"
                )
                browser.close()
                return 0 if ok else 6
            except Exception as exc:
                reason = protocol.fatal_failure_reason(exc) if hasattr(
                    protocol, "fatal_failure_reason"
                ) else f"fatal_{type(exc).__name__}"
                net = net_error_reason(exc)
                if net:
                    reason = "proxy_error"
                log(f"FATAL: {reason} {exc}")
                try:
                    screenshot(page, "pw_register_fatal.png")
                except Exception:
                    pass
                failure_result(email, reason, password=password, originId=origin_id)
                try:
                    browser.close()
                except Exception:
                    pass
                return 6
    finally:
        release_fingerprint_window(bit_client, bit_profile_id)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        reason = protocol.fatal_failure_reason(exc)
        log(f"FATAL: {reason}")
        try:
            failure_result(str(os.environ.get("EA_EMAIL") or "").strip(), reason)
        except Exception as result_error:
            log(
                "FATAL: could not persist failure result "
                f"({type(result_error).__name__})"
            )
        raise

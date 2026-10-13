#!/usr/bin/env python3
"""EA Full Setup (一条龙注册流水线) via single Playwright browser session.

Executes in a single Chromium browser session under a sticky residential proxy:
  Phase 1: EA Registration (realistic human ID, keystroke jitter, Graph OTP)
           -> Immediate Checkpoint & EA Account persistence
  Phase 1.5: EA Session Bridge (myaccount authentication, login OTP via Graph if prompted)
  Phase 2: Steam Linking (Same session navigation, Steam OAuth, Steam Guard OTP)
           -> Immediate Checkpoint & Link persistence
  Phase 3: 2FA Binding (Same session navigation, EA Identity OTP, TOTP secret extraction & activation)
           -> Immediate Checkpoint & TOTP Secret persistence

Provides automatic retry loops for each phase (e.g. reload and retry on transient network/SPA stalls),
ensuring that any partially completed assets (EA, Steam, Link) are NEVER lost even if a later step fails.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright


from apex_registration.utils.browser_profile import (  # noqa: E402
    pick_register_country,
    rand_gamer_id,
    save_page_screenshot,
)
from apex_registration.utils.flow_runtime import FlowRuntime  # noqa: E402
from apex_registration.recovery import restore_checkpoint, transport_failure

from . import try_ea_once as protocol  # noqa: E402
from . import ea_pw_register as pw_reg  # noqa: E402
from . import ea_pw_link_2fa as pw_link  # noqa: E402

ROOT = Path(__file__).resolve().parent
ARTIFACT_ROOT = Path(os.environ.get("ACCOUNT_FLOW_OUTPUT_DIR") or ROOT)
OUT_LOG = ARTIFACT_ROOT / "last_run.log"
OUT_RESULT = ARTIFACT_ROOT / "ea_full_setup_result.json"
OUT_EA_RESULT = ARTIFACT_ROOT / "protocol_register_result.json"
OUT_LINK_RESULT = ARTIFACT_ROOT / "pw_link_2fa_result.json"
EMAIL_POOL = Path(os.environ.get("ACCOUNT_FLOW_EMAIL_POOL") or "/tmp/emails.json")
STEAM_ACCOUNT_FILE = Path(os.environ.get("STEAM_ACCOUNT_FILE") or ROOT.parent / "steam" / "accounts_succ.txt")

# Real fingerprint browser over residential proxy: React / SPA mounts slowly.
# Prefer one long wait over short timeouts that burn a full retry cycle.
SLOW_UI_MS = 45_000
MED_UI_MS = 30_000
NAV_MS = 30_000
RUNTIME = FlowRuntime(ARTIFACT_ROOT)


class FullSetupError(RuntimeError):
    def __init__(self, code: str, message: str, stage: str, *, needs_review: bool = True):
        super().__init__(message)
        self.code, self.stage = code, stage
        self.needs_review = needs_review


def log(msg: str) -> None:
    safe = re.sub(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", "[EMAIL]", str(msg), flags=re.I)
    safe = re.sub(r"(https?://)[^/@\s]+@", r"\1[AUTH]@", safe, flags=re.I)
    safe = re.sub(r"([?&](?:code|token|stoken|state|sid|sessionid)=)[^&\s]+", r"\1[REDACTED]", safe, flags=re.I)
    line = f"[{time.strftime('%H:%M:%S')}] {safe}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    try:
        with OUT_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


def safe_url(url: str) -> str:
    return pw_link.safe_url(url)


def document_html(page) -> str:
    """Read the document, retrying while Playwright is mid-navigation.

    ``page.content()`` raises if the email step (or a later SPA hop) is
    still changing the document. That used to abort the whole task.
    """
    last_error = None
    for _ in range(4):
        try:
            return page.content()
        except Exception as exc:
            last_error = exc
            if "navigat" not in str(exc).lower():
                raise
            try:
                page.wait_for_timeout(300)
            except Exception:
                pass
    log(f"WARN document read during navigation: {type(last_error).__name__}")
    return ""


def dump_page_html(page, name: str, limit: int = 20000) -> None:
    """Keep a bounded HTML dump when screenshots are skipped or login stalls."""
    try:
        html = page.content()
    except Exception:
        return
    if not isinstance(html, str) or not html:
        return
    try:
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
        path = ARTIFACT_ROOT / name
        path.write_text(html[: max(1000, limit)], encoding="utf-8")
        log(f"html dump saved: {name}")
    except Exception:
        return


def screenshot(page, name: str) -> None:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTIFACT_ROOT / name
    log(f"screenshot start: {name}")
    with RUNTIME.step("screenshot", RUNTIME.operation.get("stage", "diagnostics"), "保存诊断截图", 8):
        if save_page_screenshot(page, path, full_page=True, timeout=2500, cdp_fallback=False):
            log(f"screenshot saved: {name}")
        else:
            log(f"screenshot skipped: {name}")
    # Do not call page.content() here. It can stall past the 8s screenshot
    # watchdog and abort a successful create/login before Steam linking.


def close_task_browser(browser, result: dict, client=None, profile_id=None) -> None:
    with RUNTIME.step("browser_close", result["stage"], "关闭本任务浏览器", 20):
        # Ask Roxy to close first so it can wait() its Chrome children.
        # Playwright CDP close kills Chrome out from under Roxy, which leaves
        # RoxyChrome/chrome_crashpad zombies because the sidecar is PID 1.
        if client and profile_id and not pw_reg.keep_fingerprint_window():
            try:
                client.close(profile_id)
            except Exception as exc:
                log(f"Roxy close failed: {type(exc).__name__}; falling back to Playwright close")
        try:
            browser.close()
        except Exception as exc:
            log(f"browser close failed: {type(exc).__name__}; parent will clean recorded profile")


def atomic_json_checkpoint(path: Path, payload: dict) -> None:
    # Readers must only observe a complete old or new checkpoint, never truncation.
    data = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def save_checkpoint(result: dict[str, Any]) -> None:
    """Save multi-format checkpoints so existing monitors, services, and tests can inspect progress."""
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)
    # The primary recovery record is mandatory before any account mutation.
    atomic_json_checkpoint(OUT_RESULT, result)
    try:
        # Compatibility checkpoint: EA registration result
        ea_data = {
            "email": result.get("ea_email"),
            "password": result.get("ea_password"),
            "originId": result.get("ea_origin_id"),
            "account_login_verified": result.get("account_login_verified", False),
            "account_profile_completed": result.get("account_profile_completed", False),
            "register_engine": pw_reg._register_engine_name(),
            "ok_guess": result.get("ea_registered", False),
            "failure_reason": result.get("ea_failure_reason"),
            "connect_cookies": result.get("connect_cookies"),
        }
        atomic_json_checkpoint(OUT_EA_RESULT, ea_data)

        # Compatibility checkpoint: Link & 2FA result
        link_data = {
            "ea_email": result.get("ea_email"),
            "steam_user": result.get("steam_user"),
            "steam_link": result.get("steam_link", False),
            "linked_steam_persona": result.get("linked_steam_persona"),
            "totp": result.get("totp", False),
            "totp_secret": result.get("totp_secret"),
            "backup_codes": result.get("backup_codes") or [],
            "identity_verified": result.get("account_login_verified", False),
            "stage": result.get("stage", "init"),
            "error_code": result.get("error_code"),
            "error_message": result.get("error_message"),
            "needs_review": result.get("needs_review", False),
        }
        atomic_json_checkpoint(OUT_LINK_RESULT, link_data)
    except Exception as exc:
        log(f"WARN compatibility checkpoint write error: {type(exc).__name__}")


def load_pool_mailboxes() -> dict[str, dict]:
    pool_file = EMAIL_POOL if EMAIL_POOL.is_file() else ARTIFACT_ROOT / "mailboxes.json"
    if not pool_file.is_file():
        return {}
    try:
        data = json.loads(pool_file.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {m["email"].lower(): m for m in data if isinstance(m, dict) and "email" in m}
        elif isinstance(data, dict):
            return {k.lower(): v for k, v in data.items() if isinstance(v, dict)}
    except Exception as e:
        log(f"WARN load mailboxes error: {e}")
    return {}


def get_mailbox_record(email: str, mailboxes: dict[str, dict]) -> dict | None:
    if not email:
        return None
    email_l = email.strip().lower()
    if email_l in mailboxes:
        return mailboxes[email_l]
    return None


def load_steam_info() -> dict[str, str] | None:
    f = None
    env_file = os.environ.get("STEAM_ACCOUNT_FILE")
    if env_file and Path(env_file).is_file():
        f = Path(env_file)
    elif (ARTIFACT_ROOT / "steam_account.txt").is_file():
        f = ARTIFACT_ROOT / "steam_account.txt"
    elif STEAM_ACCOUNT_FILE.is_file():
        f = STEAM_ACCOUNT_FILE

    if not f or not f.is_file():
        return None
    content = f.read_text(encoding="utf-8").strip()
    if not content:
        return None
    for line in content.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        if len(parts) >= 3:
            return {
                "username": parts[0].strip(),
                "password": parts[1].strip(),
                "email": parts[2].strip(),
            }
    return None


def wait_mail_otp(
    mailbox: dict,
    keys: tuple[str, ...],
    *,
    timeout: int = 60,
    code_re: str = r"(\d{6,8})",
    after_ts: float | None = None,
) -> str | None:
    requested_at = time.time() if after_ts is None else after_ts
    t0 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(requested_at))
    return pw_link.wait_mail_code(mailbox, t0, keys, timeout=timeout, code_re=code_re)


def wait_mailbox_code(
    page,
    mailbox: dict,
    keys: tuple[str, ...],
    after_ts: float,
    *,
    timeout: int | None = None,
    code_re: str = r"(\d{6,8})",
) -> str | None:
    """Poll Graph, click send/resend once if nothing arrives, then keep polling.

    ``after_ts`` stays put so a code that landed before the resend still counts.
    """
    budget = int(timeout if timeout is not None else protocol.mail_wait_seconds())
    started = time.monotonic()
    resent = False
    while True:
        remaining = budget - (time.monotonic() - started)
        if remaining <= 0:
            return None
        slice_s = 40 if not resent else remaining
        slice_s = max(1, int(min(slice_s, remaining)))
        code = wait_mail_otp(
            mailbox, keys, timeout=slice_s, code_re=code_re, after_ts=after_ts,
        )
        if code:
            return code
        if resent or page is None:
            return None
        clicked = pw_link.click_send_or_resend(page)
        log(
            "mailbox still empty; clicked send/resend"
            if clicked
            else "mailbox still empty; no send/resend control"
        )
        resent = True


def account_json(page, path: str, *, method: str = "GET", data: dict | None = None):
    """First-party EA Account Management JSON RPC bridge in-page."""
    return page.evaluate(
        """async ({path, method, data}) => {
          const headerMeta = document.querySelector('meta[name="csrf-header-name"]');
          const headerName = headerMeta?.content || '';
          const token = headerName
            ? document.querySelector(`meta[name="${CSS.escape(headerName)}"]`)?.content || ''
            : '';
          const headers = {'X-Requested-With':'XMLHttpRequest'};
          if (headerName && token) headers[headerName] = token;
          let url = path;
          let body;
          if (method === 'GET' && data) {
            url += '?' + new URLSearchParams(data).toString();
          } else if (data) {
            const form = new URLSearchParams(data);
            const telemetry = {
              'am-pbd':'pbd', 'am-pbdt':'pbdt', 'am-pbde':'pbde',
              'am-df':'df', 'am-dft':'dft', 'am-dfe':'dfe'
            };
            for (const [id, name] of Object.entries(telemetry)) {
              const value = document.getElementById(id)?.value || '';
              if (value) form.append(name, value);
            }
            headers['Content-Type'] = 'application/x-www-form-urlencoded';
            body = form.toString();
          }
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), 15000);
          try {
            const response = await fetch(url, {
              method, credentials:'same-origin', headers, body, signal:controller.signal
            });
            const payload = await response.json();
            return {ok:response.ok, status:response.status, payload};
          } catch (_) {
            return {ok:false, status:0, payload:{errorCode:controller.signal.aborted ? 'account_rpc_timeout' : 'fetch_failed'}};
          } finally {
            clearTimeout(timer);
          }
        }""",
        {"path": path, "method": method, "data": data},
    )


def wait_registration_code(page, mailbox: dict, requested_at: float, *, timeout_ms=MED_UI_MS) -> str:
    """Only poll email after EA has actually entered the verification step."""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        html, text = document_html(page), pw_reg.visible_text(page)
        if pw_reg.looks_rate_limited(html, text):
            raise FullSetupError("ea_rate_limited", "EA 提示请求次数过多", "submit_email", needs_review=False)
        if pw_reg.looks_already_registered(html, text):
            raise FullSetupError("ea_email_already_registered", "EA 已确认该邮箱注册过", "submit_email", needs_review=False)
        if pw_reg.is_email_verify_step(protocol.cur_step(html), text, html):
            break
        if time.monotonic() >= deadline:
            raise FullSetupError("ea_email_submit_unconfirmed", "提交邮箱后未进入验证码页面，尚未确认邮件已发送", "submit_email", needs_review=False)
        page.wait_for_timeout(250)
    RUNTIME.start("registration_mail", "email_verify", "等待 EA 注册邮件验证码", 190)
    code = pw_reg.wait_graph_code(mailbox, requested_at, page=page)
    if not code:
        if pw_reg.looks_rate_limited(document_html(page), pw_reg.visible_text(page)):
            raise FullSetupError("ea_rate_limited", "EA 提示请求次数过多", "email_verify", needs_review=False)
        # Missing mail is a retryable delivery failure, never proof of an existing account.
        raise FullSetupError("ea_email_verify_timeout", "已进入验证码页面，但未收到 EA 注册验证码", "email_verify", needs_review=False)
    return code


STEAM_GUARD_INPUT = (
    "input[name=authcode]:visible, #authcode:visible, #twofactorcode_entry:visible, "
    "input[autocomplete='one-time-code']:visible, input[maxlength='1']:visible"
)


def steam_login_state(page) -> str:
    if page.is_closed():
        return "redirected"
    host = urlparse(page.url).hostname or ""
    if host == "ea.com" or host.endswith(".ea.com"):
        return "redirected"
    if not pw_link.is_steam_page(page.url):
        return "pending"
    if page.locator(STEAM_GUARD_INPUT).count():
        return "guard"
    text = pw_link.page_body_text(page)
    if re.search(r"incorrect (?:account|user|password)|check your password|invalid password|密码不正确|密碼不正確|帳戶名稱或密碼", text, re.I):
        return "rejected"
    if not pw_link.steam_password_field_present(page):
        consent = page.get_by_role("button", name=pw_link.STEAM_OAUTH_CONSENT_RE).first
        if consent.count() and consent.is_visible():
            return "consent"
    # Guard text alone is not an authenticated session: React may mount its inputs later.
    if re.search(r"Steam Guard|two.factor|验证码|驗證碼", text, re.I):
        return "pending"
    if not pw_link.steam_password_field_present(page) and pw_link.steam_authenticated_or_consent(page):
        return "consent"
    return "pending"


_STEAM_SESSION_READY = ("finalizelogin", "/login/settoken")


def _watch_steam_session(page) -> dict:
    """Notice Steam's own login APIs. The guard form can stay up after they succeed."""
    seen = {"ready": False, "detach": None}

    def on_response(response):
        try:
            url = str(response.url or "")
            status = int(response.status or 0)
        except Exception:
            return
        if status < 200 or status >= 300:
            return
        blob = f"{urlparse(url).path} {url}".lower()
        if any(marker in blob for marker in _STEAM_SESSION_READY):
            seen["ready"] = True

    try:
        page.on("response", on_response)
        seen["detach"] = on_response
    except Exception:
        pass
    return seen


def _close_steam_session_watch(page, watch: dict) -> None:
    detach = watch.get("detach")
    if detach is None:
        return
    try:
        page.remove_listener("response", detach)
    except Exception:
        pass


def finish_steam_login(page, mailbox: dict | None, requested_at: float, result: dict, *, timeout_ms=MED_UI_MS) -> None:
    deadline = time.monotonic() + timeout_ms / 1000
    submitted = False
    watch = _watch_steam_session(page)
    try:
        while True:
            state = steam_login_state(page)
            if watch["ready"] and state != "rejected":
                log("Steam session API completed; leaving the guard form")
                return
            if state in {"consent", "redirected"}:
                return
            if state == "rejected":
                raise FullSetupError("steam_login_credentials_rejected", "Steam 拒绝了登录凭据", "steam_login")
            if state == "guard" and not submitted:
                result["stage"] = "steam_guard"
                save_checkpoint(result)
                if not mailbox:
                    raise FullSetupError("steam_guard_mailbox_missing", "Steam 要求验证码，但任务没有可读取的 Steam 邮箱", "steam_guard", needs_review=False)
                with RUNTIME.step("steam_guard_mail", "steam_guard", "等待 Steam Guard 验证码", 75):
                    code = wait_mail_otp(mailbox, ("steam", "guard", "access", "sign in", "login code"),
                                         timeout=60, code_re=r"\b([A-Z2-9]{5})\b", after_ts=requested_at)
                if not code:
                    raise FullSetupError("steam_guard_code_missing", "未收到 Steam Guard 验证码，登录尚未完成", "steam_guard", needs_review=False)
                boxes = page.locator(STEAM_GUARD_INPUT)
                if not boxes.count():
                    if steam_login_state(page) in {"consent", "redirected"} or watch["ready"]:
                        return
                    raise FullSetupError("steam_guard_input_missing", "Steam Guard 验证码输入框已消失，登录尚未确认", "steam_guard")
                first = boxes.first
                if first.get_attribute("maxlength") == "1":
                    if boxes.count() != len(code):
                        raise FullSetupError("steam_guard_input_invalid", "Steam Guard 输入框数量与验证码不一致", "steam_guard")
                    for index, char in enumerate(code):
                        boxes.nth(index).fill(char)
                else:
                    first.fill(code)
                submitted = True  # Filling the last digit may submit automatically.
                page.wait_for_timeout(750)
                if steam_login_state(page) == "guard":
                    button = page.get_by_role("button", name=re.compile(r"^(Submit|Sign in|Continue|提交|登入|登录|继续|繼續)$", re.I)).first
                    if button.count() and button.is_visible() and button.is_enabled():
                        try:
                            button.click(timeout=3000)
                        except PWTimeout:
                            pass  # Reconcile the delivered click; never submit it again.
                deadline = time.monotonic() + timeout_ms / 1000
            if watch["ready"]:
                log("Steam session API completed; leaving the guard form")
                return
            if time.monotonic() >= deadline:
                if submitted:
                    raise FullSetupError("steam_guard_unconfirmed", "Steam Guard 提交后未确认通过验证", "steam_guard")
                raise FullSetupError(
                    "steam_login_transition_missing",
                    "Steam 登录后未进入验证码或授权页面",
                    "steam_login",
                    needs_review=False,
                )
            page.wait_for_timeout(250)
    finally:
        _close_steam_session_watch(page, watch)


def checkpoint_tfa_enabled(result: dict) -> None:
    result["totp"] = True
    save_checkpoint(result)


_STEAM_ACCOUNT_PICKER_PATHS = {"/oauth/login", "/openid/login"}


def steam_oauth_account_picker(url: str) -> bool:
    """The signed-in 登录 control only continues OAuth on these paths.

    The same label on the guard form and on /login/home finishes a normal
    Steam login and abandons the EA return URL.
    """
    path = (urlparse(url or "").path or "").rstrip("/").lower()
    return path in _STEAM_ACCOUNT_PICKER_PATHS


def steam_guard_visible(page) -> bool:
    try:
        return bool(page.locator(STEAM_GUARD_INPUT).count())
    except Exception:
        return False


def steam_oauth_handed_off(result: dict) -> bool:
    """True after the account picker, Allow, or the EA link button was submitted."""
    return bool(
        result.get("steam_oauth_account_login_submitted")
        or result.get("steam_oauth_authorize_submitted")
        or result.get("ea_link_consent_submitted")
    )


def steam_consent_pending(context) -> bool:
    """A live Steam tab means the EA callback has not taken over yet."""
    if pw_link.ea_steam_callback_reached(context):
        return False
    for candidate in pw_link.iter_live_pages(context):
        if pw_link.is_steam_page(getattr(candidate, "url", "") or ""):
            return True
    return False


def advance_steam_consent(page, submitted: set[str]) -> str | None:
    """Steam's account picker and permission grant are separate submissions."""
    if page.is_closed() or not pw_link.is_steam_page(page.url) or pw_link.steam_password_field_present(page):
        return None
    # finalizelogin can return while this form is still on screen. 登录 here
    # navigates to /login/home and consumes the one account-picker click.
    if steam_guard_visible(page):
        return None
    allow = page.get_by_role("button", name=pw_link.STEAM_OAUTH_CONSENT_RE).first
    try:
        allow_visible = bool(allow.count() and allow.is_visible())
    except Exception:
        allow_visible = False
    if allow_visible:
        action = "authorize"
    elif steam_oauth_account_picker(page.url):
        action = "account_login"
    else:
        return None
    if action in submitted:
        return None
    # Remember the specific submission before clicking: navigation can lose the
    # click reply. A delivered account-login must not suppress the later Allow.
    if pw_link.click_oauth_consent(page, on_ea=False, before_click=lambda: submitted.add(action)):
        return action
    return None


def reconcile_tfa_identity(page, code: str, am_json) -> None:
    if not pw_link.identity_challenge_visible(page):
        return
    log("Identity modal remained visible, executing in-page RPC fallback...")
    reply = am_json("/am/data/1/verify-code", method="POST", data={"code": code})
    # A delayed UI submission may have completed while the fallback was running.
    if not pw_link.identity_challenge_visible(page):
        return
    payload = reply.get("payload") or {}
    if payload.get("errorCode") not in (None, "", 0, "0") or payload.get("success") is False:
        raise FullSetupError("tfa_identity_rejected", "EA 未接受本次身份验证码", "tfa_identity")
    if not reply.get("ok"):
        raise FullSetupError("tfa_identity_unconfirmed", "身份验证码请求未返回成功结果", "tfa_identity")
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    pw_link.dismiss_cookies(page)
    with RUNTIME.step("tfa_open_settings", "tfa_setup", "打开 2FA 设置面板", 40):
        pw_link.open_tfa_settings(page, timeout_ms=MED_UI_MS, confirm_open=True)
    page.wait_for_timeout(2000)
    if pw_link.identity_challenge_visible(page):
        raise FullSetupError("tfa_identity_unconfirmed", "身份验证码提交后仍停留在验证页面", "tfa_identity")


def activate_tfa(am_json, code: str) -> bool:
    # Submit once, then use the existing final status check. A stale UI input
    # must not cause a second submission of the same accepted code.
    reply = am_json("/am/data/1/verify-tfa-code", method="POST", data={"code": code})
    status = am_json("/am/data/1/security-privacy")
    if status.get("ok") and pw_link.app_2fa_enabled(status.get("payload") or {}):
        return True
    payload = reply.get("payload") or {}
    if payload.get("errorCode") not in (None, "", 0, "0") or payload.get("success") is False:
        raise FullSetupError("tfa_activation_rejected", "EA 拒绝了 Authenticator 激活验证码", "tfa_activation")
    return False


def refresh_completed_session(context, page, result: dict) -> None:
    """Optional session enrichment must run after the complete business checkpoint."""
    try:
        with RUNTIME.step("capture_cookies", "complete", "更新 EA 会话", 20):
            cookies = pw_reg.recapture_connect_cookies(context, page=page)
        if cookies:
            result["connect_cookies"] = cookies
            save_checkpoint(result)
    except Exception as exc:
        log(f"WARN completed session refresh skipped: {type(exc).__name__}")


def ea_creation_transitioned(url: str) -> bool:
    # initref/redirect_uri already contain ea.com/login_check on the create
    # form. Only the actual destination can confirm that navigation advanced.
    parsed = urlparse(str(url))
    host = (parsed.hostname or "").lower()
    return (host == "ea.com" or host.endswith(".ea.com")) and not parsed.path.rstrip("/").endswith("/juno/create")


def submit_ea_creation(page, result: dict) -> None:
    RUNTIME.start("create_submit", "create_account", "提交 EA 创建表单并确认跳转", 75)
    # Preserve the attempted identity before the first possible side effect.
    result["ea_creation_submitted"] = True
    save_checkpoint(result)
    log("EA creation attempt checkpoint saved")
    submitted = False
    for _ in range(8):
        if "juno/create" not in page.url:
            # The preceding click completed despite losing its reply.
            submitted = True
            break
        pw_reg.check_tos(page)
        loc = page.locator(
            "#btnSubmit:visible, a.otkbtn-primary:visible, "
            "#basicInfoNextBtn:visible, #createBtn:visible"
        ).first
        try:
            if loc.count() and loc.is_visible():
                aria = (loc.get_attribute("aria-disabled") or "").lower()
                cls = (loc.get_attribute("class") or "").lower()
                if aria in {"true", "1"} or "disabled" in cls.split():
                    page.wait_for_timeout(400)
                    continue
                loc.click(timeout=5000)
                submitted = True
                break
        except PWTimeout:
            # The browser may have delivered the click before losing the reply.
            # Reconcile this attempt rather than creating twice on a slow page.
            log("create click timed out; verifying the existing submission")
            submitted = True
            break
        except Exception as exc:
            raise FullSetupError("ea_create_submit_failed", "EA 创建动作失败，已保留凭据待复核", "create_account") from exc
    if not submitted:
        # A click timeout may have navigated already; do not submit an
        # unrelated control on the newly loaded login page.
        if "juno/create" in page.url:
            submitted = pw_reg.click_next(page)
        if not submitted and "juno/create" in page.url:
            raise FullSetupError("ea_create_submit_unconfirmed", "EA 创建按钮提交未确认，已保留本次凭据", "create_account")
    try:
        page.wait_for_url(
            ea_creation_transitioned,
            timeout=NAV_MS,
        )
    except Exception:
        pass
    page.wait_for_timeout(3000)
    log(f"after create submit url={safe_url(page.url)}")
    screenshot(page, "pw_after_create_submit.png")

    if "juno/create" in page.url:
        text = pw_reg.visible_text(page)
        # Reading the whole document can block across navigation in Roxy.
        # The server error is a dedicated element; hidden validation labels
        # must not be mistaken for an actual registration rejection.
        error = page.locator(".int-registration-error:visible").first
        reg_err = None
        try:
            if error.count():
                reg_err = error.inner_text(timeout=2000).strip() or None
        except PWTimeout:
            pass
        log(f"WARN still on juno/create after submit, reason={reg_err}, text={text[:150]}")
        screenshot(page, "pw_create_failed.png")
        if reg_err:
            raise FullSetupError("ea_create_rejected", "EA 创建表单返回错误，注册未确认", "create_account")
        # An ambiguous submit must be reconciled by login, not repeated.
        log("Create result ambiguous; verifying the same credentials")


def ensure_myaccount_session(
    page, context, ea_mailbox: dict, password: str, origin_id: str,
    timeout_ms: int = 60000, *, navigate: bool = True, target_page=None,
) -> bool:
    """Verify an authenticated account response; a URL change alone proves nothing.

    After Steam OAuth, EA often parks the linking callback on juno/login.
    Completing that login in place (`navigate=False`) keeps the OAuth state;
    jumping to security-privacy first drops the callback.
    """
    current = target_page or page
    target_url = "https://myaccount.ea.com/am/ui/security-privacy"
    log("Ensuring session on myaccount.ea.com..." if navigate else "Completing EA login on the current callback page...")
    otp_after = None
    for attempt in range(2):
        login_started_at = time.time()
        if navigate:
            try:
                current.goto(target_url, wait_until="domcontentloaded", timeout=timeout_ms)
            except Exception as exc:
                if isinstance(exc, PWTimeout):
                    log("myaccount navigation timed out; checking current login state")
                elif transport_failure(exc):
                    log(f"myaccount transport interrupted on attempt {attempt + 1}")
                    continue
                else:
                    raise
        pw_link.dismiss_cookies(current)
        pw_link.dismiss_password_save(current)
        pw_link.ensure_remember_device(current)
        if not pw_reg.on_myaccount(current):
            transitioned = pw_reg.complete_ea_login_form(
                current, ea_mailbox["email"], password,
                timeout_ms=SLOW_UI_MS, require_transition=True,
            )
            if not transitioned:
                frame_urls = []
                try:
                    frame_urls = [safe_url(frame.url) for frame in list(current.frames)[:6]]
                except Exception:
                    pass
                log(
                    f"login did not advance on attempt {attempt + 1} "
                    f"url={safe_url(current.url)} frames={frame_urls} "
                    f"text={pw_reg.visible_text(current)[:180]!r}"
                )
                dump_page_html(current, f"pw_login_stalled_{attempt + 1}.html")
                continue

        if pw_link.is_login_otp_page(pw_reg.page_blob(current), current.url, current.title()):
            log("Login OTP challenge detected")
            if otp_after is None:
                if pw_link.SEND_CODE_NAME_RE.search(pw_reg.page_blob(current)) or current.locator("#btnSendCode").count():
                    if not pw_link.click_send_code(current):
                        raise FullSetupError("ea_login_otp_send_failed", "EA 登录验证码发送按钮未提交", "ea_login_otp")
                otp_after = login_started_at
            else:
                # The previous code did not leave this page. Ask for another,
                # and ignore the one we already submitted.
                pw_link.click_send_or_resend(current)
                otp_after = time.time()
            code = wait_mailbox_code(
                current, ea_mailbox, pw_link.LOGIN_OTP_MAIL_KEYS, otp_after,
            )
            if not code:
                raise FullSetupError("ea_login_otp_missing", "未收到 EA 登录验证码", "ea_login_otp")
            log("Got EA login OTP")
            pw_link.ensure_remember_device(current)
            pw_reg.fill_verify_code(current, code)
            if not (pw_link.click_login_action(current) or pw_reg.click_next(current)):
                raise FullSetupError("ea_login_otp_submit_failed", "EA 登录验证码未提交", "ea_login_otp")
            log("Login OTP submitted")
        pw_reg.handle_post_create_gates(current, origin_id)
        try:
            current.wait_for_url(lambda u: "myaccount.ea.com" == urlparse(str(u)).hostname, timeout=SLOW_UI_MS)
        except PWTimeout:
            recovered = None
            for candidate in pw_link.iter_live_pages(context):
                if pw_reg.on_myaccount(candidate):
                    recovered = candidate
                    break
            if recovered is None:
                log(f"myaccount landing not confirmed on attempt {attempt + 1}")
                continue
            current = recovered
        reply = account_json(current, "/am/data/1/security-privacy")
        payload = reply.get("payload") or {}
        account = payload.get("result") or {}
        if reply.get("ok") and isinstance(account, dict) and "twoFacInfo" in account:
            log("Successfully authenticated on myaccount.ea.com!")
            if current is not page:
                try:
                    current.bring_to_front()
                except Exception:
                    pass
            return True
        log(f"account response not authenticated status={reply.get('status')}")
    screenshot(current, "pw_myaccount_session_fail.png")
    return False


def find_ea_login_page(context, fallback=None):
    for candidate in pw_link.iter_live_pages(context):
        if pw_link.ea_login_form_url(getattr(candidate, "url", "") or ""):
            return candidate
    return fallback


def find_myaccount_page(context, fallback=None):
    for candidate in pw_link.iter_live_pages(context):
        if pw_reg.on_myaccount(candidate):
            return candidate
    return fallback


def settle_ea_after_steam(
    page, context, ea_mailbox: dict, password: str, origin_id: str,
    timeout_ms: int = 45000,
) -> bool:
    """Finish Steam's EA callback the way a human tab does.

    ``steamc2s`` often passes through ``juno/login`` in the popup. That is a
    hop, not a dead session. If the original myaccount tab is still there,
    keep it. Only sign in again when that hop actually shows a login form.
    """
    deadline = time.monotonic() + max(5.0, timeout_ms / 1000)
    while time.monotonic() < deadline:
        if pw_link.click_ea_link_accounts_anywhere(context):
            log("OAuth 連結帳號 clicked after Steam callback")
            page.wait_for_timeout(400)
            continue
        if pw_link.pending_ea_link_accounts(context):
            page.wait_for_timeout(400)
            continue
        account = find_myaccount_page(context)
        # The original connected-accounts tab stays open for the whole OAuth.
        # It is not evidence that Steam handed the callback back.
        if account is not None and not steam_consent_pending(context):
            if account is not page:
                try:
                    account.bring_to_front()
                except Exception:
                    pass
            log(f"EA myaccount still live after Steam callback url={safe_url(account.url)}")
            return True
        login_page = find_ea_login_page(context)
        if login_page is not None:
            blob = pw_reg.page_blob(login_page)
            if pw_reg.login_credentials_visible(login_page) or pw_link.is_login_otp_page(
                blob, login_page.url, login_page.title()
            ):
                log("EA callback login form is real; signing in on this tab")
                return ensure_myaccount_session(
                    page, context, ea_mailbox, password, origin_id,
                    navigate=False, target_page=login_page,
                )
            if pw_reg.click_login_continue(login_page, timeout=1500):
                log("EA callback continue clicked on juno/login hop")
                login_page.wait_for_timeout(400)
                continue
        page.wait_for_timeout(400)
    account = find_myaccount_page(context)
    if (
        account is not None
        and not pw_link.pending_ea_link_accounts(context)
        and not steam_consent_pending(context)
    ):
        return True
    login_page = find_ea_login_page(context)
    if login_page is not None and (
        pw_reg.login_credentials_visible(login_page)
        or pw_link.is_login_otp_page(
            pw_reg.page_blob(login_page), login_page.url, login_page.title()
        )
    ):
        return ensure_myaccount_session(
            page, context, ea_mailbox, password, origin_id,
            navigate=False, target_page=login_page,
        )
    log("WARN Steam callback did not return to myaccount")
    dump_page_html(login_page or page, "pw_steam_callback_unsettled.html")
    return False


def finish_ea_login_after_steam(page, context, ea_mailbox: dict, password: str, origin_id: str) -> bool:
    """Back-compat wrapper; Steam callback settlement no longer assumes logout."""
    return settle_ea_after_steam(page, context, ea_mailbox, password, origin_id)


def capture_remembered_session(page, context, result: dict) -> dict | None:
    cookies = pw_reg.recapture_connect_cookies(context, page=page)
    if cookies:
        result["connect_cookies"] = cookies
        save_checkpoint(result)
    return cookies


def main() -> int:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT_LOG.write_text("")
    OUT_LOG.chmod(0o600)

    # Inputs
    ea_email = os.environ.get("EA_EMAIL", "").strip()
    proxy = os.environ.get("EA_PROXY", "").strip() or protocol.load_proxy()
    skip_steam = os.environ.get("SKIP_STEAM_LINK", "0").strip() in ("1", "true", "True")
    skip_2fa = os.environ.get("SKIP_2FA_SETUP", "0").strip() in ("1", "true", "True")
    skip_register = os.environ.get("SKIP_EA_REGISTER", "0").strip().lower() in (
        "1", "true", "yes",
    )

    mailboxes = load_pool_mailboxes()
    steam_info = None if skip_steam else load_steam_info()
    missing_steam_input = not skip_steam and not steam_info

    # Determine EA mailbox
    ea_mailbox = None
    if ea_email and ea_email.lower() in mailboxes:
        ea_mailbox = mailboxes[ea_email.lower()]
    elif mailboxes:
        first_key = next(iter(mailboxes))
        ea_mailbox = mailboxes[first_key]
        ea_email = ea_mailbox["email"]
    else:
        ea_mailbox = protocol.load_email()
        ea_email = ea_mailbox["email"]

    previous_registration = {}
    saved_run = json.loads(OUT_RESULT.read_text()) if OUT_RESULT.is_file() else {}
    if skip_register and OUT_EA_RESULT.is_file():
        try:
            previous_registration = json.loads(OUT_EA_RESULT.read_text())
        except (OSError, ValueError):
            pass

    # Master state
    result: dict[str, Any] = {
        "ea_email": ea_email,
        "ea_password": None,
        "ea_origin_id": None,
        "ea_registered": False,
        "ea_creation_submitted": False,
        "ea_failure_reason": None,
        "account_login_verified": False,
        "account_profile_completed": False,
        "connect_cookies": None,
        "steam_user": steam_info["username"] if steam_info else None,
        "steam_email": steam_info["email"] if steam_info else None,
        "steam_link": False,
        "steam_link_attempted": False,
        "steam_oauth_account_login_submitted": False,
        "steam_oauth_authorize_submitted": False,
        "ea_link_consent_submitted": False,
        "linked_steam_persona": None,
        "totp": False,
        "totp_secret": None,
        "backup_codes": [],
        "stage": "init",
        "error_code": None,
        "error_message": None,
        "needs_review": False,
        "notes": [],
        "skipped_ea_emails": [],
    }
    # A fresh browser must retain every commit marker, including OAuth handoff
    # and a pending 2FA secret. Errors belong to the previous attempt only.
    restore_checkpoint(result, saved_run)
    save_checkpoint(result)

    if missing_steam_input:
        result.update(error_code="steam_account_input_missing", error_message="一条龙缺少待关联的 Steam 账号", stage="preflight")
        save_checkpoint(result)
        return 1

    log(f"=== EA Full Setup Started ===")
    log(f"EA Target Email: {ea_email}")
    log(f"Steam Link Target: {steam_info['username'] if steam_info else 'SKIPPED'}")
    log(f"2FA Setup: {'ENABLED' if not skip_2fa else 'SKIPPED'}")
    log(f"Proxy: {proxy.split('@')[-1] if '@' in proxy else (proxy or 'direct')}")

    country = pick_register_country()
    dob_month, dob_day, dob_year = protocol.random_adult_dob()
    origin_id = result.get("ea_origin_id") or rand_gamer_id()
    password = result.get("ea_password") or protocol.rand_password()
    if skip_register:
        prev = previous_registration
        origin_id = (
            os.environ.get("EA_ORIGIN_ID", "").strip()
            or prev.get("originId")
            or origin_id
        )
        password = (
            os.environ.get("EA_PASSWORD", "").strip()
            or prev.get("password")
            or result.get("ea_password") or ""
        )
        if not password:
            result.update(error_code="ea_resume_credentials_missing", error_message="继续已有 EA 账号需要已保存的密码", stage="preflight", needs_review=True)
            save_checkpoint(result)
            return 1
    result["ea_origin_id"] = origin_id
    result["ea_password"] = password

    bit_client = None
    bit_profile_id = None
    engine_name = pw_reg._register_engine_name()

    def attach_task_browser(playwright, *, proxy, seed):
        RUNTIME.start("browser_open", "open_register", "打开并连接指纹浏览器", 150)
        attached = pw_reg.attach_browser(playwright, proxy=proxy, seed=seed,
                                        on_profile_opened=RUNTIME.record_browser)
        task_context = attached[1]

        def network_failure(req):
            if req.resource_type in {"document", "script", "font"}:
                log(f"Browser request failed: {req.resource_type} {safe_url(req.url)} {req.failure}")

        def network_response(response):
            path = urlparse(response.url).path
            if response.request.resource_type == "document" or any(
                marker in path for marker in ("AuthSession", "finalizelogin", "/login/settoken")
            ):
                log(f"Browser response: {response.status} {response.request.method} {safe_url(response.url)}")

        task_context.on("requestfailed", network_failure)
        task_context.on("response", network_response)
        return attached

    try:
      with sync_playwright() as playwright:
        browser, context, page, bit_client, bit_profile_id = attach_task_browser(
            playwright, proxy=proxy, seed=ea_email
        )
        steam_skip_emails = {
            str((steam_info or {}).get("email") or "").strip().lower()
        }
        steam_skip_emails.discard("")

        def switch_mailbox(reason: str) -> bool:
            nonlocal ea_mailbox, ea_email, page
            nxt = pw_reg.next_register_mailbox(
                mailboxes,
                burned=set(result.get("skipped_ea_emails") or []),
                current_email=ea_email,
                skip_emails=steam_skip_emails,
            )
            if not nxt:
                return False
            skipped = list(result.get("skipped_ea_emails") or [])
            skipped.append(ea_email)
            result["skipped_ea_emails"] = skipped
            # Only this explicit EA rejection may mark a mailbox as registered.
            result["already_registered_ea_emails"] = list(skipped)
            log(f"EA mailbox {reason}; switching mailbox in this window")
            screenshot(page, "pw_mailbox_switch.png")
            ea_mailbox = nxt
            ea_email = nxt["email"]
            result["ea_email"] = ea_email
            save_checkpoint(result)
            log("reloading register page after mailbox switch")
            try:
                RUNTIME.start("open_register", "open_register", "打开 EA 注册页面", 150)
                page = pw_reg.goto_register(page)
            except Exception as exc:
                log(f"WARN reload register after mailbox switch: {exc}")
            return True

        def am_json(path: str, *, method: str = "GET", data: dict | None = None):
            with RUNTIME.step("account_rpc", result["stage"], "读取或提交账号中心状态", 20):
                return account_json(page, path, method=method, data=data)

        # =====================================================================
        # PHASE 1: EA Registration
        # =====================================================================
        result["stage"] = "open_register"
        save_checkpoint(result)
        log(
            f"GET ea.com/register country={country} "
            f"dob={dob_year}-{dob_month}-{dob_day} engine={engine_name}"
        )

        creation_submitted = False
        if skip_register:
            log("SKIP_EA_REGISTER: reusing existing EA credentials in this browser")
            creation_submitted = True
        for reg_attempt in range(1, 5):
            if skip_register:
                break
            try:
                if not pw_reg.browser_still_connected(browser, context):
                    raise RuntimeError("Browser has been closed")
                RUNTIME.start("open_register", "open_register", "打开 EA 注册页面", 150)
                page = pw_reg.goto_register(page)
            except Exception as exc:
                log(f"WARN open register attempt {reg_attempt} failed: {type(exc).__name__}")
                # The supervisor verifies cleanup before reopening the browser.
                raise

            html = document_html(page)
            log(f"landed {safe_url(page.url)} len={len(html)}")
            text = pw_reg.visible_text(page)
            if pw_reg.looks_technical_issue(html, text, page.url):
                log(f"WARN EA technical issue page attempt={reg_attempt}")
                screenshot(page, "pw_technical_issue.png")
                if reg_attempt >= 3:
                    result["ea_failure_reason"] = "technical_issue"
                    result["error_code"] = "ea_technical_issue"
                    result["error_message"] = "EA 注册页返回技术错误"
                    save_checkpoint(result)
                    close_task_browser(browser, result, bit_client, bit_profile_id)
                    return 4
                page = pw_reg.recover_transient_register(
                    page, f"technical issue attempt {reg_attempt}"
                )
                continue

            RUNTIME.start("register_dob", "open_register", "提交生日与地区", 90)
            # 1.1 DOB
            if not pw_reg.fill_register_country_dob(
                page, country, dob_month, dob_day, dob_year
            ):
                log("WARN country/DOB step did not advance")
                screenshot(page, "pw_dob_stuck.png")
                if pw_reg.looks_technical_issue(
                    pw_reg.page_blob(page), pw_reg.visible_text(page), page.url
                ):
                    page = pw_reg.recover_transient_register(page, "dob technical issue")
                else:
                    page = pw_reg.recover_transient_register(page, "dob stuck")
                if reg_attempt >= 4:
                    result["ea_failure_reason"] = "dob_submit_failed"
                    result["error_code"] = "ea_register_failed"
                    save_checkpoint(result)
                    close_task_browser(browser, result, bit_client, bit_profile_id)
                    return 2
                continue

            RUNTIME.start("register_email", "submit_email", "填写并提交注册邮箱", 90)
            # 1.2 Email
            result["stage"] = "submit_email"
            save_checkpoint(result)
            email_box = page.locator("#email, input[name=email]").first
            try:
                email_box.wait_for(state="visible", timeout=SLOW_UI_MS)
            except PWTimeout:
                log("FAIL email field missing after DOB")
                screenshot(page, "pw_email_missing.png")
                if reg_attempt >= 4:
                    result["ea_failure_reason"] = "email_step_missing"
                    result["error_code"] = "ea_register_failed"
                    save_checkpoint(result)
                    close_task_browser(browser, result, bit_client, bit_profile_id)
                    return 2
                continue

            try:
                current_value = (email_box.input_value() or "").strip()
            except Exception:
                current_value = ""
            if current_value.lower() != ea_email.lower():
                pw_reg.human_delay(1.0, 2.0)
                try:
                    email_box.fill("")
                except Exception:
                    pass
                pw_reg.human_type(page, email_box, ea_email)
                page.keyboard.press("Tab")
                page.wait_for_timeout(1000)

            # Early detection of already registered error on input blur
            html = document_html(page)
            text = pw_reg.visible_text(page)
            if pw_reg.looks_already_registered(html, text):
                if switch_mailbox("already registered"):
                    continue
                log("FAIL: email already registered with EA (detected on email input blur)")
                screenshot(page, "pw_already_registered.png")
                result["ea_failure_reason"] = "email_already_registered"
                result["error_code"] = "ea_email_already_registered"
                result["error_message"] = "该邮箱已在 EA 注册过"
                save_checkpoint(result)
                close_task_browser(browser, result, bit_client, bit_profile_id)
                return 2

            pw_reg.human_delay(0.6, 1.2)
            started_email_ts = time.time()
            log("POST email")
            try:
                page.locator("#basicInfoNextBtn[aria-disabled='false'], #basicInfoNextBtn:not(.disabled)").first.wait_for(state="visible", timeout=MED_UI_MS)
            except Exception:
                pass
            if not pw_reg.click_next(page):
                log("WARN email submit click failed")
                continue
            page.wait_for_timeout(2500)
            screenshot(page, "pw_after_email.png")

            html = document_html(page)
            text = pw_reg.visible_text(page)
            cur_step = protocol.cur_step(html)
            log(f"after email cur_step={cur_step} url={safe_url(page.url)}")
            if pw_reg.looks_already_registered(html, text):
                if switch_mailbox("already registered"):
                    continue
                log("FAIL: email already registered with EA")
                screenshot(page, "pw_already_registered.png")
                result["ea_failure_reason"] = "email_already_registered"
                result["error_code"] = "ea_email_already_registered"
                result["error_message"] = "该邮箱已在 EA 注册过"
                save_checkpoint(result)
                close_task_browser(browser, result, bit_client, bit_profile_id)
                return 2

            if pw_reg.looks_rate_limited(html, text):
                log("FAIL: EA rate limit detected ('您提出請求次數過多，請稍候再試')")
                screenshot(page, "pw_rate_limited.png")
                result["ea_failure_reason"] = "ea_rate_limited"
                result["error_code"] = "ea_rate_limited"
                result["error_message"] = "EA 提示请求次数过多，请更换代理 IP 重试"
                save_checkpoint(result)
                close_task_browser(browser, result, bit_client, bit_profile_id)
                return 4

            result["stage"] = "email_verify"
            save_checkpoint(result)
            log("wait EA verification email via Graph")
            code = wait_registration_code(page, ea_mailbox, started_email_ts)

            log("got EA verification code")
            pw_reg.human_delay(1.0, 2.0)
            pw_reg.fill_verify_code(page, code)
            pw_reg.human_delay(0.6, 1.2)
            log("POST email verify code")
            if not pw_reg.click_next(page):
                log("WARN verify code submit failed")
                continue
            page.wait_for_timeout(3000)

            RUNTIME.start("create_credentials", "create_account", "填写并检查 EA ID 与密码", 150)
            # 1.4 Origin ID & Password
            result["stage"] = "create_account"
            save_checkpoint(result)
            id_box = page.locator("input[name=originId], #originId, input[name=originID]").first
            pw_box = page.locator("input[name=password], #password, input[type=password]").first
            try:
                id_box.wait_for(state="visible", timeout=SLOW_UI_MS)
            except PWTimeout:
                log("WARN create fields not visible")
                continue

            # Ensure acceptable unique Origin ID
            id_accepted = False
            for id_try in range(1, 6):
                pw_reg.human_delay(0.4, 0.8)
                id_box.click()
                id_box.fill("")
                pw_reg.human_type(page, id_box, origin_id)
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
                    result["ea_origin_id"] = origin_id
                    continue
                else:
                    log(f"EA ID '{origin_id}' valid and available!")
                    id_accepted = True
                    break

            if not id_accepted:
                log("WARN could not find acceptable EA ID after 5 tries")
                continue

            pw_reg.human_delay(0.4, 0.8)
            if not pw_reg.fill_secret(page, pw_box, password):
                raise FullSetupError("ea_register_password_fill_failed", "注册密码未正确填入，已停止提交", "create_account")
            page.keyboard.press("Tab")
            page.wait_for_timeout(500)

            pw_reg.check_tos(page)
            pw_reg.human_delay(0.8, 1.5)
            log(f"POST create/password origin_id={origin_id}")

            submit_ea_creation(page, result)

            creation_submitted = True
            break

        if not creation_submitted:
            log("FAIL: EA registration failed after retries")
            result["error_code"] = "ea_register_failed"
            result["needs_review"] = False
            save_checkpoint(result)
            close_task_browser(browser, result, bit_client, bit_profile_id)
            return 2

        # 1.5 Verify Login & Land on myaccount
        result["stage"] = "verify_account"
        save_checkpoint(result)
        RUNTIME.start("login_session", "verify_account", "验证 EA 登录及账号中心会话", 300)
        session_ready = ensure_myaccount_session(page, context, ea_mailbox, password, origin_id)
        if not session_ready:
            log("FAIL: registration and account session remain unconfirmed")
            # Losing this browser session does not undo a previously verified account.
            result["error_code"] = "myaccount_session_failed"
            result["error_message"] = "EA 创建已提交，但登录和账号中心状态未确认，凭据已保留待复核"
            result["needs_review"] = True
            save_checkpoint(result)
            close_task_browser(browser, result, bit_client, bit_profile_id)
            return 3

        result["ea_registered"] = True
        result["account_login_verified"] = True
        result["account_profile_completed"] = True
        save_checkpoint(result)
        log("EA registration verified checkpoint saved")
        with RUNTIME.step("capture_cookies", "ea_registered", "保存 EA 会话", 20):
            captured_cookies = capture_remembered_session(page, context, result)
        if not (captured_cookies and captured_cookies.get("remid")):
            log("WARN remid cookie not present; Steam link still uses the current browser session")

        # 【阶段检查点 1】：EA 注册成功即刻固化落库准备
        save_checkpoint(result)
        screenshot(page, "pw_ea_registered.png")
        log("=== PHASE 1 COMPLETE: EA Account Created & Logged In! ===")

        # =====================================================================
        # PHASE 2: Steam Linking (Same browser session & proxy)
        # =====================================================================
        if skip_steam or result.get("steam_link"):
            log("Steam link skipped by configuration")
        else:
            result["stage"] = "steam_oauth"
            save_checkpoint(result)
            log("=== PHASE 2: Starting Steam Link in same session ===")
            closed_tabs = pw_link.purge_steam_session(context, keep_page=page)
            if closed_tabs:
                log(f"Cleared leftover Steam tabs/cookies before OAuth ({closed_tabs})")
            pw_link.dismiss_cookies(page)

            # Intercept Steam OAuth requests from network
            steam_oauth_urls: list[str] = []

            def _on_req(req):
                u = req.url
                parsed = urlparse(u)
                if (req.method == "GET" and req.resource_type == "document"
                        and pw_link.is_steam_page(u)
                        and parsed.path.rstrip("/") in {"/openid/login", "/oauth/login", "/oauth/loginform"}):
                    if u not in steam_oauth_urls:
                        steam_oauth_urls.append(u)
                        log(f"Intercepted Steam URL: {safe_url(u)}")

            page.on("request", _on_req)
            context.on("page", lambda p: p.on("request", _on_req))

            steam_linked = False
            last_link_failure = ("steam_link_not_committed", "EA 未显示已关联 Steam，可以重新关联")
            steam_page = None
            for link_attempt in range(1, 4):
                steam_oauth_urls.clear()
                if steam_page is not None and steam_page is not page and not steam_page.is_closed():
                    steam_page.close()
                steam_page = None
                RUNTIME.start("steam_link", "steam_oauth", "确认 Steam 关联", 300)
                log(f"Navigating to Connected Accounts (attempt {link_attempt})...")
                page.goto("https://myaccount.ea.com/am/ui/connected-accounts", wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                pw_link.dismiss_cookies(page)

                # Check if already linked
                conn_res = am_json("/am/data/1/connected-accounts")
                if not conn_res.get("ok"):
                    raise FullSetupError("steam_link_status_unavailable", "无法读取 EA 关联状态，保留原配对后恢复", "link_verify")
                already_linked = pw_link.steam_connection_from_payload(conn_res.get("payload") or {})
                if already_linked.get("status") == "linked":
                    persona = already_linked.get("persona") or pw_link.extract_linked_steam_persona(page)
                    result["steam_link"] = True
                    result["linked_steam_persona"] = persona
                    log(f"Steam is ALREADY linked! persona={persona}")
                    steam_linked = True
                    save_checkpoint(result)
                    break

                if steam_oauth_handed_off(result):
                    # A lost callback is not proof that authorization failed.
                    # Reconcile only; never click Allow again in a new browser.
                    verify_res = pw_link.wait_for_steam_linked(page, timeout_ms=45_000)
                    if verify_res.get("status") == "linked":
                        result.update(steam_link=True, linked_steam_persona=verify_res.get("persona"))
                        save_checkpoint(result)
                        steam_linked = True
                        break
                    raise FullSetupError("steam_link_commit_unconfirmed", "Steam 授权已提交，EA 最终关联状态仍未确认，保留原配对", "link_verify")

                # Click Steam tile
                tile_clicked = pw_link.click_steam_tile(page)
                if not tile_clicked:
                    page.wait_for_timeout(2000)
                    tile_clicked = pw_link.click_steam_tile(page)

                if not tile_clicked:
                    log(f"WARN steam tile missing on attempt {link_attempt}; reloading page")
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    continue

                # Wait for Link Steam modal to render
                page.wait_for_timeout(1500)
                try:
                    page.locator(pw_link.STEAM_LINK_MODAL_SELECTOR).first.wait_for(
                        state="visible", timeout=MED_UI_MS
                    )
                    log("steam modal visible")
                except Exception as e:
                    log(f"steam modal wait note: {e}")

                pages_before = set(id(pg) for pg in context.pages)
                pw_link.dismiss_cookies(page)
                pw_link.prepare_steam_oauth_click(page)
                steam_page = None
                try:
                    with context.expect_page(timeout=MED_UI_MS) as popup_info:
                        # Persist before dispatch. A successful browser click
                        # can lose its CDP reply; the pair must stay reserved
                        # until the callback is reconciled.
                        result["steam_link_attempted"] = True
                        save_checkpoint(result)
                        if not pw_link.click_modal_continue(page):
                            page.get_by_role(
                                "button", name=pw_link.CONTINUE_LABEL_RE
                            ).last.click(force=True, timeout=5000)
                    steam_page = popup_info.value
                    try:
                        steam_page.wait_for_load_state("domcontentloaded", timeout=NAV_MS)
                    except Exception:
                        pass
                    log(f"Steam OAuth popup opened: {safe_url(steam_page.url)}")
                except PWTimeout:
                    page.wait_for_timeout(2500)
                    log(f"no popup after Continue, url={safe_url(page.url)} pages={len(context.pages)}")
                    for pg in context.pages:
                        if id(pg) not in pages_before:
                            steam_page = pg
                            log(f"found page {safe_url(pg.url)}")
                            break
                except Exception as exc:
                    log(f"Popup wait note: {exc}")

                target_steam_page = pw_link.ensure_live_steam_oauth_page(
                    context,
                    page,
                    steam_page,
                    steam_oauth_urls,
                    am_json=am_json,
                    timeout_ms=MED_UI_MS,
                )
                if target_steam_page is None or not pw_link.is_steam_page(target_steam_page.url):
                    error = pw_link.am_error_code_from_page(steam_page) if steam_page is not None else None
                    last_link_failure = (
                        "ea_steam_oauth_rejected" if error else "steam_oauth_page_missing",
                        f"EA 关联入口返回错误 {error}" if error else "未打开 Steam 授权页，关联尚未开始，可以重新关联",
                    )
                    log(f"Steam OAuth did not open: {last_link_failure[0]} error={error}")
                    continue
                log(f"Steam OAuth page: {safe_url(getattr(target_steam_page, 'url', '') or '')}")

                pw_reg.human_delay(1.0, 2.0)

                # Fill Steam credentials unless this tab is already signed in.
                result["stage"] = "steam_login"
                save_checkpoint(result)
                RUNTIME.start("steam_login_form", "steam_login", "等待并提交 Steam 登录表单", 150)
                log(f"Waiting for Steam login credentials inputs on {safe_url(target_steam_page.url)}...")
                user_box, pw_box = pw_link.wait_for_steam_credentials(
                    target_steam_page, timeout_ms=SLOW_UI_MS
                )
                if not pw_link.steam_credentials_required(user_box, pw_box):
                    if pw_link.steam_authenticated_or_consent(target_steam_page):
                        log("Steam already authenticated; skipping credential form")
                    else:
                        log(f"Steam page has neither credentials nor authenticated state; url={safe_url(target_steam_page.url)} inventory={pw_link.steam_login_inventory(target_steam_page)}; trying page reload...")
                        try:
                            target_steam_page.reload(wait_until="commit", timeout=NAV_MS)
                            target_steam_page.wait_for_timeout(4000)
                            user_box, pw_box = pw_link.wait_for_steam_credentials(
                                target_steam_page, timeout_ms=SLOW_UI_MS
                            )
                        except Exception as re_err:
                            log(f"Steam reload note: {re_err}")
                        if not pw_link.steam_credentials_required(user_box, pw_box) and not pw_link.steam_authenticated_or_consent(target_steam_page):
                            last_link_failure = ("steam_login_form_missing", "Steam 登录表单未出现，关联尚未开始，可以重新关联")
                            log("Steam inputs still not found; closing popup and retrying attempt")
                            if steam_page and steam_page != page and not steam_page.is_closed():
                                steam_page.close()
                            continue

                steam_login_started_at = time.time()
                if pw_link.steam_credentials_required(user_box, pw_box):
                    if pw_link.fill_steam_login(
                        target_steam_page,
                        user_box,
                        pw_box,
                        steam_info["username"],
                        steam_info["password"],
                    ):
                        log("filled Steam credentials")
                    else:
                        raise FullSetupError("steam_login_fill_failed", "Steam 登录凭据未正确填入", "steam_login")
                    pw_reg.human_delay(0.6, 1.2)
                    log("Submitting Steam credentials...")
                    if not pw_link.click_login_action(target_steam_page):
                        log("Steam sign-in click missed; submitting the password field")
                        pw_link.reveal_steam_password_signin(target_steam_page)
                        if not pw_link.submit_steam_password(target_steam_page):
                            raise FullSetupError("steam_login_submit_failed", "Steam 登录按钮未提交", "steam_login")
                    target_steam_page.wait_for_timeout(800)

                # Also handle Guard when a reused tab starts at the challenge,
                # and wait for delayed React inputs instead of checking only once.
                RUNTIME.start("steam_login_finish", "steam_login", "等待 Steam 登录或验证码验证完成", 120)
                finish_steam_login(
                    target_steam_page,
                    get_mailbox_record(steam_info["email"], mailboxes),
                    steam_login_started_at,
                    result,
                )

                # Authorize / Consent Loop (OpenID consent on Steam -> Link accounts on EA)
                result["stage"] = "steam_consent"
                save_checkpoint(result)
                RUNTIME.start("steam_consent", "steam_consent", "确认 Steam 账号并允许 EA 授权", 150)
                log("Handling Steam/EA OAuth consent confirmation...")
                confirm_deadline = time.time() + 60
                steam_rejected = False
                consent_reloaded = False
                steam_consent_submitted: set[str] = set()
                while time.time() < confirm_deadline:
                    if target_steam_page != page and target_steam_page.is_closed():
                        log("Steam popup closed (link likely committed)")
                        break
                    switched = pw_link.resolve_steam_oauth_page(
                        context, target_steam_page
                    )
                    if switched is not None:
                        target_steam_page = switched
                    if pw_link.is_steam_page(getattr(target_steam_page, "url", "") or ""):
                        did_steam = advance_steam_consent(target_steam_page, steam_consent_submitted)
                        if did_steam:
                            if did_steam == "account_login":
                                result["steam_oauth_account_login_submitted"] = True
                            elif did_steam == "authorize":
                                result["steam_oauth_authorize_submitted"] = True
                            save_checkpoint(result)
                            log(f"Steam OAuth submitted: {did_steam}")
                    did_ea = pw_link.click_ea_link_accounts_anywhere(
                        context,
                        before_click=(
                            lambda: (
                                result.__setitem__("ea_link_consent_submitted", True),
                                save_checkpoint(result),
                            )
                        ) if not result["ea_link_consent_submitted"] else None,
                    ) if not result["ea_link_consent_submitted"] else False
                    if did_ea:
                        log("OAuth consent clicked (on_ea=True)")
                        break
                    if pw_link.ea_steam_callback_reached(context):
                        result["ea_link_consent_submitted"] = True
                        save_checkpoint(result)
                        log("EA Steam callback reached")
                        break
                    rejection, masked = pw_link.steam_link_rejection_from_context(context)
                    if rejection:
                        result["error_code"] = rejection
                        result["already_linked_ea_email"] = masked
                        result["error_message"] = (
                            "该 Steam 已绑定其他 EA 账号，无法再关联到当前 EA"
                        )
                        if masked:
                            result["error_message"] += f"（EA {masked}）"
                        result["stage"] = "link_verify"
                        result["notes"].append(
                            "ea rejected steam as already linked elsewhere"
                        )
                        log("STEAM already linked to another EA; stopping Steam retries")
                        save_checkpoint(result)
                        steam_rejected = True
                        break
                    if (
                        not consent_reloaded
                        and not steam_consent_submitted
                        and pw_link.is_steam_page(getattr(target_steam_page, "url", "") or "")
                        and time.time() > confirm_deadline - 25
                    ):
                        log("consent not advancing; reload Steam OAuth page")
                        try:
                            if pw_link.is_steam_page(
                                getattr(target_steam_page, "url", "") or ""
                            ):
                                target_steam_page.reload(
                                    wait_until="commit", timeout=NAV_MS
                                )
                            else:
                                page.reload(
                                    wait_until="domcontentloaded", timeout=NAV_MS
                                )
                        except Exception as re_err:
                            log(f"WARN consent reload: {re_err}")
                        consent_reloaded = True
                    try:
                        target_steam_page.wait_for_timeout(500)
                    except Exception:
                        break

                if steam_rejected:
                    break

                # Back on EA pages: confirm link if the dialog is on another tab.
                # Do not close the Steam tab first — EA's 連結帳號 often lands
                # after Steam 登录, and closing the popup aborts the callback.
                if not result["ea_link_consent_submitted"]:
                    page.wait_for_timeout(400)
                    if pw_link.click_ea_link_accounts_anywhere(
                        context,
                        before_click=lambda: (
                            result.__setitem__("ea_link_consent_submitted", True),
                            save_checkpoint(result),
                        ),
                    ):
                        log("OAuth consent clicked during EA callback reconciliation")
                page.wait_for_timeout(400)

                RUNTIME.start("link_session_recover", "link_verify", "等待 Steam 回调回到账号中心", 90)
                if not settle_ea_after_steam(
                    page, context, ea_mailbox, password, origin_id,
                ):
                    last_link_failure = (
                        "ea_link_session_unconfirmed",
                        "Steam 授权后未能回到 EA 关联页，可以重新关联确认",
                    )
                    log("WARN EA callback did not return to myaccount")
                    # Allow was not submitted, so another pass cannot double-bind.
                    if steam_oauth_handed_off(result):
                        break
                    continue
                account_page = find_myaccount_page(context, fallback=page)
                if "connected-accounts" not in (getattr(account_page, "url", "") or ""):
                    account_page.goto(
                        "https://myaccount.ea.com/am/ui/connected-accounts",
                        wait_until="domcontentloaded",
                        timeout=NAV_MS,
                    )
                    account_page.wait_for_timeout(400)

                RUNTIME.start("steam_link_verify", "link_verify", "读取 EA 最终关联状态", 90)
                verify_res = pw_link.wait_for_steam_linked(account_page, timeout_ms=45_000)
                if verify_res.get("status") == "logged_out":
                    log("connected accounts bounced to EA login; waiting for callback tab")
                    RUNTIME.start("link_session_recover", "link_verify", "等待 Steam 回调回到账号中心", 90)
                    if settle_ea_after_steam(
                        page, context, ea_mailbox, password, origin_id,
                    ):
                        account_page = find_myaccount_page(context, fallback=page)
                        if "connected-accounts" not in (getattr(account_page, "url", "") or ""):
                            account_page.goto(
                                "https://myaccount.ea.com/am/ui/connected-accounts",
                                wait_until="domcontentloaded",
                                timeout=NAV_MS,
                            )
                            account_page.wait_for_timeout(400)
                        RUNTIME.start("steam_link_verify", "link_verify", "读取 EA 最终关联状态", 60)
                        verify_res = pw_link.wait_for_steam_linked(account_page, timeout_ms=30_000)
                if verify_res.get("status") == "linked":
                    result["steam_link"] = True
                    result["linked_steam_persona"] = verify_res.get("persona")
                    log(f"=== PHASE 2 COMPLETE: Steam Linked! persona={verify_res.get('persona')} ===")
                    steam_linked = True
                    # 【阶段检查点 2】：Steam 关联成功即刻固化落库准备
                    save_checkpoint(result)
                    if steam_page and steam_page != page and not steam_page.is_closed():
                        try:
                            steam_page.close()
                        except Exception:
                            pass
                    break
                else:
                    last_link_failure = ("steam_link_not_committed", "EA 未显示已关联 Steam，可以重新关联")
                    log(f"WARN steam link not confirmed on attempt {link_attempt}")
                    # Once the picker, Allow, or EA 連結帳號 was submitted,
                    # another attempt can authorize a second time. A modal
                    # click alone has not handed Steam off yet.
                    if steam_oauth_handed_off(result):
                        break

            if not steam_linked:
                if result.get("error_code") == "steam_already_linked_elsewhere":
                    log("FAIL: Steam is already linked to another EA account")
                    result["needs_review"] = False
                else:
                    log("FAIL: Steam link could not be confirmed; EA account is preserved!")
                    result["needs_review"] = False
                    result["error_code"], result["error_message"] = last_link_failure
                result["stage"] = "link_verify"
                save_checkpoint(result)
                return 1

        # =====================================================================
        # PHASE 3: 2FA Binding (Same browser session & proxy)
        # =====================================================================
        if skip_2fa or result.get("totp"):
            log("2FA setup skipped by configuration")
        else:
            result["stage"] = "tfa_setup"
            save_checkpoint(result)
            log("=== PHASE 3: Starting 2FA Setup in same session ===")

            tfa_success = False
            tfa_error = ("tfa_activation_unconfirmed", "Authenticator 激活状态未确认", "tfa_setup")
            for tfa_attempt in range(1, 4):
                RUNTIME.start("tfa_setup", "tfa_setup", "开启并确认 EA 2FA", 180)
                log(f"Opening Security & Privacy settings (attempt {tfa_attempt})...")
                page.goto("https://myaccount.ea.com/am/ui/security-privacy", wait_until="domcontentloaded")
                try:
                    page.locator(
                        '[data-testid="enter-tfa-button"]:visible, #enter-tfa-button:visible'
                    ).first.wait_for(timeout=8000)
                except Exception:
                    page.wait_for_timeout(800)
                pw_link.dismiss_cookies(page)

                # Check if already enabled
                tfa_check = am_json("/am/data/1/security-privacy")
                if not tfa_check.get("ok"):
                    raise FullSetupError("tfa_status_unavailable", "无法读取 EA 2FA 状态，保留已有密钥", "tfa_setup")
                if pw_link.app_2fa_enabled(tfa_check.get("payload") or {}):
                    log("2FA is ALREADY enabled on this EA account!")
                    if not result.get("totp_secret"):
                        result["tfa_existing_enabled"] = True
                        save_checkpoint(result)
                        raise FullSetupError("tfa_existing_secret_missing", "EA 已开启 APP 2FA，但本任务没有可验证的密钥", "tfa_setup")
                    checkpoint_tfa_enabled(result)
                    tfa_success = True
                    break

                if result.get("totp_secret"):
                    raise FullSetupError("tfa_activation_unconfirmed", "已保存 Authenticator 密钥，激活尚未确认；保留密钥且不重新生成", "tfa_activation")

                # Trigger 2FA Enable
                try:
                    with RUNTIME.step("tfa_open_settings", "tfa_setup", "打开并确认 2FA 设置面板", 40):
                        pw_link.open_tfa_settings(page, timeout_ms=MED_UI_MS, confirm_open=True)
                except Exception as open_err:
                    tfa_error = ("tfa_settings_open_failed", "2FA 设置入口未打开，尚未进入激活步骤", "tfa_setup")
                    log(f"WARN open_tfa_settings attempt {tfa_attempt} failed: {open_err}")
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_timeout(800)
                    continue
                page.wait_for_timeout(400)
                pw_link.dismiss_cookies(page)

                # Handle Identity Verification OTP (EA requires email OTP before modifying 2FA)
                if pw_link.identity_challenge_visible(page):
                    result["stage"] = "tfa_identity"
                    save_checkpoint(result)
                    log("Identity challenge modal displayed, requesting security code...")
                    identity_started_at = time.time()
                    sent = pw_link.click_send_code(page)
                    page.wait_for_timeout(1500)
                    if not sent and not page.locator("#verify-identity-verify-code-input:visible, input[name=verificationCode]:visible").count():
                        tfa_error = ("tfa_identity_send_failed", "身份验证码发送按钮未生效", "tfa_identity")
                        continue
                    id_code = wait_mailbox_code(
                        page,
                        ea_mailbox,
                        pw_link.LOGIN_OTP_MAIL_KEYS + ("code",),
                        identity_started_at,
                    )
                    if not id_code:
                        tfa_error = ("tfa_identity_code_missing", "未收到 EA 身份验证码", "tfa_identity")
                        log("WARN identity code timeout")
                        # The wait already resent once and polled the mailbox.
                        break
                    log("Got identity verification code")
                    pw_reg.human_delay(1.0, 1.8)

                    # Paloma modal scoped input
                    modal_candidates = page.locator(
                        "[data-testid='@paloma-modal-theme-id'], [data-testid='@paloma-modal-overlay-id']"
                    )
                    modal = None
                    for mi in range(modal_candidates.count() - 1, -1, -1):
                        c = modal_candidates.nth(mi)
                        try:
                            if c.is_visible() and c.locator("#verify-identity-verify-code-input:visible, [data-testid='verify-identity-verify-code-input'] input:visible, input:visible").count():
                                modal = c
                                break
                        except Exception:
                            continue
                    box = None
                    if modal is not None:
                        stable_boxes = modal.locator(
                            "input#verify-identity-verify-code-input:visible, input[aria-label='verify-identity-verify-code-input']:visible, [data-testid='verify-identity-verify-code-input'] input:visible"
                        )
                        box = stable_boxes.first if stable_boxes.count() else modal.locator("input:visible").first
                    else:
                        box = page.locator("#verify-identity-verify-code-input:visible, input[name=verificationCode], #twoFactorCode, input[type=text]:visible").first

                    if box and box.count() and box.is_visible():
                        box.fill(id_code)
                        pw_reg.human_delay(0.5, 1.0)
                        pw_link.click_login_action(page)
                        page.wait_for_timeout(3000)

                    try:
                        reconcile_tfa_identity(page, id_code, am_json)
                    except FullSetupError as exc:
                        tfa_error = (exc.code, str(exc), exc.stage)
                        continue

                # Click turn-on button if visible. Native Playwright click on
                # the desktop duplicate can sit 30s behind an overlay.
                try:
                    if pw_link.click_tfa_turn_on(page):
                        page.wait_for_timeout(400)
                    pw_link.click_tfa_method_next(page)
                    page.wait_for_timeout(400)
                    pw_link.continue_tfa_app_installation(page)
                    page.wait_for_timeout(400)
                except PWTimeout as click_err:
                    tfa_error = (
                        "tfa_control_timeout",
                        "2FA 开启控件被挡住，未进入激活步骤",
                        "tfa_setup",
                    )
                    log(f"WARN tfa controls timed out on attempt {tfa_attempt}: {click_err}")
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_timeout(800)
                    continue

                # Extract Secret: UI or in-page am_json fallback
                html = document_html(page)
                text = pw_reg.visible_text(page)
                secret = pw_link.extract_totp_secret(html, text)
                if not secret:
                    log("UI secret extraction failed; trying in-page am_json RPC fallback...")
                    rpc_res = am_json("/am/data/1/get-verify-qrcode", data={"codeType": "APP"})
                    if rpc_res.get("ok") and rpc_res.get("payload"):
                        payload = rpc_res["payload"]
                        res_obj = payload.get("result") or {}
                        secret = str(res_obj.get("secKey") or payload.get("key") or payload.get("secret") or "").replace(" ", "")

                if not secret:
                    tfa_error = ("tfa_secret_not_found", "2FA 设置未返回 Authenticator 密钥", "tfa_secret")
                    log(f"WARN TOTP secret not found on attempt {tfa_attempt}")
                    continue

                tfa_error = ("tfa_activation_unconfirmed", "Authenticator 密钥已保存，但激活状态未确认", "tfa_activation")
                log("TOTP secret captured successfully")
                result["totp_secret"] = secret
                # 【核心保护检查点】：立即固化 TOTP 密钥到磁盘，绝不丢失！
                save_checkpoint(result)

                # Generate code and activate
                remaining = 30 - (int(time.time()) % 30)
                if remaining < 8:
                    page.wait_for_timeout((remaining + 1) * 1000)
                totp_code = pw_link.totp(secret)
                log("Submitting TOTP activation")

                try:
                    enabled = activate_tfa(am_json, totp_code)
                except FullSetupError as exc:
                    tfa_error = (exc.code, str(exc), exc.stage)
                    continue
                if enabled:
                    checkpoint_tfa_enabled(result)
                    log("=== PHASE 3 COMPLETE: 2FA Enabled Successfully! ===")
                    tfa_success = True
                    break
                else:
                    log(f"WARN 2FA enable unconfirmed on attempt {tfa_attempt}")

            if not tfa_success:
                log("FAIL: 2FA enable unconfirmed; prior assets preserved!")
                result["needs_review"] = True
                if not result.get("error_code"):
                    result["error_code"], result["error_message"], result["stage"] = tfa_error
                else:
                    result["notes"].append("2FA 开启也未最终确认")
                save_checkpoint(result)

        # Preserve the actual failed stage; reaching the end is not success.
        if result.get("error_code") or result.get("needs_review"):
            save_checkpoint(result)
            log("Full setup stopped with completed assets preserved")
            return 1
        # Finalize
        RUNTIME.start("finalize", "complete", "保存完成状态和会话", 40)
        result["stage"] = "complete"
        save_checkpoint(result)
        log("=== All Phases Finished! ===")
        refresh_completed_session(context, page, result)
        close_task_browser(browser, result, bit_client, bit_profile_id)
    except Exception as exc:
        log(f"FAIL full setup aborted: {type(exc).__name__} operation={RUNTIME.operation.get('name')}")
        if not result.get("error_code"):
            detail = str(exc)
            for sensitive in (password, ea_email, (steam_info or {}).get("password")):
                if sensitive:
                    detail = detail.replace(sensitive, "[REDACTED]")
            name = type(exc).__name__
            if isinstance(exc, (FullSetupError, pw_reg.LoginFormError)):
                result["error_code"] = exc.code
                result["error_message"] = detail
                result["stage"] = getattr(exc, "stage", "verify_account")
                result["needs_review"] = getattr(exc, "needs_review", True)
            elif "Roxy" in name or "Roxy" in detail or "50100" in detail:
                result["error_code"] = "roxy_unavailable"
                result["error_message"] = f"RoxyBrowser 无法打开指纹窗口：{detail[:300]}"
            else:
                result["error_code"] = ("ea_network_error" if transport_failure(exc) else
                                        "ea_action_timeout" if isinstance(exc, PWTimeout) else "ea_full_setup_crashed")
                result["stage"] = RUNTIME.operation.get("stage") or result["stage"]
                result["error_message"] = f"{name} ({RUNTIME.operation.get('name')}): {detail[:300]}"
                result["needs_review"] = True
            save_checkpoint(result)
        return 1
    finally:
        # The service also cleans the recorded profile after killing a stuck child.
        RUNTIME.start("browser_cleanup", result["stage"], "清理本任务浏览器", 20)
        try:
            pw_reg.release_fingerprint_window(bit_client, bit_profile_id)
        finally:
            RUNTIME.close()

    return 0


if __name__ == "__main__":
    sys.exit(main())

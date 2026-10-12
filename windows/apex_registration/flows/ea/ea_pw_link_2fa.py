#!/usr/bin/env python3
"""Playwright: login EA myaccount SPA -> bind Steam -> open free App Authenticator TOTP.

Fixes vs previous runs:
  - dismiss cookie banner before clicks
  - Steam tile via testid + parent button (.first)
  - modal Continue with expect_page / same-tab steam openid
  - 2FA via row "Two-factor authentication" (not random Enable text)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import random
import re
import shutil
import struct
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright

from apex_registration.utils.browser_profile import (  # noqa: E402
    ACCEPT_LANGUAGE,
    LOCALE as BROWSER_LOCALE,
    playwright_proxy,
    save_page_screenshot,
)

ROOT = Path(__file__).resolve().parent
# Optional proxy fallback, only read when EA_PROXY is unset. The backend always
# injects EA_PROXY, so deployments never need this file.
PROXY_CFG = Path(
    os.environ.get("ACCOUNT_FLOW_PROXY_CONFIG", "").strip()
    or ROOT / "proxy-config.json"
)
ARTIFACT_ROOT = Path(os.environ.get("ACCOUNT_FLOW_OUTPUT_DIR") or ROOT)
POOL = Path(os.environ.get("ACCOUNT_FLOW_EMAIL_POOL") or "/tmp/emails.json")
LOG = ARTIFACT_ROOT / "pw_link_2fa.log"
RES = ARTIFACT_ROOT / "pw_link_2fa_result.json"
# CLI defaults only; the backend passes both paths via env.
EA_ACCOUNT_FILE = ROOT / "protocol_register_result.json"
STEAM_ACCOUNT_FILE = ROOT.parent / "steam" / "accounts_succ.txt"


def log(m: str) -> None:
    safe = re.sub(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        "[EMAIL]",
        str(m),
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
    with LOG.open("a") as f:
        f.write(line + "\n")


def save_screenshot(target, name: str, *, full_page: bool = False) -> None:
    """Never let font/screenshot stalls fail a link or 2FA task."""
    if target is None:
        return
    path = ARTIFACT_ROOT / name
    if not save_page_screenshot(target, path, full_page=full_page, timeout=2500):
        log(f"WARN screenshot {name}: TimeoutError")


def safe_url(value: str) -> str:
    """Keep only scheme/host/path so OAuth tokens never enter logs."""
    try:
        parsed = urlparse(str(value))
        if parsed.scheme and parsed.netloc:
            return parsed._replace(params="", query="", fragment="").geturl()
    except Exception:
        pass
    return str(value).split("?", 1)[0]


def capture_connect_cookies(context, page=None) -> dict | None:
    from . import ea_pw_register as pw_reg
    return pw_reg.capture_connect_cookies(context, page=page)


def preserve_safe_link_diagnostics(result: dict) -> None:
    # Runner task directories already retain all registration evidence.
    return


def direct_mode() -> bool:
    return os.environ.get("ACCOUNT_FLOW_DIRECT", "").strip().lower() in {
        "1", "true", "yes"
    }


def load_proxy() -> str:
    env = str(os.environ.get("EA_PROXY") or "").strip()
    if env:
        return env
    if not PROXY_CFG.exists():
        raise RuntimeError(
            "missing proxy: set EA_PROXY (or point ACCOUNT_FLOW_PROXY_CONFIG "
            "at a json file with a 'proxy' key)"
        )
    proxy = str(json.loads(PROXY_CFG.read_text()).get("proxy") or "").strip()
    if not proxy:
        raise RuntimeError("missing proxy: set EA_PROXY or a 'proxy' key in the config")
    return proxy


NET_ERROR_RE = re.compile(r"net::(ERR_[A-Z_0-9]+)")


def net_error_reason(exc: BaseException | str) -> str | None:
    """Return the Chromium ``net::ERR_*`` code behind a navigation failure."""
    match = NET_ERROR_RE.search(str(exc))
    return match.group(1) if match else None


def proxy_error_message(reason: str) -> str:
    """Human-readable cause for a transport failure, for the task record."""
    hints = {
        "ERR_EMPTY_RESPONSE": "代理出口无响应",
        "ERR_CONNECTION_CLOSED": "代理连接被关闭",
        "ERR_CONNECTION_RESET": "代理连接被重置",
        "ERR_CONNECTION_REFUSED": "代理拒绝连接",
        "ERR_CONNECTION_TIMED_OUT": "代理连接超时",
        "ERR_TIMED_OUT": "代理连接超时",
        "ERR_PROXY_CONNECTION_FAILED": "无法连接代理服务",
        "ERR_TUNNEL_CONNECTION_FAILED": "代理隧道建立失败（可能是代理认证或额度问题）",
        "ERR_NAME_NOT_RESOLVED": "代理无法解析 EA 域名",
        "ERR_SSL_PROTOCOL_ERROR": "代理出口 TLS 握手失败",
    }
    hint = hints.get(reason, "网络不可用")
    return f"EA 页面无法通过代理打开：{hint}（{reason}），请重试或更换代理出口"




def totp(secret: str) -> str:
    key = secret.strip().replace(" ", "").upper()
    key += "=" * ((8 - len(key) % 8) % 8)
    raw = base64.b32decode(key, casefold=True)
    t = int(time.time() // 30)
    dig = hmac.new(raw, struct.pack(">Q", t), hashlib.sha1).digest()
    off = dig[-1] & 0x0F
    code = (struct.unpack(">I", dig[off : off + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return str(code).zfill(6)


def graph_token(rec: dict) -> str:
    client = requests.Session()
    if direct_mode():
        client.trust_env = False
    r = client.post(
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        data={
            "client_id": rec["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": rec["refresh_token"],
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=40,
    )
    j = r.json()
    if not j.get("access_token"):
        raise RuntimeError(j)
    if j.get("refresh_token"):
        rec["refresh_token"] = j["refresh_token"]
    return j["access_token"]


def wait_mail_code(
    rec: dict,
    after: str,
    keys: tuple[str, ...],
    timeout: int = 90,
    code_re: str = r"(\d{6,8})",
) -> str | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            at = graph_token(rec)
            client = requests.Session()
            if direct_mode():
                client.trust_env = False
            r = client.get(
                "https://graph.microsoft.com/v1.0/me/messages?$top=12&$orderby=receivedDateTime%20desc&$select=subject,body,receivedDateTime",
                headers={"Authorization": f"Bearer {at}"},
                timeout=40,
            )
            r.raise_for_status()
            for msg in r.json().get("value") or []:
                rec_t = msg.get("receivedDateTime") or ""
                if rec_t < after:
                    continue
                subj = str(msg.get("subject") or "")
                body = str((msg.get("body") or {}).get("content") or "")
                low = (subj + " " + body).lower()
                if not any(k in low for k in keys):
                    continue
                m = re.search(code_re, subj) or re.search(
                    code_re, re.sub(r"<[^>]+>", " ", body)
                )
                if m:
                    log(f"mail code found at {rec_t}")
                    return m.group(1)
        except Exception as e:
            log(f"graph err {e}")
        log("wait mail...")
        time.sleep(4)
    return None


LOGIN_ACTION_NAME_RE = re.compile(
    r"next|continue|sign in|log in|submit|verify|"
    r"下一步|继续|繼續|登录|登入|提交|验证|驗證",
    re.I,
)
LOGIN_ACTION_SELECTORS = (
    "#logInBtn",
    "#btnLogin",
    "#btnSubmit",
    "#continue",
    "a#continue[role=button]",
    "a#continue",
    "input[type=submit]",
    "button[type=submit]",
)
LOGIN_OTP_HINT_RE = re.compile(
    r"two factor|enter your code|security code|onetimecode|twofactorcode|"
    r"verify.?code|dynamicchallenge|"
    r"双重身份|兩重身份|验证您的身份|驗證您的身份|"
    r"验证码|驗證碼|发送代码|發送代碼|发送验证码|發送驗證碼|"
    r"输入代码|輸入代碼",
    re.I,
)
SEND_CODE_NAME_RE = re.compile(
    r"send(?:\s+a)?\s+code|发送代码|發送代碼|发送验证码|發送驗證碼",
    re.I,
)
RESEND_CODE_NAME_RE = re.compile(
    r"resend|re-send|重新发送|重新發送|請求重新發送|请求重新发送|再次发送|再次發送",
    re.I,
)
CONTINUE_LABEL_RE = re.compile(
    r"^(Continue|Lanjutkan|Continuar|Continuer|Fortfahren|次へ|계속|继续|繼續)\b",
    re.I,
)
EA_LINK_ACCOUNTS_RE = re.compile(
    r"^(Link accounts?|連結帳號|連結帳戶|關聯帳號|關聯帳戶|連接帳號|关联账户|"
    r"绑定账户|绑定帐户|綁定賬戶|綁定帳戶|"
    r"アカウントをリンク|アカウントリンク|アカウントを連携|"
    r"連携する|リンクする|Steamアカウントをリンク|Steamをリンク|"
    r"Tautkan akun|Tautkan akaun)$",
    re.I,
)
STEAM_OAUTH_CONSENT_RE = re.compile(
    r"^(Allow|Agree|Authorize|允许|允許|同意|授權|許可|承認)$",
    re.I,
)
STEAM_OAUTH_LOGIN_CONSENT_RE = re.compile(
    r"^(登录|登入|Sign In|Login|Masuk|ログイン|サインイン)$",
    re.I,
)
STEAM_POST_LOGIN_RE = re.compile(
    r"allow|agree|authorize|授权|允許|允许|同意|"
    r"steam guard|two-factor|确认|確認|已登录|signed in",
    re.I,
)
NOT_LINKED_RE = re.compile(r"Not linked|未連結|未关联|尚未連結", re.I)
STEAM_LINK_MODAL_RE = re.compile(
    r"Link your Steam account|連結您的 Steam|連結你的 Steam|"
    r"連結你的 Steam 帳戶|連結您的 Steam 帳戶|"
    r"連接你的 Steam 帳戶|关联你的 Steam 帐户|"
    r"将您的 EA账户绑定至 Steam|將您的 EA賬戶綁定至 Steam|"
    r"Tautkan Steam Anda",
    re.I,
)
STEAM_LINK_MODAL_SELECTOR = (
    '[data-testid="connected-accounts-modal-continue-button"], '
    '[role="dialog"], dialog, [aria-modal="true"]'
)
STEAM_OAUTH_LOGIN_BUTTON = "#imageLogin"
LOGIN_OTP_MAIL_KEYS = (
    "security code",
    "ea",
    "verification",
    "验证码",
    "驗證碼",
    "安全代码",
    "安全代碼",
)
LOGIN_OTP_INPUT = (
    "#twoFactorCode, input[name=oneTimeCode], input[name=emailVerifyCode], "
    "input[autocomplete=one-time-code]"
)
def login_action_name_matches(text: str) -> bool:
    return bool(LOGIN_ACTION_NAME_RE.search(text or ""))


def send_code_label_matches(text: str) -> bool:
    return bool(SEND_CODE_NAME_RE.search((text or "").strip()))


def continue_label_matches(text: str) -> bool:
    return bool(CONTINUE_LABEL_RE.search((text or "").strip()))


def ea_link_accounts_label_matches(text: str) -> bool:
    return bool(EA_LINK_ACCOUNTS_RE.search((text or "").replace("\n", " ").strip()))


def _norm_persona(value: str) -> str:
    return (value or "").strip().casefold()


def steam_persona_matches(label: str, *, username: str, email: str) -> bool:
    needle = _norm_persona(label)
    if not needle:
        return False
    local = (email or "").split("@", 1)[0]
    return needle in {_norm_persona(username), _norm_persona(local)}


def linked_steam_persona_from_html(html: str) -> str:
    """Read only the Steam card; other providers use the same title prefix."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html or "", "html.parser")
    names = set()
    for unlink in soup.select('[data-testid="unlink-external-button-Steam"]'):
        root = unlink.parent
        for _ in range(8):
            if root is None:
                break
            providers = root.select('[data-testid^="connected-accounts-linked-section-pre-title-"]')
            if providers:
                if any(node.get("data-testid") != "connected-accounts-linked-section-pre-title-Steam" for node in providers):
                    break
                for node in root.select('[data-testid^="connected-accounts-linked-section-title-"]'):
                    text = node.get_text(strip=True)
                    suffix = node.get("data-testid", "").removeprefix("connected-accounts-linked-section-title-")
                    name = text or suffix
                    if name and name.casefold() != "steam":
                        names.add(name)
                if names:
                    break
            root = root.parent
    return next(iter(names)) if len(names) == 1 else ""


def extract_linked_steam_persona(page) -> str:
    try:
        return linked_steam_persona_from_html(page.content())
    except Exception:
        return ""


def steam_connection_from_payload(payload: dict) -> dict:
    rows = (payload.get("result") or {}).get("extPtyCon") or []
    steams = [row for row in rows if row.get("extPtyConTyp") == "STEAM"]
    if len(steams) != 1:
        return {"status": "unknown"}
    row = steams[0]
    return {
        "status": "linked" if row.get("idx") else "unlinked",
        "persona": str(row.get("disNm") or "").strip(),
        "source": "ea_connected_accounts",
    }


def account_identity_matches(payload: dict, account: dict) -> bool:
    info = payload.get("result") or {}
    return bool(
        str(info.get("eaid") or "").casefold() == str(account.get("originId") or "").casefold()
        and str(info.get("em") or "").casefold() == str(account.get("email") or "").casefold()
        and info.get("eaid") and info.get("em")
    )


def app_2fa_enabled(payload: dict) -> bool:
    info = (payload.get("result") or {}).get("twoFacInfo") or {}
    return info.get("sts") == "ENABLED" and info.get("twoFacTyp") == "APP"


def checkpoint_result(result: dict) -> None:
    # Write before an external commit, so timeouts cannot discard a captured key.
    pending = RES.with_suffix(".pending")
    pending.write_text(json.dumps(result, ensure_ascii=False))
    pending.chmod(0o600)
    pending.replace(RES)


def classify_ea_link_rejection(text: str) -> str | None:
    blob = text or ""
    if re.search(
        r"already linked to your EA Account|Linked Accounts Found|"
        r"can'?t be linked because your Steam|"
        r"已关联到您的 EA|已連結到您的 EA|已連結至您|"
        r"找到連結的帳號|無法連結您的帳號",
        blob,
        re.I,
    ):
        return "steam_already_linked_elsewhere"
    return None


def extract_already_linked_ea_email(text: str) -> str | None:
    match = re.search(
        r"(?:EA Account for|已連結至您|已連結到您|已关联至您|已关联到您)\s*"
        r"([A-Z0-9._%+*-]+@[A-Z0-9.-]+\.[A-Z]{2,})",
        text or "",
        re.I,
    )
    if not match:
        return None
    return match.group(1).rstrip(".,")


def match_masked_email(mask: str, emails: list[str]) -> list[str]:
    needle = (mask or "").strip().rstrip(".,")
    if not needle or "@" not in needle:
        return []
    local, _, domain = needle.partition("@")
    if "*" not in local:
        return [email for email in emails if email.lower() == needle.lower()]
    prefix = local.split("*", 1)[0].lower()
    domain_l = domain.lower()
    matched = []
    for email in emails:
        user, _, dom = email.partition("@")
        if dom.lower() == domain_l and user.lower().startswith(prefix):
            matched.append(email)
    return matched


def login_verification_dialog_visible(
    html: str = "", text: str = "", url: str = ""
) -> bool:
    if "login-verification" in (url or "").lower():
        return True
    blob = f"{html}\n{text}"
    return bool(
        re.search(
            r"選擇您的驗證方法|选择您的验证方法|Choose your verification method",
            blob,
            re.I,
        )
    )


def dismiss_password_save(page) -> None:
    """Dismiss Chrome/Roxy 'save password' bubbles that cover EA buttons."""
    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    for name in (
        "一律不要",
        "不用了，謝謝",
        "不用了，谢谢",
        "Never",
        "Not now",
        "Don't save",
        "Never save",
    ):
        try:
            loc = page.get_by_text(name, exact=True).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=800)
                log(f"dismissed password save via {name}")
                return
        except Exception:
            continue


REMEMBER_DEVICE_RE = re.compile(
    r"記住這台裝置|记住这台设备|Remember this device|"
    r"Keep me signed in|保持登入|保持登录|記住我|记住我",
    re.I,
)
REMEMBER_ME_SELECTORS = (
    "input[name=rememberMe]",
    "input#rememberMe",
    "input[name=remember]",
    "input[name=rememberDevice]",
    "input#rememberDevice",
    "input[type=checkbox][name*=remember]",
    "input[type=checkbox][id*=remember]",
)


def _page_frames(page):
    roots = [page]
    try:
        roots.extend(frame for frame in list(page.frames) if frame is not page)
    except Exception:
        pass
    return roots


def ensure_remember_device(page) -> bool:
    """Check EA 'remember this device' so remid can be issued.

    The control is often a hidden checkbox on the email/password form, not
    the OTP page. ``force`` is required; ``is_visible()`` misses it. After
    create, Juno can park it in an iframe.
    """
    def _checked(root) -> bool:
        for sel in REMEMBER_ME_SELECTORS:
            loc = root.locator(sel).first
            try:
                if loc.count() and loc.is_checked():
                    return True
            except Exception:
                continue
        return False

    for root in _page_frames(page):
        if _checked(root):
            return True
        for sel in REMEMBER_ME_SELECTORS:
            loc = root.locator(sel).first
            try:
                if loc.count():
                    loc.check(force=True, timeout=800)
                    if _checked(root):
                        log("EA remember-me checked")
                        return True
            except Exception:
                continue
        try:
            checked = root.evaluate(
                """() => {
                  const re = /記住這台|记住这台|Remember this device|Keep me signed in|保持登入|保持登录|記住我|记住我/i;
                  const nodes = [
                    ...document.querySelectorAll(
                      'input[name=rememberMe], #rememberMe, input[name=rememberDevice], #rememberDevice, input[type=checkbox]'
                    ),
                  ];
                  for (const el of nodes) {
                    const label = (el.labels && el.labels[0] && el.labels[0].innerText) || '';
                    const nearby = (el.parentElement && el.parentElement.innerText) || '';
                    const named = `${el.name || ''} ${el.id || ''} ${label} ${nearby.slice(0, 120)}`;
                    if (!re.test(named) && el.name !== 'rememberMe' && el.id !== 'rememberMe'
                        && el.name !== 'rememberDevice' && el.id !== 'rememberDevice') {
                      continue;
                    }
                    el.checked = true;
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return !!el.checked;
                  }
                  return false;
                }"""
            )
            if checked:
                log("EA remember-me checked via DOM")
                return True
        except Exception:
            pass
        for loc in (
            root.locator("label[for=rememberMe], label[for=remember]").first,
            root.get_by_text(REMEMBER_DEVICE_RE, exact=False).first,
        ):
            try:
                if loc.count():
                    loc.click(timeout=800)
                    if _checked(root):
                        log("EA remember-me checked via label")
                        return True
            except Exception:
                continue
    log("WARN EA remember-me control not found")
    return False


def choose_login_verification_app(page) -> bool:
    """Prefer authenticator over email on EA's post-2FA method picker."""
    try:
        html = page.content()
        text = re.sub(r"\s+", " ", page.locator("body").inner_text(timeout=4000))
        url = page.url or ""
    except Exception:
        html, text, url = "", "", ""
    if not login_verification_dialog_visible(html, text, url):
        return False
    picked = page.evaluate(
        """() => {
          const nodes = [...document.querySelectorAll('div,li,section,label,button')];
          for (const node of nodes) {
            const t = (node.innerText || '').replace(/\\s+/g, ' ').trim();
            if (!t || t.length > 240) continue;
            if (!/驗證器應用程式|验证器应用|Authenticator/.test(t)) continue;
            if (!/選擇|选择|Select/.test(t)) continue;
            const btn = [...node.querySelectorAll('button,[role=button],a,span')].find(
              (el) => /^(選擇|选择|Select)$/i.test((el.innerText || '').trim())
            );
            if (btn) { btn.click(); return 'app'; }
            node.click();
            return 'app-card';
          }
          return '';
        }"""
    )
    page.wait_for_timeout(600)
    advanced = click_tfa_method_next(page)
    if not advanced:
        try:
            page.get_by_role(
                "button", name=re.compile(r"^(下一頁|下一页|下一步|Next)$", re.I)
            ).first.click(timeout=3000)
            advanced = True
        except Exception:
            advanced = False
    log(f"login verification method picked={picked or 'none'} next={advanced}")
    return bool(picked or advanced)


def is_login_otp_page(html: str, url: str = "", title: str = "") -> bool:
    blob = f"{html}\n{url}\n{title}"
    if LOGIN_OTP_HINT_RE.search(blob):
        return True
    lowered = url.lower()
    return "verifycode" in lowered or "dynamicchallenge" in lowered


def click_send_code(page, timeout: int = 8000) -> bool:
    try:
        page.get_by_role("button", name=SEND_CODE_NAME_RE).first.click(timeout=timeout)
        return True
    except Exception:
        pass
    for selector in ("#btnSendCode", "#logInBtn"):
        try:
            loc = page.locator(selector).first
            if not loc.is_visible(timeout=1000):
                continue
            label = ""
            try:
                label = loc.inner_text(timeout=500)
            except Exception:
                pass
            if selector == "#logInBtn" and label and not send_code_label_matches(label):
                continue
            loc.click(timeout=timeout)
            return True
        except Exception:
            continue
    return False


def click_send_or_resend(page, timeout: int = 4000) -> bool:
    """Click EA's send button, or the same control once its label is 重新发送."""
    if click_send_code(page, timeout=min(1500, timeout)):
        return True
    try:
        page.get_by_role("button", name=RESEND_CODE_NAME_RE).first.click(timeout=timeout)
        return True
    except Exception:
        pass
    try:
        loc = page.locator(
            "#resend, #btnResend, a:text-matches('重新發送|重新发送|請求重新發送|请求重新发送|Resend', 'i')"
        ).first
        if loc.count() and loc.is_visible():
            loc.click(timeout=timeout)
            return True
    except Exception:
        pass
    return False


def click_login_action(page, timeout: int = 8000) -> bool:
    """Click EA juno login continue.

    The visible control is often ``<a id="logInBtn" role="button">下一步</a>``,
    not ``button[type=submit]``. English "NEXT" still works via role name.
    """
    before_url = page.url
    try:
        page.get_by_role("button", name=LOGIN_ACTION_NAME_RE).first.click(timeout=timeout)
        return True
    except Exception:
        try:
            if page.url != before_url:
                return True
        except Exception:
            pass
    for selector in LOGIN_ACTION_SELECTORS:
        try:
            loc = page.locator(selector).first
            if loc.is_visible(timeout=1000):
                loc.click(timeout=timeout)
                return True
        except Exception:
            continue
    return False


def dismiss_cookies(page) -> None:
    # TrustArc often lives in an iframe. HK copy uses 您的隱私選擇, not Accept.
    roots = [page]
    try:
        roots.extend(frame for frame in list(page.frames) if frame is not page)
    except Exception:
        pass
    selectors = (
        "#truste-consent-button",
        "#truste-consent-close",
        "a.truste-close",
        "#consent_blackbar button",
    )
    labels = (
        "Accept All",
        "Accept",
        "I Accept",
        "Agree",
        "全部接受",
        "接受全部",
        "同意全部",
        "我同意",
        "允許全部",
        "允许全部",
        "您的隱私選擇",
        "您的隐私选择",
        "同意",
        "接受",
    )
    for root in roots:
        for sel in selectors:
            try:
                loc = root.locator(f"{sel}:visible").first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500, force=True)
                    page.wait_for_timeout(400)
                    log(f"dismissed cookie via {sel}")
                    return
            except Exception:
                continue
        for name in labels:
            try:
                loc = root.get_by_text(name, exact=True).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=1500, force=True)
                    page.wait_for_timeout(400)
                    log(f"dismissed cookie via {name}")
                    return
            except Exception:
                continue
    try:
        page.evaluate(
            """() => {
              const ban = [...document.querySelectorAll('button, [role=button], a')];
              for (const b of ban) {
                const t = (b.innerText||'').replace(/\\s+/g,' ').trim();
                if (/^(accept all|accept|i accept|agree|全部接受|接受全部|同意全部|我同意|允許全部|允许全部|您的隱私選擇|您的隐私选择|同意|接受|×|x)$/i.test(t)) {
                  b.click(); return t;
                }
              }
              for (const sel of [
                '#truste-consent-track', '#consent_blackbar',
                '.truste_cm_outer', '#truste-consent-content'
              ]) {
                document.querySelectorAll(sel).forEach((el) => {
                  el.style.display = 'none';
                });
              }
              return null;
            }"""
        )
    except Exception:
        pass


def visible_count(locator) -> int:
    """Count only rendered matches; the EA SPA keeps hidden duplicate trees."""
    try:
        total = locator.count()
    except Exception:
        return 0
    found = 0
    for index in range(total):
        try:
            if locator.nth(index).is_visible(timeout=0):
                found += 1
        except Exception:
            continue
    return found


def identity_challenge_visible(page) -> bool:
    """Return whether the rendered EA identity challenge is still open."""
    headings = page.get_by_text(
        re.compile(r"^\s*(Verify your identity|验证您的身份|驗證您的身份)\s*$", re.I),
        exact=True,
    )
    for i in range(headings.count()):
        try:
            if headings.nth(i).is_visible():
                return True
        except Exception:
            continue
    return False


STEAM_URL_RE = re.compile(
    r"https?:\\?/\\?/(?:[A-Za-z0-9.-]+\.)?(?:steamcommunity\.com|steampowered\.com)"
    r"[^\s\"'<>\\]*",
    re.I,
)
EA_AM_ERROR_RE = re.compile(r"errorCode[\"']?\s*[:=]\s*[\"']?(\d+)", re.I)
_STEAM_URL_SKIP = (
    "reportclienterror",
    "/favicon",
    ".png",
    ".jpg",
    ".css",
    ".woff",
    ".ico",
)
PREPARE_STEAM_OAUTH_CLICK_JS = """() => {
  window.__steamOpenUrl = window.__steamOpenUrl || null;
  window.__eaLinkExternal = window.__eaLinkExternal || null;
  const origOpen = window.open;
  window.open = function(url) {
    try { if (url) window.__steamOpenUrl = String(url); } catch (e) {}
    return origOpen.apply(this, arguments);
  };
  if (!window.__eaFetchHooked) {
    window.__eaFetchHooked = true;
    const origFetch = window.fetch;
    window.fetch = async function() {
      const res = await origFetch.apply(this, arguments);
      try {
        const raw = arguments[0];
        const url = String(typeof raw === 'string' ? raw : (raw && raw.url) || '');
        if (
          url.indexOf('link-external-party') !== -1
          || url.indexOf('steamcommunity') !== -1
          || url.indexOf('steampowered') !== -1
        ) {
          window.__eaLinkExternal = await res.clone().text();
        }
      } catch (e) {}
      return res;
    };
  }
  const btn = document.querySelector(
    '[data-testid="connected-accounts-modal-continue-button"]'
  );
  const hrefOf = (el) => (el && (el.getAttribute('href') || el.href)) || null;
  window.__eaContinueHref = hrefOf(btn) || hrefOf(btn && btn.querySelector('a[href]'));
  if (btn) {
    if (btn.tagName === 'A') {
      btn.removeAttribute('href');
      btn.removeAttribute('target');
    }
    const nested = btn.querySelector('a[href]');
    if (nested) {
      nested.removeAttribute('href');
      nested.removeAttribute('target');
    }
  }
  return {
    href: window.__eaContinueHref,
    tag: btn ? btn.tagName : null,
    html: btn ? String(btn.outerHTML).slice(0, 400) : '',
  };
}"""


def is_steam_page(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain)
               for domain in ("steamcommunity.com", "steampowered.com"))


def _unescape_steam_url(value: str) -> str:
    text = (value or "").strip().strip("\\\"'")
    text = text.replace("\\/", "/").replace("\\u0026", "&").replace("&amp;", "&")
    return text.rstrip(").,;\\\"'")


def extract_steam_oauth_urls(*blobs: object) -> list[str]:
    """Pull steamcommunity/steampowered URLs out of AM JSON, HTML, or hooks."""
    found: list[str] = []
    seen: set[str] = set()

    def add(url: str) -> None:
        raw = _unescape_steam_url(url)
        if not raw.startswith("http"):
            return
        if not is_steam_page(raw):
            return
        lowered = raw.lower()
        if any(skip in lowered for skip in _STEAM_URL_SKIP):
            return
        if raw not in seen:
            seen.add(raw)
            found.append(raw)

    def walk(obj: object, depth: int = 0) -> None:
        if obj is None or depth > 12:
            return
        if isinstance(obj, str):
            for match in STEAM_URL_RE.finditer(obj):
                add(match.group(0))
            stripped = obj.strip()
            if stripped[:1] in "{[":
                try:
                    walk(json.loads(stripped), depth + 1)
                except Exception:
                    pass
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in {
                    "url", "href", "redirect", "redirecturl", "redirect_url",
                    "authurl", "lnk", "redurl", "location",
                } and isinstance(value, str):
                    add(value)
                walk(value, depth + 1)
            return
        if isinstance(obj, (list, tuple)):
            for item in obj:
                walk(item, depth + 1)

    for blob in blobs:
        walk(blob)
    return [
        url
        for url in found
        if not any(other != url and other.startswith(url) for other in found)
    ]


def prefer_steam_oauth_url(urls: list[str] | None) -> str | None:
    ranked: list[tuple[int, str]] = []
    for url in urls or []:
        low = url.lower()
        if not is_steam_page(url):
            continue
        score = 1
        if "/oauth/login" in low and "loginform" not in low:
            score = 4
        elif "/openid/login" in low:
            score = 4
        elif "loginform" in low:
            score = 3
        elif "login.steampowered.com" in low or "/login" in low:
            score = 2
        ranked.append((score, url))
    ranked.sort(key=lambda item: -item[0])
    return ranked[0][1] if ranked else None


def am_error_code_from_text(text: str) -> str | None:
    match = EA_AM_ERROR_RE.search(text or "")
    return match.group(1) if match else None


def am_error_code_from_page(page) -> str | None:
    blobs: list[str] = []
    try:
        blobs.append(getattr(page, "url", "") or "")
    except Exception:
        pass
    try:
        blobs.append(page.inner_text("body", timeout=2000)[:4000])
    except Exception:
        pass
    return am_error_code_from_text("\n".join(blobs))


def steam_urls_from_document(page) -> list[str]:
    blobs: list[object] = []
    try:
        blobs.append(getattr(page, "url", "") or "")
    except Exception:
        pass
    try:
        blobs.append(page.inner_text("body", timeout=2000)[:20000])
    except Exception:
        pass
    try:
        blobs.append(page.content()[:40000])
    except Exception:
        pass
    return extract_steam_oauth_urls(*blobs)


def prepare_steam_oauth_click(page) -> dict:
    """Stop Continue from GETting /am/data/1/link-external-party as a document."""
    try:
        info = page.evaluate(PREPARE_STEAM_OAUTH_CLICK_JS) or {}
    except Exception as exc:
        log(f"prepare steam oauth click {type(exc).__name__}")
        return {}
    href = str(info.get("href") or "")
    tag = str(info.get("tag") or "")
    html = str(info.get("html") or "").replace("\n", " ")[:240]
    log(f"steam continue control tag={tag} href={safe_url(href)} html={html}")
    return info if isinstance(info, dict) else {}


def hooked_steam_oauth_urls(page) -> list[str]:
    try:
        captured = page.evaluate(
            """() => ({
              open: window.__steamOpenUrl || null,
              body: window.__eaLinkExternal || null,
              href: window.__eaContinueHref || null,
            })"""
        ) or {}
    except Exception:
        captured = {}
    return extract_steam_oauth_urls(
        captured.get("open"), captured.get("body"), captured.get("href")
    )


EA_STEAM_RELAY_URL = "https://myaccount.ea.com/am/data/1/link-external-party?externalParty=STEAM"


def _goto_steam_oauth(page, url: str) -> bool:
    try:
        page.goto(str(url), wait_until="domcontentloaded", timeout=45000)
        return is_steam_page(getattr(page, "url", "") or "")
    except Exception as exc:
        log(f"Steam OAuth goto {type(exc).__name__}: {net_error_reason(exc) or exc}")
        return False


def ensure_live_steam_oauth_page(
    context,
    parent_page,
    steam_page=None,
    captured_urls: list[str] | None = None,
    *,
    am_json=None,
    timeout_ms: int = 30000,
):
    """Turn EA's JSON/juno relay into a real steamcommunity OAuth tab."""
    urls = list(captured_urls or [])
    urls.extend(hooked_steam_oauth_urls(parent_page))
    error = None
    if steam_page is not None:
        error = am_error_code_from_page(steam_page)
        if error:
            log(
                f"EA AM popup errorCode={error} "
                f"url={safe_url(getattr(steam_page, 'url', '') or '')}"
            )
            parsed = urlparse(getattr(steam_page, "url", "") or "")
            if parsed.hostname == "myaccount.ea.com" and parsed.path == "/am/data/1/link-external-party":
                # EA's current UI navigates this popup with externalParty=STEAM.
                # It is a redirect endpoint, not a JSON POST RPC. Keep its opener
                # so the successful callback can notify the account page.
                log("Retry EA Steam relay with the selected provider")
                try:
                    steam_page.goto(EA_STEAM_RELAY_URL, wait_until="commit", timeout=30000)
                except PWTimeout:
                    pass  # It may already be following the redirect chain.
        else:
            urls.extend(steam_urls_from_document(steam_page))
    found = await_steam_oauth_page(context, steam_page, timeout_ms=timeout_ms)
    found_url = getattr(found, "url", "") if found is not None else ""
    if found is not None and is_steam_page(found_url) and not str(found_url).startswith("about:"):
        return found
    target = prefer_steam_oauth_url(urls)
    if target:
        log(f"open Steam OAuth {safe_url(target)}")
        dest = None
        try:
            if (
                steam_page is not None
                and steam_page is not parent_page
                and not steam_page.is_closed()
            ):
                dest = steam_page
        except Exception:
            dest = None
        if dest is None or dest is parent_page:
            if dest is parent_page:
                log("Steam OAuth would replace the EA tab; opening a new tab instead")
            dest = context.new_page()
        if _goto_steam_oauth(dest, target):
            steam_page = dest
        else:
            steam_page = dest
    # Do not present an EA JSON rejection as a Steam page to the login loop.
    if not target:
        return None
    found = await_steam_oauth_page(context, steam_page, timeout_ms=timeout_ms)
    return found if found is not None and is_steam_page(getattr(found, "url", "") or "") else None


def resolve_steam_oauth_page(context, preferred=None):
    """Prefer the real Steam tab when EA's juno relay is still the popup handle."""
    pages = []
    try:
        pages = list(getattr(context, "pages", None) or [])
    except Exception:
        pages = []
    ordered = []
    if preferred is not None:
        ordered.append(preferred)
    ordered.extend(pages)
    seen: set[int] = set()
    for page in ordered:
        marker = id(page)
        if marker in seen:
            continue
        seen.add(marker)
        try:
            if page.is_closed():
                continue
        except Exception:
            continue
        if is_steam_page(getattr(page, "url", "") or ""):
            return page
    return preferred


def await_steam_oauth_page(context, preferred=None, timeout_ms: int = 25000):
    """Wait until a Steam tab exists; EA's login relay often opens first."""
    deadline = time.monotonic() + max(0.2, timeout_ms / 1000)
    last = preferred
    while True:
        found = resolve_steam_oauth_page(context, last)
        url = getattr(found, "url", "") if found is not None else ""
        if (
            found is not None
            and is_steam_page(url)
            and not str(url).startswith("about:")
        ):
            return found
        if time.monotonic() >= deadline:
            return found or last
        last = found or last
        try:
            if last is not None:
                last.wait_for_timeout(400)
            else:
                time.sleep(0.4)
        except Exception:
            time.sleep(0.4)


def steam_credentials_required(user_box, pw_box) -> bool:
    return user_box is not None and pw_box is not None


def steam_cookie_domain(domain: str) -> bool:
    host = (domain or "").lstrip(".").lower()
    return any(
        host == name or host.endswith("." + name)
        for name in ("steamcommunity.com", "steampowered.com")
    )


def close_stray_oauth_pages(context, keep_page=None) -> int:
    """Close leftover Steam / EA relay tabs on a reused fingerprint window."""
    closed = 0
    for page in list(iter_live_pages(context)):
        if keep_page is not None and page is keep_page:
            continue
        url = getattr(page, "url", "") or ""
        if not (is_steam_page(url) or "signin.ea.com" in url):
            continue
        try:
            page.close()
            closed += 1
        except Exception:
            continue
    return closed


def purge_steam_session(context, keep_page=None) -> int:
    """Forget prior Steam identity so the next OAuth login uses the intended account."""
    closed = close_stray_oauth_pages(context, keep_page=keep_page)
    try:
        cookies = list(context.cookies() or [])
    except Exception:
        return closed
    steam_cookies = [
        cookie for cookie in cookies if steam_cookie_domain(cookie.get("domain") or "")
    ]
    if not steam_cookies:
        return closed
    try:
        for cookie in steam_cookies:
            context.clear_cookies(
                name=cookie.get("name"),
                domain=cookie.get("domain"),
                path=cookie.get("path") or "/",
            )
        return closed
    except TypeError:
        remaining = [
            cookie
            for cookie in cookies
            if not steam_cookie_domain(cookie.get("domain") or "")
        ]
        context.clear_cookies()
        if remaining:
            context.add_cookies(remaining)
        return closed


def steam_authenticated_or_consent(page) -> bool:
    """Return true only when Steam rendered a post-login state we can act on."""
    if page is None or not is_steam_page(getattr(page, "url", "") or ""):
        return False
    try:
        text = page.locator("body").inner_text(timeout=1500)
    except Exception:
        text = ""
    if STEAM_POST_LOGIN_RE.search(text or ""):
        return True
    try:
        image = page.locator(STEAM_OAUTH_LOGIN_BUTTON).first
        return bool(
            image.count()
            and image.is_visible()
            and not steam_password_field_present(page)
        )
    except Exception:
        return False


def tfa_settings_visible(page) -> bool:
    controls = page.locator(
        '[data-testid="tfa-turn-on-button"]:visible, '
        '[data-testid="tfa-section-segment-control-app"]:visible, '
        '[data-testid="tfa-section-next-button"]:visible'
    )
    return bool(controls.count()) or identity_challenge_visible(page)


def open_tfa_settings(page, timeout_ms: int = 45000, *, confirm_open: bool = False) -> None:
    """Wait for the security SPA to render before opening its visible control."""
    deadline = time.monotonic() + timeout_ms / 1000
    if confirm_open and tfa_settings_visible(page):
        return
    entry = page.locator(
        '[data-testid="enter-tfa-button"]:visible, #enter-tfa-button:visible'
    ).first
    first_wait = min(timeout_ms, 12000) if confirm_open else timeout_ms
    try:
        entry.wait_for(state="visible", timeout=first_wait)
    except PWTimeout:
        # A blank security route can be a failed SPA load. Reload once before
        # any setup mutation; a populated but changed page needs diagnostics.
        body = ""
        try:
            body = page.locator("body").inner_text(timeout=1500) or ""
        except Exception:
            body = ""
        if body.strip():
            raise
        page.reload(wait_until="domcontentloaded")
        remaining = max(1, int((deadline - time.monotonic()) * 1000))
        entry.wait_for(state="visible", timeout=min(remaining, 8000))
    dismiss_cookies(page)
    try:
        entry.evaluate("el => el.click()")
    except Exception:
        try:
            entry.click(
                timeout=min(3000, max(1, int((deadline - time.monotonic()) * 1000))),
                no_wait_after=True,
                force=True,
            )
        except PWTimeout:
            if not confirm_open:
                raise
            if tfa_settings_visible(page):
                return
    if not confirm_open:
        return
    retried = False
    settle_until = time.monotonic() + 1.2
    panel_deadline = min(deadline, time.monotonic() + 5)
    while time.monotonic() < panel_deadline:
        if tfa_settings_visible(page):
            return
        if time.monotonic() >= settle_until and not retried and entry.count() and entry.is_visible() and entry.is_enabled():
            # Roxy may stall native input dispatch after actionability checks.
            # Re-resolve the same visible control and verify its resulting UI.
            try:
                entry.evaluate("el => el.click()")
            except Exception:
                pass
            retried = True
        page.wait_for_timeout(150)
    raise PWTimeout("2FA settings entry did not open a settings panel or identity challenge")


def steam_password_field_present(page) -> bool:
    try:
        box = page.locator("input[type='password'], #steamPassword").first
        return bool(box.count())
    except Exception:
        return False


def steam_password_field_visible(page) -> bool:
    try:
        box = page.locator("input[type='password']").first
        return bool(box.count() and box.is_visible())
    except Exception:
        return False


def click_named_role_button(page, labels, *, before_click=None) -> bool:
    locators = (
        page.get_by_role("button", name=labels),
        page.get_by_role("link", name=labels),
        page.get_by_text(labels),
    )
    for located in locators:
        try:
            count = located.count()
        except Exception:
            continue
        for i in range(count):
            button = located.nth(i)
            try:
                if not (button.is_visible() and button.is_enabled()):
                    continue
            except Exception:
                continue
            if before_click:
                before_click()
            try:
                button.evaluate("el => el.click()")
            except Exception:
                try:
                    if page.is_closed():
                        return True
                except Exception:
                    return True
                try:
                    button.click(timeout=3000, no_wait_after=True, force=True)
                except Exception:
                    try:
                        if page.is_closed():
                            return True
                    except Exception:
                        return True
                    continue
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
            return True
    return False


def ea_login_form_url(url: str) -> bool:
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower().rstrip("/")
    return host.endswith("signin.ea.com") and path.endswith("/juno/login")


def ea_steam_callback_reached(context) -> bool:
    for page in iter_live_pages(context):
        url = (getattr(page, "url", "") or "").lower()
        if "callback/linking" in url or "callbacksteam" in url or "/juno/steamc2s" in url:
            return True
    return False


def click_oauth_consent(page, *, on_ea: bool, before_click=None) -> bool:
    # Accessible button roles include both <button> text and <input> value.
    # Steam's legacy OAuth consent uses Allow or #imageLogin ("Login");
    # the HK already-signed-in picker uses 登录 / Sign In. Prefer Allow, and
    # only treat Login/登录 as consent when no password field is on the page.
    labels = [EA_LINK_ACCOUNTS_RE] if on_ea else [STEAM_OAUTH_CONSENT_RE]
    if not on_ea and not steam_password_field_present(page):
        labels.append(STEAM_OAUTH_LOGIN_CONSENT_RE)
    try:
        for pattern in labels:
            if click_named_role_button(page, pattern, before_click=before_click):
                return True
        if on_ea and not ea_login_form_url(getattr(page, "url", "") or ""):
            continue_btn = page.locator("#continue, a#continue[role=button]").first
            try:
                if continue_btn.count():
                    if before_click:
                        before_click()
                    continue_btn.evaluate("el => el.click()")
                    try:
                        page.wait_for_timeout(400)
                    except Exception:
                        pass
                    return True
            except Exception:
                pass
        if not on_ea and not steam_password_field_present(page):
            image = page.locator(STEAM_OAUTH_LOGIN_BUTTON).first
            if image.count() and image.is_visible():
                if before_click:
                    before_click()
                image.click(timeout=8000, no_wait_after=True)
                try:
                    page.wait_for_timeout(400)
                except Exception:
                    pass
                return True
    except Exception as exc:
        if page.is_closed():
            return True
        log(f"oauth consent {type(exc).__name__}")
        return False
    return False


def iter_live_pages(context):
    try:
        pages = list(getattr(context, "pages", None) or [])
    except Exception:
        pages = []
    for page in pages:
        try:
            if page.is_closed():
                continue
        except Exception:
            continue
        yield page


def page_body_text(page, timeout_ms: int = 1500) -> str:
    """Read visible body text without blocking 30s on EA's juno loading document."""
    locator_fn = getattr(page, "locator", None)
    if callable(locator_fn):
        try:
            return page.locator("body").inner_text(timeout=timeout_ms) or ""
        except Exception:
            return ""
    try:
        return page.inner_text("body") or ""
    except Exception:
        return ""


def steam_link_rejection_from_context(context) -> tuple[str | None, str | None]:
    """EA's already-linked page often lands on a later tab, not the myaccount one."""
    for page in iter_live_pages(context):
        text = page_body_text(page)
        if not text:
            continue
        code = classify_ea_link_rejection(text)
        if code:
            return code, extract_already_linked_ea_email(text)
    return None, None


def pending_ea_link_accounts(context) -> bool:
    """True while the Steam callback popup still shows 連結帳號."""
    for page in iter_live_pages(context):
        url = getattr(page, "url", "") or ""
        if is_steam_page(url):
            continue
        if EA_LINK_ACCOUNTS_RE.match(_page_title(page)):
            return True
        try:
            button = page.get_by_role("button", name=EA_LINK_ACCOUNTS_RE).first
            if button.count() and button.is_visible():
                return True
        except Exception:
            pass
        try:
            link = page.get_by_role("link", name=EA_LINK_ACCOUNTS_RE).first
            if link.count() and link.is_visible():
                return True
        except Exception:
            pass
        try:
            modal = page.locator(
                '[data-testid="connected-accounts-modal-continue-button"]'
            ).first
            if modal.count() and modal.is_visible():
                return True
        except Exception:
            pass
    return False


def click_ea_link_accounts_anywhere(context, *, before_click=None) -> bool:
    """Click 連結帳號 on whichever EA tab actually rendered it.

    Steam's callback often parks that button on ``juno/login`` in the popup
    while the original myaccount tab stays logged in. Skipping every login URL
    left the human control unclicked.
    """
    for page in iter_live_pages(context):
        url = getattr(page, "url", "") or ""
        if is_steam_page(url):
            continue
        # The last confirm sits on the account page as a modal. A generic
        # Continue there is a different control and must stay unclicked.
        if "myaccount.ea.com" in url.lower():
            if click_modal_continue(page, testid_only=True):
                return True
            if click_named_role_button(page, EA_LINK_ACCOUNTS_RE, before_click=before_click):
                return True
            continue
        title = _page_title(page)
        if (
            ea_login_form_url(url)
            and _login_email_visible(page)
            and not EA_LINK_ACCOUNTS_RE.match(title)
        ):
            continue
        if click_oauth_consent(page, on_ea=True, before_click=before_click):
            return True
    return False


def _page_title(page) -> str:
    try:
        return (page.title() or "").strip()
    except Exception:
        return ""


def _login_email_visible(page) -> bool:
    try:
        loc = page.locator("#email, input[name=email]").first
        return bool(loc.count() and loc.is_visible())
    except Exception:
        return False


def _dom_click(locator, timeout_ms: int = 3000) -> bool:
    """Click without sitting on Playwright actionability behind an overlay."""
    try:
        locator.evaluate("el => el.click()")
        return True
    except Exception:
        try:
            locator.click(timeout=timeout_ms, no_wait_after=True, force=True)
            return True
        except Exception:
            return False


def click_tfa_turn_on(page) -> bool:
    """Open the 2FA method modal from the visible 開啟/Turn on control.

    EA keeps mobile and desktop copies. Playwright's native click on the
    visible duplicate can sit for 30s behind a cookie/paloma overlay even
    after the locator already resolved. Drive the same visible control via JS.
    """
    status = js_click_visible(
        page,
        """() => {
          const nodes = [...document.querySelectorAll(
            '[data-testid="tfa-turn-on-button"], #tfa-turn-on-button'
          )];
          for (const b of nodes) {
            const r = b.getBoundingClientRect();
            if (r.width > 2 && r.height > 2 && !b.disabled) {
              b.scrollIntoView({block:'center'});
              b.click();
              return 'turn-on';
            }
          }
          return 'miss';
        }""",
    )
    if status == "turn-on":
        log("2fa click turn-on testid")
        try:
            page.wait_for_timeout(400)
        except Exception:
            pass
        return True
    buttons = page.get_by_test_id("tfa-turn-on-button")
    try:
        total = buttons.count()
    except Exception:
        total = 0
    for i in range(total):
        candidate = buttons.nth(i)
        try:
            if not candidate.is_visible():
                continue
        except Exception:
            continue
        if not _dom_click(candidate):
            continue
        log("2fa click turn-on testid")
        try:
            page.wait_for_timeout(400)
        except Exception:
            pass
        return True
    log(f"2fa click turn-on missed status={status}")
    return False


def click_tfa_method_next(page, timeout_ms: int = 8000) -> bool:
    """Advance the selected 2FA method, including EA's Hong Kong 下一頁 control."""
    stable = page.locator(
        '#tfa-section-next-button:visible, '
        '[data-testid="tfa-section-next-button"]:visible'
    )
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        candidates = stable
        if not candidates.count():
            # Text fallback stays inside a modal, away from background actions.
            candidates = page.locator('[role="dialog"]:visible').get_by_role(
                "button", name=re.compile(r"^(Next|下一頁|下一页|下一步|下一个)$", re.I)
            )
        if not candidates.count():
            return False
        for candidate in candidates.all():
            if candidate.is_visible() and candidate.is_enabled():
                if _dom_click(candidate, timeout_ms=min(3000, timeout_ms)):
                    return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(100)


def continue_tfa_app_installation(page) -> bool:
    """Skip the optional app-download step, stopping before key verification."""
    installation = re.compile(
        r"安裝驗證器應用程式|安装验证器应用|install[^\n]{0,60}authenticator", re.I
    )
    for dialog in page.get_by_role("dialog").all():
        if not dialog.is_visible() or not installation.search(dialog.inner_text()):
            continue
        buttons = dialog.get_by_role(
            "button", name=re.compile(r"^(Continue|繼續|继续)$", re.I)
        )
        for button in buttons.all():
            if button.is_visible() and button.is_enabled():
                if _dom_click(button):
                    return True
    return False


STEAM_PASSWORD_ENTRY_RE = re.compile(
    r"sign in with (your )?account name|account name and password|"
    r"use (your )?password|sign in another way|"
    r"帳戶名稱|账户名称|使用密碼|使用密码",
    re.I,
)


def reveal_steam_password_signin(page) -> bool:
    """If Steam is sitting on QR-only React, switch to the account/password form."""
    status = js_click_visible(
        page,
        """() => {
          const re = /sign in with (your )?account name|account name and password|use (your )?password|sign in another way|帳戶名稱|账户名称|使用密碼|使用密码/i;
          for (const el of document.querySelectorAll('a,button,[role=button],span')) {
            const t = (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim();
            if (!t || t.length > 80 || !re.test(t)) continue;
            const r = el.getBoundingClientRect();
            if (r.width > 2 && r.height > 2) {
              el.click();
              return 'password-entry';
            }
          }
          return 'miss';
        }""",
    )
    if status == "password-entry":
        log("Steam QR gate switched to password sign-in")
        try:
            page.wait_for_timeout(300)
        except Exception:
            pass
        return True
    return False


def wait_for_steam_credentials(page, timeout_ms: int = 45000):
    """Wait for Steam's React inputs or a real post-login consent control.

    Steam can finish ``domcontentloaded`` and even time out ``networkidle``
    before the credential inputs mount. A visible Sign In button alone is not
    sufficient because the loading login page renders it before the inputs.
    Search iframes too: oauth/loginform sometimes mounts the fields off the
    main frame. Hidden-but-attached inputs still count — Roxy's missing
    Windows fonts often leave the classic #steamPassword at 0x0.
    """
    deadline = time.monotonic() + timeout_ms / 1000
    password_sel = "input[type='password'], #steamPassword, input[name=password]"
    user_sel = (
        "#steamAccountName, input[name=username], input[type='text'], "
        "input[type='email'], input[autocomplete='username']"
    )
    attached = None
    started = time.monotonic()
    revealed = False
    while time.monotonic() < deadline:
        if not is_steam_page(page.url):
            page.wait_for_timeout(1000)
            continue
        roots = [page]
        try:
            roots.extend(frame for frame in list(page.frames) if frame is not page)
        except Exception:
            pass
        for root in roots:
            password = root.locator(password_sel).first
            anchored_user = root.locator(
                "xpath=(//input[@type='password'])[1]/preceding::input[@type='text'][1]"
            ).first
            fallback_user = root.locator(user_sel).first
            user = anchored_user if anchored_user.count() else fallback_user
            try:
                if password.count() and user.count():
                    attached = (user, password)
                    visible = False
                    try:
                        visible = bool(password.is_visible() and user.is_visible())
                    except Exception:
                        visible = False
                    if visible:
                        try:
                            if not password.is_enabled() or not user.is_enabled():
                                continue
                        except Exception:
                            pass
                        page.wait_for_timeout(random.randint(200, 400))
                        return user, password
                    # Roxy often mounts #steamPassword at 0x0. fill_steam_login
                    # already force-fills; waiting out the 45s budget just to
                    # return these same locators is pure idle time.
                    page.wait_for_timeout(250)
                    return user, password
            except Exception:
                continue

        consent = page.get_by_role("button", name=STEAM_OAUTH_CONSENT_RE)
        try:
            if any(consent.nth(i).is_visible() for i in range(consent.count())):
                return None, None
        except Exception:
            pass
        try:
            image = page.locator(STEAM_OAUTH_LOGIN_BUTTON).first
            if (
                image.count()
                and image.is_visible()
                and not steam_password_field_present(page)
            ):
                return None, None
        except Exception:
            pass
        if (
            attached is None
            and not revealed
            and time.monotonic() - started >= 2.0
        ):
            revealed = reveal_steam_password_signin(page)
        page.wait_for_timeout(250)
    return attached if attached else (None, None)


def steam_login_inventory(page) -> dict:
    try:
        return page.evaluate(
            """() => {
              const all = [];
              const visit = (doc, frame) => {
                if (!doc) return;
                for (const el of doc.querySelectorAll('input,button')) {
                  all.push({
                    frame,
                    tag: el.tagName,
                    type: el.type || '',
                    name: el.name || '',
                    id: el.id || '',
                    w: el.offsetWidth,
                    h: el.offsetHeight,
                    disabled: !!el.disabled,
                  });
                }
              };
              visit(document, 'top');
              for (const iframe of document.querySelectorAll('iframe')) {
                try { visit(iframe.contentDocument, iframe.src || 'iframe'); }
                catch (e) {}
              }
              return {
                url: location.href,
                title: document.title,
                frames: document.querySelectorAll('iframe').length,
                inputs: all.slice(0, 40),
                text: ((document.body && document.body.innerText) || '').slice(0, 240),
              };
            }"""
        )
    except Exception as exc:
        return {"error": type(exc).__name__}


def fill_steam_login(page, user_box, pass_box, username: str, password: str) -> bool:
    from . import ea_pw_register as pw_reg
    try:
        pw_reg.human_delay(1.0, 2.0)
        pw_reg.human_type(page, user_box, username)
        pw_reg.human_delay(0.4, 0.9)
        pw_reg.fill_secret(page, pass_box, password)
        try:
            if user_box.input_value() != username:
                user_box.fill(username)
        except Exception:
            pass
        return True
    except Exception as exc:
        log(f"human_type steam {type(exc).__name__}; force fill")
    try:
        user_box.fill(username, force=True, timeout=4000)
        pass_box.fill(password, force=True, timeout=4000)
        return True
    except Exception:
        pass
    try:
        return bool(
            page.evaluate(
                """({user, password}) => {
                  const setVal = (el, value) => {
                    const proto = window.HTMLInputElement.prototype;
                    const desc = Object.getOwnPropertyDescriptor(proto, 'value');
                    desc.set.call(el, value);
                    el.dispatchEvent(new Event('input', {bubbles:true}));
                    el.dispatchEvent(new Event('change', {bubbles:true}));
                  };
                  const pass = document.querySelector(
                    "input[type='password'], #steamPassword, input[name=password]"
                  );
                  let userEl = document.querySelector(
                    "#steamAccountName, input[name=username], input[type=email], input[autocomplete=username]"
                  );
                  if (!userEl && pass) {
                    const inputs = [...document.querySelectorAll('input')];
                    userEl = inputs.find((el) => el !== pass && (el.type === 'text' || el.type === 'email')) || null;
                  }
                  if (!userEl || !pass) return false;
                  setVal(userEl, user);
                  setVal(pass, password);
                  return true;
                }""",
                {"user": username, "password": password},
            )
        )
    except Exception as exc:
        log(f"js fill steam {type(exc).__name__}")
        return False


def submit_steam_password(page) -> bool:
    """Submit a filled password form when the Sign in click never landed.

    Steam can keep polling the QR session after the password fields are
    filled. One form submit is what starts BeginAuthSessionViaCredentials.
    Enter alone does not submit when the form has no submit button.
    """
    try:
        submitted = page.evaluate(
            """() => {
              const pass = document.querySelector(
                "input[type='password'], #steamPassword, input[name=password]"
              );
              if (!pass) return false;
              const form = pass.closest('form');
              if (form && typeof form.requestSubmit === 'function') {
                try { form.requestSubmit(); return true; } catch (e) {}
              }
              const btn = document.querySelector(
                'button[type=submit], input[type=submit], #login_btn, .btn_blue_steamui'
              );
              if (btn) { btn.click(); return true; }
              return false;
            }"""
        )
        if submitted:
            return True
    except Exception as exc:
        log(f"WARN steam password submit: {type(exc).__name__}")
    password = page.locator(
        "input[type='password'], #steamPassword, input[name=password]"
    ).first
    try:
        if password.count():
            password.press("Enter", timeout=3000)
            return True
    except Exception as exc:
        log(f"WARN steam password enter: {type(exc).__name__}")
    return False


def steam_auth_page_is_live(page) -> bool:
    """Steam OAuth tab only. EA's JSON relay popup is not a Steam login."""
    if page is None:
        return False
    try:
        if page.is_closed():
            return False
    except Exception:
        return False
    url = getattr(page, "url", "") or ""
    return is_steam_page(url) or url.startswith("about:")


def classify_steam_link_state(
    *,
    linked_visible: int,
    unlinked_visible: int,
    has_visible_unlink: bool,
    body_text: str,
) -> str:
    """Classify the final Steam card without trusting a click or hidden DOM."""
    linked_text = bool(
        re.search(
            r"\bLinked\b[\s\S]{0,800}\bSteam\b[\s\S]{0,400}\bUnlink\b",
            body_text,
            re.I,
        )
    )
    if has_visible_unlink and (linked_visible > 0 or linked_text):
        return "linked"
    if unlinked_visible > 0:
        return "unlinked"
    return "unknown"


def connected_accounts_rpc(page) -> dict:
    """Read Steam link state from EA's in-page AM JSON, not the SPA shell."""
    try:
        reply = page.evaluate(
            """async () => {
              const headerMeta = document.querySelector('meta[name="csrf-header-name"]');
              const headerName = headerMeta?.content || '';
              const token = headerName
                ? document.querySelector(`meta[name="${CSS.escape(headerName)}"]`)?.content || ''
                : '';
              const headers = {'X-Requested-With':'XMLHttpRequest'};
              if (headerName && token) headers[headerName] = token;
              const controller = new AbortController();
              const timer = setTimeout(() => controller.abort(), 2500);
              try {
                const response = await fetch('/am/data/1/connected-accounts', {
                  method: 'GET', credentials: 'same-origin', headers,
                  signal: controller.signal,
                });
                const payload = await response.json();
                return {ok: response.ok, status: response.status, payload};
              } catch (_) {
                return {ok: false, status: 0, payload: {}};
              } finally {
                clearTimeout(timer);
              }
            }"""
        )
    except Exception:
        return {"status": "unknown"}
    if not isinstance(reply, dict) or not reply.get("ok"):
        return {"status": "unknown"}
    state = steam_connection_from_payload(reply.get("payload") or {})
    if state.get("status") == "linked":
        return {
            "status": "linked",
            "persona": state.get("persona"),
            "linked_visible": 1,
            "unlinked_visible": 0,
            "has_visible_unlink": True,
            "source": "ea_connected_accounts",
        }
    return state


def inspect_steam_link_state(page) -> dict:
    url = getattr(page, "url", "") or ""
    if ea_login_form_url(url):
        return {
            "status": "logged_out",
            "linked_visible": 0,
            "unlinked_visible": 0,
            "has_visible_unlink": False,
        }
    linked_visible = visible_count(
        page.locator(
            '[data-testid="connected-accounts-linked-section-pre-title-Steam"]',
            has_text=re.compile(r"^Steam$", re.I),
        )
    )
    unlinked_visible = visible_count(
        page.locator(
            '[data-testid^="connected-accounts-unlinked-section-title-"]',
            has_text=re.compile(r"^Steam$", re.I),
        )
    )
    # The current EA DOM gives the linked provider label a ``pre-title``
    # testid and the button a provider-specific testid.  The older selector
    # looked for ``linked-section-title-Steam``, which is actually the account
    # name field and caused a false negative even while the Steam card and its
    # Unlink button were visible.
    unlink_buttons = page.locator('[data-testid="unlink-external-button-Steam"]')
    has_visible_unlink = False
    try:
        has_visible_unlink = any(
            unlink_buttons.nth(i).is_visible(timeout=0)
            for i in range(unlink_buttons.count())
        )
    except Exception:
        has_visible_unlink = False
    # Roxy can leave the document shell half loaded. Never spend Playwright's
    # default 30s timeout inside the 45s reconciliation budget.
    body_text = page.locator("body").inner_text(timeout=1500)
    return {
        "status": classify_steam_link_state(
            linked_visible=linked_visible,
            unlinked_visible=unlinked_visible,
            has_visible_unlink=has_visible_unlink,
            body_text=body_text,
        ),
        "linked_visible": linked_visible,
        "unlinked_visible": unlinked_visible,
        "has_visible_unlink": has_visible_unlink,
    }


def wait_for_steam_linked(page, timeout_ms: int = 40000) -> dict:
    """Wait in the OAuth browser session for Linked + Steam + visible Unlink.

    A direct SPA navigation can occasionally render only the account shell when
    proxied, so split the budget across the first load and one bounded reload.
    """
    last_state = {
        "status": "unknown",
        "linked_visible": 0,
        "unlinked_visible": 0,
        "has_visible_unlink": False,
    }
    deadline = time.monotonic() + max(1000, timeout_ms) / 1000
    for attempt in range(2):
        while time.monotonic() < deadline:
            try:
                rpc_state = connected_accounts_rpc(page)
            except Exception:
                rpc_state = {"status": "unknown"}
            if rpc_state.get("status") == "linked":
                return rpc_state
            try:
                last_state = inspect_steam_link_state(page)
            except Exception as exc:
                last_state = {
                    **last_state,
                    "status": "unknown",
                    "error": type(exc).__name__,
                }
            if last_state["status"] in {"linked", "logged_out"}:
                return last_state
            page.wait_for_timeout(min(400, max(100, int((deadline - time.monotonic()) * 1000))))
        if attempt == 0:
            log("steam linked state not ready; reloading connected accounts once")
            remaining = max(1000, int((deadline - time.monotonic()) * 1000))
            try:
                page.reload(wait_until="domcontentloaded", timeout=remaining)
            except Exception:
                pass
            dismiss_cookies(page)
    return last_state


def flow_succeeded(
    result: dict, *, skip_steam: bool = False, skip_2fa: bool = False
) -> bool:
    return bool(
        result.get("login")
        and (skip_2fa or result.get("totp"))
        and (skip_steam or result.get("steam_link"))
    )


def js_click_visible(page, script: str) -> str:
    """Run JS that returns a string status; must click a visible (bbox>0) element."""
    try:
        return str(page.evaluate(script) or "null")
    except Exception as e:
        return f"err:{e}"


def click_steam_tile(page) -> bool:
    status = js_click_visible(
        page,
        """() => {
          const titles = [...document.querySelectorAll(
            '[data-testid="connected-accounts-unlinked-section-title-Steam"]'
          )];
          for (const el of titles) {
            const btn = el.closest('button') || el;
            const r = btn.getBoundingClientRect();
            if (r.width > 2 && r.height > 2) {
              btn.scrollIntoView({block:'center'});
              btn.click();
              return 'testid-bbox';
            }
          }
          // any button whose visible text is exactly Steam
          for (const btn of document.querySelectorAll('button,[role=button]')) {
            const t = (btn.innerText||'').trim();
            if (t === 'Steam' || /^Steam\\s*$/.test(t)) {
              const r = btn.getBoundingClientRect();
              if (r.width > 2 && r.height > 2) {
                btn.scrollIntoView({block:'center'});
                btn.click();
                return 'text-steam';
              }
            }
          }
          // force first testid ancestor button even if bbox 0
          if (titles[0]) {
            const btn = titles[0].closest('button') || titles[0];
            btn.click();
            return 'force-first';
          }
          return 'miss';
        }""",
    )
    log(f"steam tile js={status}")
    return status not in ("miss", "null") and not status.startswith("err:")


def click_modal_continue(page, *, testid_only: bool = False) -> bool:
    testid = page.locator(
        '[data-testid="connected-accounts-modal-continue-button"]:visible'
    ).first
    try:
        if testid.count() and testid.is_visible() and testid.is_enabled():
            # On Linux Roxy, native click can stall in its element stability
            # check before sending any click, despite this modal being ready.
            # Submit this visible modal control once through the DOM. Mixing a
            # timed-out native click with a second DOM click can clear Steam's
            # provider and replace the popup with externalParty=.
            testid.evaluate("el => el.click()", timeout=3000)
            log("modal continue DOM click=testid")
            return True
    except PWTimeout:
        # Reconcile a possibly delivered DOM click instead of submitting twice.
        log("modal Continue timed out; reconciling the existing popup")
        return True
    except Exception:
        pass
    if testid_only:
        log("modal continue js=skipped-generic")
        return False
    status = js_click_visible(
        page,
        """() => {
          const roots = [
            ...document.querySelectorAll('[role=dialog], dialog, [aria-modal=true]'),
            document.body,
          ];
          const testid = document.querySelector(
            '[data-testid="connected-accounts-modal-continue-button"]'
          );
          if (testid) {
            const r = testid.getBoundingClientRect();
            if (r.width > 2 && r.height > 2) {
              testid.scrollIntoView({block:'center'});
              testid.click();
              return 'testid';
            }
          }
          for (const root of roots) {
            if (!root) continue;
            const buttons = [...root.querySelectorAll('button,[role=button]')];
            for (const b of buttons) {
              const t = (b.innerText||'').replace(/\\s+/g,' ').trim();
              if (/^(Continue|Lanjutkan|Continuar|Continuer|Fortfahren|次へ|계속|继续|繼續|続行|連携する|リンクする)(?:\\s|$)/i.test(t)
                  && !/Cancel|取消|Batal|Batal/i.test(t)) {
                const r = b.getBoundingClientRect();
                if (r.width > 2 && r.height > 2) {
                  b.scrollIntoView({block:'center'});
                  b.click();
                  return 'continue:' + t.slice(0,40);
                }
              }
            }
          }
          return 'miss';
        }""",
    )
    log(f"modal continue js={status}")
    return status not in ("miss", "null") and not status.startswith("err:")


BAD_SECRETS = {
    "ACCOUNTSSECURITY",
    "SECURITYPRIVACY",
    "CONNECTEDACCOUNTS",
    "TWOFACTORAUTHENTI",
    "AUTHENTICATION",
    "MANAGELINKEDACCOU",
}


def extract_totp_secret(html: str, page_text: str) -> str | None:
    for pat in (
        r"secret=([A-Z2-7]{16,})",
        r"otpauth://totp/[^\s\"']+secret=([A-Z2-7]{16,})",
        r"data-secret=[\"']([A-Z2-7]{16,})[\"']",
    ):
        m = re.search(pat, html, re.I)
        if m:
            cand = m.group(1).upper()
            if cand not in BAD_SECRETS and len(cand) >= 16:
                return cand
    # spaced groups like ABCD EFGH IJKL MNOP
    for m in re.finditer(r"\b([A-Z2-7]{4}(?:[\s\-]+[A-Z2-7]{4}){3,15})\b", page_text, re.I):
        cand = re.sub(r"[\s\-]+", "", m.group(1)).upper()
        if 16 <= len(cand) <= 64 and cand not in BAD_SECRETS:
            return cand
    for m in re.finditer(r"\b([A-Z2-7]{16,64})\b", page_text, re.I):
        cand = m.group(1).upper()
        if cand in BAD_SECRETS or cand.startswith("HTTP"):
            continue
        # An all-letter token is a display name or heading. Real keys from the
        # QR step include a digit 2-7; those still fall through to get-verify-qrcode.
        if re.fullmatch(r"[A-Z]+", cand):
            continue
        return cand
    return None


def load_ea_account() -> dict:
    path = Path(os.environ.get("EA_ACCOUNT_FILE") or EA_ACCOUNT_FILE)
    account = json.loads(path.read_text())
    if not account.get("ok_guess"):
        raise RuntimeError(f"EA registration result is not successful: {path}")
    if not all(account.get(key) for key in ("email", "password", "originId")):
        raise RuntimeError(f"EA registration result lacks credentials: {path}")
    return account


def load_latest_steam_account() -> dict:
    path = Path(os.environ.get("STEAM_ACCOUNT_FILE") or STEAM_ACCOUNT_FILE)
    lines = [line for line in path.read_text().splitlines() if line.strip()]
    for line in reversed(lines):
        fields = line.split("----", 4)
        if len(fields) >= 4 and fields[3].strip().lower() == "true":
            username, password, email = (field.strip() for field in fields[:3])
            if username and password and email:
                return {"username": username, "password": password, "email": email}
    raise RuntimeError(f"no successful Steam account found: {path}")



def _seed_existing_ea_into_env() -> None:
    os.environ["SKIP_EA_REGISTER"] = "1"
    if os.environ.get("EA_EMAIL", "").strip() and os.environ.get("EA_PASSWORD", "").strip():
        return
    path = Path(os.environ.get("EA_ACCOUNT_FILE") or EA_ACCOUNT_FILE)
    if not path.is_file():
        return
    try:
        account = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(account, dict):
        return
    if account.get("email"):
        os.environ.setdefault("EA_EMAIL", str(account["email"]))
    if account.get("password"):
        os.environ.setdefault("EA_PASSWORD", str(account["password"]))
    origin = account.get("originId") or account.get("ea_id")
    if origin:
        os.environ.setdefault("EA_ORIGIN_ID", str(origin))


def main() -> int:
    """Standalone Steam link / 2FA uses the same fingerprint pipeline as full setup."""
    _seed_existing_ea_into_env()
    from . import ea_pw_full_setup as full_setup
    return full_setup.main()


if __name__ == "__main__":
    raise SystemExit(main())

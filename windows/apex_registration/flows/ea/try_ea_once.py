#!/usr/bin/env python3
"""One-shot EA (juno) register trial.

Driven by the backend through env vars:
  - EA_EMAIL / ACCOUNT_FLOW_EMAIL_POOL: the Graph Outlook mailbox to use
  - EA_PROXY: DataImpulse Hong Kong egress
  - ACCOUNT_FLOW_OUTPUT_DIR: where the result json is written
"""

from __future__ import annotations

import calendar
import json
import os
import random
import re
import string
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

from apex_registration.utils.browser_profile import (  # noqa: E402
    ea_register_url,
    new_chrome_session,
    pick_register_country,
    rand_gamer_id,
    register_locale,
)

ROOT = Path(__file__).resolve().parent
# Optional proxy fallback, only read when EA_PROXY is unset. The backend always
# injects EA_PROXY, so deployments never need this file.
PROXY_CFG = Path(
    os.environ.get("ACCOUNT_FLOW_PROXY_CONFIG", "").strip()
    or ROOT / "proxy-config.json"
)
ARTIFACT_ROOT = Path(os.environ.get("ACCOUNT_FLOW_OUTPUT_DIR") or ROOT)
EMAIL_JSON = ROOT / "email.json"
EMAIL_POOL = Path(os.environ.get("ACCOUNT_FLOW_EMAIL_POOL") or "/tmp/emails.json")
OUT_OK = ARTIFACT_ROOT / "accounts_succ.txt"
OUT_FAIL = ARTIFACT_ROOT / "protocol_register_failures.jsonl"
OUT_LOG = ARTIFACT_ROOT / "last_run.log"
OUT_RESULT = ARTIFACT_ROOT / "protocol_register_result.json"
LOGIN_TARGET = "https://myaccount.ea.com/am/ui/security-privacy"
ADULT_AGE_MIN = 21
ADULT_AGE_MAX = 40


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
    with OUT_LOG.open("a") as f:
        f.write(line + "\n")


def write_failure_result(
    email: str, reason: str, response=None, **details
) -> None:
    payload = {
        "email": email,
        "password": None,
        "originId": None,
        "final_url": getattr(response, "url", None),
        "final_step": cur_step(getattr(response, "text", "")) if response else None,
        "ok_guess": False,
        "account_login_verified": False,
        "account_profile_completed": False,
        "failure_reason": reason,
    }
    payload.update(details)
    with OUT_FAIL.open("a") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
    OUT_FAIL.chmod(0o600)
    OUT_RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    OUT_RESULT.chmod(0o600)


def fatal_failure_reason(exc: Exception) -> str:
    if isinstance(exc, requests.exceptions.ProxyError):
        return "proxy_error"
    if isinstance(exc, requests.exceptions.Timeout):
        return "network_timeout"
    if isinstance(exc, requests.exceptions.ConnectionError):
        return "network_error"
    if isinstance(exc, requests.exceptions.HTTPError):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        return f"http_error_{status}" if status else "http_error"
    return f"fatal_{type(exc).__name__}"


def direct_mode() -> bool:
    return os.environ.get("ACCOUNT_FLOW_DIRECT", "").strip().lower() in {
        "1", "true", "yes"
    }


def random_adult_dob() -> tuple[str, str, str]:
    """Return unpadded (month, day, year) for an adult aged 21–40."""
    now = time.localtime()
    age = random.randint(ADULT_AGE_MIN, ADULT_AGE_MAX)
    year = int(now.tm_year) - age
    month = random.randint(1, 12)
    day = random.randint(1, calendar.monthrange(year, month)[1])
    return str(month), str(day), str(year)


def mail_wait_seconds() -> int:
    """How long to keep polling Graph for an EA code.

    The configured wait is often 180s. A 120s clamp cut off the extra mailbox
    refreshes that still deliver a code after one resend.
    """
    try:
        return max(
            30,
            min(180, int(os.environ.get("GAME_ACCOUNTS_EMAIL_WAIT_SECONDS", "90"))),
        )
    except ValueError:
        return 90


def direct_http_session() -> requests.Session:
    session = requests.Session()
    if direct_mode():
        session.trust_env = False
    return session


def load_proxy() -> str | None:
    if direct_mode():
        return None
    # Prefer explicit env (e.g. resin tunnel): EA_PROXY=http://user:pass@127.0.0.1:12260
    env = str(os.environ.get("EA_PROXY") or "").strip()
    if env:
        return env
    if not PROXY_CFG.exists():
        raise RuntimeError(
            "missing proxy: set EA_PROXY (or point ACCOUNT_FLOW_PROXY_CONFIG "
            "at a json file with a 'proxy' key)"
        )
    cfg = json.loads(PROXY_CFG.read_text())
    proxy = str(cfg.get("proxy") or "").strip()
    if not proxy:
        raise RuntimeError("missing proxy: set EA_PROXY or a 'proxy' key in the config")
    return proxy


def load_email() -> dict:
    requested = str(os.environ.get("EA_EMAIL") or "").strip()
    if not requested:
        return json.loads(EMAIL_JSON.read_text())

    if not EMAIL_POOL.exists():
        raise RuntimeError(f"email pool not found: {EMAIL_POOL}")
    pool = json.loads(EMAIL_POOL.read_text())
    if not isinstance(pool, list):
        raise RuntimeError(f"email pool must be a JSON list: {EMAIL_POOL}")

    used: set[str] = set()
    for path in [EMAIL_JSON, OUT_OK, OUT_FAIL, OUT_RESULT, ROOT / "ea_taken.txt"]:
        if not path.exists():
            continue
        used.update(
            value.lower()
            for value in re.findall(
                r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
                path.read_text(errors="ignore"),
                re.I,
            )
        )

    wanted = requested.lower()
    candidates = (
        sorted(
            pool,
            key=lambda item: str(item.get("account_status") or "") == "available"
            if isinstance(item, dict)
            else True,
        )
        if wanted == "auto"
        else pool
    )
    for item in candidates:
        if not isinstance(item, dict):
            continue
        email = str(item.get("email") or "").strip().lower()
        if not email or not item.get("client_id") or not item.get("refresh_token"):
            continue
        if wanted == "auto":
            # account_status belongs to the GPT account inventory, not the
            # Outlook mailbox and not EA registration state.
            if email in used:
                continue
        elif email != wanted:
            continue
        selected = dict(item)
        selected["_pool_selected"] = True
        return selected

    raise RuntimeError(f"no usable mailbox found for EA_EMAIL={requested!r}")


def graph_access_token(email_data: dict) -> str:
    r = direct_http_session().post(
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        data={
            "client_id": email_data["client_id"],
            "grant_type": "refresh_token",
            "refresh_token": email_data["refresh_token"],
            "scope": "https://graph.microsoft.com/.default",
        },
        timeout=40,
    )
    j = r.json()
    if not j.get("access_token"):
        raise RuntimeError(f"graph token failed: {j}")
    if j.get("refresh_token"):
        email_data["refresh_token"] = j["refresh_token"]
        if not email_data.get("_pool_selected"):
            EMAIL_JSON.write_text(json.dumps(email_data, indent=2))
    return j["access_token"]


def graph_find_ea_code(access_token: str, *, after_ts: float) -> str | None:
    """Find 6-digit EA verification code from recent inbox mails."""
    headers = {"Authorization": f"Bearer {access_token}"}
    # order by receivedDateTime desc
    url = (
        "https://graph.microsoft.com/v1.0/me/messages"
        "?$top=20&$orderby=receivedDateTime%20desc"
        "&$select=subject,body,receivedDateTime,from"
    )
    r = direct_http_session().get(url, headers=headers, timeout=40)
    if r.status_code != 200:
        log(f"graph messages http {r.status_code}: {r.text[:200]}")
        return None

    code_re = re.compile(r"\b(\d{6})\b")
    for msg in r.json().get("value") or []:
        received = msg.get("receivedDateTime") or ""
        # rough filter: only after we started (UTC ISO)
        try:
            # 2026-07-27T06:00:00Z
            t = calendar.timegm(time.strptime(received[:19], "%Y-%m-%dT%H:%M:%S"))
            if t + 30 < after_ts:  # allow small skew
                continue
        except Exception:
            pass

        subject = str(msg.get("subject") or "")
        body = str((msg.get("body") or {}).get("content") or "")
        blob = subject + "\n" + body
        low = blob.lower()
        if not any(x in low for x in ["ea", "electronic arts", "origin", "security code", "verification", "verify"]):
            # still try if looks like code-only mail
            pass
        # Prefer EA-ish subjects
        if any(x in low for x in ["ea", "origin", "electronic", "verify", "security code", "your code"]):
            codes = code_re.findall(re.sub(r"<[^>]+>", " ", blob))
            # filter common non-codes years etc - 6 digit only, not starting with 19/20 years alone
            for c in codes:
                if c.startswith(("19", "20")) and int(c[:4]) > 1990:
                    continue
                log("candidate EA verification code received")
                return c
        # fallback: any 6 digit in EA-related body
        if "ea" in low or "origin" in low:
            codes = code_re.findall(re.sub(r"<[^>]+>", " ", blob))
            if codes:
                log("fallback EA verification code received")
                return codes[0]
    return None


def cur_step(html: str) -> str | None:
    m = re.search(r'curStep\s*=\s*"([^"]+)"', html)
    return m.group(1) if m else None


def classify_create_response(
    step: str | None, url: str, visible_text: str = ""
) -> str:
    """Classify the EA ID/password submit so error pages are not treated as success."""
    step_l = (step or "").lower()
    url_l = (url or "").lower()
    blob = f"{step_l}\n{visible_text}"
    if ("technicalissue" in step_l or "无法完成您的请求" in blob
            or "無法完成您的請求" in blob or "目前發生技術問題" in blob
            or "目前发生技术问题" in blob):
        return "technical_issue"
    left_create = "juno/create" not in url_l and "signin.ea.com/p/juno" not in url_l
    if left_create or (step_l and "basicinfo" not in step_l):
        return "left_create"
    return "basic_info"


def registration_error_reason(html: str) -> str | None:
    """Read the server-injected error, not the form's hidden validation labels."""
    error = BeautifulSoup(html, "html.parser").select_one(".int-registration-error")
    text = " ".join(error.get_text(" ", strip=True).split()) if error else ""
    if not text:
        return None
    if re.search(r"already\s+registered|已註冊|已注册", text, re.I):
        return "email_already_registered"
    return "server_registration_error"


def unresolved_failure_reason(final_step: str | None) -> str:
    step_l = (final_step or "").lower()
    if "technicalissue" in step_l:
        return "technical_issue"
    return "create_unconfirmed"


def page_text(html: str) -> str:
    return re.sub(r"\s+", " ", BeautifulSoup(html, "html.parser").get_text(" ", strip=True))[:400]


def hidden_fields(soup: BeautifulSoup, form: object | None = None) -> dict[str, str]:
    data: dict[str, str] = {}
    root = form or soup
    for inp in root.find_all("input"):
        if inp.get("type") == "hidden" and inp.get("name"):
            data[inp["name"]] = inp.get("value") or ""
    return data


def find_form_with(soup: BeautifulSoup, *names: str):
    for form in soup.find_all("form"):
        present = {inp.get("name") for inp in form.find_all(["input", "select"])}
        if all(n in present for n in names):
            return form
    return None


def submit_flow_form(
    session, response, form, updates: dict[str, str]
):
    data = hidden_fields(BeautifulSoup(response.text, "html.parser"), form)
    data.update(updates)
    action = urljoin(response.url, form.get("action") or response.url)
    parsed = urlparse(action)
    return session.post(
        action,
        data=data,
        headers={
            "Referer": response.url,
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=45,
        allow_redirects=True,
    )


def persist_connect_cookies(session: requests.Session) -> None:
    """Keep remid/sid from a completed login for the parent process to encrypt."""
    sid = remid = None
    remid_expires_at = None
    for cookie in session.cookies:
        if cookie.name == "sid":
            sid = cookie.value
        elif cookie.name == "remid":
            remid = cookie.value
            if cookie.expires:
                remid_expires_at = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(cookie.expires))
                )
    if not sid and not remid:
        return
    payload = {
        "sid": sid,
        "remid": remid,
        "remid_expires_at": remid_expires_at,
    }
    path = ARTIFACT_ROOT / "ea_connect_cookies.json"
    path.write_text(json.dumps(payload, ensure_ascii=False))
    path.chmod(0o600)


def protocol_finish_registration(
    email: str, password: str, origin_id: str, proxy: str | None
) -> str:
    """Verify login and finish a pending EA ID/privacy upgrade if required."""
    session = new_chrome_session(proxy)
    response = session.get(LOGIN_TARGET, timeout=45, allow_redirects=True)
    password_submitted = False
    upgrade_submitted = False

    for _ in range(12):
        if "myaccount.ea.com" in response.url and "signin.ea.com" not in response.url:
            persist_connect_cookies(session)
            return "complete"
        if "signin.ea.com" not in response.url:
            persist_connect_cookies(session)
            return "complete"

        step = cur_step(response.text) or ""
        soup = BeautifulSoup(response.text, "html.parser")
        if "AccountIdentifier" in step:
            form = soup.find("form", id="login-form") or find_form_with(soup, "email")
            if not form:
                return "unsupported"
            response = submit_flow_form(
                session,
                response,
                form,
                {
                    "email": email,
                    "loginMethod": "emailPassword",
                    "passkeySupportStatus": "not_supported",
                    "_eventId": "submit",
                },
            )
        elif "Password" in step:
            if password_submitted:
                return "password_rejected"
            form = soup.find("form", id="login-form") or find_form_with(soup, "password")
            if not form:
                return "unsupported"
            response = submit_flow_form(
                session,
                response,
                form,
                {
                    "password": password,
                    "loginMethod": "emailPassword",
                    "_eventId": "submit",
                },
            )
            password_submitted = True
        elif step == "juno/upgrade":
            if upgrade_submitted:
                return "id_rejected"
            form = soup.find("form", id="upgradeForm") or find_form_with(soup, "originId")
            if not form:
                return "unsupported"
            response = submit_flow_form(
                session,
                response,
                form,
                {"originId": origin_id, "_eventId": "submit"},
            )
            upgrade_submitted = True
        elif step == "juno/upgradePrivacySettings":
            form = soup.find("form", id="privacyForm") or soup.find("form")
            if not form:
                return "unsupported"
            response = submit_flow_form(
                session,
                response,
                form,
                {
                    "friendVisibility": "EVERYONE",
                    "_emailVisibility": "on",
                    "emailVisibility": "on",
                    "_contactMe": "on",
                    "_eventId": "submit",
                },
            )
        elif step == "juno/tosUpdate":
            form = soup.find("form", id="tosForm") or soup.find("form")
            if not form:
                return "unsupported"
            response = submit_flow_form(
                session,
                response,
                form,
                {
                    "_readAccept": "on",
                    "readAccept": "on",
                    "_eventId": "accept",
                },
            )
        elif "dynamicchallenge" in step.lower():
            # Password and any pending upgrade were accepted. Do not send a
            # login OTP merely to prove account creation.
            return "complete"
        elif not step:
            location = re.search(
                r"window\.location\s*=\s*['\"]([^'\"]+)['\"]", response.text
            )
            if location:
                response = session.get(
                    urljoin(response.url, location.group(1)),
                    timeout=45,
                    allow_redirects=True,
                )
                continue
            forms = soup.find_all("form")
            if len(forms) == 1:
                response = submit_flow_form(session, response, forms[0], {})
                continue
            return "unsupported"
        else:
            return "unsupported"
    return "unsupported"


def rand_password(n: int = 14) -> str:
    # EA: mix upper lower digit symbol
    chars = string.ascii_letters + string.digits + "!@#$%"
    while True:
        p = "".join(random.choice(chars) for _ in range(n))
        if (
            re.search(r"[A-Z]", p)
            and re.search(r"[a-z]", p)
            and re.search(r"\d", p)
            and re.search(r"[!@#$%]", p)
        ):
            return p


def rand_id() -> str:
    return rand_gamer_id()


def main() -> int:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT_LOG.write_text("")
    OUT_LOG.chmod(0o600)
    proxy = load_proxy()
    email_data = load_email()
    email = email_data["email"]
    log(f"mailbox selected domain={email.rsplit('@', 1)[-1]}")
    log(
        "network=direct"
        if proxy is None
        else f"proxy={proxy.split('@')[-1] if '@' in proxy else proxy}"
    )

    s = new_chrome_session(proxy)

    country = pick_register_country()
    dob_month, dob_day, dob_year = random_adult_dob()

    # 1) open register
    register_url = ea_register_url()
    language = register_locale().accept_language.split(",")[0]
    log(f"GET {register_url} country={country} dob={dob_year}-{dob_month}-{dob_day} lang={language}")
    r = s.get(register_url, timeout=45, allow_redirects=True)
    r.raise_for_status()
    log(f"landed {r.url} step={cur_step(r.text)} len={len(r.text)}")
    soup = BeautifulSoup(r.text, "html.parser")

    # 2) DOB + country
    form = find_form_with(soup, "country", "dobYear") or find_form_with(soup, "country")
    data = hidden_fields(soup, form)
    data.update(
        {
            "country": country,
            "dobMonth": dob_month,
            "dobDay": dob_day,
            "dobYear": dob_year,
            "_eventId": "submit",
            "underage": "false",
        }
    )
    log(f"POST dob/country country={country} dob={dob_year}-{dob_month}-{dob_day} fields={list(data.keys())}")
    r = s.post(
        r.url,
        data=data,
        headers={
            "Referer": r.url,
            "Origin": "https://signin.ea.com",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=45,
        allow_redirects=True,
    )
    log(f"after dob step={cur_step(r.text)} url={r.url} len={len(r.text)}")
    log(f"text={page_text(r.text)}")
    if "captcha" in r.text.lower() and re.search(r"6L[0-9A-Za-z_-]{20,}|data-sitekey", r.text):
        log("WARNING: captcha sitekey appeared at email step")
    # options captchaName
    m = re.search(r"'captchaName'\s*:\s*\"([^\"]+)\"", r.text) or re.search(
        r"'captchaName'\s*:\s*'([^']+)'", r.text
    )
    if m:
        log(f"captchaName={m.group(1)}")

    # 3) email collection
    soup = BeautifulSoup(r.text, "html.parser")
    form = find_form_with(soup, "email")
    data = hidden_fields(soup, form)
    data.update(
        {
            "email": email,
            "parentEmail": "",
            "_eventId": "submit",
            "readAccept": "true",
            "_readAccept": "on",
            "contactMe": "false",
            "_contactMe": "on",
        }
    )
    # underage checkbox if present
    if soup.find(attrs={"name": "readAcceptUnderage"}):
        data["readAcceptUnderage"] = "true"
        data["_readAcceptUnderage"] = "on"

    started = time.time()
    log("POST email")
    r = s.post(
        r.url,
        data=data,
        headers={
            "Referer": r.url,
            "Origin": "https://signin.ea.com",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=45,
        allow_redirects=True,
    )
    log(f"after email step={cur_step(r.text)} url={r.url} len={len(r.text)}")
    log(f"text={page_text(r.text)}")
    step = cur_step(r.text) or ""
    if "already registered" in r.text.lower() or "alreadyRegistered" in r.text:
        log("FAIL: email already registered with EA")
        write_failure_result(email, "email_already_registered", r)
        return 2
    if "mandatoryEmailVerification" not in step and "verify" not in step.lower():
        # maybe captcha challenge page
        if re.search(r"recaptcha|hcaptcha|6L[0-9A-Za-z_-]{20,}", r.text, re.I):
            log("FAIL: captcha required at this step (not implemented yet)")
            Path(ARTIFACT_ROOT / "captcha_page.html").write_text(r.text)
            write_failure_result(email, "captcha_required", r)
            return 3
        log(f"WARN unexpected step after email: {step}")

    # 4) poll mailbox for 6-digit code
    log("wait EA verification email via Graph")
    code = None
    wait_seconds = mail_wait_seconds()
    wait_started = time.monotonic()
    attempts = 0
    while time.monotonic() - wait_started < wait_seconds:
        attempts += 1
        try:
            at = graph_access_token(email_data)
            code = graph_find_ea_code(at, after_ts=started - 10)
        except Exception as e:
            log(f"graph err: {e}")
        if code:
            break
        elapsed = int(time.monotonic() - wait_started)
        log(f"no code yet attempt={attempts} elapsed={elapsed}s")
        remaining = wait_seconds - (time.monotonic() - wait_started)
        if remaining > 0:
            time.sleep(min(5, remaining))
    if not code:
        log(f"FAIL: no verification code received within {wait_seconds}s")
        Path(ARTIFACT_ROOT / "verify_page.html").write_text(r.text)
        write_failure_result(
            email,
            "email_verification_timeout",
            r,
            mail_wait_seconds=wait_seconds,
        )
        return 4
    log("got EA verification code")

    # 5) submit verification code
    soup = BeautifulSoup(r.text, "html.parser")
    form = find_form_with(soup, "emailVerifyCode") or soup.find("form")
    data = hidden_fields(soup, form)
    data.update(
        {
            "emailVerifyCode": code,
            "_eventId": "submit",
        }
    )
    # some pages use split boxes; hidden emailVerifyCode is enough if present
    for idx, ch in enumerate(code, 1):
        # not always named, but harmless if ignored
        data[f"emailVerifyCode-{idx}"] = ch

    log("POST email verify code")
    r = s.post(
        r.url,
        data=data,
        headers={
            "Referer": r.url,
            "Origin": "https://signin.ea.com",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        timeout=45,
        allow_redirects=True,
    )
    log(f"after verify step={cur_step(r.text)} url={r.url} len={len(r.text)}")
    log(f"text={page_text(r.text)}")
    Path(ARTIFACT_ROOT / "after_verify.html").write_text(r.text)

    if "invalid" in page_text(r.text).lower() and "code" in page_text(r.text).lower():
        if cur_step(r.text) and "verify" in (cur_step(r.text) or ""):
            log("FAIL: code rejected")
            write_failure_result(email, "email_verification_rejected", r)
            return 5

    # 6) try password / basic info if present
    soup = BeautifulSoup(r.text, "html.parser")
    password = rand_password()
    ea_id = rand_id()
    failure_reason = None
    account_login_verified = False
    account_profile_completed = False
    form = None
    for cand in soup.find_all("form"):
        names = {inp.get("name") for inp in cand.find_all(["input", "select"])}
        if "password" in names or "originId" in names or "email" in names and "password" in names:
            form = cand
            break
        if any(n and ("password" in n.lower() or "origin" in n.lower()) for n in names if n):
            form = cand
            break

    input_names = sorted(
        {
            inp.get("name")
            for f in soup.find_all("form")
            for inp in f.find_all(["input", "select"])
            if inp.get("name")
        }
    )
    log(f"form input names={input_names}")

    if form or any(n and "password" in n for n in input_names if n):
        # Working browser-like payload (adult, nocaptcha path).
        # Retry the same proposed identity if confirmation is delayed.
        last_r = r
        for attempt in range(1, 6):
            # EA may have committed the preceding POST while still displaying
            # basicInfo. Retain both its password and ID across retries so a
            # later login check cannot publish a different, unclaimed ID.
            data = {
                "cid": "",
                "_eventId": "submit",
                "originId": ea_id,
                "password": password,
                "_readAccept": "on",
                "readAccept": "true",
                "_readAcceptUnderage": "on",
                "thirdPartyCaptchaResponse": "",
            }
            log(f"POST create/password attempt={attempt}")
            last_r = s.post(
                r.url,
                data=data,
                headers={
                    "Referer": r.url,
                    "Origin": "https://signin.ea.com",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=45,
                allow_redirects=True,
            )
            log(
                f"after password step={cur_step(last_r.text)} "
                f"url={last_r.url} len={len(last_r.text)}"
            )
            log(f"text={page_text(last_r.text)}")
            Path(ARTIFACT_ROOT / "after_password.html").write_text(last_r.text)
            registration_error = registration_error_reason(last_r.text)
            if registration_error == "email_already_registered":
                # A prior create may have succeeded without its login check
                # succeeding yet. Verify its unchanged credentials first.
                if attempt > 1:
                    finish_status = protocol_finish_registration(email, password, ea_id, proxy)
                    if finish_status == "complete":
                        account_login_verified = True
                        account_profile_completed = True
                        log("preceding registration credentials and EA profile verified")
                        r = last_r
                        break
                failure_reason = "email_already_registered"
                log("FAIL: EA reports that this mailbox is already registered")
                r = last_r
                break
            if registration_error:
                failure_reason = "server_registration_error"
                log("FAIL: EA returned a registration error")
                r = last_r
                break
            step_now = cur_step(last_r.text) or ""
            outcome = classify_create_response(
                step_now, last_r.url, page_text(last_r.text)
            )
            if outcome == "technical_issue":
                log("EA returned technicalIssue after create/password")
                finish_status = protocol_finish_registration(
                    email, password, ea_id, proxy
                )
                log(f"technicalIssue login check={finish_status}")
                if finish_status == "complete":
                    account_login_verified = True
                    account_profile_completed = True
                    log("registration credentials and EA profile verified")
                    r = last_r
                    break
                if attempt < 5:
                    time.sleep(1)
                    continue
                failure_reason = "technical_issue"
                r = last_r
                break
            if outcome == "left_create":
                r = last_r
                break
            # EA can commit the new account while leaving this Web Flow on
            # basicInfo. Verify the exact credentials before retrying creation.
            finish_status = protocol_finish_registration(email, password, ea_id, proxy)
            if finish_status == "password_rejected":
                time.sleep(2)
                finish_status = protocol_finish_registration(email, password, ea_id, proxy)
            if finish_status == "id_rejected":
                # The email/password account already exists. Keep its password
                # and only claim another ID through the upgrade flow.
                for completion_attempt in range(1, 6):
                    ea_id = rand_id()
                    log(f"retry EA ID through upgrade attempt={completion_attempt}")
                    finish_status = protocol_finish_registration(
                        email, password, ea_id, proxy
                    )
                    if finish_status != "id_rejected":
                        break
            if finish_status == "complete":
                account_login_verified = True
                account_profile_completed = True
                log("registration credentials and EA profile verified")
                r = last_r
                break
            # Still on basicInfo: retry without replacing the proposed identity.
            r = last_r
            time.sleep(0.5)
        else:
            r = last_r

    # A redirect away from the create form is not sufficient evidence. Verify
    # the exact generated credentials before publishing a successful result.
    if (
        not account_login_verified
        and "password" in locals()
        and "ea_id" in locals()
        and not failure_reason
    ):
        finish_status = protocol_finish_registration(email, password, ea_id, proxy)
        log(f"post-create login check={finish_status}")
        if finish_status == "complete":
            account_login_verified = True
            account_profile_completed = True
            log("registration credentials and EA profile verified after redirect")

    # 7) accept only an account whose generated credentials were verified
    final_step = cur_step(r.text)
    final_url = r.url
    ok = bool(account_login_verified and account_profile_completed)
    if not ok and not failure_reason:
        failure_reason = unresolved_failure_reason(final_step)

    connect_cookies = None
    cookie_path = ARTIFACT_ROOT / "ea_connect_cookies.json"
    if cookie_path.is_file():
        try:
            connect_cookies = json.loads(cookie_path.read_text())
        except (OSError, json.JSONDecodeError):
            connect_cookies = None
    line = {
        "email": email,
        "password": password if "password" in locals() else None,
        "originId": ea_id if "ea_id" in locals() else None,
        "final_url": final_url,
        "final_step": final_step,
        "ok_guess": ok,
        "account_login_verified": (
            account_login_verified if "account_login_verified" in locals() else False
        ),
        "account_profile_completed": (
            account_profile_completed
            if "account_profile_completed" in locals()
            else False
        ),
        "failure_reason": failure_reason if not ok and "failure_reason" in locals() else None,
        "connect_cookies": connect_cookies if ok else None,
    }
    history_path = OUT_OK if ok else OUT_FAIL
    with history_path.open("a") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    history_path.chmod(0o600)
    OUT_RESULT.write_text(json.dumps(line, ensure_ascii=False, indent=2) + "\n")
    OUT_RESULT.chmod(0o600)
    log(f"DONE ok_guess={ok} saved={history_path}")
    log(
        "result saved="
        f"{OUT_RESULT} final_step={final_step!r} "
        f"connect_cookies={bool(connect_cookies and connect_cookies.get('remid'))}"
    )
    return 0 if ok else 6


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        reason = fatal_failure_reason(e)
        log(f"FATAL: {reason}")
        try:
            write_failure_result(
                str(os.environ.get("EA_EMAIL") or "").strip(), reason
            )
        except Exception as result_error:
            log(f"FATAL: could not persist failure result ({type(result_error).__name__})")
        raise

#!/usr/bin/env python3
"""One-shot Steam register trial.

Driven by the backend through env vars:
  - STEAM_EMAIL / ACCOUNT_FLOW_EMAIL_POOL: the Graph Outlook mailbox to use
  - STEAM_PROXY: sticky residential egress (1024proxy when configured)
  - CAPTCHALY_KEY: hCaptcha Enterprise solver
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
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

# Sibling imports (captcha_run_client) work when this file is launched as a
# script. Loading it with importlib does not put this directory on sys.path.
from apex_registration.utils.browser_profile import (  # noqa: E402
    UA,
    chrome_headers,
    new_chrome_session,
    register_locale,
    steam_timezone_cookie,
)
from apex_registration.utils.proxy import as_http_forward_proxy, with_sticky_session  # noqa: E402

from .captcha_run_client import (
    CAPTCHA_RUN_STEAM_SITE_KEY,
    CaptchaRunError,
    solve_hcaptcha_steam,
)
from .captchaly_client import CaptchalyError, solve_hcaptcha_enterprise

ROOT = Path(__file__).resolve().parent
# Optional proxy fallback, only read when STEAM_PROXY is unset. The backend
# always injects STEAM_PROXY, so deployments never need this file.
PROXY_CFG = Path(
    os.environ.get("ACCOUNT_FLOW_PROXY_CONFIG", "").strip()
    or ROOT / "proxy-config.json"
)
ARTIFACT_ROOT = Path(os.environ.get("ACCOUNT_FLOW_OUTPUT_DIR") or ROOT)
EMAIL_LINE_FILE = ROOT / "email_password.txt"
EMAIL_POOL = Path(os.environ.get("ACCOUNT_FLOW_EMAIL_POOL") or "/tmp/emails.json")
# Only used by the ``STEAM_EMAIL=ea-latest`` CLI shortcut; the backend always
# passes a concrete address.
EA_RESULT = Path(
    os.environ.get("EA_RESULT_FILE", "").strip()
    or ROOT.parent / "ea" / "protocol_register_result.json"
)
PROXY_FILE = ARTIFACT_ROOT / "proxy_ips.txt"
OUT_OK = ARTIFACT_ROOT / "accounts_succ.txt"
OUT_ERR = ARTIFACT_ROOT / "rgerror.txt"
OUT_RESULT = ARTIFACT_ROOT / "steam_register_result.json"
OUT_LOG = ARTIFACT_ROOT / "last_run.log"

CAPTCHA_RUN_KEY = ""

JOIN_URL = "https://store.steampowered.com/join/"


def steam_join_headers() -> dict[str, str]:
    """Headers for this exit. Built per request so the country is already chosen."""
    headers = chrome_headers(navigate=False)
    headers.update(
        {
            "Accept": "*/*",
            "Accept-Language": register_locale().accept_language,
            "User-Agent": UA,
            "Referer": JOIN_URL,
            "Origin": "https://store.steampowered.com",
            "Connection": "keep-alive",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
    )
    return headers


def apply_steam_locale(session) -> None:
    locale = register_locale()
    session.cookies.set("Steam_Language", locale.steam_language, domain="steampowered.com")
    offset = steam_timezone_cookie()
    if offset is not None:
        session.cookies.set("timezoneOffset", offset, domain="steampowered.com")


def log(msg: str) -> None:
    safe = re.sub(
        r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}",
        "[EMAIL]",
        str(msg),
        flags=re.I,
    )
    safe = re.sub(r"(https?://)[^/@\s]+@", r"\1[AUTH]@", safe, flags=re.I)
    safe = re.sub(
        r"([?&](?:code|token|stoken|state|sid|sessionid|creationid)=)[^&\s]+",
        r"\1[REDACTED]",
        safe,
        flags=re.I,
    )
    safe = re.sub(r"(sessionid=)[^\s]+", r"\1[REDACTED]", safe, flags=re.I)
    line = f"[{time.strftime('%H:%M:%S')}] {safe}"
    print(line, flush=True)
    try:
        with OUT_LOG.open("a") as handle:
            handle.write(line + "\n")
    except OSError:
        pass


def direct_mode() -> bool:
    return os.environ.get("ACCOUNT_FLOW_DIRECT", "").strip().lower() in {
        "1", "true", "yes"
    }


def mail_wait_seconds() -> int:
    try:
        return max(
            30,
            min(180, int(os.environ.get("GAME_ACCOUNTS_EMAIL_WAIT_SECONDS", "90"))),
        )
    except ValueError:
        return 90


def load_proxy() -> str | None:
    if direct_mode():
        return None
    configured = os.environ.get("STEAM_PROXY", "").strip()
    if not configured:
        if not PROXY_CFG.exists():
            raise RuntimeError(
                "missing proxy: set STEAM_PROXY (or point ACCOUNT_FLOW_PROXY_CONFIG "
                "at a json file with a 'proxy' key)"
            )
        configured = str(json.loads(PROXY_CFG.read_text()).get("proxy") or "").strip()
    if not configured:
        raise RuntimeError("missing proxy: set STEAM_PROXY or a 'proxy' key in the config")
    return as_http_forward_proxy(configured)


def load_captcha_run_key() -> str:
    """Prefer an environment/config secret while retaining the existing trial key."""
    from_env = os.environ.get("CAPTCHA_RUN_KEY", "").strip()
    if from_env:
        return from_env
    config_path = ROOT / "config.json"
    if config_path.exists():
        configured = str(json.loads(config_path.read_text()).get("clientKey") or "").strip()
        if configured:
            return configured
    if CAPTCHA_RUN_KEY.strip():
        return CAPTCHA_RUN_KEY.strip()
    raise RuntimeError("missing CaptchaRun key (CAPTCHA_RUN_KEY or config.json clientKey)")


def load_captchaly_key() -> str:
    key = os.environ.get("CAPTCHALY_KEY", "").strip()
    if not key:
        raise RuntimeError("missing Captchaly key (CAPTCHALY_KEY)")
    return key


def load_captcha_provider() -> str:
    provider = os.environ.get("STEAM_CAPTCHA_PROVIDER", "captchaly").strip().lower()
    if provider in {"captchaly", "captcha.chaly"}:
        return "captchaly"
    raise RuntimeError("unsupported STEAM_CAPTCHA_PROVIDER; only captchaly is allowed")


def require_captcha_token(token, last_error, provider: str):
    """Keep the solver's typed error so the task UI can classify it."""
    if token:
        return token
    if last_error is not None:
        raise last_error
    raise RuntimeError(f"{provider} returned no token")


def parse_proxy(proxy_url: str) -> dict[str, Any]:
    # support http://user:pass@host:port
    u = urlparse(proxy_url if "://" in proxy_url else f"http://{proxy_url}")
    return {
        "proxyType": (u.scheme or "http").replace("https", "http"),
        "proxyAddress": u.hostname,
        "proxyPort": int(u.port or 80),
        "proxyLogin": u.username or "",
        "proxyPassword": u.password or "",
        "url": proxy_url if "://" in proxy_url else f"http://{proxy_url}",
    }


def captchaly_bind_proxy(proxy_url: str | None) -> str | None:
    """Captchaly accepts HTTP proxies; SOCKS5 1024 endpoints 503 the solver.

    Resin is rewritten to ``resin.pokonyan.com`` so the solver can open the
    same public proxy the registration submit uses.
    """
    if not (proxy_url or "").strip():
        return None
    from apex_registration.utils.proxy import public_resin_proxy

    http_url = public_resin_proxy(as_http_forward_proxy(proxy_url))
    scheme = (urlparse(http_url).scheme or "").lower()
    if scheme in {"http", "https"}:
        return http_url
    return None


def classify_ajaxverifyemail(payload: dict) -> str | None:
    """Map Steam join/ajaxverifyemail into a stable error_code.

    ``success == 1`` is the only OK. Email-already-used used to be dumped into
    ``steam_captcha_rejected``, which made full-setup retry the same mailbox.
    """
    if not isinstance(payload, dict):
        return "steam_email_rejected"
    success = payload.get("success")
    if success == 1:
        return None
    details = str(
        payload.get("details")
        or payload.get("message")
        or payload.get("strError")
        or ""
    )
    blob = details.lower()
    taken_needles = (
        "already",
        "taken",
        "in use",
        "exists",
        "duplicate",
        "已註冊",
        "已注册",
        "已被使用",
        "此電子郵件",
    )
    if success in (13, 14) or any(needle in blob for needle in taken_needles):
        return "steam_email_already_registered"
    if success == 101:
        return "steam_captcha_rejected"
    if success in (25, 84, 87):
        return "steam_rate_limited"
    return "steam_email_rejected"


def load_mailbox_pool(primary: dict[str, str]) -> list[dict[str, str]]:
    pool = [primary]
    seen = {str(primary.get("email") or "").strip().lower()}
    seen.discard("")
    try:
        if EMAIL_POOL.is_file():
            raw = json.loads(EMAIL_POOL.read_text(encoding="utf-8"))
            rows = raw if isinstance(raw, list) else list((raw or {}).values())
            for item in rows:
                if not isinstance(item, dict):
                    continue
                email = str(item.get("email") or "").strip()
                if not email or email.lower() in seen:
                    continue
                if not all(item.get(key) for key in ("email", "client_id", "refresh_token")):
                    continue
                pool.append(
                    {
                        "email": email,
                        "password": str(item.get("password") or ""),
                        "client_id": str(item["client_id"]),
                        "refresh_token": str(item["refresh_token"]),
                    }
                )
                seen.add(email.lower())
    except Exception:
        pass
    return pool


def load_email_line() -> dict[str, str]:
    requested = os.environ.get("STEAM_EMAIL", "").strip()
    if requested:
        if requested.lower() == "ea-latest":
            requested = str(json.loads(EA_RESULT.read_text()).get("email") or "").strip()
        pool = json.loads(EMAIL_POOL.read_text())
        for record in pool:
            if not isinstance(record, dict):
                continue
            if str(record.get("email") or "").strip().lower() != requested.lower():
                continue
            if not all(record.get(key) for key in ("email", "client_id", "refresh_token")):
                break
            return {
                "email": str(record["email"]),
                "password": str(record.get("password") or ""),
                "client_id": str(record["client_id"]),
                "refresh_token": str(record["refresh_token"]),
            }
        raise RuntimeError("requested Steam mailbox is unavailable in the Graph pool")
    line = EMAIL_LINE_FILE.read_text().strip().splitlines()[0].strip()
    parts = line.split("----")
    if len(parts) < 4:
        raise RuntimeError(f"email line needs email----password----client_id----refresh_token, got {len(parts)} parts")
    return {
        "email": parts[0],
        "password": parts[1],
        "client_id": parts[2],
        "refresh_token": parts[3],
    }


def graph_client() -> requests.Session:
    client = requests.Session()
    client.trust_env = False
    proxy = os.environ.get('STEAM_PROXY', '').strip() or os.environ.get('EA_PROXY', '').strip()
    if proxy and not direct_mode():
        parsed = urlparse(proxy)
        if parsed.scheme == 'socks5':
            proxy = parsed._replace(scheme='socks5h').geturl()
        client.proxies = {'http': proxy, 'https': proxy}
    return client


def graph_access_token(email_data: dict[str, str]) -> str:
    data = {
        "client_id": email_data["client_id"],
        "refresh_token": email_data["refresh_token"],
        "grant_type": "refresh_token",
        "scope": "https://graph.microsoft.com/.default",
    }
    client = graph_client()
    r = client.post(
        "https://login.microsoftonline.com/consumers/oauth2/v2.0/token",
        data=data,
        timeout=30,
    )
    j = r.json()
    token = j.get("access_token")
    if not token:
        raise RuntimeError(f"graph token failed: {j}")
    if j.get("refresh_token"):
        email_data["refresh_token"] = j["refresh_token"]
    return token


def graph_find_steam_link(access_token: str, *, after_ts: float) -> str | None:
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {
        "$top": "15",
        "$select": "subject,body,receivedDateTime,from",
        "$orderby": "receivedDateTime desc",
    }
    client = graph_client()
    r = client.get(
        "https://graph.microsoft.com/v1.0/me/messages",
        headers=headers,
        params=params,
        timeout=30,
    )
    if r.status_code != 200:
        log(f"graph messages http {r.status_code}: {r.text[:200]}")
        return None
    pat = re.compile(
        r"https://store\.steampowered\.com/account/newaccountverification\?[^\s\"'<>]+",
        re.I,
    )
    for msg in r.json().get("value") or []:
        received = str(msg.get("receivedDateTime") or "")
        try:
            received_ts = calendar.timegm(
                time.strptime(received[:19], "%Y-%m-%dT%H:%M:%S")
            )
            if received_ts + 30 < after_ts:
                continue
        except (TypeError, ValueError):
            pass
        subject = str(msg.get("subject") or "")
        body = str((msg.get("body") or {}).get("content") or "")
        if "steam" not in subject.lower() and "steam" not in body.lower():
            # still search body
            pass
        for url in pat.findall(body):
            url = (
                url.replace("&amp;", "&")
                .replace("=3D", "=")
                .replace("=\r\n", "")
                .replace("\r\n", "")
                .replace("=\n", "")
                .replace("\n", "")
            )
            return url
    return None


def captcha_run_hcaptcha_steam(
    api_key: str,
) -> tuple[str, str | None]:
    """Solve through CaptchaRun's fixed HCaptchaSteam integration."""
    log(f"captcha.run create HCaptchaSteam fixed_sitekey={CAPTCHA_RUN_STEAM_SITE_KEY[:12]}...")
    solution = solve_hcaptcha_steam(api_key)
    log(f"captcha.run token ok len={len(solution.token)} gid_present={bool(solution.gid)}")
    return solution.token, solution.gid


def capsolver_hcaptcha(api_key: str, sitekey: str, proxy_info: dict[str, Any], *, use_proxy: bool = True) -> str:
    """CapSolver path — Steam sitekey is typically blocked."""
    if use_proxy:
        task: dict[str, Any] = {
            "type": "HCaptchaTask",
            "websiteURL": JOIN_URL,
            "websiteKey": sitekey,
            "isInvisible": False,
            "proxyType": proxy_info["proxyType"],
            "proxyAddress": proxy_info["proxyAddress"],
            "proxyPort": proxy_info["proxyPort"],
            "proxyLogin": proxy_info["proxyLogin"],
            "proxyPassword": proxy_info["proxyPassword"],
            "userAgent": UA,
        }
    else:
        task = {
            "type": "HCaptchaTaskProxyLess",
            "websiteURL": JOIN_URL,
            "websiteKey": sitekey,
            "isInvisible": False,
            "userAgent": UA,
        }

    log(f"CapSolver createTask type={task['type']} sitekey={sitekey[:12]}...")
    create = requests.post(
        "https://api.capsolver.com/createTask",
        json={"clientKey": api_key, "task": task},
        timeout=60,
    ).json()
    if create.get("errorId"):
        raise RuntimeError(f"capsolver createTask failed: {create}")
    task_id = create.get("taskId")
    if not task_id:
        raise RuntimeError(f"capsolver no taskId: {create}")
    log(f"CapSolver taskId={task_id}")

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(3)
        poll = requests.post(
            "https://api.capsolver.com/getTaskResult",
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        ).json()
        if poll.get("errorId"):
            raise RuntimeError(f"capsolver getTaskResult failed: {poll}")
        status = poll.get("status")
        if status == "ready":
            sol = poll.get("solution") or {}
            token = sol.get("gRecaptchaResponse") or sol.get("token") or sol.get("respKey")
            if not token:
                raise RuntimeError(f"capsolver ready but empty token: {poll}")
            log(f"CapSolver token ok len={len(token)}")
            return token
        if status and status not in {"idle", "processing"}:
            raise RuntimeError(f"capsolver unexpected status: {poll}")
        log(f"CapSolver status={status}")
    raise RuntimeError("capsolver timeout")


def yescaptcha_hcaptcha(
    api_key: str,
    api_base: str,
    sitekey: str,
    proxy_info: dict[str, Any] | None = None,
    *,
    use_proxy: bool = False,
) -> str:
    if use_proxy and proxy_info:
        # YesCaptcha uses proxy string form for HCaptchaTask
        proxy_str = (
            f"{proxy_info['proxyType']}://{proxy_info['proxyLogin']}:"
            f"{proxy_info['proxyPassword']}@{proxy_info['proxyAddress']}:{proxy_info['proxyPort']}"
        )
        task: dict[str, Any] = {
            "type": "HCaptchaTask",
            "websiteURL": JOIN_URL,
            "websiteKey": sitekey,
            "proxy": proxy_str,
            "userAgent": UA,
        }
    else:
        task = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": JOIN_URL,
            "websiteKey": sitekey,
            "userAgent": UA,
        }
    log(f"YesCaptcha createTask type={task['type']} sitekey={sitekey[:12]}...")
    create = requests.post(
        f"{api_base}/createTask",
        json={"clientKey": api_key, "task": task},
        timeout=60,
    ).json()
    if create.get("errorId"):
        raise RuntimeError(f"yescaptcha createTask failed: {create}")
    task_id = create.get("taskId")
    if not task_id:
        raise RuntimeError(f"yescaptcha no taskId: {create}")
    log(f"YesCaptcha taskId={task_id}")

    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(3)
        poll = requests.post(
            f"{api_base}/getTaskResult",
            json={"clientKey": api_key, "taskId": task_id},
            timeout=30,
        ).json()
        if poll.get("errorId"):
            raise RuntimeError(f"yescaptcha getTaskResult failed: {poll}")
        status = poll.get("status")
        if status == "ready":
            sol = poll.get("solution") or {}
            token = sol.get("gRecaptchaResponse") or sol.get("token")
            if not token:
                raise RuntimeError(f"yescaptcha ready but empty token: {poll}")
            log(f"YesCaptcha token ok len={len(token)}")
            return token
        log(f"YesCaptcha status={status}")
    raise RuntimeError("yescaptcha timeout")


def rand_name(n: int | None = None) -> str:
    n = n or random.randint(10, 14)
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def rand_password(n: int | None = None) -> str:
    n = n or random.randint(12, 16)
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(random.choice(chars) for _ in range(n))


def account_name_check_payload(account_name: str, creation_id: str) -> dict[str, Any]:
    """Build the Steam username check payload after email verification."""
    return {
        "accountname": account_name,
        "count": 1,
        "creationid": creation_id,
    }


def _proxy_transport_error(exc: BaseException) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(
        needle in text
        for needle in (
            "ssl",
            "curl: (35)",
            "curl: (56)",
            "curl: (7)",
            "connection closed",
            "connection reset",
            "timed out",
            "timeout",
            "proxy connect",
        )
    )


def _rotate_sticky_proxy(raw_proxy: str, seed: str) -> str:
    """Replace the 1024 sticky sid. A dead exit stays dead for its whole TTL."""
    return with_sticky_session(raw_proxy, seed)


def open_steam_join(proxy_url: str | None, email: str):
    """Load the Steam join page, rotating the sticky exit after a transport failure."""
    raw_proxy = (
        os.environ.get("EA_PROXY", "").strip()
        or os.environ.get("STEAM_PROXY", "").strip()
        or (proxy_url or "")
    )
    current = proxy_url
    last_error: BaseException | None = None
    for attempt in range(1, 4):
        info = parse_proxy(current) if current else None
        session = new_chrome_session(None if info is None else info["url"])
        session.headers.update(steam_join_headers())
        apply_steam_locale(session)
        session.timeout = 45
        log(f"GET join page attempt={attempt}")
        try:
            response = session.get(JOIN_URL, params={"snr": "1_60_4__62"}, timeout=45)
            response.raise_for_status()
            return session, response, info
        except Exception as exc:
            last_error = exc
            if attempt >= 3 or not _proxy_transport_error(exc) or not raw_proxy:
                raise
            log(f"join page transport failed: {type(exc).__name__}; rotating sticky proxy")
            raw_proxy = _rotate_sticky_proxy(
                raw_proxy, f"{email}-join-{attempt}-{int(time.time())}"
            )
            os.environ["EA_PROXY"] = raw_proxy
            os.environ["STEAM_PROXY"] = raw_proxy
            current = as_http_forward_proxy(raw_proxy)
            time.sleep(2)
    if last_error is not None:
        raise last_error
    raise RuntimeError("Steam join page did not load")


def main() -> int:
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    OUT_LOG.write_text("")
    OUT_LOG.chmod(0o600)
    proxy_url = load_proxy()
    proxy_info = parse_proxy(proxy_url) if proxy_url else None
    captcha_provider = load_captcha_provider()
    captcha_key = (
        load_captchaly_key()
        if captcha_provider == "captchaly"
        else load_captcha_run_key()
    )
    email_data = load_email_line()
    mailbox_pool = load_mailbox_pool(email_data)
    skipped_steam_emails: list[str] = []
    log(f"mailbox selected domain={email_data['email'].rsplit('@', 1)[-1]}")
    log(
        "network=direct"
        if proxy_info is None
        else f"proxy={proxy_info['proxyAddress']}:{proxy_info['proxyPort']}"
    )
    log(f"captcha_provider={captcha_provider} key_loaded={bool(captcha_key)}")

    session, r, proxy_info = open_steam_join(proxy_url, email_data["email"])
    if proxy_info is not None:
        proxy_url = proxy_info["url"]
        PROXY_FILE.write_text(
            f"{proxy_info['proxyAddress']}:{proxy_info['proxyPort']}:"
            f"{proxy_info['proxyLogin']}:{proxy_info['proxyPassword']}\n"
        )
        PROXY_FILE.chmod(0o600)
    soup = BeautifulSoup(r.content, "html.parser")
    init_el = soup.find("input", {"id": "init_id"})
    init_id = init_el["value"] if init_el and init_el.get("value") else None
    if not init_id:
        log("init_id missing on first page load, will retry later")
    else:
        log(f"init_id={init_id[:16]}...")

    headers = steam_join_headers()
    sessionid = None
    email_requested_at = time.time()
    mailbox_index = 0
    captcha_retries = 0
    last_verify = {}
    last_error_code = "steam_email_rejected"

    while mailbox_index < len(mailbox_pool):
        email_data = mailbox_pool[mailbox_index]
        log("refreshcaptcha")
        cap = session.post(
            "https://store.steampowered.com/join/refreshcaptcha/",
            data={"count": "1", "hcaptcha": "1"},
            timeout=45,
        )
        cap.raise_for_status()
        capj = cap.json()
        steam_sitekey = capj.get("sitekey")
        steam_gid = capj.get("gid")
        log(f"steam_sitekey={steam_sitekey} steam_gid={steam_gid} type={capj.get('type')}")
        if not steam_sitekey or steam_gid is None:
            raise RuntimeError(f"bad captcha payload: {capj}")

        token = None
        captcha_gid = None
        last_err = None
        if captcha_provider == "captchaly":
            try:
                bind_proxy = captchaly_bind_proxy(proxy_url)
                log(
                    "captchaly create hcaptcha-enterprise "
                    f"sitekey={str(steam_sitekey)[:12]}... "
                    f"proxy_bound={bool(bind_proxy)}"
                )
                try:
                    solution = solve_hcaptcha_enterprise(
                        captcha_key,
                        sitekey=str(steam_sitekey),
                        website_url=JOIN_URL,
                        proxy=bind_proxy,
                    )
                except CaptchalyError as bound_exc:
                    if not bind_proxy:
                        raise
                    log(
                        f"captchaly proxy-bound failed: {bound_exc}; retrying proxyless"
                    )
                    solution = solve_hcaptcha_enterprise(
                        captcha_key,
                        sitekey=str(steam_sitekey),
                        website_url=JOIN_URL,
                    )
                token = solution.token
                log(
                    "captchaly token ok "
                    f"len={len(token)} deducted={solution.deducted} duration={solution.duration}"
                )
            except CaptchalyError as e:
                last_err = e
                log(f"captchaly failed: {e}")
        else:
            try:
                token, captcha_gid = captcha_run_hcaptcha_steam(captcha_key)
                log(f"captcha.run gid_present={bool(captcha_gid)}")
            except CaptchaRunError as e:
                last_err = e
                log(f"captcha.run failed: {e}")
        token = require_captcha_token(token, last_err, captcha_provider)

        if not init_id:
            r = session.get(JOIN_URL, params={"snr": "1_60_4__62"}, timeout=45)
            soup = BeautifulSoup(r.content, "html.parser")
            init_el = soup.find("input", {"id": "init_id"})
            init_id = init_el["value"] if init_el and init_el.get("value") else None
        if not init_id:
            raise RuntimeError("init_id still missing")

        cookie_str = "; ".join(f"{k}={v}" for k, v in session.cookies.get_dict().items())
        log(f"cookies={list(session.cookies.get_dict().keys())}")
        headers = steam_join_headers()
        headers.update({"Cookie": cookie_str, "X-Requested-With": "XMLHttpRequest"})
        data = {
            "email": email_data["email"],
            "captchagid": captcha_gid or steam_gid,
            "captcha_text": token,
            "elang": register_locale().steam_elang,
            "init_id": init_id,
            "guest": "false",
        }
        email_requested_at = time.time()
        vr = session.post(
            "https://store.steampowered.com/join/ajaxverifyemail",
            headers=headers,
            data=data,
            timeout=45,
        )
        try:
            vj = vr.json()
        except Exception:
            raise RuntimeError(f"ajaxverifyemail non-json: {vr.text[:300]}") from None
        last_verify = vj
        log(
            "ajaxverifyemail "
            f"status={vr.status_code} success={vj.get('success')} "
            f"details_present={bool(vj.get('details'))}"
        )
        error_code = classify_ajaxverifyemail(vj)
        if error_code is None:
            sessionid = vj.get("sessionid")
            log(f"sessionid={sessionid}")
            break
        OUT_ERR.write_text(
            (OUT_ERR.read_text() if OUT_ERR.exists() else "")
            + f"{email_data['email']}\tajaxverifyemail\t{vj}\n"
        )
        last_error_code = error_code
        if error_code == "steam_email_already_registered":
            skipped_steam_emails.append(email_data["email"])
            log("Steam mailbox already registered; switching mailbox")
            mailbox_index += 1
            captcha_retries = 0
            continue
        if error_code == "steam_captcha_rejected" and captcha_retries < 2:
            captcha_retries += 1
            log("Steam captcha rejected; retrying with a fresh token")
            continue
        OUT_RESULT.write_text(json.dumps({
            "success": False,
            "stage": "captcha_verify" if error_code == "steam_captcha_rejected" else "submit_email",
            "error_code": error_code,
            "email": email_data["email"],
            "skipped_steam_emails": skipped_steam_emails,
            "steam_success": vj.get("success"),
        }, ensure_ascii=False, indent=2))
        OUT_RESULT.chmod(0o600)
        log(f"FAIL captcha/email step: {vj}")
        return 2
    else:
        OUT_RESULT.write_text(json.dumps({
            "success": False,
            "stage": "submit_email",
            "error_code": "steam_email_already_registered",
            "email": email_data["email"],
            "skipped_steam_emails": skipped_steam_emails,
            "steam_success": last_verify.get("success"),
        }, ensure_ascii=False, indent=2))
        OUT_RESULT.chmod(0o600)
        log("FAIL: every Steam mailbox in the pool is already registered")
        return 2

    log("wait steam verification mail via Graph")
    verified = False
    wait_seconds = mail_wait_seconds()
    wait_started = time.monotonic()
    while time.monotonic() - wait_started < wait_seconds:
        # poll steam
        chk = session.post(
            "https://store.steampowered.com/join/ajaxcheckemailverified",
            data={"creationid": sessionid},
            headers=headers,
            timeout=30,
        )
        try:
            cj = chk.json()
        except Exception:
            cj = {}
        log(f"checkemailverified={cj}")
        if cj.get("success") == 1:
            verified = True
            break

        # pull mail
        try:
            at = graph_access_token(email_data)
            link = graph_find_steam_link(
                at,
                after_ts=email_requested_at - 10,
            )
            if link:
                log(f"found verify link: {link[:80]}...")
                gr = session.get(link, headers={"User-Agent": UA, "Referer": JOIN_URL}, timeout=45)
                log(f"open verify link status={gr.status_code} len={len(gr.text)}")
                time.sleep(3)
        except Exception as e:
            log(f"graph/mail error: {e}")
        remaining = wait_seconds - (time.monotonic() - wait_started)
        if remaining > 0:
            time.sleep(min(4, remaining))

    if not verified:
        # one more check
        chk = session.post(
            "https://store.steampowered.com/join/ajaxcheckemailverified",
            data={"creationid": sessionid},
            headers=headers,
            timeout=30,
        )
        try:
            cj = chk.json()
        except Exception:
            cj = {}
        if cj.get("success") != 1:
            OUT_RESULT.write_text(json.dumps({
                "success": False,
                "stage": "email_verify",
                "error_code": "steam_email_verify_timeout",
                "email": email_data["email"],
                "mail_wait_seconds": wait_seconds,
            }, ensure_ascii=False, indent=2))
            OUT_RESULT.chmod(0o600)
            log(f"email verify timeout after {wait_seconds}s, last={cj}")
            return 3

    # create account
    account_name = rand_name()
    for _ in range(8):
        cr = session.post(
            "https://store.steampowered.com/join/checkavail/",
            data=account_name_check_payload(account_name, str(sessionid)),
            headers=headers,
            timeout=30,
        )
        try:
            avail = cr.json()
        except Exception:
            avail = {}
        log(
            "checkavail "
            f"status={cr.status_code} available={avail.get('bAvailable')} "
            f"suggestions={len(avail.get('rgSuggestions') or [])}"
        )
        if avail.get("bAvailable"):
            break
        suggestions = avail.get("rgSuggestions") or []
        account_name = suggestions[0] if suggestions else rand_name()
    else:
        raise RuntimeError("no available account name")

    password = None
    for _ in range(10):
        password = rand_password()
        pr = session.post(
            "https://store.steampowered.com/join/checkpasswordavail/",
            data={"accountname": account_name, "password": password, "count": 1},
            headers=headers,
            timeout=30,
        )
        try:
            pavail = pr.json()
        except Exception:
            pavail = {}
        if pavail.get("bAvailable"):
            break
    else:
        raise RuntimeError("no available password")

    log(f"createaccount name={account_name}")
    create = session.post(
        "https://store.steampowered.com/join/createaccount/",
        data={
            "accountname": account_name,
            "password": password,
            "count": 0,
            "lt": 1,
            "creation_sessionid": sessionid,
            "embedded_appid": 0,
            "guest": False,
        },
        headers=headers,
        timeout=45,
    )
    try:
        result = create.json()
    except Exception:
        result = {"raw": create.text[:300]}

    ok = bool(result.get("bSuccess"))
    safe_result = {
        "bSuccess": result.get("bSuccess"),
        "bInSteamClient": result.get("bInSteamClient"),
        "eresult": result.get("eresult"),
    }
    log(
        "createaccount "
        f"status={create.status_code} success={ok} eresult={result.get('eresult')}"
    )
    line = (
        f"{account_name}----{password}----{email_data['email']}----"
        f"{ok}----{safe_result}\n"
    )
    with OUT_OK.open("a") as f:
        f.write(line)
    OUT_OK.chmod(0o600)
    OUT_RESULT.write_text(json.dumps({
        "success": ok,
        "stage": "completed" if ok else "create_account",
        "error_code": None if ok else "steam_create_failed",
        "email": email_data["email"],
        "username": account_name if ok else None,
        "password": password if ok else None,
        "skipped_steam_emails": skipped_steam_emails,
        "bSuccess": result.get("bSuccess"),
        "eresult": result.get("eresult"),
    }, ensure_ascii=False, indent=2))
    OUT_RESULT.chmod(0o600)
    log(f"DONE ok={ok} saved={OUT_OK}")
    return 0 if ok else 4


if __name__ == "__main__":
    try:
        # prepare email file from pool export if missing or empty
        if not EMAIL_LINE_FILE.exists() or not EMAIL_LINE_FILE.read_text().strip():
            src = Path("/tmp/steam_email_one.txt")
            if src.exists():
                EMAIL_LINE_FILE.write_text(src.read_text())
        sys.exit(main())
    except Exception as e:
        ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
        is_captchaly_error = isinstance(e, CaptchalyError)
        OUT_RESULT.write_text(json.dumps({
            "success": False,
            "stage": "captcha_solve" if is_captchaly_error else "fatal",
            "error_code": "captchaly_failed" if is_captchaly_error else type(e).__name__,
            "error_detail": str(e)[:1000],
        }, ensure_ascii=False, indent=2))
        OUT_RESULT.chmod(0o600)
        log(f"FATAL: {e}")
        sys.exit(1)

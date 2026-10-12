"""
全局代理配置

规则：
- 默认走 DataImpulse HTTP 代理（注册、登录、探活、Team checkout 等）
- Plus 付款链接走 DataImpulse HTTP 代理
- 支付专用走 DataImpulse HTTP 代理
- 代理优先读取环境变量，其次使用内置默认值
- sticky=True 时为单次任务注入 sessid，整条注册链路粘住同一出口 IP
"""

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from typing import Dict, Optional
from urllib.parse import quote, unquote, urlparse, urlunparse

# ─── DataImpulse HTTP 代理 ────────────────────────────────────
# 凭证只从环境变量来。不要在这里写死默认值——这个文件是进版本库的，
# 写死等于把代理账号连同代码一起分发出去。


class ProxyNotConfigured(RuntimeError):
    """缺少代理配置时抛出，而不是静默直连暴露真实出口 IP。"""


def _require(*names: str) -> str:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    raise ProxyNotConfigured(
        f"未配置代理：请设置 {' 或 '.join(names)}（形如 "
        "http://user:pass@gw.dataimpulse.com:823）"
    )


def dataimpulse_sessid(seed: str) -> str:
    """Stable DataImpulse sessid from any seed (email, task id, retry marker).

    The gateway keeps sticky IPs for about 30 minutes per sessid. Reusing the
    same seed for register / link / 2FA / probe keeps one exit for that window
    and still pins later calls to Hong Kong via ``__cr.hk`` on the base URL.
    """
    digest = hashlib.sha256((seed or "").strip().lower().encode("utf-8")).hexdigest()
    return f"ea{digest[:18]}"


def _inject_sticky_sessid(proxy_url: str, sessid: Optional[str] = None) -> str:
    """在 DataImpulse 用户名中注入/替换 sessid，实现粘性 IP。

    官方文档写 ``login__cr.hk;sessid.abc``。分号在 URL userinfo 里要编码，
    实测网关也接受 ``login__cr.hk__sessid.abc``，和官方 sessid 打到同一出口。
    这里用双下划线形式，避免 ``requests`` 把 ``;`` 吃掉。
    """
    raw = (proxy_url or "").strip()
    if not raw:
        return raw
    sid = re.sub(r"[^A-Za-z0-9]", "", (sessid or secrets.token_hex(8)).strip())[:20]
    if not sid:
        return raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    if not parsed.username:
        return raw
    user = unquote(parsed.username)
    user = re.sub(r"(?:__|;)sessid[.=-][A-Za-z0-9_-]+", "", user, flags=re.I)
    user = user.rstrip("_;")
    user = f"{user}__sessid.{sid}"
    password = unquote(parsed.password or "")
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@" if password or user else ""
    return f"{parsed.scheme}://{auth}{host}{port}"


def _is_711proxy(*, host: str = "", user: str = "") -> bool:
    """711 sells several gateway names. The username shape is the stable mark.

    ``global.rotgb.trustsource.live`` is one of those gateways. A dashboard
    user looks like ``USER-zone-custom-region-JP``.
    """
    host = (host or "").lower()
    if "711proxy" in host or "trustsource.live" in host:
        return True
    return "-zone-custom" in (user or "").lower()


def _is_1024proxy(proxy_url: str, *, host: str = "", user: str = "") -> bool:
    if _is_711proxy(host=host, user=user):
        return False
    return "1024proxy" in (host or "").lower() or bool(re.search(r"-sid-[^-]+", user or ""))


def _711_session_id(seed: str) -> str:
    """Eight digits. 711 sticky sessions are ``session-<8 digits>``."""
    digest = hashlib.sha256((seed or "").strip().lower().encode("utf-8")).hexdigest()
    return "".join(str(int(char, 16) % 10) for char in digest[:8])


def pin_711_region_user(user: str, region: str, seed: str) -> str:
    """Point a 711proxy username at one country and one sticky session.

    Dashboard usernames look like ``user-zone-custom-region-US-session-12345678-sessTime-5``.
    ``sessTime`` is minutes, from 5 to 180. City and state filters are removed
    so the gateway targets the country, the same way 1024 drops ``-city-``.
    """
    code = (region or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError(f"invalid proxy region {region!r}")
    raw = (user or "").strip()
    if not raw:
        return raw
    raw = re.sub(r"-st-[^-]+", "", raw, flags=re.I)
    raw = re.sub(r"-city-[^-]+", "", raw, flags=re.I)
    session = _711_session_id(seed)
    if re.search(r"-region-[A-Za-z]{2}", raw, flags=re.I):
        raw = re.sub(r"-region-[A-Za-z]{2}", f"-region-{code}", raw, count=1, flags=re.I)
    elif re.search(r"-session-", raw, flags=re.I):
        raw = re.sub(r"-session-", f"-region-{code}-session-", raw, count=1, flags=re.I)
    else:
        raw = f"{raw}-region-{code}"
    if re.search(r"-session-[^-]+", raw, flags=re.I):
        raw = re.sub(r"-session-[^-]+", f"-session-{session}", raw, count=1, flags=re.I)
    else:
        raw = f"{raw}-session-{session}"
    if re.search(r"-sessTime-\d+", raw, flags=re.I):
        raw = re.sub(r"-sessTime-\d+", "-sessTime-60", raw, count=1, flags=re.I)
    else:
        raw = f"{raw}-sessTime-60"
    return raw


def normalize_proxy_url(raw: str, *, default_scheme: str = "http") -> str:
    """Accept a URL or the 711 dashboard line ``host:port:user:password``."""
    text = (raw or "").strip()
    if not text or "://" in text:
        return text
    parts = text.split(":")
    if len(parts) < 4 or not parts[1].isdigit():
        return text
    host, port, user = parts[0], parts[1], parts[2]
    password = ":".join(parts[3:])
    scheme = default_scheme or "http"
    if port == "22000":
        scheme = "socks5"
    return (
        f"{scheme}://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"
    )


def pin_1024_region_user(user: str, region: str) -> str:
    """Point a 1024proxy username at one country and drop city filters.

    ``region`` is a two-letter code such as HK, US, or JP. City and state
    filters are removed so the gateway does not keep a narrower target.
    """
    code = (region or "").strip().upper()
    if not re.fullmatch(r"[A-Z]{2}", code):
        raise ValueError(f"invalid proxy region {region!r}")
    raw = (user or "").strip()
    if not raw:
        return raw
    raw = re.sub(r"-st-[^-]+", "", raw, flags=re.I)
    raw = re.sub(r"-city-[^-]+", "", raw, flags=re.I)
    raw = re.sub(r"-asn-[^-]+", "", raw, flags=re.I)
    if re.search(r"-region-[A-Za-z]{2}", raw, flags=re.I):
        raw = re.sub(r"-region-[A-Za-z]{2}", f"-region-{code}", raw, count=1, flags=re.I)
    elif re.search(r"-sid-", raw, flags=re.I):
        raw = re.sub(r"-sid-", f"-region-{code}-sid-", raw, count=1, flags=re.I)
    else:
        raw = f"{raw}-region-{code}"
    return raw


def pin_1024_hong_kong_user(user: str) -> str:
    """Force 1024proxy username targeting onto Hong Kong.

    The dashboard username is ``login-region-XX-sid-...-t-N``. Missing or
    non-HK ``region`` values (or leftover US/JP city filters) are why Roxy
    windows sometimes opened on Japanese EA defaults despite a HK locale.
    """
    return pin_1024_region_user(user, "HK")


def with_sticky_session(proxy_url: str, seed: str, *, region: str | None = None) -> str:
    """Pin a sticky session onto DataImpulse or 1024proxy-style usernames."""
    raw = (proxy_url or "").strip()
    if not raw:
        return raw
    try:
        parsed = urlparse(raw)
    except Exception:
        return raw
    host = (parsed.hostname or "").lower()
    user = unquote(parsed.username or "")
    if _is_711proxy(host=host, user=user):
        user = pin_711_region_user(user, region or "HK", seed)
        password = unquote(parsed.password or "")
        port = f":{parsed.port}" if parsed.port else ""
        auth = (
            f"{quote(user, safe='')}:{quote(password, safe='')}@"
            if password or user
            else ""
        )
        return f"{parsed.scheme}://{auth}{parsed.hostname}{port}"
    if _is_1024proxy(raw, host=host, user=user):
        sid = re.sub(r"[^A-Za-z0-9]", "", dataimpulse_sessid(seed))[:8]
        user = pin_1024_region_user(user, region) if region else pin_1024_hong_kong_user(user)
        if re.search(r"-sid-[^-]+", user):
            user = re.sub(r"-sid-[^-]+", f"-sid-{sid}", user, count=1)
        else:
            user = f"{user}-sid-{sid}"
        if re.search(r"-t-\d+", user):
            user = re.sub(r"-t-\d+", "-t-60", user, count=1)
        else:
            user = f"{user}-t-60"
        password = unquote(parsed.password or "")
        port = f":{parsed.port}" if parsed.port else ""
        auth = (
            f"{quote(user, safe='')}:{quote(password, safe='')}@"
            if password or user
            else ""
        )
        return f"{parsed.scheme}://{auth}{parsed.hostname}{port}"
    return _inject_sticky_sessid(raw, sessid=dataimpulse_sessid(seed))


RESIN_PUBLIC_HOST = "resin.pokonyan.com"
RESIN_PUBLIC_PORT = 2260
_RESIN_PRIVATE_HOSTS = frozenset({
    "127.0.0.1",
    "localhost",
    "::1",
    "host.docker.internal",
    "resin",
    "34.94.168.61",
})


def public_resin_proxy(proxy_url: str) -> str:
    """Point a Resin forward proxy at the public ingress.

    Captchaly solves from its own network. A loopback tunnel, the Docker
    name ``resin``, or the old numeric host is not reachable from there.
    Other proxies are left unchanged.
    """
    raw = (proxy_url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if host == RESIN_PUBLIC_HOST and port in (None, RESIN_PUBLIC_PORT):
        return raw
    if port not in {2260, 12260} and host not in _RESIN_PRIVATE_HOSTS and host != RESIN_PUBLIC_HOST:
        return raw
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    auth = (
        f"{quote(user, safe='')}:{quote(password, safe='')}@"
        if user or password
        else ""
    )
    scheme = parsed.scheme or "http"
    return f"{scheme}://{auth}{RESIN_PUBLIC_HOST}:{RESIN_PUBLIC_PORT}"


def as_http_forward_proxy(proxy_url: str) -> str:
    """Same credentials/host, HTTP CONNECT. curl_cffi and Captchaly need this.

    1024 SOCKS5 works in Roxy, but curl_cffi on macOS can fail with
    OPENSSL_internal, and Captchaly 503s SOCKS5 endpoints.
    """
    raw = (proxy_url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    if (parsed.scheme or "").lower() not in {"socks5", "socks5h"}:
        return raw
    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or ""
    port = f":{parsed.port}" if parsed.port else ""
    auth = (
        f"{quote(user, safe='')}:{quote(password, safe='')}@"
        if password or user
        else ""
    )
    return f"http://{auth}{host}{port}"


@dataclass(frozen=True)
class ProxyExit:
    """Geo / ISP facts for one sticky proxy egress IP."""

    country: Optional[str] = None
    ip: str = ""
    asn: Optional[int] = None
    isp: str = ""
    org: str = ""
    hosting: bool = False
    mobile: bool = False


_ASN_RE = re.compile(r"AS(\d+)", re.I)
# Consumer home broadband that actually shows up in the 1024 HK pool.
_HOME_ASNS = frozenset({4760, 9269})
_MOBILE_ASNS = frozenset({10118, 17924, 58453})
_BUS_MARKERS = (
    "enterprise",
    "hgc",
    "hutchison global",
    "pccw global",
    "new world",
    "domain five",
    "global communications",
)
_HOME_MARKERS = (
    "mass internet",
    "netvigator",
    "hong kong broadband network",
    "hkbn",
    "hong kong telecommunications",
    "hkt limited",
    "pccw ims",
)


def _coerce_asn(*values: object) -> Optional[int]:
    for value in values:
        if value is None or isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value if value > 0 else None
        text = str(value).strip()
        if not text:
            continue
        matched = _ASN_RE.search(text)
        if matched:
            return int(matched.group(1))
        if text.isdigit():
            return int(text)
    return None


def parse_proxy_exit(body: str) -> ProxyExit:
    """Parse ipinfo / ip-api / ipwho-style JSON (or a bare country code)."""
    text = (body or "").strip()
    if not text:
        return ProxyExit()
    if re.fullmatch(r"[A-Za-z]{2}", text):
        return ProxyExit(country=text.upper())
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return ProxyExit()
    if not isinstance(payload, dict):
        return ProxyExit()
    country = None
    for key in ("country", "countryCode", "country_code"):
        value = str(payload.get(key) or "").strip()
        if re.fullmatch(r"[A-Za-z]{2}", value):
            country = value.upper()
            break
    isp = str(payload.get("isp") or "").strip()
    org = str(payload.get("org") or "").strip()
    as_field: object = payload.get("as") or payload.get("asn")
    conn = payload.get("connection")
    if isinstance(conn, dict):
        isp = isp or str(conn.get("isp") or "").strip()
        org = org or str(conn.get("org") or "").strip()
        if as_field in (None, ""):
            as_field = conn.get("asn")
    hosting = bool(payload.get("hosting"))
    mobile = bool(payload.get("mobile"))
    privacy = payload.get("privacy")
    if isinstance(privacy, dict):
        hosting = hosting or bool(privacy.get("hosting"))
    return ProxyExit(
        country=country,
        ip=str(payload.get("ip") or payload.get("query") or "").strip(),
        asn=_coerce_asn(as_field, org, isp),
        isp=isp,
        org=org,
        hosting=hosting,
        mobile=mobile,
    )


def _exit_summary(exit_info: ProxyExit) -> str:
    label = exit_info.org or exit_info.isp or "未知运营商"
    if exit_info.asn:
        return f"{label} / AS{exit_info.asn}"
    return label


def home_broadband_rejection(exit_info: ProxyExit) -> Optional[str]:
    """Return a Chinese reason when the exit is not HK consumer home broadband."""
    blob = f"{exit_info.isp} {exit_info.org}".lower()
    summary = _exit_summary(exit_info)
    if exit_info.hosting:
        return f"代理出口是机房/hosting（{summary}）"
    if exit_info.mobile or exit_info.asn in _MOBILE_ASNS:
        return f"代理出口是移动网络（{summary}）"
    if any(marker in blob for marker in _BUS_MARKERS):
        return f"代理出口是商宽（{summary}）"
    if exit_info.asn in _HOME_ASNS or any(marker in blob for marker in _HOME_MARKERS):
        return None
    if not exit_info.asn and not exit_info.isp and not exit_info.org:
        return "无法确认代理出口是否家宽"
    return f"代理出口不是已知家宽运营商（{summary}）"


def get_proxy(sticky: bool = False, sessid: Optional[str] = None) -> Dict[str, str]:
    """返回全局代理字典，供 requests.Session / curl_cffi 使用。

    sticky=True 或传入 sessid：为本次调用固定出口 IP（适合整单注册）。
    """
    proxy_url = _require("GPT_PROXY")
    if sticky or sessid:
        proxy_url = _inject_sticky_sessid(proxy_url, sessid=sessid)
    return {"http": proxy_url, "https": proxy_url}


def get_plus_proxy() -> Dict[str, str]:
    """返回 Plus 付款链接专用代理"""
    proxy_url = _require("GPT_PLUS_PROXY", "GPT_PROXY")
    return {"http": proxy_url, "https": proxy_url}


def get_pay_proxy(use_proxy: bool = True, country: str = "AU",
                  state: str = "", city: str = "") -> str:
    """支付专用代理。use_proxy=False 返回空串（直连）"""
    if not use_proxy:
        return ""
    return _require("GPT_PAY_PROXY", "GPT_PROXY")


def get_proxy_or_none(use_proxy: bool = True) -> Optional[Dict[str, str]]:
    """便捷方法：use_proxy=False 时返回 None"""
    return get_proxy() if use_proxy else None


def resin_task_proxy(
    proxy_url: str,
    *,
    task_id: str | None,
    endpoint: str = "",
    platform: str | None = None,
) -> str:
    """Build a Resin proxy URL with a per-task sticky account.

    Resin's forward-proxy identity is ``Platform.Account:token``.  Keep the
    configured platform/token, replace any old Account with the current task,
    and optionally route the ingress through the local SSH tunnel.  The tunnel
    override changes only Resin's ingress address; node selection still happens
    on the remote Resin instance.
    """
    raw = (proxy_url or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.hostname:
        return raw

    configured_user = unquote(parsed.username or "")
    platform = (
        (platform or "").strip()
        or os.environ.get("GAME_ACCOUNTS_RESIN_PLATFORM", "").strip()
        or (configured_user.split(".", 1)[0] or "Default").strip()
    )
    if task_id:
        account = "".join(ch for ch in task_id if ch.isalnum())[:20]
        username = f"{platform}.{account}" if account else platform
    else:
        username = platform

    host = parsed.hostname
    port = parsed.port
    scheme = parsed.scheme
    endpoint_value = (endpoint or "").strip()
    if endpoint_value:
        endpoint_parsed = urlparse(
            endpoint_value
            if "://" in endpoint_value
            else f"{scheme}://{endpoint_value}"
        )
        if endpoint_parsed.hostname:
            host = endpoint_parsed.hostname
            port = endpoint_parsed.port
            scheme = endpoint_parsed.scheme or scheme

    password = unquote(parsed.password or "")
    port_part = f":{port}" if port else ""
    auth = f"{quote(username, safe='')}:{quote(password, safe='')}@"
    return f"{scheme}://{auth}{host}{port_part}"

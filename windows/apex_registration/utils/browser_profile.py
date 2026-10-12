"""Hong Kong Chrome profile shared by EA/Steam register, probe, and Playwright.

Protocol calls impersonate Chrome TLS via curl_cffi. The browser context uses
the same locale, language, timezone, and country so the account looks like a
Hong Kong Windows Chrome user.
"""

from __future__ import annotations

import base64
import hashlib
import os
import random
import re
from typing import Any
from urllib.parse import urlparse

try:
    from curl_cffi import requests as browser_requests
except ImportError:  # pragma: no cover
    browser_requests = None

import requests

from apex_registration.utils.proxy import as_http_forward_proxy

COUNTRY = "HK"
LOCALE = "zh-HK"
TIMEZONE = "Asia/Hong_Kong"
ACCEPT_LANGUAGE = "zh-HK,zh;q=0.9,en;q=0.8"
EA_REGISTER_URL = "https://www.ea.com/zh-hk/register"
EA_JUNO_CREATE_URL = (
    "https://accounts.ea.com/connect/auth"
    "?display=junoWeb/create&response_type=code"
    "&redirect_uri=https://www.ea.com/login_check"
    "&locale=zh_HK&client_id=EADOTCOM-WEB-SERVER"
)
STEAM_ELANG = "7"
STEAM_LANGUAGE_COOKIE = "tchinese"
STEAM_TIMEZONE_OFFSET = "28800,0"
CHROME_IMPERSONATE = "chrome"
HK_GEO = {"longitude": 114.1694, "latitude": 22.3193}
# Residential centroids used as the jitter base. Keep pins on land, not the harbour.
_HK_NEIGHBORHOODS = (
    (HK_GEO["latitude"], HK_GEO["longitude"]),  # Tsim Sha Tsui
    (22.2819, 114.1585),  # Central
    (22.2783, 114.1747),  # Causeway Bay
    (22.2856, 114.2180),  # Sai Wan Ho
    (22.2471, 114.1558),  # Aberdeen
    (22.3363, 114.1994),  # Wong Tai Sin
    (22.3130, 114.2258),  # Kwun Tong
    (22.3074, 114.2570),  # Tseung Kwan O
    (22.3810, 114.1877),  # Sha Tin
    (22.3706, 114.1145),  # Tsuen Wan
    (22.3913, 113.9758),  # Tuen Mun
    (22.4445, 114.0222),  # Yuen Long
    (22.3754, 114.1780),  # Tai Wai
    (22.4508, 114.1646),  # Fanling
)
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/142.0.0.0 Safari/537.36"
)
SEC_CH_UA = '"Google Chrome";v="142", "Chromium";v="142", "Not_A Brand";v="24"'
VIEWPORTS = (
    {"width": 1366, "height": 768},
    {"width": 1440, "height": 900},
    {"width": 1536, "height": 864},
    {"width": 1920, "height": 1080},
)
PLAYWRIGHT_CHROME_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    "--enforce-webrtc-ip-permission-check",
    "--disable-background-networking",
)

# EA ID is ASCII only: 4-16 chars, letter first, letters/digits/underscore.
# Juno rejects CJK. Generated IDs stay at 8-16 and lowercase.
# Joined text cannot contain ea / origin / admin / support.
# A 16-character base32 blob is rejected: the 2FA scraper reads it as a secret.
_EA_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,15}$")
_EA_ID_MIN = 8
_EA_ID_MAX = 16
_RESERVED_WORDS = ("ea", "origin", "admin", "support")
# Less common given names. Top chart names plus a year (emma2001) are already
# taken on EA and read as a default account.
_GIVEN_NAME_WORDS = (
    "soren", "leif", "nils", "idris", "maren", "torin", "brynn", "silas",
    "callum", "freya", "orion", "cassia", "espen", "astrid", "ingrid",
    "sigrid", "petra", "marta", "noemi", "ansel", "bodhi", "cyrus",
    "dante", "gideon", "holden", "joren", "kasper", "lachlan", "magnus",
    "niall", "perrin", "quincy", "stellan", "ulric", "vesper", "willem",
    "yorick", "cosmo", "faron", "inigo", "lennox", "merrick", "osman",
    "pippa", "renzo", "tamsin", "weston", "arno", "bram", "cedric",
    "dorian", "elin", "farid", "greta", "helga", "ilya", "johan",
    "karim", "lars", "malin", "oskar", "pavel", "ramon", "saskia",
    "tove", "vadim", "wynn",
)
# Specific, not the first animal everyone types. fox/wolf/moon/cloud plus a
# year was taken immediately (cloud2001).
_LIKED_THING_WORDS = (
    "juniper", "heron", "marigold", "cedar", "pebble", "starling",
    "birch", "coral", "cactus", "harbor", "sparrow", "thistle",
    "fennel", "quartz", "lantern", "compass", "fiddle", "bramble",
    "sorrel", "yarrow", "saffron", "paprika", "nutmeg", "anise",
    "cairn", "fjord", "atoll", "mesa", "butte", "heath", "moor",
    "kiln", "cello", "banjo", "loom", "keel", "dune", "glen", "cove",
)


def _usable_word(word: str) -> bool:
    low = (word or "").strip().lower()
    if not re.fullmatch(r"[a-z]{3,8}", low):
        return False
    return not any(bad in low for bad in _RESERVED_WORDS)


def _clean_words(words: tuple[str, ...]) -> tuple[str, ...]:
    seen: list[str] = []
    for word in words:
        low = word.strip().lower()
        if _usable_word(low) and low not in seen:
            seen.append(low)
    return tuple(seen)


_GIVEN_NAMES = _clean_words(_GIVEN_NAME_WORDS)
_LIKED_THINGS = _clean_words(_LIKED_THING_WORDS)


def _valid_ea_id(candidate: str) -> str | None:
    if not candidate or not _EA_ID_RE.fullmatch(candidate):
        return None
    if not (_EA_ID_MIN <= len(candidate) <= _EA_ID_MAX):
        return None
    # A 16-character base32 blob is what the 2FA scraper reads off the page header.
    if re.fullmatch(r"[A-Za-z2-7]{16}", candidate):
        return None
    low = candidate.lower()
    if any(word in low for word in _RESERVED_WORDS):
        return None
    return candidate


def _id_name_thing() -> str | None:
    name = random.choice(_GIVEN_NAMES)
    thing = random.choice(_LIKED_THINGS)
    return _valid_ea_id(f"{name}_{thing}")


def _id_two_things() -> str | None:
    left, right = random.sample(list(_LIKED_THINGS), 2)
    return _valid_ea_id(f"{left}_{right}")


def rand_gamer_id() -> str:
    """Pronounceable handle. ASCII only; Juno rejects CJK in originId.

    A less common given name plus one specific thing. Birth years and
    fox/wolf/cloud are the crowded defaults, so they are not used.
    """
    builders = (_id_name_thing, _id_two_things)
    weights = (78, 22)
    for _ in range(40):
        candidate = random.choices(builders, weights=weights)[0]()
        if candidate:
            return candidate
    return "soren_cairn"


def pick_register_country() -> str:
    forced = os.environ.get("EA_REGISTER_COUNTRY", COUNTRY).strip().upper()
    return forced or COUNTRY


def chrome_headers(*, navigate: bool = True) -> dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,image/apng,*/*;q=0.8"
        ),
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    if navigate:
        headers.update(
            {
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Upgrade-Insecure-Requests": "1",
            }
        )
    return headers


def viewport_for(seed: str) -> dict[str, int]:
    digest = hashlib.sha256((seed or "").encode("utf-8")).digest()
    return dict(VIEWPORTS[digest[0] % len(VIEWPORTS)])


def geolocation_for(seed: str) -> dict[str, float]:
    """Stable HK coordinate near a residential centroid. Same seed, same pin."""
    digest = hashlib.sha256((seed or "").encode("utf-8")).digest()
    lat0, lng0 = _HK_NEIGHBORHOODS[digest[0] % len(_HK_NEIGHBORHOODS)]
    lat = lat0 + (digest[1] / 255.0 - 0.5) * 0.016
    lng = lng0 + (digest[2] / 255.0 - 0.5) * 0.016
    return {
        "latitude": round(min(22.52, max(22.21, lat)), 6),
        "longitude": round(min(114.41, max(113.89, lng)), 6),
    }


def playwright_context_options(seed: str = "") -> dict[str, Any]:
    return {
        "user_agent": UA,
        "locale": LOCALE,
        "timezone_id": TIMEZONE,
        "geolocation": geolocation_for(seed),
        "permissions": ["geolocation"],
        "color_scheme": "light",
        "viewport": viewport_for(seed),
        "extra_http_headers": {"Accept-Language": ACCEPT_LANGUAGE},
    }


def new_chrome_session(proxy_url: str | None = None):
    """Chrome-TLS session for EA/Steam site calls. Graph/captcha stay on requests."""
    if browser_requests is not None:
        session = browser_requests.Session(impersonate=CHROME_IMPERSONATE)
    else:
        session = requests.Session()
    if hasattr(session, "trust_env"):
        session.trust_env = False
    if proxy_url:
        session.proxies = {"http": proxy_url, "https": proxy_url}
    session.headers.update(chrome_headers())
    return session


def playwright_proxy(proxy_url: str | None) -> dict[str, str] | None:
    """Chromium rejects SOCKS5 with a username/password. Same host over HTTP CONNECT."""
    raw = as_http_forward_proxy((proxy_url or "").strip())
    if not raw:
        return None
    parsed = urlparse(raw)
    if not parsed.hostname:
        return None
    port = f":{parsed.port}" if parsed.port else ""
    return {
        "server": f"{parsed.scheme or 'http'}://{parsed.hostname}{port}",
        "username": parsed.username or "",
        "password": parsed.password or "",
    }


def save_page_screenshot(
    page,
    path: str | os.PathLike[str],
    *,
    full_page: bool = True,
    timeout: int = 2500,
    cdp_fallback: bool = True,
) -> bool:
    """Write a PNG without stalling the flow on fingerprint fonts.

    Playwright's ``page.screenshot`` waits for fonts to finish loading. On the
    Roxy sidecar that wait can sit until the timeout even after Chromium logs
    ``fonts loaded``. CDP ``Page.captureScreenshot`` does not wait.
    """
    dest = os.fspath(path)
    # Only explicitly masked failure captures are eligible for remote diagnostics.
    # Fail closed: no CDP fallback can bypass the mask.
    if re.search(r'fail|error|timeout|blocked|captcha', os.path.basename(dest), re.I):
        try:
            page.screenshot(path=dest + '.support.png', full_page=False, timeout=timeout,
                mask=[page.locator('input,textarea,[contenteditable],img,canvas,svg,code,pre,[class*="code"],[id*="code"],[class*="secret"],[id*="secret"]'),
                      page.get_by_text(re.compile(r'[\w.+-]+@[\w.-]+|\b[0-9]{6,}\b|\b[A-Z2-7]{16,}\b'))])
        except Exception:
            pass
    try:
        page.screenshot(path=dest, full_page=full_page, timeout=timeout)
        return True
    except Exception:
        pass
    if not cdp_fallback:
        # A diagnostic must not block the business flow on an unbounded CDP send.
        return False
    try:
        session = page.context.new_cdp_session(page)
    except Exception:
        return False
    try:
        payload: dict[str, Any] = {"format": "png"}
        if full_page:
            payload["captureBeyondViewport"] = True
        result = session.send("Page.captureScreenshot", payload)
        data = result.get("data") if isinstance(result, dict) else None
        if not data:
            return False
        with open(dest, "wb") as handle:
            handle.write(base64.b64decode(data))
        return True
    except Exception:
        return False
    finally:
        try:
            session.detach()
        except Exception:
            pass

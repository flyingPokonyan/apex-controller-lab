"""Windows Chrome identity shared by EA/Steam register, probe, and Playwright.

One registration presents one browser: BitBrowser, the curl_cffi protocol
client, and captcha requests claim the same Chrome major on Windows. Language
and timezone follow the country of the selected exit rather than a fixed HK.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import os
import random
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional
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
_EA_SITE = {
    "HK": ("zh-hk", "zh_HK"),
    "US": ("en-us", "en_US"),
    "JP": ("ja", "ja_JP"),
}
EA_REGISTER_URL = "https://www.ea.com/zh-hk/register"
EA_JUNO_CREATE_URL = (
    "https://accounts.ea.com/connect/auth"
    "?display=junoWeb/create&response_type=code"
    "&redirect_uri=https://www.ea.com/login_check"
    "&locale=zh_HK&client_id=EADOTCOM-WEB-SERVER"
)


def ea_register_url() -> str:
    """Register page for this exit. Resolved when the request is made."""
    slug, _locale = _EA_SITE[current_country()]
    return f"https://www.ea.com/{slug}/register"


def ea_juno_create_url() -> str:
    _slug, locale = _EA_SITE[current_country()]
    return (
        "https://accounts.ea.com/connect/auth"
        "?display=junoWeb/create&response_type=code"
        "&redirect_uri=https://www.ea.com/login_check"
        f"&locale={locale}&client_id=EADOTCOM-WEB-SERVER"
    )


@dataclass(frozen=True)
class RegionLocale:
    accept_language: str
    steam_language: str
    steam_elang: str
    utc_offset: Optional[int]
    timezone_id: Optional[str]


# Steam ELanguage ids: 0 English, 7 Traditional Chinese, 10 Japanese.
# The US spans several zones, so its offset must come from the exit itself.
REGION_LOCALES = {
    "HK": RegionLocale(ACCEPT_LANGUAGE, "tchinese", "7", 8 * 3600, "Asia/Hong_Kong"),
    "US": RegionLocale("en-US,en;q=0.9", "english", "0", None, None),
    "JP": RegionLocale("ja,en-US;q=0.9,en;q=0.8", "japanese", "10", 9 * 3600, "Asia/Tokyo"),
}


def current_country() -> str:
    region = os.environ.get("EA_REGISTER_COUNTRY", "").strip().upper()
    return region if region in REGION_LOCALES else COUNTRY


def register_locale() -> RegionLocale:
    return REGION_LOCALES[current_country()]


def accept_language() -> str:
    return register_locale().accept_language


def exit_utc_offset(geo) -> Optional[int]:
    """Seconds east of UTC that a browser at this exit would report."""
    if geo is None:
        return None
    if getattr(geo, "utc_offset", None) is not None:
        return geo.utc_offset
    name = getattr(geo, "timezone", "")
    if not name:
        return None
    try:
        from zoneinfo import ZoneInfo

        offset = dt.datetime.now(ZoneInfo(name)).utcoffset()
    except Exception:
        # Windows Python has no IANA database unless tzdata is installed.
        return None
    return int(offset.total_seconds()) if offset is not None else None


def steam_timezone_cookie() -> Optional[str]:
    """Steam's own ``timezoneOffset`` value, or None when the exit's zone is unknown."""
    raw = os.environ.get("EA_REGISTER_UTC_OFFSET", "").strip()
    offset = int(raw) if re.fullmatch(r"-?\d{1,5}", raw) else register_locale().utc_offset
    return None if offset is None else f"{offset},0"


def _chrome_major() -> int:
    raw = os.environ.get("GAME_ACCOUNTS_BIT_CORE", "").strip()
    return int(raw) if raw.isdigit() else 130


# BitBrowser's kernel; every other client in the task claims this version too.
CHROME_MAJOR = _chrome_major()

_GREASE_CHARS = (" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_")
_GREASE_VERSIONS = ("8", "99", "24")
_BRAND_ORDERS = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))


def sec_ch_ua(major: int) -> str:
    """The brand list a real Chrome of this major sends; Chromium seeds it with the major."""
    grease = (
        f"Not{_GREASE_CHARS[major % 11]}A{_GREASE_CHARS[(major + 1) % 11]}Brand",
        _GREASE_VERSIONS[major % 3],
    )
    brands: list[tuple[str, str]] = [("", "")] * 3
    order = _BRAND_ORDERS[major % 6]
    brands[order[0]] = grease
    brands[order[1]] = ("Chromium", str(major))
    brands[order[2]] = ("Google Chrome", str(major))
    return ", ".join(f'"{name}";v="{version}"' for name, version in brands)


def impersonation_target(major: int, available: Iterable[str]) -> str:
    """Newest curl_cffi Chrome profile that is not newer than the claimed browser."""
    best: Optional[tuple[int, str]] = None
    for name in available:
        matched = re.fullmatch(r"chrome(\d+)a?", str(name))
        if not matched:
            continue
        version = int(matched.group(1))
        if version <= major and (best is None or version > best[0]):
            best = (version, str(name))
    return best[1] if best else "chrome"


def _curl_targets() -> list[str]:
    try:
        from curl_cffi.requests import BrowserType
    except ImportError:
        return []
    return [str(getattr(item, "value", item)) for item in BrowserType]


CHROME_IMPERSONATE = impersonation_target(CHROME_MAJOR, _curl_targets())
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
    f"Chrome/{CHROME_MAJOR}.0.0.0 Safari/537.36"
)
SEC_CH_UA = sec_ch_ua(CHROME_MAJOR)
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
# Real EA IDs come in many shapes. One fixed template (lowercase name_thing)
# made every new account look alike, so several common shapes are drawn.
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
    "tove", "vadim", "wynn", "adrian", "alden", "amara", "anton",
    "bianca", "camila", "carmen", "colin", "conor", "daria", "declan",
    "elias", "emil", "esme", "felix", "fiona", "gavin", "hugo", "imogen",
    "iris", "ivan", "jasper", "jonas", "julian", "kira", "lena", "levi",
    "lucia", "luka", "milo", "mira", "nadia", "nico", "nora", "owen",
    "paloma", "quinn", "rafael", "rory", "rowan", "selma", "simon",
    "talia", "theo", "tobias", "vera", "viktor", "wren", "yara", "zane",
    "zora", "abel", "boris", "clara", "dmitri", "edgar", "flora",
    "hector", "ines", "jakob", "kamil", "linus", "matteo", "nadine",
    "otto", "priya", "rasmus", "stefan", "tomas", "ursula", "vince",
)
_SURNAME_WORDS = (
    "holt", "marsh", "pryor", "quill", "rowe", "sato", "tanner", "vance",
    "wilde", "yates", "abbot", "baird", "cole", "dalton", "ellis", "finch",
    "grant", "hale", "ivers", "jensen", "keller", "lowe", "mercer", "nolan",
    "oakes", "parr", "reid", "shaw", "thorne", "voss", "walsh", "york",
    "ames", "blake", "crane", "doyle", "fenn", "gale", "hart", "irwin",
    "joyce", "kerr", "lyle", "moss", "nash", "orton", "pike", "stone",
    "trent", "vale", "ward", "berg", "dahl", "falk", "lund", "moller",
    "nyberg", "strand", "varga", "novak", "kovac", "horvat", "rossi",
    "conti", "moretti", "costa", "silva", "brandt", "fischer", "hoffman",
    "kruger", "lang", "meyer", "richter", "schulz", "wagner", "zimmer",
)
_ADJECTIVE_WORDS = (
    "quiet", "rapid", "amber", "lucky", "silent", "brave", "calm", "crimson",
    "dusty", "fuzzy", "golden", "hollow", "icy", "jolly", "keen", "lunar",
    "misty", "noble", "pale", "rusty", "salty", "tidy", "urban", "vivid",
    "wild", "cosmic", "frosty", "gentle", "humble", "lazy", "mellow",
    "nimble", "proud", "rogue", "sly", "stormy", "swift", "tiny", "witty",
    "bold", "fancy", "grim", "neon", "polar", "scarlet", "sunny", "velvet",
)
_NOUN_WORDS = (
    "juniper", "heron", "marigold", "cedar", "pebble", "starling",
    "birch", "coral", "cactus", "harbor", "sparrow", "thistle",
    "fennel", "quartz", "lantern", "compass", "fiddle", "bramble",
    "sorrel", "yarrow", "saffron", "paprika", "nutmeg", "anise",
    "cairn", "fjord", "atoll", "mesa", "butte", "heath", "moor",
    "kiln", "cello", "banjo", "loom", "keel", "dune", "glen", "cove",
    "otter", "falcon", "lynx", "raven", "badger", "comet", "ember",
    "fable", "glacier", "jigsaw", "kettle", "lagoon", "nomad", "orchid",
    "pixel", "quiver", "rocket", "saber", "tundra", "vortex", "walrus",
    "zephyr", "anchor", "bandit", "cinder", "ferret", "gopher", "hornet",
    "iguana", "jackal", "koala", "lemur", "magpie", "narwhal", "oriole",
    "panda", "quokka", "robin", "salmon", "toucan", "wombat", "mango",
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
_SURNAMES = _clean_words(_SURNAME_WORDS)
_ADJECTIVES = _clean_words(_ADJECTIVE_WORDS)
_NOUNS = _clean_words(_NOUN_WORDS)
# The previous single template drew from these two lists.
_LIKED_THINGS = _NOUNS


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


def _cap(word: str) -> str:
    return word[:1].upper() + word[1:]


def _digits(low: int, high: int) -> str:
    # Birth years are the crowded default (emma2001 was already taken).
    while True:
        value = random.randint(low, high)
        if not 1900 <= value <= 2099:
            return str(value)


# (weight, builder). Shapes seen on real gamer tags: FirstLast, name plus
# digits, AdjectiveNoun, initials, and the occasional underscore.
_ID_SHAPES = (
    (20, lambda: _cap(random.choice(_GIVEN_NAMES)) + _cap(random.choice(_SURNAMES))),
    (12, lambda: random.choice(_GIVEN_NAMES) + random.choice(_SURNAMES) + _digits(10, 99)),
    (12, lambda: random.choice((str.lower, _cap))(random.choice(_GIVEN_NAMES)) + _digits(100, 9999)),
    (14, lambda: _cap(random.choice(_ADJECTIVES)) + _cap(random.choice(_NOUNS))),
    (8, lambda: random.choice(_ADJECTIVES) + random.choice(_NOUNS) + _digits(10, 999)),
    (10, lambda: _cap(random.choice(_GIVEN_NAMES)) + random.choice(_SURNAMES)[0].upper() + _digits(10, 99)),
    (8, lambda: random.choice(_NOUNS) + _digits(100, 9999)),
    (8, lambda: f"{random.choice(_GIVEN_NAMES)}_{random.choice(_NOUNS)}"),
    (4, lambda: f"{_cap(random.choice(_GIVEN_NAMES))}_{_cap(random.choice(_SURNAMES))}"),
)


def rand_gamer_id() -> str:
    """Pronounceable handle. ASCII only; Juno rejects CJK in originId."""
    weights = [weight for weight, _build in _ID_SHAPES]
    builders = [build for _weight, build in _ID_SHAPES]
    for _ in range(60):
        candidate = _valid_ea_id(random.choices(builders, weights=weights)[0]())
        if candidate:
            return candidate
    return f"{random.choice(_GIVEN_NAMES)}{random.choice(_SURNAMES)}{_digits(10, 99)}"[:_EA_ID_MAX]


def pick_register_country() -> str:
    forced = os.environ.get("EA_REGISTER_COUNTRY", COUNTRY).strip().upper()
    return forced or COUNTRY


def chrome_headers(*, navigate: bool = True) -> dict[str, str]:
    headers = {
        "User-Agent": UA,
        "Accept-Language": accept_language(),
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
    locale = register_locale()
    options: dict[str, Any] = {
        "user_agent": UA,
        "locale": locale.accept_language.split(",")[0],
        "color_scheme": "light",
        "viewport": viewport_for(seed),
        "extra_http_headers": {"Accept-Language": locale.accept_language},
    }
    timezone_id = os.environ.get("EA_REGISTER_TIMEZONE", "").strip() or locale.timezone_id
    if timezone_id:
        options["timezone_id"] = timezone_id
    if current_country() == COUNTRY:
        options["geolocation"] = geolocation_for(seed)
        options["permissions"] = ["geolocation"]
    return options


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

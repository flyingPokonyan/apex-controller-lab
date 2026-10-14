"""Proxy exit lookup for the desktop executor, independent of Forge services."""
import os
import requests
from urllib.parse import urlsplit, urlunsplit
from .proxy import parse_proxy_exit


def probe_exit(proxy_url):
    # Match Forge's --socks5-hostname path. Local DNS may return a Clash
    # fake-IP which a residential SOCKS exit cannot route.
    parsed = urlsplit(proxy_url)
    if parsed.scheme == 'socks5':
        proxy_url = urlunsplit(parsed._replace(scheme='socks5h'))
    targets = [os.environ.get("GAME_ACCOUNTS_PROXY_GEO_URL") or "https://ipinfo.io/json",
               "http://ip-api.com/json/?fields=status,countryCode,query,as,org,isp,timezone"]
    errors = []
    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {"http": proxy_url, "https": proxy_url}
        for target in dict.fromkeys(targets):
            try:
                response = session.get(target, timeout=float(os.environ.get("GAME_ACCOUNTS_PROXY_PROBE_TIMEOUT", "20")))
                response.raise_for_status()
                geo = parse_proxy_exit(response.text)
                if geo.country:
                    return geo, None
                errors.append(f"{urlsplit(target).hostname}: 未返回国家代码")
            except requests.RequestException as exc:
                # Exception text can contain authenticated proxy URLs.
                status = getattr(getattr(exc, 'response', None), 'status_code', None)
                detail = f'HTTP {status}' if status else type(exc).__name__
                errors.append(f"{urlsplit(target).hostname}: {detail}")
    return None, "代理出口探测失败（" + "；".join(errors) + "）"


def probe_exit_country(proxy_url):
    geo, error = probe_exit(proxy_url)
    return geo.country if geo else None, error

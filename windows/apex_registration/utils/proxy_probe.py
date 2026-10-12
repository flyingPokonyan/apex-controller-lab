"""Proxy exit lookup for the desktop executor, independent of Forge services."""
import os
import requests
from .proxy import parse_proxy_exit


def probe_exit_country(proxy_url):
    targets = [os.environ.get("GAME_ACCOUNTS_PROXY_GEO_URL") or "https://ipinfo.io/json",
               "http://ip-api.com/json/?fields=status,countryCode,query"]
    with requests.Session() as session:
        session.trust_env = False
        session.proxies = {"http": proxy_url, "https": proxy_url}
        for target in dict.fromkeys(targets):
            try:
                response = session.get(target, timeout=float(os.environ.get("GAME_ACCOUNTS_PROXY_PROBE_TIMEOUT", "20")))
                response.raise_for_status()
                country = parse_proxy_exit(response.text).country
                if country:
                    return country, None
            except requests.RequestException:
                continue
    return None, "代理出口探测失败或没有返回国家代码"

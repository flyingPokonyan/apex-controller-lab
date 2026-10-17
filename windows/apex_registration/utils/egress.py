"""Public registration network facts; never persist a proxy URL or credentials."""
import ipaddress
import json
import re
from pathlib import Path


def public_exit(raw):
    if not isinstance(raw, dict):
        return None
    provider = raw.get('provider') if raw.get('provider') in ('711', '1024', 'resin', 'direct', 'clash') else ''
    try:
        ip = str(ipaddress.ip_address(str(raw.get('ip') or '')))
    except ValueError:
        ip = ''
    country = str(raw.get('country') or '').upper()
    country = country if re.fullmatch('[A-Z]{2}', country) else ''
    asn = raw.get('asn')
    asn = asn if type(asn) is int and 0 < asn <= 4294967295 else None
    org = str(raw.get('org') or '')[:200]
    if re.search(r'://|@|[\x00-\x1f]|(?i:password|token|secret|authorization)\s*[:=]', org):
        org = ''
    attempt = raw.get('attempt', 1)
    if type(attempt) is not int or not 1 <= attempt <= 100 or not (provider or ip or country):
        return None
    fact = dict(attempt=attempt, provider=provider, ip=ip, country=country, asn=asn, org=org)
    # The Clash node an EA registration went through; Forge prefers it for this account's games.
    node = raw.get('node')
    if provider == 'clash' and isinstance(node, str) and 0 < len(node) <= 120 \
            and not re.search(r'://|@|[\x00-\x1f]|(?i:password|token|secret|authorization)\s*[:=]', node):
        fact['node'] = node
    return fact


def exit_fields(directory):
    """Used by child uploads and by the supervisor after a killed child."""
    directory = Path(directory)
    facts = []
    for name in ('proxy_exits.json', 'proxy_exit.json'):
        try:
            raw = json.loads((directory / name).read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        values = raw if isinstance(raw, list) else [raw]
        facts.extend(item for value in values[:100] if (item := public_exit(value)))
    by_attempt = {item['attempt']: item for item in facts}
    exits = sorted(by_attempt.values(), key=lambda item: item['attempt'])
    result = {'exit': exits[-1], 'exits': exits} if exits else {}
    try:
        creation = public_exit(json.loads((directory / 'ea_creation_exit.json').read_text(encoding='utf-8')))
    except (OSError, ValueError):
        creation = None
    if creation:
        result['creationExit'] = creation
    return result

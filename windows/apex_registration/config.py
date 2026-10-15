"""Machine-local registration settings, kept separate from Forge device identity."""
import json
from pathlib import Path
from urllib.parse import urlparse

DEFAULTS = {
    'bitbrowserUrl': 'http://127.0.0.1:54345',
    'bitbrowserApiKey': '',
    'forgeProxy': 'direct',
    'timeoutSeconds': 1800,
    # Newest Chrome kernel already downloaded in this BitBrowser; empty keeps 130.
    'bitbrowserCore': '',
}


def config_path(runner_config=None):
    directory = Path(runner_config).parent if runner_config else Path(__file__).resolve().parents[1]
    return directory / 'registration.private.json'


def load_config(path):
    data = dict(DEFAULTS)
    if Path(path).exists():
        try:
            supplied = json.loads(Path(path).read_text(encoding='utf-8-sig'))
        except (OSError, ValueError) as exc:
            raise ValueError('registration.private.json 无法读取或 JSON 格式有误') from exc
        if not isinstance(supplied, dict):
            raise ValueError('registration.private.json 必须是 JSON 对象')
        data.update(supplied)
    for key in ('bitbrowserUrl', 'bitbrowserApiKey', 'forgeProxy', 'bitbrowserCore'):
        if not isinstance(data[key], str):
            raise ValueError('注册配置字段类型错误：' + key)
        data[key] = data[key].strip()
    api = urlparse(data['bitbrowserUrl'])
    if api.scheme not in ('http', 'https') or not api.hostname:
        raise ValueError('bitbrowserUrl 必须为完整的 HTTP 接口地址')
    if data['forgeProxy'] and data['forgeProxy'].lower() != 'direct':
        transport = urlparse(data['forgeProxy'])
        if transport.scheme not in ('http', 'https') or not transport.hostname:
            raise ValueError('forgeProxy 应为 direct 或完整的 HTTP 代理地址')
    if data['bitbrowserCore'] and not (data['bitbrowserCore'].isdigit() and 100 <= int(data['bitbrowserCore']) <= 999):
        raise ValueError('bitbrowserCore 应为 BitBrowser 内核主版本号，例如 "136"')
    timeout = data['timeoutSeconds']
    if type(timeout) not in (int, float) or not 60 <= timeout <= 14400:
        raise ValueError('timeoutSeconds 应在 60 到 14400 秒之间')
    return data


def environment(data):
    values = {
        'GAME_ACCOUNTS_BIT_API': data['bitbrowserUrl'],
        'GAME_ACCOUNTS_BIT_API_KEY': data['bitbrowserApiKey'],
        'FORGE_PROXY': data['forgeProxy'] or 'direct',
    }
    if data.get('bitbrowserCore'):
        values['GAME_ACCOUNTS_BIT_CORE'] = data['bitbrowserCore']
    return values

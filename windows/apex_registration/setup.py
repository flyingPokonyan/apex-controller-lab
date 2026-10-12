"""One-time interactive setup for machine-local registration settings."""
import getpass
import json
import os
from pathlib import Path
import tempfile
from .config import config_path, load_config


def save_config(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    path = config_path()
    data = load_config(path)
    print('注册设置只保存在本机，更新代码和下载 Runner 身份配置不会覆盖它。')
    for field, label, secret in (
        ('bitbrowserUrl', 'BitBrowser Local API 地址', False),
        ('bitbrowserApiKey', 'BitBrowser API Token（未启用鉴权可留空）', True),
        ('proxyUrl', '注册代理完整 URL', True),
        ('steamCaptchaKey', 'Steam Captchaly Key（有 Steam 库存时可留空）', True),
        ('forgeProxy', '访问 Forge 的代理（direct 表示直连）', False),
    ):
        current = '已配置' if secret and data[field] else str(data[field])
        prompt = f'{label} [{current}]，留空保持，输入 - 清空：'
        value = (getpass.getpass(prompt) if secret else input(prompt)).strip()
        if value:
            data[field] = '' if value == '-' else value
    # Validate before replacing the user's existing settings.
    with tempfile.TemporaryDirectory() as directory:
        draft = Path(directory) / 'registration.private.json'
        save_config(draft, data)
        load_config(draft)
    save_config(path, data)
    print(f'已保存：{path}')
    print('重启 Runner 后生效；到 Forge 开启“允许注册”。' if data['proxyUrl'] else '尚未配置注册代理；Runner 将继续游戏，不领取注册任务。')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print('注册配置未保存：' + str(exc))
        raise SystemExit(1)

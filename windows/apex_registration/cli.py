"""Register a Forge-assigned desktop task using the Controller-owned runtime."""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse
import uuid


ROOT = Path(__file__).resolve().parents[1]
FORGE_API_URL = ""
LOCAL_PROXY = "direct"
FORGE_USERNAME = ""
FORGE_PASSWORD = ""
CAPTCHALY_KEY = ""


def _refuse_unless_windows() -> None:
    if os.name == "nt" or sys.platform == "darwin":
        return
    raise RuntimeError("注册执行器需要 Windows 桌面；macOS 仅供本地调试")


def acquire_game_lock():
    """Manual registration shares the Controller lock; managed children inherit its owner."""
    if os.name != 'nt' or os.environ.get('APEX_REGISTRATION_MANAGED') == '1':
        return None
    default = ROOT / 'runs' / 'play.lock'
    path = Path(os.environ.get('APEX_PLAY_LOCK') or default)
    import msvcrt
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open('a+b')
    try:
        handle.seek(0)
        if not handle.read(1):
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise RuntimeError('这台机器正在执行游戏或另一轮注册，请先结束当前任务')
    return handle


def _use_local_proxy() -> None:
    """Apply the configured Forge transport independently from the signup proxy."""
    proxy = os.environ.get("FORGE_PROXY", "").strip() or LOCAL_PROXY
    if proxy.lower() == 'direct':
        urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
        return
    urllib.request.install_opener(
        urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        )
    )


def _forge_headers(token: str = "") -> dict[str, str]:
    # Cloudflare rejects urllib's default client. A normal browser signature
    # is what the site allows through the local proxy.
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/136.0.0.0 Safari/537.36"
        ),
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def checkout_bundle(api_base: str, token: str, request_id: str) -> dict:
    """Take an EA mailbox plus Steam inventory or a Steam mailbox."""
    url = api_base.rstrip("/") + "/api/game-accounts/ea/desktop-registration/tasks"
    runner_task = os.environ.get('FORGE_RUNNER_TASK_ID', '').strip()
    if runner_task:
        url = api_base.rstrip('/') + '/v1/runner/registration-tasks/' + runner_task
    headers = _forge_headers(token)
    headers["Idempotency-Key"] = request_id
    request = urllib.request.Request(
        url,
        data=None if runner_task else json.dumps({"request_id": request_id}).encode(),
        method="GET" if runner_task else "POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"取号失败 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise RuntimeError(f"取号时连不上 Forge: {reason}") from exc
    if isinstance(payload, dict) and payload.get("state") == "succeeded":
        return payload
    ea_mailbox = payload.get("ea_mailbox") if isinstance(payload, dict) else None
    steam = payload.get("steam") if isinstance(payload, dict) else None
    steam_mailbox = payload.get("steam_mailbox") if isinstance(payload, dict) else None
    if not isinstance(ea_mailbox, dict) or not ea_mailbox.get("refresh_token"):
        raise RuntimeError("取号响应里没有 EA 邮箱")
    if not isinstance(steam, dict) or steam.get("source") not in {"inventory", "register"}:
        raise RuntimeError("取号响应里没有 Steam 来源")
    if not isinstance(steam_mailbox, dict) or not steam_mailbox.get("refresh_token"):
        raise RuntimeError("取号响应里没有 Steam 收码邮箱")
    if steam["source"] == "inventory" and not steam.get("username"):
        raise RuntimeError("取号响应里的闲置 Steam 没有用户名")
    if steam["source"] == "inventory" and not steam.get("password"):
        raise RuntimeError("取号响应里的闲置 Steam 没有密码")
    return payload


def write_private_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_mailbox_pool(directory: Path, mailboxes: list[dict]) -> Path:
    path = directory / "mailboxes.json"
    write_private_json(path, mailboxes)
    return path


def write_steam_account(directory: Path, username: str, password: str, email: str) -> Path:
    path = directory / "steam_account.txt"
    path.write_text(f"{username}----{password}----{email}\n", encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return path


def apply_bundle(output_dir: Path, payload: dict) -> str:
    """Write the pool and Steam file. Return ``inventory`` or ``register``."""
    runtime = payload.get('registration')
    if runtime is not None:
        if not isinstance(runtime, dict) or not runtime.get('proxyUrl'):
            raise RuntimeError('Forge 下发的注册代理不完整')
        # This task's assigned vendor takes precedence over machine overrides.
        os.environ['EA_PROXY'] = str(runtime['proxyUrl'])
        os.environ['CAPTCHALY_KEY'] = str(runtime.get('steamCaptchaKey') or '')
    ea_mailbox = payload["ea_mailbox"]
    steam_mailbox = payload["steam_mailbox"]
    mailboxes = [ea_mailbox]
    if str(steam_mailbox["email"]).lower() != str(ea_mailbox["email"]).lower():
        mailboxes.append(steam_mailbox)
    pool = write_mailbox_pool(output_dir, mailboxes)
    os.environ["ACCOUNT_FLOW_EMAIL_POOL"] = str(pool)
    os.environ["EA_EMAIL"] = str(ea_mailbox["email"])
    os.environ.pop("SKIP_STEAM_LINK", None)
    os.environ.pop("SKIP_2FA_SETUP", None)
    steam = payload["steam"]
    if steam["source"] == "inventory":
        steam_path = write_steam_account(
            output_dir, steam["username"], steam["password"], steam["email"]
        )
        os.environ["STEAM_ACCOUNT_FILE"] = str(steam_path)
    else:
        os.environ["STEAM_EMAIL"] = str(steam_mailbox["email"])
        os.environ.pop("STEAM_ACCOUNT_FILE", None)
    write_private_json(output_dir / "windows_trial.json", payload)
    return steam["source"]


def forge_api_url() -> str:
    return os.environ.get("FORGE_API_URL", "").strip() or FORGE_API_URL


def login_token(api_base: str) -> str:
    """Use the configured Forge login to obtain a token."""
    url = api_base.rstrip("/") + "/api/auth/login"
    username = os.environ.get("FORGE_USERNAME", "").strip() or FORGE_USERNAME
    password = os.environ.get("FORGE_PASSWORD", "").strip() or FORGE_PASSWORD
    if not password:
        raise RuntimeError("请设置 FORGE_TOKEN，或 FORGE_USERNAME / FORGE_PASSWORD。")
    body = json.dumps({"username": username, "password": password}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers=_forge_headers(),
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Forge 登录失败 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise RuntimeError(f"连不上 Forge {api_base}: {reason}") from exc
    token = payload.get("token") if isinstance(payload, dict) else None
    if not token:
        raise RuntimeError("Forge 登录响应里没有令牌")
    return str(token)


def prepare_bundle(output_dir: Path) -> str:
    """Reuse a saved checkout, otherwise take one full-setup pair from Forge."""
    _use_local_proxy()
    saved = output_dir / "windows_trial.json"
    if saved.is_file():
        payload = json.loads(saved.read_text(encoding="utf-8"))
        source = apply_bundle(output_dir, payload)
        sys.stderr.write("沿用本机已取的号，不再向 Forge 取新号。\n")
        return source
    api_base = forge_api_url()
    request_file = output_dir / "forge_request.json"
    if request_file.is_file():
        pending = json.loads(request_file.read_text(encoding="utf-8"))
        if pending["api_url"] != api_base:
            raise RuntimeError("本机待领取任务属于另一个 Forge 地址，请使用原地址重试。")
    else:
        pending = {"request_id": str(uuid.uuid4()), "api_url": api_base}
        write_private_json(request_file, pending)
    token = os.environ.get("FORGE_TOKEN", "").strip() or login_token(api_base)
    payload = checkout_bundle(api_base, token, pending["request_id"])
    if payload.get("state") == "succeeded":
        write_private_json(output_dir / "forge_receipt.json", payload)
        raise RuntimeError("这项注册任务已回传完成，请使用 --new 开始下一项。")
    payload["forge_api_url"] = api_base
    source = apply_bundle(output_dir, payload)
    steam = payload["steam"]
    if source == "inventory":
        detail = f"闲置 Steam {steam['username']}"
    else:
        detail = f"Steam 邮箱 {payload['steam_mailbox']['email']}"
    sys.stderr.write(
        f"已取 EA 邮箱 {payload['ea_mailbox']['email']}，{detail}。"
        "完成步骤会回传 Forge，原始结果同时保留在本机。\n"
    )
    return source


def register_steam_then_record(output_dir: Path) -> None:
    """Register Steam with the existing protocol script, then hand it to full setup."""
    existing = output_dir / "steam_account.txt"
    if existing.is_file() and "----" in existing.read_text(encoding="utf-8"):
        os.environ["STEAM_ACCOUNT_FILE"] = str(existing)
        sys.stderr.write("本机已有 Steam 账号文件，跳过 Steam 注册。\n")
        return
    if not os.environ.get("CAPTCHALY_KEY", "").strip():
        raise RuntimeError(
            "这次需要新注册 Steam，但没有 CAPTCHALY_KEY。号已经取走，补上密钥后重跑即可，不会再取新号。"
        )
    os.environ["STEAM_PROXY"] = os.environ["EA_PROXY"]
    os.environ["STEAM_CAPTCHA_PROVIDER"] = "captchaly"
    from .flows.steam import try_steam_once as module
    capture_mailbox_refresh(module, output_dir)
    code = module.main()
    result_path = output_dir / "steam_register_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
    if code != 0 or not result.get("success") or not result.get("username") or not result.get("password"):
        raise RuntimeError("Steam 注册没有完成，EA 还没开始。")
    steam_path = write_steam_account(
        output_dir,
        str(result["username"]),
        str(result["password"]),
        str(result.get("email") or os.environ["STEAM_EMAIL"]),
    )
    os.environ["STEAM_ACCOUNT_FILE"] = str(steam_path)


def read_json(directory: Path, name: str) -> dict:
    path = directory / name
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def sync_result(output_dir: Path, *, finished: bool, exit_code: int | None = None) -> dict:
    """Persist the upload first; retry only the upload if Forge is unavailable."""
    bundle = read_json(output_dir, "windows_trial.json")
    task_id = bundle.get("task_id") or bundle.get("hold_task_id")
    if not task_id:
        raise RuntimeError("本地目录没有 Forge 取号任务，不能回传。")
    result = read_json(output_dir, "ea_full_setup_result.json")
    proto = read_json(output_dir, "protocol_register_result.json")
    payload = {"result": result, "steam_result": read_json(output_dir, "steam_register_result.json"),
               "register_engine": proto.get("register_engine"), "finished": finished,
               "exit_code": exit_code, "steam_hold_task_id": bundle.get("steam_hold_task_id"),
               "mailbox_updates": bundle.get("mailbox_updates", [])}
    write_private_json(output_dir / "forge_pending_result.json", payload)
    api_base = bundle.get("forge_api_url") or forge_api_url()
    _use_local_proxy()
    token = os.environ.get("FORGE_TOKEN", "").strip() or login_token(api_base)
    endpoint = '/v1/runner/registration-tasks' if os.environ.get('FORGE_RUNNER_TASK_ID') else '/api/game-accounts/ea/desktop-registration/tasks'
    req = urllib.request.Request(api_base.rstrip("/") + f"{endpoint}/{task_id}/result",
        data=json.dumps(payload, ensure_ascii=False).encode(), method="PUT", headers=_forge_headers(token))
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            receipt = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Forge 回传失败 HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
        raise RuntimeError("Forge 暂时无法接收结果，资料已保存；用 --sync-only 补交。") from exc
    write_private_json(output_dir / "forge_receipt.json", receipt)
    if bundle.get("mailbox_updates"):
        bundle.pop("mailbox_updates", None)
        write_private_json(output_dir / "windows_trial.json", bundle)
    (output_dir / "forge_pending_result.json").unlink(missing_ok=True)
    return receipt


def try_sync_result(output_dir: Path, *, finished: bool, exit_code: int | None = None) -> bool:
    try:
        receipt = sync_result(output_dir, finished=finished, exit_code=exit_code)
        sys.stderr.write(f"Forge 已保存任务 {receipt['task_id']}：{receipt['state']}。\n")
        if finished and exit_code == 0 and receipt["state"] != "succeeded":
            sys.stderr.write("Forge 尚未确认完整成功，已保存为待复核。\n")
            return False
        return True
    except (RuntimeError, ValueError, OSError) as exc:
        sys.stderr.write(str(exc) + "\n")
        return False


def capture_mailbox_refresh(module, output_dir: Path) -> None:
    """Record a rotated Graph refresh token from either mail helper name."""
    name = next((item for item in ("graph_access_token", "graph_token") if hasattr(module, item)), "")
    if not name:
        return
    original = getattr(module, name)

    def exchange(record):
        old = record.get("refresh_token")
        token = original(record)
        if record.get("refresh_token") and record["refresh_token"] != old:
            bundle = read_json(output_dir, "windows_trial.json")
            updates = bundle.setdefault("mailbox_updates", [])
            for key in ("ea_mailbox", "steam_mailbox"):
                mailbox = bundle[key]
                if mailbox["email"].casefold() == str(record.get("email", "")).casefold():
                    prior = next((u for u in updates if u["id"] == mailbox["id"]), None)
                    if prior:
                        prior["refresh_token"] = record["refresh_token"]
                    else:
                        updates.append({"id":mailbox["id"],"previous_refresh_token":old,"refresh_token":record["refresh_token"]})
                    mailbox["refresh_token"] = record["refresh_token"]
            write_private_json(output_dir / "windows_trial.json", bundle)
            write_mailbox_pool(output_dir, [bundle["ea_mailbox"], bundle["steam_mailbox"]])
        return token

    setattr(module, name, exchange)


def finished_locally(output_dir: Path) -> bool:
    result = read_json(output_dir, "ea_full_setup_result.json")
    return bool(result.get("stage") == "complete" and not result.get("error_code") and not result.get("needs_review")
                and all(result.get(k) is True for k in ("ea_registered", "account_login_verified", "account_profile_completed", "steam_link", "totp"))
                and result.get("totp_secret"))


def select_output_dir(args) -> Path:
    configured = args.output_dir or os.environ.get("ACCOUNT_FLOW_OUTPUT_DIR", "").strip()
    if configured:
        directory = Path(configured)
        if args.new and (directory / "windows_trial.json").exists():
            raise RuntimeError("--new 需要一个新的输出目录，已有资料会保留。")
        return directory
    root = Path(os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()) / "apex-controller-registrations"
    current = read_json(root, "current.json")
    if args.new or not current:
        current = {"directory": str(root / str(uuid.uuid4()))}
        write_private_json(root / "current.json", current)
    return Path(current["directory"])


def write_account_card(output_dir: Path) -> Path | None:
    """Copy the finished EA login and the 2FA secret into one local file."""
    result_path = output_dir / "ea_full_setup_result.json"
    if not result_path.is_file():
        return None
    result = json.loads(result_path.read_text(encoding="utf-8"))
    link_path = output_dir / "pw_link_2fa_result.json"
    link = json.loads(link_path.read_text(encoding="utf-8")) if link_path.is_file() else {}
    secret = result.get("totp_secret") or link.get("totp_secret")
    card = {
        "ea_email": result.get("ea_email") or link.get("ea_email"),
        "ea_password": result.get("ea_password"),
        "ea_origin_id": result.get("ea_origin_id"),
        "totp_secret": secret,
        "backup_codes": result.get("backup_codes") or link.get("backup_codes") or [],
        "steam_user": result.get("steam_user") or link.get("steam_user"),
        "steam_link": bool(result.get("steam_link") or link.get("steam_link")),
        "totp": bool(result.get("totp") or link.get("totp") or secret),
    }
    path = output_dir / "windows_account.json"
    write_private_json(path, card)
    return path


WINDOWS_PROXY_REGIONS = ("HK", "US", "JP")
RESIN_PLATFORMS = {"HK": "YtooHK", "US": "YtooUS", "JP": "YtooJP"}


def uses_resin_proxy(proxy_url: str) -> bool:
    """True when the URL is the Resin forward proxy, not the 1024 gateway."""
    parsed = urlparse((proxy_url or "").strip())
    return parsed.port in {2260, 12260}


def proxy_vendor(proxy_url: str) -> str:
    host = (urlparse((proxy_url or "").strip()).hostname or "").lower()
    if "711proxy" in host or "trustsource.live" in host:
        return "711proxy"
    if "1024proxy" in host:
        return "1024proxy"
    if uses_resin_proxy(proxy_url):
        return "resin"
    return host or "proxy"


def configured_proxy() -> str:
    proxy = (os.environ.get("EA_PROXY") or os.environ.get("GAME_ACCOUNTS_REGISTER_PROXY") or "").strip()
    if not proxy:
        raise RuntimeError("Forge 尚未下发注册代理，请确认两端已更新")
    return proxy


def choose_region_proxy(proxy_url: str, seed: str, *, probe, attempts: int = 6) -> str:
    """Pick HK, US, or JP and keep the exit only when the country matches.

    ``probe`` returns ``(country_code, error)``. A 1024 region label is not
    trusted until the live exit reports that same country. ``EA_REGISTER_REGION``
    pins one of HK, US, or JP instead of drawing.
    """
    from apex_registration.utils.proxy import with_sticky_session

    pinned = os.environ.get("EA_REGISTER_REGION", "").strip().upper()
    if pinned and pinned not in WINDOWS_PROXY_REGIONS:
        raise RuntimeError(f"不支持的注册国家 {pinned}")
    errors: list[str] = []
    for attempt in range(max(1, attempts)):
        region = pinned or random.choice(WINDOWS_PROXY_REGIONS)
        sticky = with_sticky_session(
            proxy_url,
            f"{seed}:{region}:{attempt}:{random.randrange(1 << 16)}",
            region=region,
        )
        country, error = probe(sticky)
        seen = (country or "").strip().upper()
        if seen == region:
            os.environ["EA_REGISTER_COUNTRY"] = region
            return sticky
        errors.append(f"{region}=>{seen or error or 'unknown'}")
    raise RuntimeError("注册出口不在抽中的国家：" + "；".join(errors))


def choose_resin_proxy(proxy_url: str, seed: str, *, probe, attempts: int = 6) -> str:
    """Pick a Ytoo HK, US, or JP platform and keep it when the exit matches.

    Resin selects the node from the platform name. The account suffix keeps
    that node sticky for this registration. ``probe`` returns
    ``(country_code, error)``.
    """
    from .utils.proxy import resin_task_proxy as _resin_task_proxy

    errors: list[str] = []
    for attempt in range(max(1, attempts)):
        region = random.choice(WINDOWS_PROXY_REGIONS)
        task_id = f"ea{region.lower()}{attempt}{random.randrange(1 << 20):x}"
        sticky = _resin_task_proxy(
            proxy_url,
            task_id=task_id,
            platform=RESIN_PLATFORMS[region],
        )
        country, error = probe(sticky)
        seen = (country or "").strip().upper()
        if seen == region:
            os.environ["EA_REGISTER_COUNTRY"] = region
            from apex_registration.utils.proxy import public_resin_proxy

            return public_resin_proxy(sticky)
        errors.append(f"{RESIN_PLATFORMS[region]}=>{seen or error or 'unknown'}")
    raise RuntimeError("Resin 出口不在抽中的国家：" + "；".join(errors))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="领取 Forge 注册任务、在桌面注册并回传")
    parser.add_argument("--output-dir", help="已有测试目录或任务输出目录")
    parser.add_argument("--sync-only", action="store_true", help="只回传已有结果，不启动浏览器")
    parser.add_argument("--new", action="store_true", help="创建下一项注册任务，保留旧目录")
    parser.add_argument("--resume", action="store_true", help="使用已保存 EA 凭据继续关联和 2FA")
    args = parser.parse_args(argv)
    # Managed children receive a validated snapshot from their parent.
    if os.environ.get('APEX_REGISTRATION_MANAGED') != '1':
        from .config import config_path, load_config, environment
        for key, value in environment(load_config(config_path())).items():
            os.environ.setdefault(key, value)
    try:
        output_dir = select_output_dir(args)
    except RuntimeError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    if args.sync_only or finished_locally(output_dir):
        complete = finished_locally(output_dir)
        return 0 if try_sync_result(output_dir, finished=True, exit_code=0 if complete else 1) else 3
    previous = read_json(output_dir, "ea_full_setup_result.json")
    if previous.get("ea_creation_submitted") or previous.get("ea_registered"):
        if not args.resume or not previous.get("account_login_verified"):
            sys.stderr.write("本机已有 EA 创建记录。先用 --sync-only 保存成果；已复核的 EA 可用 --resume 继续。\n")
            return 2
        write_private_json(output_dir / "ea_previous_result.json", previous)
        os.environ.update(SKIP_EA_REGISTER="1", EA_PASSWORD=previous["ea_password"], EA_ORIGIN_ID=previous["ea_origin_id"])
    else:
        os.environ.pop("SKIP_EA_REGISTER", None)
    _refuse_unless_windows()
    execution_lock = acquire_game_lock()  # Keep the handle alive until this invocation returns.
    os.environ["GAME_ACCOUNTS_ROXY_HOST"] = "windows"
    # The shared flow adapter uses the local BitBrowser in managed mode.
    # Default is BitBrowser. Set chrome-incognito to open Google Chrome
    # with a clean window and no browser proxy.
    os.environ.setdefault("GAME_ACCOUNTS_DESKTOP_BROWSER", "bitbrowser")
    os.environ.setdefault("GAME_ACCOUNTS_BIT_API", "http://127.0.0.1:54345")
    os.environ.setdefault("GAME_ACCOUNTS_EA_REGISTER_ENGINE", "roxybrowser")
    # Desktop Roxy listens on the official port. 50100 is the Linux sidecar.
    os.environ.setdefault("GAME_ACCOUNTS_ROXY_API", "http://127.0.0.1:50000")
    os.environ.setdefault("CAPTCHALY_KEY", CAPTCHALY_KEY)
    os.environ.setdefault("GAME_ACCOUNTS_EMAIL_WAIT_SECONDS", "180")
    os.environ.setdefault(
        "NO_PROXY",
        "login.microsoftonline.com,graph.microsoft.com,login.live.com",
    )
    os.environ["ACCOUNT_FLOW_OUTPUT_DIR"] = str(output_dir)

    from apex_registration.utils.bitbrowser import BitBrowserClient, uses_bitbrowser

    desktop = os.environ.get("GAME_ACCOUNTS_DESKTOP_BROWSER", "").strip().lower()
    if desktop in {"chrome", "chrome-incognito", "incognito"}:
        client = None
    elif uses_bitbrowser():
        client = BitBrowserClient()
        if not client.health():
            sys.stderr.write(
                "本机 BitBrowser 接口不可达。先打开 BitBrowser，并确认 API 在 "
                f"{client.base_url}。\n"
            )
            return 2
    else:
        from apex_registration.utils.roxybrowser import RoxyBrowserClient

        client = RoxyBrowserClient()
        if not client.health():
            sys.stderr.write(
                "本机 Roxy Local API 不可达。先打开桌面版 Roxy，登录，并确认 API 在 "
                f"{client.base_url}。\n"
            )
            return 2

    try:
        source = prepare_bundle(output_dir)
        os.environ["EA_PROXY"] = configured_proxy()
        from .utils.proxy_probe import probe_exit_country as _probe_exit_country

        chooser = choose_resin_proxy if uses_resin_proxy(os.environ["EA_PROXY"]) else choose_region_proxy
        sticky = chooser(
            os.environ["EA_PROXY"],
            os.environ.get("EA_EMAIL", "windows"),
            probe=_probe_exit_country,
        )
        os.environ["EA_PROXY"] = sticky
        os.environ["STEAM_PROXY"] = sticky
        if desktop in {"chrome", "chrome-incognito", "incognito"}:
            # The incognito window has no proxy, so do not force the
            # proxy's country onto the EA form.
            os.environ["EA_REGISTER_COUNTRY"] = "AUTO"
        sys.stderr.write(
            f"本次注册国家 {os.environ.get('EA_REGISTER_COUNTRY', '')}，"
            f"代理 {proxy_vendor(os.environ.get('EA_PROXY', ''))}。\n"
        )
        if source == "register":
            register_steam_then_record(output_dir)
            try_sync_result(output_dir, finished=False)
    except RuntimeError as exc:
        sys.stderr.write(str(exc) + "\n")
        if (output_dir / "windows_trial.json").exists():
            try_sync_result(output_dir, finished=True, exit_code=2)
        return 2

    from .flows.ea import ea_pw_full_setup as module
    capture_mailbox_refresh(module.protocol, output_dir)
    capture_mailbox_refresh(module.pw_link, output_dir)
    save_checkpoint = module.save_checkpoint
    last_milestone = None

    def checkpoint(result):
        nonlocal last_milestone
        save_checkpoint(result)
        milestone = tuple(bool(result.get(k)) for k in ("ea_creation_submitted", "account_login_verified", "steam_link", "totp_secret", "totp")) + (result.get("error_code"), result.get("stage") == "complete")
        if milestone != last_milestone and any(milestone):
            last_milestone = milestone
            try_sync_result(output_dir, finished=False)

    module.save_checkpoint = checkpoint
    try:
        code = module.main()
    except Exception:
        try_sync_result(output_dir, finished=True, exit_code=1)
        raise
    card = write_account_card(output_dir)
    if card:
        saved = json.loads(card.read_text(encoding="utf-8"))
        if saved.get("totp_secret"):
            sys.stderr.write(f"2FA 密钥已写入 {card}\n")
        else:
            sys.stderr.write(f"账号卡已写入 {card}，里面还没有 2FA 密钥。\n")
    uploaded = try_sync_result(output_dir, finished=True, exit_code=code)
    return code if code else 0 if uploaded else 3


if __name__ == "__main__":
    raise SystemExit(main())

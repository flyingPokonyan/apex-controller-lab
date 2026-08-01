from __future__ import annotations

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import sys
from typing import Mapping
from urllib.parse import urlparse


DEFAULT_STEAM_ID64_BASE = 76561197960265728
MAX_STEAM_ID64 = DEFAULT_STEAM_ID64_BASE + 0xFFFFFFFF


class RunnerConfigurationError(ValueError):
    """The private Runner configuration is incomplete or unsafe."""


class RunnerIdentityMismatch(RuntimeError):
    """The configured account is not the account active on this machine."""


@dataclass(frozen=True)
class IdentityVerification:
    status: str
    observed_platform: str | None = None
    observed_platform_account_id: str | None = None
    message: str | None = None

    def as_manifest(self) -> dict[str, object]:
        return {
            "status": self.status,
            "observedPlatform": self.observed_platform,
            "observedPlatformAccountId": self.observed_platform_account_id,
            "message": self.message,
        }


@dataclass(frozen=True)
class RunnerSettings:
    enabled: bool
    account_id: str | None = None
    device_id: str | None = None
    account_label: str | None = None
    report_url: str | None = None
    report_token: str | None = field(default=None, repr=False)
    lease_url: str | None = None
    provider_token: str | None = field(default=None, repr=False)
    expected_platform: str | None = None
    expected_platform_account_id: str | None = None
    config_path: Path | None = None

    def safe_manifest(self, verification: IdentityVerification) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "accountId": self.account_id,
            "deviceId": self.device_id,
            "accountLabel": self.account_label,
            "reportUrl": self.report_url,
            "leaseUrl": self.lease_url,
            "expectedPlatform": self.expected_platform,
            "expectedPlatformAccountId": self.expected_platform_account_id,
            "identityVerification": verification.as_manifest(),
        }

    @property
    def display_account(self) -> str:
        if self.account_label:
            return self.account_label
        if not self.account_id:
            return "未绑定"
        if len(self.account_id) <= 10:
            return self.account_id
        return f"{self.account_id[:6]}...{self.account_id[-4:]}"


def _clean(value: object | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _parse_bool(value: object | None, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise RunnerConfigurationError(f"无法识别 enabled 值：{value!r}")


def _validate_id(name: str, value: str | None) -> str:
    if value is None:
        raise RunnerConfigurationError(f"启用远程上报时缺少 {name}")
    if not 1 <= len(value) <= 128:
        raise RunnerConfigurationError(f"{name} 长度必须在 1 到 128 之间")
    return value


def _validate_report_url(value: str | None) -> str:
    if value is None:
        raise RunnerConfigurationError("启用远程上报时缺少 reportUrl")
    return _validate_https_url(value, "reportUrl")


def _validate_https_url(value: str, name: str) -> str:
    parsed = urlparse(value)
    if (
        not parsed.netloc
        or parsed.query
        or parsed.fragment
        or parsed.username
        or parsed.password
    ):
        raise RunnerConfigurationError(f"{name} 必须是完整的接口 URL")
    if parsed.scheme == "https":
        return value
    if parsed.scheme == "http" and _is_private_test_host(parsed.hostname):
        return value
    raise RunnerConfigurationError(f"{name} 必须使用 HTTPS（本机或私网联调地址除外）")


def _is_private_test_host(hostname: str | None) -> bool:
    if hostname == "localhost":
        return True
    if hostname is None:
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return False
    return address.is_loopback or address.is_private


def _lease_url_from_base(value: str) -> str:
    base = _validate_https_url(value, "providerBaseUrl").rstrip("/")
    if base.endswith("/v1/runner/account-leases"):
        return base
    return f"{base}/v1/runner/account-leases"


def _read_payload(path: Path | None) -> dict[str, object]:
    if path is None:
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise RunnerConfigurationError(f"Runner 私有配置不存在：{path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise RunnerConfigurationError(f"Runner 私有配置无法读取：{error}") from error
    if not isinstance(payload, dict):
        raise RunnerConfigurationError("Runner 私有配置根节点必须是对象")
    try:
        schema_version = int(payload.get("schemaVersion", 1))
    except (TypeError, ValueError) as error:
        raise RunnerConfigurationError(
            "Runner 私有配置 schemaVersion 必须是整数"
        ) from error
    if schema_version != 1:
        raise RunnerConfigurationError("Runner 私有配置只支持 schemaVersion=1")
    return payload


def load_runner_settings(
    *,
    explicit_path: Path | None = None,
    default_path: Path | None = None,
    environ: Mapping[str, str] | None = None,
    managed: bool = False,
) -> RunnerSettings:
    """Load the optional account-scoped private configuration.

    Environment values override the JSON file so a managed Windows host can
    inject secrets without rewriting the installation. When neither source
    contains reporting values, the existing local-only play mode is preserved.
    """

    env = os.environ if environ is None else environ
    configured_path = explicit_path
    if configured_path is None and env.get("APEX_RUNNER_CONFIG"):
        configured_path = Path(env["APEX_RUNNER_CONFIG"]).expanduser()
    if configured_path is None and default_path is not None and default_path.exists():
        configured_path = default_path
    payload = _read_payload(configured_path)

    def setting(env_name: str, json_name: str) -> str | None:
        return _clean(env.get(env_name, payload.get(json_name)))

    report_url = setting("APEX_REPORT_URL", "reportUrl")
    report_token = setting("APEX_REPORT_TOKEN", "reportToken")
    lease_url = setting("APEX_LEASE_URL", "leaseUrl")
    provider_base_url = setting("APEX_PROVIDER_BASE_URL", "providerBaseUrl")
    if lease_url is None and provider_base_url is not None:
        lease_url = _lease_url_from_base(provider_base_url)
    provider_token = setting("APEX_PROVIDER_TOKEN", "providerToken")
    account_id = setting("APEX_ACCOUNT_ID", "accountId")
    device_id = setting("APEX_DEVICE_ID", "deviceId")
    account_label = setting("APEX_ACCOUNT_LABEL", "accountLabel")
    expected_platform = setting("APEX_EXPECTED_PLATFORM", "expectedPlatform")
    expected_platform_account_id = setting(
        "APEX_EXPECTED_PLATFORM_ACCOUNT_ID",
        "expectedPlatformAccountId",
    )

    has_reporting_value = any((report_url, report_token))
    has_provider_value = any((lease_url, provider_token))
    enabled_raw: object | None = env.get("APEX_REPORT_ENABLED", payload.get("enabled"))
    enabled = _parse_bool(enabled_raw, default=has_reporting_value)

    if not managed and bool(account_id) != bool(device_id):
        raise RunnerConfigurationError("accountId 和 deviceId 必须同时配置")
    if managed and account_id is not None:
        raise RunnerConfigurationError("托管模式的 accountId 必须来自服务端租约")
    if managed and device_id is None:
        raise RunnerConfigurationError("托管模式必须配置 deviceId")
    if bool(lease_url) != bool(provider_token):
        raise RunnerConfigurationError("leaseUrl 和 providerToken 必须同时配置")
    if managed and not has_provider_value:
        raise RunnerConfigurationError("托管模式必须配置 leaseUrl/providerToken")
    if bool(expected_platform) != bool(expected_platform_account_id):
        raise RunnerConfigurationError(
            "expectedPlatform 和 expectedPlatformAccountId 必须同时配置"
        )
    if expected_platform and not account_id:
        raise RunnerConfigurationError("平台账号核验必须同时配置 accountId/deviceId")
    if expected_platform is not None:
        expected_platform = expected_platform.lower()
        if expected_platform not in {"steam", "ea"}:
            raise RunnerConfigurationError("expectedPlatform 只支持 steam 或 ea")
        if expected_platform == "steam":
            assert expected_platform_account_id is not None
            steam_id64 = (
                int(expected_platform_account_id)
                if expected_platform_account_id.isdigit()
                else 0
            )
            if not DEFAULT_STEAM_ID64_BASE < steam_id64 <= MAX_STEAM_ID64:
                raise RunnerConfigurationError(
                    "expectedPlatformAccountId 必须是有效的 Steam ID64"
                )

    if not enabled:
        if has_reporting_value:
            raise RunnerConfigurationError(
                "远程上报已关闭，但仍配置了 reportUrl/reportToken；请删除或显式启用"
            )
        return RunnerSettings(
            enabled=False,
            account_id=account_id,
            device_id=device_id,
            account_label=account_label,
            lease_url=(
                None if lease_url is None else _validate_https_url(lease_url, "leaseUrl")
            ),
            provider_token=provider_token,
            expected_platform=expected_platform,
            expected_platform_account_id=expected_platform_account_id,
            config_path=configured_path,
        )

    return RunnerSettings(
        enabled=True,
        account_id=(
            None if managed else _validate_id("accountId", account_id)
        ),
        device_id=_validate_id("deviceId", device_id),
        account_label=account_label,
        report_url=_validate_report_url(report_url),
        report_token=_validate_token(report_token),
        lease_url=(
            None if lease_url is None else _validate_https_url(lease_url, "leaseUrl")
        ),
        provider_token=(
            None
            if provider_token is None
            else _validate_token(provider_token, name="providerToken")
        ),
        expected_platform=expected_platform,
        expected_platform_account_id=expected_platform_account_id,
        config_path=configured_path,
    )


def _validate_token(value: str | None, *, name: str = "reportToken") -> str:
    if value is None:
        raise RunnerConfigurationError(f"缺少 {name}")
    if not 1 <= len(value) <= 512:
        raise RunnerConfigurationError(f"{name} 长度必须在 1 到 512 之间")
    return value


def read_active_steam_id64() -> str | None:
    """Return Steam's active Windows user as the stable 64-bit account ID."""

    if sys.platform != "win32":
        return None
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Valve\Steam\ActiveProcess",
        ) as key:
            active_user, _ = winreg.QueryValueEx(key, "ActiveUser")
    except (OSError, ImportError):
        return None
    try:
        account_id32 = int(active_user)
    except (TypeError, ValueError):
        return None
    if not 0 < account_id32 <= 0xFFFFFFFF:
        return None
    return str(DEFAULT_STEAM_ID64_BASE + account_id32)


def verify_runner_identity(
    settings: RunnerSettings,
    *,
    active_steam_id64: str | None = None,
) -> IdentityVerification:
    if not settings.account_id:
        return IdentityVerification("UNBOUND")
    if not settings.expected_platform:
        return IdentityVerification(
            "CONFIGURED",
            message="未配置平台稳定 ID，只能确认选择的 Runner 账号",
        )
    if settings.expected_platform != "steam":
        return IdentityVerification(
            "UNAVAILABLE",
            observed_platform=settings.expected_platform,
            message="当前 Runner 尚不能读取 EA 客户端的稳定账号 ID",
        )

    observed = active_steam_id64
    if observed is None:
        observed = read_active_steam_id64()
    if observed is None:
        return IdentityVerification(
            "UNAVAILABLE",
            observed_platform="steam",
            message="无法读取 Steam ActiveUser",
        )
    if observed != settings.expected_platform_account_id:
        return IdentityVerification(
            "MISMATCH",
            observed_platform="steam",
            observed_platform_account_id=observed,
            message="Steam 当前账号与 Runner 绑定不一致",
        )
    return IdentityVerification(
        "VERIFIED",
        observed_platform="steam",
        observed_platform_account_id=observed,
    )


def require_identity_match(verification: IdentityVerification) -> None:
    if verification.status == "MISMATCH":
        raise RunnerIdentityMismatch(verification.message or "Runner 账号身份不一致")

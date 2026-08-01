from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.instance_lock import AlreadyRunningError, SingleInstanceLock
from apex_automation.runner_identity import (
    RunnerConfigurationError,
    load_runner_settings,
    require_identity_match,
    verify_runner_identity,
)


class RunnerSettingsTest(unittest.TestCase):
    def test_no_private_configuration_keeps_local_mode_enabled(self) -> None:
        settings = load_runner_settings(environ={})

        self.assertFalse(settings.enabled)
        self.assertIsNone(settings.account_id)
        self.assertEqual(verify_runner_identity(settings).status, "UNBOUND")

    def test_loads_an_account_scoped_private_file_without_exposing_token(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.private.json"
            path.write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "enabled": True,
                        "reportUrl": "https://runner.example/v1/runner/reports",
                        "reportToken": "secret-device-token",
                        "deviceId": "dev_123",
                        "accountId": "acct_456",
                        "accountLabel": "测试账号",
                        "expectedPlatform": "steam",
                        "expectedPlatformAccountId": "76561198000000000",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            settings = load_runner_settings(explicit_path=path, environ={})
            verification = verify_runner_identity(
                settings,
                active_steam_id64="76561198000000000",
            )
            manifest = settings.safe_manifest(verification)

            self.assertTrue(settings.enabled)
            self.assertEqual(settings.account_id, "acct_456")
            self.assertEqual(settings.report_token, "secret-device-token")
            self.assertEqual(verification.status, "VERIFIED")
            self.assertNotIn("token", json.dumps(manifest).lower())
            self.assertNotIn("secret-device-token", json.dumps(manifest))

    def test_environment_overrides_file_and_partial_reporting_fails_closed(
        self,
    ) -> None:
        with self.assertRaisesRegex(RunnerConfigurationError, "reportToken"):
            load_runner_settings(
                environ={
                    "APEX_REPORT_URL": "https://runner.example/v1/runner/reports",
                    "APEX_ACCOUNT_ID": "acct_1",
                    "APEX_DEVICE_ID": "dev_1",
                }
            )

        settings = load_runner_settings(
            environ={
                "APEX_REPORT_URL": "http://127.0.0.1:9999/v1/runner/reports",
                "APEX_REPORT_TOKEN": "token",
                "APEX_ACCOUNT_ID": "acct_env",
                "APEX_DEVICE_ID": "dev_env",
            }
        )
        self.assertEqual(settings.account_id, "acct_env")

    def test_plain_http_remote_endpoint_is_rejected(self) -> None:
        with self.assertRaisesRegex(RunnerConfigurationError, "HTTPS"):
            load_runner_settings(
                environ={
                    "APEX_REPORT_URL": "http://runner.example/reports",
                    "APEX_REPORT_TOKEN": "token",
                    "APEX_ACCOUNT_ID": "acct_1",
                    "APEX_DEVICE_ID": "dev_1",
                }
            )

    def test_managed_reporting_binds_device_but_not_a_static_account(self) -> None:
        settings = load_runner_settings(
            managed=True,
            environ={
                "APEX_REPORT_URL": "https://runner.example/reports",
                "APEX_REPORT_TOKEN": "token",
                "APEX_DEVICE_ID": "dev_managed",
                "APEX_LEASE_URL": "https://runner.example/v1/runner/account-leases",
                "APEX_PROVIDER_TOKEN": "provider-token",
            },
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.device_id, "dev_managed")
        self.assertIsNone(settings.account_id)
        with self.assertRaisesRegex(RunnerConfigurationError, "来自服务端租约"):
            load_runner_settings(
                managed=True,
                environ={
                    "APEX_REPORT_URL": "https://runner.example/reports",
                    "APEX_REPORT_TOKEN": "token",
                    "APEX_DEVICE_ID": "dev_managed",
                    "APEX_ACCOUNT_ID": "must-not-be-static",
                    "APEX_LEASE_URL": "https://runner.example/v1/runner/account-leases",
                    "APEX_PROVIDER_TOKEN": "provider-token",
                },
            )

        self.assertEqual(
            settings.lease_url,
            "https://runner.example/v1/runner/account-leases",
        )
        self.assertEqual(settings.provider_token, "provider-token")
        self.assertNotIn("provider-token", repr(settings))
        self.assertNotIn(
            "provider-token",
            json.dumps(settings.safe_manifest(verify_runner_identity(settings))),
        )

    def test_managed_provider_base_url_is_backward_compatible(self) -> None:
        settings = load_runner_settings(
            managed=True,
            environ={
                "APEX_REPORT_URL": "https://runner.example/v1/runner/reports",
                "APEX_REPORT_TOKEN": "report-token",
                "APEX_DEVICE_ID": "dev_managed",
                "APEX_PROVIDER_BASE_URL": "https://runner.example",
                "APEX_PROVIDER_TOKEN": "provider-token",
            },
        )

        self.assertEqual(
            settings.lease_url,
            "https://runner.example/v1/runner/account-leases",
        )

        with self.assertRaisesRegex(RunnerConfigurationError, "完整"):
            load_runner_settings(
                environ={
                    "APEX_REPORT_URL": "https://user:password@runner.example/reports",
                    "APEX_REPORT_TOKEN": "token",
                    "APEX_ACCOUNT_ID": "acct_1",
                    "APEX_DEVICE_ID": "dev_1",
                }
            )

    def test_invalid_schema_and_unscoped_platform_identity_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runner.json"
            path.write_text('{"schemaVersion": "not-a-number"}', encoding="utf-8")
            with self.assertRaisesRegex(RunnerConfigurationError, "整数"):
                load_runner_settings(explicit_path=path, environ={})

        with self.assertRaisesRegex(RunnerConfigurationError, "accountId"):
            load_runner_settings(
                environ={
                    "APEX_EXPECTED_PLATFORM": "steam",
                    "APEX_EXPECTED_PLATFORM_ACCOUNT_ID": "76561198000000000",
                }
            )

        with self.assertRaisesRegex(RunnerConfigurationError, "Steam ID64"):
            load_runner_settings(
                environ={
                    "APEX_REPORT_URL": "https://runner.example/reports",
                    "APEX_REPORT_TOKEN": "token",
                    "APEX_ACCOUNT_ID": "acct_1",
                    "APEX_DEVICE_ID": "dev_1",
                    "APEX_EXPECTED_PLATFORM": "steam",
                    "APEX_EXPECTED_PLATFORM_ACCOUNT_ID": "99999999999999999999",
                }
            )

    def test_a_different_active_steam_user_must_stop_before_input(self) -> None:
        settings = load_runner_settings(
            environ={
                "APEX_REPORT_URL": "https://runner.example/reports",
                "APEX_REPORT_TOKEN": "token",
                "APEX_ACCOUNT_ID": "acct_1",
                "APEX_DEVICE_ID": "dev_1",
                "APEX_EXPECTED_PLATFORM": "steam",
                "APEX_EXPECTED_PLATFORM_ACCOUNT_ID": "76561198000000000",
            }
        )
        verification = verify_runner_identity(
            settings,
            active_steam_id64="76561198000000001",
        )

        self.assertEqual(verification.status, "MISMATCH")
        with self.assertRaisesRegex(RuntimeError, "不一致"):
            require_identity_match(verification)


class SingleInstanceLockTest(unittest.TestCase):
    def test_only_one_play_session_can_hold_the_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "play.lock"
            first = SingleInstanceLock(path)
            second = SingleInstanceLock(path)
            first.acquire()
            self.addCleanup(first.release)

            with self.assertRaises(AlreadyRunningError):
                second.acquire()

            first.release()
            second.acquire()
            second.release()


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock, patch


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.account_orchestrator import (
    AccountCycleOutcome,
    AccountCycleResult,
)
from apex_automation.runner_identity import RunnerSettings


CV2_AVAILABLE = importlib.util.find_spec("cv2") is not None
if CV2_AVAILABLE:
    from apex_automation import cli


@unittest.skipUnless(CV2_AVAILABLE, "account-cycle CLI requires the Windows OpenCV runtime")
class AccountCycleCliTest(unittest.TestCase):
    def test_check_reads_current_lease_without_claiming(self) -> None:
        settings = RunnerSettings(
            enabled=True,
            device_id="device_1",
            report_url="https://runner.example/v1/runner/reports",
            report_token="report-token",
            lease_url="https://runner.example/v1/runner/account-leases",
            provider_token="provider-token",
        )
        provider = Mock()
        provider.current.return_value = None

        with (
            patch.object(cli, "load_runner_settings", return_value=settings),
            patch.object(cli, "HttpAccountProvider", return_value=provider),
        ):
            exit_code = cli.run_account_cycle_check()

        self.assertEqual(exit_code, 0)
        provider.current.assert_called_once_with()
        provider.claim.assert_not_called()

    def test_hybrid_preflight_failure_stops_before_provider_can_claim(self) -> None:
        settings = RunnerSettings(
            enabled=True,
            device_id="device_1",
            report_url="https://runner.example/v1/runner/reports",
            report_token="report-token",
            lease_url="https://runner.example/v1/runner/account-leases",
            provider_token="provider-token",
        )
        provider = Mock()
        play_session = Mock()
        capture = Mock()
        capture.__enter__ = Mock(return_value=Mock())
        capture.__exit__ = Mock(return_value=False)
        driver = Mock()
        driver.preflight.side_effect = RuntimeError("preflight failed")

        with (
            patch.object(cli.sys, "platform", "win32"),
            patch.object(cli, "load_config", return_value=object()),
            patch.object(
                cli, "load_runner_settings", return_value=settings
            ) as settings_loader,
            patch.object(cli, "HttpAccountProvider", return_value=provider) as factory,
            patch.object(
                cli,
                "_build_play_session_runner",
                return_value=(play_session, object()),
            ) as build_session,
            patch.object(cli, "DxcamFrameSource", return_value=capture),
            patch.object(cli, "WindowsEaHybridDriver", return_value=driver),
        ):
            exit_code = cli.run_account_cycle(Path("managed.json"))

        self.assertEqual(exit_code, 1)
        settings_loader.assert_called_once_with(
            explicit_path=None,
            default_path=cli.REPOSITORY_ROOT
            / "windows"
            / "account-cycle.private.json",
            managed=True,
        )
        factory.assert_called_once_with(
            settings.lease_url,
            settings.provider_token,
            client_version=cli.__version__,
        )
        build_session.assert_called_once()
        driver.preflight.assert_called_once_with()
        provider.claim.assert_not_called()
        provider.current.assert_not_called()

    def test_once_runs_a_single_cycle_and_never_loops(self) -> None:
        settings = RunnerSettings(
            enabled=True,
            device_id="device_1",
            report_url="https://runner.example/v1/runner/reports",
            report_token="report-token",
            lease_url="https://runner.example/v1/runner/account-leases",
            provider_token="provider-token",
        )
        capture = Mock()
        capture.__enter__ = Mock(return_value=Mock())
        capture.__exit__ = Mock(return_value=False)
        driver = Mock()
        orchestrator = Mock()
        orchestrator.run_once.return_value = AccountCycleResult(
            outcome=AccountCycleOutcome.COMPLETED,
            lease_id="lease_1",
        )

        with (
            patch.object(cli.sys, "platform", "win32"),
            patch.object(cli, "load_config", return_value=object()),
            patch.object(cli, "load_runner_settings", return_value=settings),
            patch.object(cli, "HttpAccountProvider", return_value=Mock()),
            patch.object(
                cli,
                "_build_play_session_runner",
                return_value=(Mock(), object()),
            ),
            patch.object(cli, "SingleInstanceLock", return_value=Mock()),
            patch.object(cli, "DxcamFrameSource", return_value=capture),
            patch.object(cli, "WindowsEaHybridDriver", return_value=driver),
            patch.object(cli, "AccountOrchestrator", return_value=orchestrator),
        ):
            exit_code = cli.run_account_cycle(Path("managed.json"), once=True)

        self.assertEqual(exit_code, 0)
        orchestrator.run_once.assert_called_once_with()
        orchestrator.run_forever.assert_not_called()


if __name__ == "__main__":
    unittest.main()

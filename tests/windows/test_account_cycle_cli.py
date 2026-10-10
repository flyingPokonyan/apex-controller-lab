from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
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
    def setUp(self):
        # CLI tests must never start a real network worker or touch run outboxes.
        self.diagnostics = self.enterContext(patch.object(cli, "DiagnosticReporter")).return_value
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(cli, "REPOSITORY_ROOT", root))
        self.enterContext(patch.object(cli.ManagedRuntime, "from_environment", return_value=None))

    @staticmethod
    def config():
        return SimpleNamespace(
            environment={"captureBackend": "dxgi", "outputIndex": 0}
        )

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
        driver.ensure_started.side_effect = RuntimeError("preflight failed")

        with (
            patch.object(cli.sys, "platform", "win32"),
            patch.object(cli, "load_config", return_value=self.config()),
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
        driver.ensure_started.assert_called_once_with()
        provider.claim.assert_not_called()
        provider.current.assert_not_called()
        self.diagnostics.start.assert_called_once_with()
        self.diagnostics.stop.assert_called_once_with()

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
            patch.object(cli, "load_config", return_value=self.config()),
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

    def test_managed_recovery_is_not_blocked_by_ea_preflight(self):
        from apex_automation.orchestration_state import OrchestrationCheckpoint
        from apex_automation.managed_runtime import ManagedRuntime
        settings = RunnerSettings(enabled=True, device_id="device_1", lease_url="https://test.invalid/v1/runner/account-leases",
                                  provider_token="test", report_url="https://test.invalid/v1/runner/reports", report_token="test")
        store = cli.AtomicCheckpointStore(cli.REPOSITORY_ROOT / "windows/runs/account-cycle-status.json")
        store.save(OrchestrationCheckpoint(device_id="device_1", lease_id="existing", lease_fence=1, account_id="acct"))
        capture = Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))
        driver = Mock()
        driver.ensure_started.side_effect = AssertionError("Recovery must happen before preflight")
        orchestrator = Mock()
        orchestrator.run_forever.return_value = 0
        runtime = ManagedRuntime(cli.REPOSITORY_ROOT / "managed", "session")
        with (
            patch.object(cli.sys, "platform", "win32"),
            patch.object(cli, "load_config", return_value=self.config()),
            patch.object(cli, "load_runner_settings", return_value=settings),
            patch.object(cli.ManagedRuntime, "from_environment", return_value=runtime),
            patch.object(cli, "DxcamFrameSource", return_value=capture),
            patch.object(cli, "AccountOrchestrator", return_value=orchestrator),
        ):
            self.assertEqual(cli.run_account_cycle(Path("managed.json"), provider=Mock(),
                                                 ea_driver=driver, play_session=Mock()), 0)
        driver.ensure_started.assert_not_called()
        orchestrator.run_forever.assert_called_once()

    def test_network_outage_at_startup_retries_instead_of_using_failure_budget(self):
        from apex_automation.account_provider import LeaseProviderError
        from apex_automation.managed_runtime import ManagedUpdateRequested, UPDATE_EXIT
        settings = RunnerSettings(enabled=True, device_id="device_1")
        provider = Mock()
        provider.current.side_effect = LeaseProviderError("offline", retryable=True)
        runtime = Mock()
        runtime.sleep.side_effect = ManagedUpdateRequested()
        driver = Mock()
        capture = Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))
        with (
            patch.object(cli.sys, "platform", "win32"),
            patch.object(cli, "load_config", return_value=self.config()),
            patch.object(cli, "load_runner_settings", return_value=settings),
            patch.object(cli.ManagedRuntime, "from_environment", return_value=runtime),
            patch.object(cli, "DxcamFrameSource", return_value=capture),
        ):
            code = cli.run_account_cycle(Path("managed.json"), provider=provider,
                                         ea_driver=driver, play_session=Mock())
        self.assertEqual(code, UPDATE_EXIT)
        runtime.sleep.assert_called_once_with(30)
        driver.ensure_started.assert_not_called()
        provider.claim.assert_not_called()

    def test_managed_ea_preflight_error_cools_down_without_claiming(self):
        from apex_automation.ea_app import EaAppAutomationError, EaCaptchaRequired
        from apex_automation.managed_runtime import ManagedRuntime, RETRY_EXIT
        settings = RunnerSettings(enabled=True, device_id="device_1")
        provider = Mock()
        provider.current.return_value = None
        capture = Mock(__enter__=Mock(return_value=Mock()), __exit__=Mock(return_value=False))
        for error, expected in ((EaAppAutomationError("not ready"), RETRY_EXIT), (EaCaptchaRequired("captcha"), 1)):
            with self.subTest(error=type(error).__name__):
                driver = Mock()
                driver.ensure_started.side_effect = error
                runtime = ManagedRuntime(cli.REPOSITORY_ROOT / "managed", "session")
                with (
                    patch.object(cli.sys, "platform", "win32"),
                    patch.object(cli, "load_config", return_value=self.config()),
                    patch.object(cli, "load_runner_settings", return_value=settings),
                    patch.object(cli.ManagedRuntime, "from_environment", return_value=runtime),
                    patch.object(cli, "DxcamFrameSource", return_value=capture),
                ):
                    code = cli.run_account_cycle(Path("managed.json"), provider=provider,
                                                ea_driver=driver, play_session=Mock())
                self.assertEqual(code, expected)
                provider.claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()

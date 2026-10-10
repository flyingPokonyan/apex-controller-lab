from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_automation.ea_app import (
    EaAppAutomationError, EaCredentialsRejected, EaIdentityFact, EaUiRecoveryExhausted,
)
from apex_automation.ea_app_win32 import WindowsEaHybridDriver
from apex_automation.ea_pages import EaPage
from apex_automation.account_orchestrator import AccountOrchestrator, AccountCycleOutcome
from apex_automation.account_provider import FakeAccountProvider, LeaseState, SecretCredentials
from apex_automation.orchestration_state import AtomicCheckpointStore, OrchestrationCheckpoint
from test_account_orchestration import FakeEaDriver, ScriptedLeaseKeeper


class BoundedRecoveryTest(unittest.TestCase):
    def driver(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._process_running = Mock(return_value=False)
        driver.restart_app = Mock()
        driver._restart_ea_process = Mock()
        driver._ea_window = Mock(return_value=7)
        driver._observe = Mock(return_value=Mock(page=EaPage.EMAIL))
        driver._record = Mock()
        driver.notify = Mock()
        return driver

    def test_signin_repeats_the_full_identity_flow_after_restart(self):
        driver = self.driver()
        identity = EaIdentityFact('leased-player', 'fixture', True)
        driver._sign_in_once = Mock(side_effect=[EaAppAutomationError('BACK missing'), identity])
        credentials, otp = object(), object()
        self.assertIs(driver.sign_in(credentials, otp), identity)
        self.assertEqual(driver._sign_in_once.call_count, 2)
        driver._sign_in_once.assert_called_with(credentials, otp)
        driver.restart_app.assert_called_once()

    def test_successful_restart_is_not_signout_proof_and_recovery_is_bounded(self):
        driver = self.driver()
        driver._sign_out_once = Mock(return_value=False)
        for _ in range(2):
            with self.assertRaises(EaUiRecoveryExhausted):
                driver.sign_out()
        driver.restart_app.assert_called_once()
        self.assertEqual(driver._sign_out_once.call_count, 3)

    def test_login_page_without_restart_menu_reopens_ea_then_verifies_signout(self):
        driver = self.driver()
        driver._sign_out_once = Mock(side_effect=[False, True])
        driver.restart_app.side_effect = EaAppAutomationError('no help menu')
        self.assertTrue(driver.sign_out())
        driver._restart_ea_process.assert_called_once()
        self.assertEqual(driver._sign_out_once.call_count, 2)

    def test_failed_process_reopen_is_exhausted_not_another_account_failure(self):
        driver = self.driver()
        driver._sign_out_once = Mock(return_value=False)
        driver.restart_app.side_effect = EaAppAutomationError('no help menu')
        driver._restart_ea_process.side_effect = EaAppAutomationError('window missing')
        with self.assertRaises(EaUiRecoveryExhausted):
            driver.sign_out()

    def test_active_apex_prevents_restart(self):
        driver = self.driver()
        driver._process_running.return_value = True
        driver._sign_out_once = Mock(return_value=False)
        with self.assertRaises(EaUiRecoveryExhausted):
            driver.sign_out()
        driver.restart_app.assert_not_called()
        driver._restart_ea_process.assert_not_called()

    def test_password_rejection_keeps_its_existing_password_reset_path(self):
        driver = self.driver()
        driver._sign_in_once = Mock(side_effect=EaCredentialsRejected('confirmed rejection'))
        with self.assertRaises(EaCredentialsRejected):
            driver.sign_in(object(), object())
        driver.restart_app.assert_not_called()

    def test_expired_session_button_must_actually_dismiss_the_page(self):
        driver = self.driver()
        expired = Mock(page=EaPage.EXPIRED_SESSION)
        driver._observe.return_value = expired
        driver._live = lambda hwnd: hwnd
        driver._expired_session_point = Mock(return_value=(100, 100))
        driver._click_point = Mock()
        driver.sleep = lambda _: None
        with self.assertRaisesRegex(EaAppAutomationError, '仍未关闭'):
            driver._dismiss_expired_session(7)
        self.assertEqual(driver._click_point.call_count, 3)
        self.assertEqual(driver._observe.call_count, 4)


class RecoveryCheckpointTest(unittest.TestCase):
    def test_exhaustion_keeps_the_lease_and_survives_worker_restart(self):
        for failure in ('preflight', 'leftover', 'cleanup'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                lease = replace(FakeAccountProvider.lease('acct_1'), expected_ea_account_id='acct_1')
                provider = FakeAccountProvider([lease, FakeAccountProvider.lease('acct_2')],
                    credentials={'acct_1': SecretCredentials('fixture@example.test', 'fixture-secret')})
                class BrokenEa(FakeEaDriver):
                    def configure_ui_recovery(self, reserve):
                        self.reserve = reserve
                    def ensure_started(self):
                        if failure == 'preflight':
                            self.reserve('preflight')
                            raise EaUiRecoveryExhausted('still stuck')
                        return super().ensure_started()
                    def current_identity(self):
                        return EaIdentityFact('old-player', 'fixture', True) if failure == 'leftover' else None
                    def sign_in(self, *_args):
                        raise EaCredentialsRejected('rejected')
                    def sign_out(self):
                        self.reserve('signout')
                        raise EaUiRecoveryExhausted('still stuck')
                store = AtomicCheckpointStore(Path(directory) / 'checkpoint.json')
                driver = BrokenEa([], 'acct_1')
                def create():
                    return AccountOrchestrator(provider=provider, ea_driver=driver,
                        play_session=object(), checkpoint_store=store, device_id='dev_1',
                        capture_source=object(), notify=lambda _: None,
                        lease_keeper_factory=lambda _provider, value, **_kwargs: ScriptedLeaseKeeper(value))
                first = create()
                result = first.run_once()
                self.assertEqual(result.outcome, AccountCycleOutcome.PAUSED)
                self.assertEqual(result.error_code, 'EA_RECOVERY_EXHAUSTED')
                self.assertEqual(store.load().lease_id, lease.lease_id)
                self.assertEqual(provider.status(lease.lease_id, lease.lease_fence).state, LeaseState.ACTIVE)
                second = create()
                self.assertFalse(second._reserve_ui_recovery('preflight' if failure == 'preflight' else 'signout'))
                self.assertEqual(second.run_once().error_code, 'EA_RECOVERY_EXHAUSTED')
                self.assertEqual(provider.current().lease_id, lease.lease_id)
                self.assertEqual(len([call for call in provider.calls if call[0] == 'claim']), 1)
                self.assertFalse(any(call[0] == 'close' for call in provider.calls))
                self.assertFalse(second.resume_if_safe())
                self.assertTrue(second.resume())
                self.assertEqual(store.load().ea_recovery_steps, ())

    def test_old_checkpoint_defaults_to_unused_budget_and_new_budget_round_trips(self):
        original = OrchestrationCheckpoint(device_id='dev_1')
        payload = original.to_payload()
        payload.pop('eaRecoverySteps')
        self.assertEqual(OrchestrationCheckpoint.from_payload(payload).ea_recovery_steps, ())
        current = replace(original, ea_recovery_steps=('signin', 'signout'))
        self.assertEqual(OrchestrationCheckpoint.from_payload(current.to_payload()).ea_recovery_steps,
                         ('signin', 'signout'))

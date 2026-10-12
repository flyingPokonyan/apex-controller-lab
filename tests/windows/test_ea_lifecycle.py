"""Stateful lease/UI contract: clean session boundaries across many accounts.

Native window identity, focus and process ownership are covered separately by
test_ea_window_recovery and test_ea_process. Here the real orchestrator runs
against an EA session that persists across claims and worker reconstruction.
"""
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.account_orchestrator import AccountCycleOutcome, AccountOrchestrator
from apex_automation.account_provider import FakeAccountProvider, SecretCredentials
from apex_automation.ea_app import ApexExitEvidence, EaAccountBanned, EaIdentityFact, EaUiState
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_pages import EaPage
from apex_automation.orchestration_state import AtomicCheckpointStore
from test_account_orchestration import FakeDrain, FakeManagedSession, ScriptedLeaseKeeper


class Session:
    def __init__(self, bans=()):
        self.account = None
        self.bans = set(bans)
        self.game_running = False
        self.failed_signouts = 0
        self.cleanup_calls = 0
        self.log = []

    def ensure_started(self):
        page = EaPage.EMAIL if self.account is None else (
            EaPage.BANNED if self.current_page_is_banned() else EaPage.SIGNED_IN)
        observation = EaObservation((0, 0, 1920, 1080), np.zeros((1, 1)), (), page)
        # In particular a known banned surface must be usable before reading
        # its badge, so the orchestrator can clean up a leftover account.
        return WindowsEaHybridDriver._state(self, 7, observation)

    def _identity(self, _hwnd):
        return self.current_identity()

    def current_identity(self):
        if self.account is None or self.current_page_is_banned():
            return None
        return EaIdentityFact(self.account, "fixture", True)

    def current_page_is_banned(self):
        return self.account in self.bans

    def sign_in(self, credentials, _otp):
        assert self.account is None, "new credentials entered into an old session"
        self.account = credentials.login_identifier.split("@")[0]
        if self.current_page_is_banned():
            raise EaAccountBanned("fixture ban")
        return self.current_identity()

    def verify_identity(self, expected):
        assert self.account == expected
        return self.current_identity()

    def start_apex(self):
        assert self.account is not None and not self.current_page_is_banned()
        self.game_running = True

    def stop_apex(self):
        self.game_running = False
        return ApexExitEvidence(True, True)

    def sign_out(self):
        assert not self.game_running
        self.cleanup_calls += 1
        if self.failed_signouts:
            self.failed_signouts -= 1
            return False
        self.account = None
        return True


class LifecycleTest(unittest.TestCase):
    def fixture(self, accounts, bans=()):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        session = Session(bans)
        self.session = session

        class Provider(FakeAccountProvider):
            def claim(self, *args, **kwargs):
                if any(name == "close" for name, _ in self.calls):
                    assert session.account is None, "claimed another lease with an old session"
                return super().claim(*args, **kwargs)

            def close(self, *args, **kwargs):
                assert session.account is None, "claimed sign-out without actual session cleanup"
                assert not session.game_running
                return super().close(*args, **kwargs)

        self.provider = Provider(
            [replace(FakeAccountProvider.lease(a), expected_ea_account_id=a) for a in accounts],
            credentials={a: SecretCredentials(a + "@example.test", "fixture") for a in accounts})
        self.store = AtomicCheckpointStore(root / "checkpoint.json")
        return self.orchestrator()

    def orchestrator(self):
        return AccountOrchestrator(provider=self.provider, ea_driver=self.session,
            play_session=FakeManagedSession([], FakeDrain()), checkpoint_store=self.store,
            device_id="fixture", capture_source=object(), notify=lambda _: None,
            sleep=lambda _: None, completion_poll_s=0.01,
            lease_keeper_factory=lambda _provider, lease, **_kwargs: ScriptedLeaseKeeper(lease))

    def test_twelve_accounts_alternate_success_ban_and_success_without_stale_session(self):
        accounts = [f"fixture{i:04}" for i in range(12)]
        bans = set(accounts[1::3])
        runner = self.fixture(accounts, bans)
        for account in accounts:
            result = runner.run_once()
            self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
            self.assertEqual(result.error_code, "EA_ACCOUNT_BANNED" if account in bans else None)
            self.assertIsNone(self.session.account)
            self.assertFalse(self.store.load().has_lease)
        self.assertEqual(self.session.cleanup_calls, 12)
        self.assertEqual(runner.run_once().outcome, AccountCycleOutcome.NO_ACCOUNT)

    def test_cleanup_failure_survives_worker_restart_before_next_claim_and_preserves_ban(self):
        runner = self.fixture(["banned0001", "healthy002"], ["banned0001"])
        self.session.failed_signouts = 1
        result = runner.run_once()
        self.assertEqual(result.outcome, AccountCycleOutcome.PAUSED)
        self.assertEqual(self.session.account, "banned0001")
        self.assertFalse(any(name == "close" for name, _ in self.provider.calls))
        self.assertEqual(self.store.load().result_error_code, "EA_ACCOUNT_BANNED")
        runner = self.orchestrator()
        result = runner.run_once()
        self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
        self.assertEqual(result.error_code, "EA_ACCOUNT_BANNED")
        self.assertIsNone(self.session.account)
        self.assertEqual(sum(name == "claim" for name, _ in self.provider.calls), 1)
        result = runner.run_once()
        self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
        self.assertIsNone(result.error_code)

    def test_legacy_leftover_banned_page_without_readable_identity_is_signed_out_first(self):
        runner = self.fixture(["healthy002"], ["oldbanned1"])
        self.session.account = "oldbanned1"
        result = runner.run_once()
        self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
        self.assertIsNone(result.error_code)
        self.assertEqual(self.session.cleanup_calls, 2)


if __name__ == "__main__":
    unittest.main()

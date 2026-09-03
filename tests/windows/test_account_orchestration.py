from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.account_provider import (
    CleanupEvidence,
    CompletionEvidence,
    FakeAccountProvider,
    IdempotencyConflictError,
    LeaseProviderError,
    LeaseState,
    LeaseStatus,
    LeaseStaleError,
    OtpCode,
    SecretCredentials,
)
from apex_automation.account_orchestrator import (
    AccountCycleOutcome,
    AccountOrchestrator,
)
from apex_automation.ea_app import (
    ApexExitEvidence,
    EaAppAutomationError,
    EaApexDownloadRequired,
    EaIdentityFact,
    EaUiState,
    OtpChallenge,
)
from apex_automation.lease_keeper import (
    LeaseKeeper,
    LeaseKeeperSnapshot,
    LeaseKeeperState,
)
from apex_automation.orchestration_state import (
    AtomicCheckpointStore,
    OrchestrationCheckpoint,
    OrchestratorRunState,
    PendingOperation,
    WorkflowPhase,
)
from apex_automation.play_session import PlaySessionResult


class AccountProviderContractTest(unittest.TestCase):
    def test_claim_and_write_operations_are_idempotent(self) -> None:
        lease = FakeAccountProvider.lease("acct_1")
        provider = FakeAccountProvider(
            [lease],
            credentials={
                "acct_1": SecretCredentials("login@example.test", "password")
            },
        )

        first = provider.claim("claim_1", "LEVEL_TO_TARGET")
        repeated = provider.claim("claim_1", "LEVEL_TO_TARGET")
        active = provider.claim("claim_2", "LEVEL_TO_TARGET")
        credentials = provider.credentials(
            lease.lease_id,
            lease.lease_fence,
            "credentials_1",
        )
        repeated_credentials = provider.credentials(
            lease.lease_id,
            lease.lease_fence,
            "credentials_1",
        )

        self.assertIs(first, repeated)
        self.assertIs(first, active)
        self.assertIs(credentials, repeated_credentials)
        self.assertNotIn("login@example.test", repr(credentials))
        self.assertNotIn("password", repr(credentials))

    def test_reusing_operation_id_with_different_body_is_rejected(self) -> None:
        lease = FakeAccountProvider.lease("acct_1")
        provider = FakeAccountProvider([lease])
        provider.claim("claim_1", "LEVEL_TO_TARGET")
        cleanup = CleanupEvidence(True, True, True)

        provider.close(
            lease.lease_id,
            lease.lease_fence,
            "close_1",
            "FAILED",
            None,
            None,
            "LOGIN_INVALID",
            cleanup,
        )
        with self.assertRaises(IdempotencyConflictError):
            provider.close(
                lease.lease_id,
                lease.lease_fence,
                "close_1",
                "FAILED",
                None,
                None,
                "CAPTCHA",
                cleanup,
            )

    def test_otp_must_belong_to_the_current_challenge(self) -> None:
        now = datetime.now(timezone.utc)
        lease = FakeAccountProvider.lease("acct_1", now=now)
        provider = FakeAccountProvider(
            [lease],
            otp_factory=lambda challenge_id, started_at: OtpCode(
                code="123456",
                challenge_id=challenge_id,
                received_at=started_at - timedelta(minutes=10),
                expires_at=started_at - timedelta(minutes=10) + timedelta(seconds=27),
            ),
        )
        provider.claim("claim_1", "LEVEL_TO_TARGET")

        with self.assertRaisesRegex(Exception, "旧的或已过期"):
            provider.request_otp(
                lease.lease_id,
                lease.lease_fence,
                "otp_1",
                "challenge_1",
                now,
            )

    def test_otp_a_few_seconds_out_of_step_is_still_this_challenge(self) -> None:
        # Two machines, two clocks. Treating a few seconds of drift as a stale
        # code rejected every OTP a runner ever asked for.
        now = datetime.now(timezone.utc)
        lease = FakeAccountProvider.lease("acct_1", now=now)
        provider = FakeAccountProvider(
            [lease],
            otp_factory=lambda challenge_id, started_at: OtpCode(
                code="123456",
                challenge_id=challenge_id,
                received_at=started_at - timedelta(seconds=6),
                expires_at=started_at - timedelta(seconds=6) + timedelta(seconds=27),
            ),
        )
        provider.claim("claim_1", "LEVEL_TO_TARGET")

        otp = provider.request_otp(
            lease.lease_id,
            lease.lease_fence,
            "otp_1",
            "challenge_1",
            now,
        )
        self.assertEqual(otp.challenge_id, "challenge_1")


class CheckpointStoreTest(unittest.TestCase):
    def test_round_trip_contains_only_the_explicit_safe_schema(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "account-cycle-status.json"
            store = AtomicCheckpointStore(path)
            checkpoint = OrchestrationCheckpoint(
                device_id="device_1",
                workflow_phase=WorkflowPhase.EA_SIGNING_IN,
                claim_request_id="claim_1",
                lease_id="lease_1",
                lease_fence=7,
                account_id="acct_1",
                target_level=20,
                pending_operation=PendingOperation.CREDENTIALS,
                operation_id="operation_1",
            )

            saved = store.save(checkpoint)
            loaded = store.load()
            payload = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(loaded, saved)
            self.assertEqual(payload["leaseFence"], 7)
            self.assertEqual(payload["workflowPhase"], "EA_SIGNING_IN")
            self.assertNotIn("password", path.read_text(encoding="utf-8").lower())
            self.assertNotIn("otp", path.read_text(encoding="utf-8").lower())

    def test_sensitive_nested_fields_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            checkpoint = OrchestrationCheckpoint(
                device_id="device_1",
                report_evidence={"providerToken": "must-not-persist"},
            )

            with self.assertRaisesRegex(ValueError, "敏感字段"):
                store.save(checkpoint)


class LeaseKeeperTest(unittest.TestCase):
    def test_uncertain_retry_reuses_the_same_operation_id(self) -> None:
        lease = FakeAccountProvider.lease("acct_1")

        class FlakyProvider(FakeAccountProvider):
            def __init__(self):
                super().__init__([lease])
                self.operation_ids: list[str] = []
                self.failures = 1

            def renew(
                self,
                lease_id,
                lease_fence,
                operation_id,
                phase,
                run_id,
            ):
                self.operation_ids.append(operation_id)
                if self.failures:
                    self.failures -= 1
                    raise OSError("offline")
                return super().renew(
                    lease_id,
                    lease_fence,
                    operation_id,
                    phase,
                    run_id,
                )

        provider = FlakyProvider()
        keeper = LeaseKeeper(
            provider,
            lease,
            phase=lambda: "APEX_PLAYING",
            run_id=lambda: "run_1",
            operation_id_factory=lambda: "renew_1",
        )

        first = keeper.renew_once()
        second = keeper.renew_once()

        self.assertEqual(first.state, LeaseKeeperState.UNCERTAIN)
        self.assertEqual(second.state, LeaseKeeperState.CURRENT)
        self.assertEqual(provider.operation_ids, ["renew_1", "renew_1"])
        keeper.stop()

    def test_stale_fence_never_returns_to_current(self) -> None:
        lease = FakeAccountProvider.lease("acct_1")

        class StaleProvider(FakeAccountProvider):
            def renew(self, *args, **kwargs):
                raise LeaseStaleError("stale")

        keeper = LeaseKeeper(
            StaleProvider([lease]),
            lease,
            phase=lambda: "APEX_PLAYING",
            run_id=lambda: None,
        )

        snapshot = keeper.renew_once()

        self.assertEqual(snapshot.state, LeaseKeeperState.STALE)
        self.assertFalse(keeper.is_current())
        keeper.stop()


class FakeDrain:
    def __init__(
        self,
        accepted_through: int = 100,
        terminal_error: str | None = None,
    ) -> None:
        self.accepted_through = accepted_through
        self.terminal_error = terminal_error
        self.wakes = 0
        self.stopped = False

    def wake(self) -> None:
        self.wakes += 1

    def stop(self, *, flush_timeout_s: float = 5.0) -> int:
        self.stopped = True
        return 0


class FakeEaDriver:
    def __init__(self, log: list[str], ea_account_id: str) -> None:
        self.log = log
        self.ea_account_id = ea_account_id

    def ensure_started(self) -> EaUiState:
        self.log.append("ea.start")
        return EaUiState.LOGIN

    def current_identity(self):
        self.log.append("ea.current_identity")
        return None

    def sign_in(self, credentials, otp_supplier):
        self.log.append("ea.sign_in")
        now = datetime.now(timezone.utc)
        otp = otp_supplier(OtpChallenge("challenge_1", now))
        self.asserted_otp = otp.code
        return EaIdentityFact(self.ea_account_id, "fake-uia", True)

    def verify_identity(self, expected_ea_account_id: str) -> EaIdentityFact:
        self.log.append("ea.verify_identity")
        return EaIdentityFact(self.ea_account_id, "fake-uia", True)

    def restart_app(self) -> EaUiState:
        self.log.append("ea.restart")
        return EaUiState.SIGNED_IN

    def start_apex(self) -> None:
        self.log.append("apex.start")

    def stop_apex(self) -> ApexExitEvidence:
        self.log.append("apex.stop")
        return ApexExitEvidence(True, True)

    def sign_out(self) -> bool:
        self.log.append("ea.sign_out")
        return True


class ScriptedLeaseKeeper:
    def __init__(self, lease, *, state=LeaseKeeperState.CURRENT) -> None:
        self.lease = lease
        self.state = state
        self.error_code: str | None = None
        self.wakes = 0

    def snapshot(self) -> LeaseKeeperSnapshot:
        return LeaseKeeperSnapshot(
            state=self.state,
            expires_at=self.lease.expires_at,
            renew_after=self.lease.renew_after,
            pending_operation_id=None,
            error_code=self.error_code,
        )

    def is_current(self) -> bool:
        return self.state is LeaseKeeperState.CURRENT

    def start(self) -> None:
        return None

    def wake(self) -> None:
        self.wakes += 1

    def stop(self, *, timeout_s: float = 10.0) -> LeaseKeeperSnapshot:
        self.state = LeaseKeeperState.STOPPED
        return self.snapshot()


class FakeManagedSession:
    def __init__(self, log: list[str], drain: FakeDrain) -> None:
        self.log = log
        self.drain = drain

    def run(
        self,
        identity,
        progression_policy,
        capture_source,
        *,
        lease_is_current,
        on_run_started,
    ) -> PlaySessionResult:
        self.log.append("play.start")
        self.asserted_identity = identity
        self.asserted_target = progression_policy.target_level
        self.asserted_current = lease_is_current()
        on_run_started("run_1")
        return PlaySessionResult(
            status="TARGET_REACHED",
            run_id="run_1",
            account_id=identity.account_id,
            lease_id=identity.lease_id,
            lease_fence=identity.lease_fence,
            level=20,
            xp_current_approx=100,
            xp_required_approx=1000,
            progress_local_seq=8,
            lobby_progress_report_seq=10,
            run_finished_report_seq=12,
            frames=100,
            actions_sent=20,
            rounds_started=2,
            rounds_returned_to_lobby=2,
            report_drain=self.drain,
        )


class AccountOrchestratorTest(unittest.TestCase):
    def test_lease_released_from_the_console_clears_the_local_checkpoint(self) -> None:
        """An operator releasing a lease must not strand the machine.

        Releasing advances the fence, so `status()` rejects the response
        against the fence the checkpoint still holds and raises before the
        state it fetched can be read. That used to surface as LEASE_STALE on
        every subsequent run, with no way out but deleting the file by hand.
        """

        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1", fence=18)
            provider = FakeAccountProvider([lease])
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            store.save(
                OrchestrationCheckpoint(
                    device_id="device_1",
                    workflow_phase=WorkflowPhase.EA_SIGNING_OUT,
                    lease_id=lease.lease_id,
                    lease_fence=1,
                    account_id=lease.account_id,
                    target_level=20,
                )
            )

            class NeverPlay:
                def run(self, *args, **kwargs):
                    raise AssertionError("a cleared checkpoint must not replay")

            result = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=NeverPlay(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                notify=lambda _: None,
            ).run_once()

            self.assertEqual(result.outcome, AccountCycleOutcome.NO_ACCOUNT)
            self.assertFalse(store.load().has_lease)

    def test_ea_login_failure_cleans_up_closes_lease_and_clears_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = replace(
                FakeAccountProvider.lease("acct_1"),
                expected_ea_account_id="ea_1",
            )
            provider = FakeAccountProvider(
                [lease],
                credentials={
                    "acct_1": SecretCredentials("login@example.test", "password")
                },
            )
            log: list[str] = []

            class LoginFailureDriver(FakeEaDriver):
                def sign_in(self, credentials, otp_supplier):
                    self.log.append("ea.sign_in")
                    raise EaAppAutomationError("login failed")

            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            result = AccountOrchestrator(
                provider=provider,
                ea_driver=LoginFailureDriver(log, "ea_1"),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                operation_id_factory=iter(
                    ["claim_1", "renew_1", "credentials_1", "close_1"]
                ).__next__,
                notify=lambda _: None,
            ).run_once()

            self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
            close_call = [item for item in provider.calls if item[0] == "close"][-1]
            self.assertEqual(close_call[1][3], "FAILED")
            self.assertLess(log.index("apex.stop"), log.index("ea.sign_out"))
            self.assertFalse(store.load().has_lease)

    def test_happy_path_orders_cleanup_before_close_and_clears_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            lease = replace(
                FakeAccountProvider.lease("acct_1", now=now),
                expected_ea_account_id="ea_1",
            )
            log: list[str] = []

            class OrderedProvider(FakeAccountProvider):
                def close(self, *args, **kwargs):
                    log.append("provider.close")
                    return super().close(*args, **kwargs)

            provider = OrderedProvider(
                [lease],
                credentials={
                    "acct_1": SecretCredentials(
                        "login@example.test",
                        "password-value",
                    )
                },
                otp_factory=lambda challenge_id, started_at: OtpCode(
                    code="123456",
                    challenge_id=challenge_id,
                    received_at=started_at,
                    expires_at=started_at + timedelta(minutes=1),
                ),
            )
            drain = FakeDrain()
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            driver = FakeEaDriver(log, "ea_1")
            session = FakeManagedSession(log, drain)
            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=driver,
                play_session=session,
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                operation_id_factory=iter(
                    [
                        "claim_1",
                        "renew_1",
                        "credentials_1",
                        "otp_1",
                        "close_1",
                    ]
                ).__next__,
                completion_poll_s=0.1,
                sleep=lambda _: None,
                notify=lambda _: None,
            )

            result = orchestrator.run_once()

            self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
            self.assertTrue(session.asserted_current)
            self.assertEqual(session.asserted_target, 20)
            self.assertEqual(session.asserted_identity.lease_fence, lease.lease_fence)
            self.assertEqual(driver.asserted_otp, "123456")
            self.assertLess(log.index("apex.stop"), log.index("ea.sign_out"))
            self.assertLess(log.index("ea.sign_out"), log.index("provider.close"))
            self.assertTrue(drain.stopped)
            checkpoint = store.load()
            self.assertIsNotNone(checkpoint)
            self.assertFalse(checkpoint.has_lease)
            persisted = "\n".join(
                path.read_text(encoding="utf-8")
                for path in Path(directory).iterdir()
                if path.is_file()
            )
            self.assertNotIn("login@example.test", persisted)
            self.assertNotIn("password-value", persisted)
            self.assertNotIn("123456", persisted)

    def test_download_state_restarts_reverifies_and_retries_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            lease = replace(
                FakeAccountProvider.lease("acct_1", now=now),
                expected_ea_account_id="ea_1",
            )
            provider = FakeAccountProvider(
                [lease],
                credentials={
                    "acct_1": SecretCredentials("login@example.test", "password")
                },
                otp_factory=lambda challenge_id, started_at: OtpCode(
                    code="123456",
                    challenge_id=challenge_id,
                    received_at=started_at,
                    expires_at=started_at + timedelta(minutes=1),
                ),
            )
            log: list[str] = []

            class DownloadOnceDriver(FakeEaDriver):
                def __init__(self):
                    super().__init__(log, "ea_1")
                    self.starts = 0

                def start_apex(self) -> None:
                    self.starts += 1
                    self.log.append("apex.start")
                    if self.starts == 1:
                        raise EaApexDownloadRequired("Download")

            driver = DownloadOnceDriver()
            session = FakeManagedSession(log, FakeDrain())
            result = AccountOrchestrator(
                provider=provider,
                ea_driver=driver,
                play_session=session,
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                operation_id_factory=iter(
                    ["claim_1", "renew_1", "credentials_1", "otp_1", "close_1"]
                ).__next__,
                completion_poll_s=0.1,
                sleep=lambda _: None,
                notify=lambda _: None,
            ).run_once()

            self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
            self.assertEqual(driver.starts, 2)
            first_start = log.index("apex.start")
            restart = log.index("ea.restart")
            second_verify = log.index("ea.verify_identity", restart)
            second_start = log.index("apex.start", first_start + 1)
            self.assertLess(first_start, restart)
            self.assertLess(restart, second_verify)
            self.assertLess(second_verify, second_start)
            self.assertLess(second_start, log.index("play.start"))

    def test_download_state_is_only_retried_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            lease = replace(
                FakeAccountProvider.lease("acct_1", now=now),
                expected_ea_account_id="ea_1",
            )
            provider = FakeAccountProvider(
                [lease],
                credentials={
                    "acct_1": SecretCredentials("login@example.test", "password")
                },
                otp_factory=lambda challenge_id, started_at: OtpCode(
                    code="123456",
                    challenge_id=challenge_id,
                    received_at=started_at,
                    expires_at=started_at + timedelta(minutes=1),
                ),
            )
            log: list[str] = []

            class AlwaysDownloadDriver(FakeEaDriver):
                def start_apex(self) -> None:
                    self.log.append("apex.start")
                    raise EaApexDownloadRequired("Download")

            result = AccountOrchestrator(
                provider=provider,
                ea_driver=AlwaysDownloadDriver(log, "ea_1"),
                play_session=object(),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                operation_id_factory=iter(
                    ["claim_1", "renew_1", "credentials_1", "otp_1", "close_1"]
                ).__next__,
                sleep=lambda _: None,
                notify=lambda _: None,
            ).run_once()

            self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
            self.assertEqual(result.error_code, "APEX_START_FAILED")
            self.assertEqual(log.count("apex.start"), 2)
            self.assertEqual(log.count("ea.restart"), 1)

    def test_transient_renew_failure_after_apex_start_recovers_in_place(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            now = datetime.now(timezone.utc)
            lease = replace(
                FakeAccountProvider.lease("acct_1", now=now),
                expected_ea_account_id="ea_1",
            )
            provider = FakeAccountProvider(
                [lease],
                credentials={
                    "acct_1": SecretCredentials("login@example.test", "password")
                },
                otp_factory=lambda challenge_id, started_at: OtpCode(
                    code="123456",
                    challenge_id=challenge_id,
                    received_at=started_at,
                    expires_at=started_at + timedelta(minutes=1),
                ),
            )
            log: list[str] = []
            notices: list[str] = []
            sleeps: list[float] = []
            keepers: list[ScriptedLeaseKeeper] = []

            def keeper_factory(_provider, current_lease, **_kwargs):
                keeper = ScriptedLeaseKeeper(current_lease)
                keepers.append(keeper)
                return keeper

            class LeaseDropsAfterLaunch(FakeEaDriver):
                def start_apex(self) -> None:
                    super().start_apex()
                    keepers[0].state = LeaseKeeperState.UNCERTAIN

            def sleep(delay: float) -> None:
                sleeps.append(delay)
                keepers[0].state = LeaseKeeperState.CURRENT

            result = AccountOrchestrator(
                provider=provider,
                ea_driver=LeaseDropsAfterLaunch(log, "ea_1"),
                play_session=FakeManagedSession(log, FakeDrain()),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                lease_keeper_factory=keeper_factory,
                operation_id_factory=iter(
                    ["claim_1", "credentials_1", "otp_1", "close_1"]
                ).__next__,
                sleep=sleep,
                notify=notices.append,
            ).run_once()

            self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
            self.assertEqual(log.count("ea.start"), 1)
            self.assertEqual(log.count("ea.verify_identity"), 1)
            self.assertEqual(log.count("apex.start"), 1)
            self.assertLess(log.index("apex.start"), log.index("play.start"))
            self.assertEqual(sleeps, [5.0])
            self.assertEqual(keepers[0].wakes, 1)
            self.assertTrue(any("续租已恢复" in notice for notice in notices))

    def test_renew_failure_pauses_after_sixty_second_grace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")
            keeper = ScriptedLeaseKeeper(
                lease,
                state=LeaseKeeperState.UNCERTAIN,
            )
            sleeps: list[float] = []
            notices: list[str] = []
            orchestrator = AccountOrchestrator(
                provider=FakeAccountProvider(),
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                sleep=sleeps.append,
                notify=notices.append,
            )

            result = orchestrator._lease_gate(keeper)

            self.assertIsNotNone(result)
            self.assertEqual(result.outcome, AccountCycleOutcome.PAUSED)
            self.assertEqual(result.error_code, "LEASE_UNCERTAIN")
            self.assertEqual(sum(sleeps), 60.0)
            self.assertEqual(keeper.wakes, 12)
            self.assertTrue(any("60 秒内仍未恢复" in notice for notice in notices))

    def test_stale_lease_stops_immediately_without_using_the_grace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")
            keeper = ScriptedLeaseKeeper(
                lease,
                state=LeaseKeeperState.STALE,
            )
            keeper.error_code = "LEASE_STALE"
            orchestrator = AccountOrchestrator(
                provider=FakeAccountProvider(),
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                sleep=lambda _delay: self.fail("stale lease must not wait"),
                notify=lambda _message: None,
            )

            result = orchestrator._lease_gate(keeper)

            self.assertIsNotNone(result)
            self.assertEqual(result.outcome, AccountCycleOutcome.PAUSED)
            self.assertEqual(result.error_code, "LEASE_STALE")
            self.assertEqual(
                orchestrator._checkpoint.run_state,
                OrchestratorRunState.PAUSED_MANUAL,
            )

    def test_unrecovered_lease_stops_the_just_started_apex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")
            keeper = ScriptedLeaseKeeper(
                lease,
                state=LeaseKeeperState.UNCERTAIN,
            )
            log: list[str] = []
            notices: list[str] = []
            orchestrator = AccountOrchestrator(
                provider=FakeAccountProvider(),
                ea_driver=FakeEaDriver(log, "ea_1"),
                play_session=object(),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                sleep=lambda _delay: None,
                notify=notices.append,
            )

            result = orchestrator._lease_gate_after_apex_start(keeper)

            self.assertIsNotNone(result)
            self.assertEqual(result.error_code, "LEASE_UNCERTAIN")
            self.assertEqual(log, ["apex.stop"])
            self.assertTrue(any("已停止刚启动的 Apex" in item for item in notices))

    def test_claim_response_crash_recovers_current_lease_without_second_claim(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")

            class CrashAfterClaimProvider(FakeAccountProvider):
                def __init__(self):
                    super().__init__([lease])
                    self.crash = True
                    self.claim_count = 0

                def claim(self, claim_request_id, task_type):
                    self.claim_count += 1
                    result = super().claim(claim_request_id, task_type)
                    if self.crash:
                        self.crash = False
                        raise OSError("response lost")
                    return result

            provider = CrashAfterClaimProvider()
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            first = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                operation_id_factory=lambda: "claim_1",
                notify=lambda _: None,
            )

            failed = first.run_once()
            checkpoint_after_crash = store.load()
            second = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                operation_id_factory=lambda: "unused",
                notify=lambda _: None,
            )
            recovered = second._obtain_lease()

            self.assertEqual(failed.outcome, AccountCycleOutcome.PAUSED)
            self.assertEqual(checkpoint_after_crash.claim_request_id, "claim_1")
            self.assertEqual(recovered, lease)
            self.assertEqual(provider.claim_count, 1)

    def test_completion_pending_restart_drains_and_never_replays_session(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")
            provider = FakeAccountProvider(
                [lease],
                complete_on_close=False,
            )
            provider.claim("claim_1", "LEVEL_TO_TARGET")
            evidence = CompletionEvidence("run_1", 20, 10, 12)
            provider.close(
                lease.lease_id,
                lease.lease_fence,
                "close_1",
                "TARGET_REACHED",
                "run_1",
                evidence,
                "TARGET_REACHED",
                CleanupEvidence(True, True, True),
            )
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            store.save(
                OrchestrationCheckpoint(
                    device_id="device_1",
                    workflow_phase=WorkflowPhase.LEASE_COMPLETING,
                    lease_id=lease.lease_id,
                    lease_fence=lease.lease_fence,
                    account_id=lease.account_id,
                    target_level=20,
                    active_play_run_id="run_1",
                    target_reading={
                        "level": 20,
                        "xpCurrentApprox": 100,
                        "xpRequiredApprox": 1000,
                        "localSeq": 8,
                    },
                    report_evidence={
                        "runId": "run_1",
                        "lobbyProgressSeq": 10,
                        "runFinishedSeq": 12,
                    },
                )
            )
            drain = FakeDrain(accepted_through=12)
            replayed = {"value": False}

            class NeverPlay:
                def run(self, *args, **kwargs):
                    replayed["value"] = True
                    raise AssertionError("completion recovery must not replay")

            def complete(_: float) -> None:
                provider.complete(lease.lease_id)

            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=NeverPlay(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                recover_report_drain=lambda run_id: (
                    drain if run_id == "run_1" else None
                ),
                sleep=complete,
                completion_poll_s=0.1,
                notify=lambda _: None,
            )

            result = orchestrator.run_once()

            self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
            self.assertFalse(replayed["value"])
            self.assertTrue(drain.stopped)
            self.assertFalse(store.load().has_lease)

    def test_close_maps_all_session_results_to_server_outcomes(self) -> None:
        cases = (
            ("TARGET_REACHED", None, "TARGET_REACHED", AccountCycleOutcome.COMPLETED),
            ("FAILED", "PLAY_SESSION_FAILED", "FAILED", AccountCycleOutcome.COMPLETED),
            ("STOPPED", "OPERATOR_STOPPED", "RELEASED", AccountCycleOutcome.STOPPED),
            ("PLAYED", None, "RELEASED", AccountCycleOutcome.PAUSED),
            (
                "PLAYED",
                "KNOWN_STATE_STALL_UNRECOVERED",
                "RELEASED",
                AccountCycleOutcome.PAUSED,
            ),
        )
        for status, error_code, expected_outcome, expected_cycle in cases:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                lease = FakeAccountProvider.lease("acct_1")
                provider = FakeAccountProvider([lease])
                drain = FakeDrain(accepted_through=12)
                result = PlaySessionResult(
                    status=status,
                    run_id="run_1",
                    account_id=lease.account_id,
                    lease_id=lease.lease_id,
                    lease_fence=lease.lease_fence,
                    level=20 if status == "TARGET_REACHED" else 15,
                    xp_current_approx=100,
                    xp_required_approx=1000,
                    progress_local_seq=8,
                    lobby_progress_report_seq=10,
                    run_finished_report_seq=12,
                    frames=1,
                    actions_sent=0,
                    rounds_started=0,
                    rounds_returned_to_lobby=0,
                    error_code=error_code,
                    report_drain=drain,
                )
                orchestrator = AccountOrchestrator(
                    provider=provider,
                    ea_driver=object(),
                    play_session=object(),
                    checkpoint_store=AtomicCheckpointStore(
                        Path(directory) / "account-cycle-status.json"
                    ),
                    device_id="device_1",
                    capture_source=object(),
                    operation_id_factory=lambda: "close_1",
                    notify=lambda _: None,
                )
                evidence = (
                    CompletionEvidence("run_1", 20, 10, 12)
                    if status == "TARGET_REACHED"
                    else None
                )

                cycle = orchestrator._submit_close(
                    lease,
                    result,
                    evidence,
                    CleanupEvidence(True, True, True),
                )

                close_call = [item for item in provider.calls if item[0] == "close"][-1]
                self.assertEqual(close_call[1][3], expected_outcome)
                self.assertEqual(cycle.outcome, expected_cycle)
                if status == "PLAYED" and error_code is not None:
                    self.assertEqual(cycle.error_code, error_code)

    def test_failed_restart_with_level_never_becomes_target_reached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")
            provider = FakeAccountProvider([lease])
            provider.claim("claim_1", "LEVEL_TO_TARGET")
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            store.save(
                OrchestrationCheckpoint(
                    device_id="device_1",
                    workflow_phase=WorkflowPhase.LEASE_COMPLETING,
                    lease_id=lease.lease_id,
                    lease_fence=lease.lease_fence,
                    account_id=lease.account_id,
                    target_level=20,
                    active_play_run_id="run_1",
                    target_reading={"level": 15},
                    report_evidence={"runId": "run_1", "runFinishedSeq": 12},
                    result_status="FAILED",
                    result_error_code="PLAY_SESSION_FAILED",
                )
            )
            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                recover_report_drain=lambda _: FakeDrain(accepted_through=12),
                operation_id_factory=lambda: "close_1",
                notify=lambda _: None,
            )

            cycle = orchestrator.run_once()

            close_call = [item for item in provider.calls if item[0] == "close"][-1]
            self.assertEqual(close_call[1][3], "FAILED")
            self.assertEqual(cycle.outcome, AccountCycleOutcome.COMPLETED)

    def test_server_only_unsafe_states_pause_before_any_side_effect(self) -> None:
        cases = (
            (LeaseState.ACTIVE, "RUNNING", "REMOTE_RUNNING_RECOVERY_REQUIRED"),
            (
                LeaseState.COMPLETION_PENDING,
                "COMPLETION_PENDING",
                "REMOTE_COMPLETION_PENDING_RECOVERY_REQUIRED",
            ),
            (
                LeaseState.EXPIRED_UNCONFIRMED,
                "EXPIRED_UNCONFIRMED",
                "REMOTE_EXPIRED_UNCONFIRMED_RECOVERY_REQUIRED",
            ),
        )
        for state, provider_status, reason in cases:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as directory:
                lease = FakeAccountProvider.lease("acct_1")

                class RemoteOnlyProvider(FakeAccountProvider):
                    def current(self):
                        return LeaseStatus(
                            lease_id=lease.lease_id,
                            lease_fence=lease.lease_fence,
                            account_id=lease.account_id,
                            state=state,
                            expires_at=lease.expires_at,
                            renew_after=lease.renew_after,
                            provider_status=provider_status,
                        )

                provider = RemoteOnlyProvider([lease])
                orchestrator = AccountOrchestrator(
                    provider=provider,
                    ea_driver=object(),
                    play_session=object(),
                    checkpoint_store=AtomicCheckpointStore(
                        Path(directory) / "account-cycle-status.json"
                    ),
                    device_id="device_1",
                    capture_source=object(),
                    notify=lambda _: None,
                )

                result = orchestrator.run_once()

                self.assertEqual(result.outcome, AccountCycleOutcome.PAUSED)
                self.assertEqual(result.error_code, reason)
                self.assertEqual(
                    [name for name, _ in provider.calls if name in {"credentials", "renew"}],
                    [],
                )

    def test_otp_validity_survives_a_runner_clock_ahead_of_the_server(self) -> None:
        # The challenge time is the runner's clock and receivedAt is the
        # Provider's. A runner a few seconds ahead used to fail every attempt.
        server_now = datetime(2026, 8, 1, 22, 49, 40, tzinfo=timezone.utc)
        challenge_started_at = server_now + timedelta(seconds=6)
        otp = OtpCode(
            code="123456",
            challenge_id="challenge_1",
            received_at=server_now,
            expires_at=server_now + timedelta(seconds=27),
        )

        self.assertTrue(otp.valid_for("challenge_1", challenge_started_at))
        self.assertFalse(otp.valid_for("challenge_2", challenge_started_at))
        # An answer from a materially older challenge is still refused.
        stale = OtpCode(
            code="123456",
            challenge_id="challenge_1",
            received_at=server_now - timedelta(minutes=10),
            expires_at=server_now - timedelta(minutes=10) + timedelta(seconds=27),
        )
        self.assertFalse(stale.valid_for("challenge_1", challenge_started_at))

    def test_otp_lifetime_comes_from_the_provider_clock_alone(self) -> None:
        server_now = datetime(2026, 8, 1, 22, 49, 40, tzinfo=timezone.utc)
        nearly_expired = OtpCode(
            code="123456",
            challenge_id="challenge_1",
            received_at=server_now,
            expires_at=server_now + timedelta(seconds=2),
        )
        self.assertEqual(nearly_expired.lifetime_s, 2.0)
        # Still a valid answer to this challenge — it is the caller's job to
        # decide two seconds is not enough to type it.
        self.assertTrue(nearly_expired.valid_for("challenge_1", server_now))

    def test_resume_clears_a_manual_pause_the_operator_has_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            store.save(
                OrchestrationCheckpoint(
                    device_id="device_1",
                    run_state=OrchestratorRunState.PAUSED_MANUAL,
                    last_error_code="RUNNER_PAUSED",
                )
            )
            orchestrator = AccountOrchestrator(
                provider=FakeAccountProvider(),
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                notify=lambda _: None,
            )

            self.assertTrue(orchestrator.resume())
            reloaded = store.load()
            self.assertEqual(reloaded.run_state, OrchestratorRunState.ACTIVE)
            self.assertIsNone(reloaded.last_error_code)
            self.assertIsNone(reloaded.resume_phase)
            # Nothing to clear on an already active checkpoint.
            self.assertFalse(orchestrator.resume())

    def test_server_safe_release_clears_a_stale_pause_without_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")
            provider = FakeAccountProvider([lease])
            provider.claim("claim_1", "LEVEL_TO_TARGET")
            provider._statuses[lease.lease_id] = LeaseStatus(
                lease_id=lease.lease_id,
                lease_fence=lease.lease_fence,
                account_id=lease.account_id,
                state=LeaseState.CLOSED,
                provider_status="RELEASED",
            )
            provider._current_lease_id = None
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            store.save(
                OrchestrationCheckpoint(
                    device_id="device_1",
                    lease_id=lease.lease_id,
                    lease_fence=lease.lease_fence,
                    account_id=lease.account_id,
                    target_level=lease.target_level,
                    run_state=OrchestratorRunState.PAUSED_MANUAL,
                    last_error_code="LEASE_STALE",
                )
            )
            notices: list[str] = []
            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                notify=notices.append,
            )

            cycle = orchestrator.run_once()

            self.assertEqual(cycle.outcome, AccountCycleOutcome.NO_ACCOUNT)
            checkpoint = store.load()
            self.assertEqual(checkpoint.run_state, OrchestratorRunState.ACTIVE)
            self.assertFalse(checkpoint.has_lease)
            self.assertTrue(any("自动清除本地暂停" in item for item in notices))

    def test_non_lease_manual_pause_never_contacts_provider_for_auto_resume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            store.save(
                OrchestrationCheckpoint(
                    device_id="device_1",
                    run_state=OrchestratorRunState.PAUSED_MANUAL,
                    last_error_code="CAPTCHA",
                )
            )

            class MustNotCall(FakeAccountProvider):
                def current(self):
                    raise AssertionError("CAPTCHA pause must remain local and sticky")

            cycle = AccountOrchestrator(
                provider=MustNotCall(),
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                notify=lambda _: None,
            ).run_once()

            self.assertEqual(cycle.outcome, AccountCycleOutcome.PAUSED)
            self.assertEqual(cycle.error_code, "CAPTCHA")

    def test_manual_pause_survives_restart_and_retryable_pause_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = AtomicCheckpointStore(
                Path(directory) / "account-cycle-status.json"
            )
            store.save(
                OrchestrationCheckpoint(
                    device_id="device_1",
                    run_state=OrchestratorRunState.PAUSED_MANUAL,
                    last_error_code="CAPTCHA",
                )
            )

            class MustNotCall(FakeAccountProvider):
                def current(self):
                    raise AssertionError("manual pause must not contact provider")

            paused = AccountOrchestrator(
                provider=MustNotCall(),
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=store,
                device_id="device_1",
                capture_source=object(),
                notify=lambda _: None,
            ).run_once()
            self.assertEqual(paused.error_code, "CAPTCHA")

        with tempfile.TemporaryDirectory() as directory:
            class FlakyCurrent(FakeAccountProvider):
                def __init__(self):
                    super().__init__()
                    self.current_calls = 0

                def current(self):
                    self.current_calls += 1
                    if self.current_calls == 1:
                        raise LeaseProviderError(
                            "offline",
                            code="PROVIDER_UNREACHABLE",
                            retryable=True,
                        )
                    return None

            provider = FlakyCurrent()
            sleeps = 0
            orchestrator = None
            notices: list[str] = []

            def sleep(_: float) -> None:
                nonlocal sleeps
                sleeps += 1
                if sleeps == 2:
                    orchestrator.stop()

            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                sleep=sleep,
                notify=notices.append,
            )

            self.assertEqual(orchestrator.run_forever(idle_s=1), 0)
            self.assertEqual(provider.current_calls, 3)
            self.assertTrue(
                any(
                    "PROVIDER_UNREACHABLE" in notice and "自动重试" in notice
                    for notice in notices
                )
            )

    def test_terminal_reporting_error_pauses_instead_of_polling_forever(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lease = FakeAccountProvider.lease("acct_1")
            provider = FakeAccountProvider([lease])
            result = PlaySessionResult(
                status="FAILED",
                run_id="run_1",
                account_id=lease.account_id,
                lease_id=lease.lease_id,
                lease_fence=lease.lease_fence,
                level=10,
                xp_current_approx=None,
                xp_required_approx=None,
                progress_local_seq=None,
                lobby_progress_report_seq=None,
                run_finished_report_seq=3,
                frames=1,
                actions_sent=0,
                rounds_started=0,
                rounds_returned_to_lobby=0,
                report_drain=FakeDrain(
                    accepted_through=0,
                    terminal_error="HTTP 401",
                ),
            )
            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                notify=lambda _: None,
            )

            paused = orchestrator._wait_for_completion(
                lease, result, LeaseState.CLOSED
            )

            self.assertEqual(paused.outcome, AccountCycleOutcome.PAUSED)
            self.assertEqual(paused.error_code, "REPORTING_TERMINAL_ERROR")

    def test_no_account_reports_the_retry_delay_before_sleeping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            provider = FakeAccountProvider()
            provider.claim_retry_after_s = 45.0
            notices: list[str] = []
            sleeps: list[float] = []
            orchestrator = None

            def sleep(delay: float) -> None:
                sleeps.append(delay)
                orchestrator.stop()

            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=object(),
                play_session=object(),
                checkpoint_store=AtomicCheckpointStore(
                    Path(directory) / "account-cycle-status.json"
                ),
                device_id="device_1",
                capture_source=object(),
                sleep=sleep,
                notify=notices.append,
            )

            self.assertEqual(orchestrator.run_forever(idle_s=30), 0)
            self.assertEqual(sleeps, [45.0])
            self.assertEqual(
                notices,
                [
                    "账号池暂无可领账号；Runner 仍在运行，"
                    "45 秒后再次尝试领号"
                ],
            )


if __name__ == "__main__":
    unittest.main()

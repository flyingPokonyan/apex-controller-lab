"""EA transitions that were judged as failures although EA was fine.

Each case replays a Forge evidence sequence from 2026-10-16:

- lease 3073: the password was accepted, EA closed its login window and the
  main window stayed blank, then showed a spinner, then the signed-in Home.
- lease 3074: the entry click landed just before a new account's library
  tour popped up; after closing the tour EA sat on Home with Apex in the left
  column and the Runner reported that Play was missing.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))

from apex_automation.account_orchestrator import AccountCycleOutcome, AccountOrchestrator
from apex_automation.account_provider import FakeAccountProvider, OtpCode, SecretCredentials
from apex_automation.ea_app import (
    EaApexControlMissing,
    EaApexStartFailed,
    EaAppAutomationError,
    EaIdentityFact,
    EaIdentityMismatch,
    EaIdentityUnconfirmed,
    EaWindowUnavailable,
)
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_pages import EaPage, classify_page
from apex_automation.ocr_obstacles import OcrToken
from apex_automation.orchestration_state import AtomicCheckpointStore
from test_account_orchestration import FakeDrain, FakeEaDriver, FakeManagedSession

WINDOW = (0, 0, 1920, 1080)


def observation(*placed: tuple[str, int, int], page: EaPage | None = None) -> EaObservation:
    tokens = tuple(OcrToken(text, 0.99, (x - 50, y - 10, x + 50, y + 10)) for text, x, y in placed)
    return EaObservation(
        rect=WINDOW,
        frame=np.zeros((1, 1), dtype=np.uint8),
        tokens=tokens,
        page=page or classify_page(token.normalized for token in tokens),
        hwnd=1,
        process_id=42,
        source_verified=True,
    )


SPINNER = observation(page=EaPage.UNKNOWN)
SIGNED_IN = observation(("Home", 180, 145), ("Library", 190, 225), ("Apex Legends", 205, 360),
                        page=EaPage.SIGNED_IN)
PASSWORD = observation(("Enter your password", 960, 300), ("PASSWORD", 860, 380), ("SIGN IN", 960, 490),
                       page=EaPage.PASSWORD)
EMAIL = observation(("Sign in", 960, 300), ("Email or EA ID", 960, 380), ("NEXT", 960, 490),
                    page=EaPage.EMAIL)
HOME = observation(("Home", 180, 145), ("Free-to-play games", 220, 430), ("Apex Legends", 260, 650),
                   page=EaPage.SIGNED_IN)
APEX_PAGE = observation(("Apex Legends", 800, 280), ("PLAY", 1150, 700), page=EaPage.SIGNED_IN)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def scripted(items):
    """Return each scripted frame in turn; an exception in the script is raised."""

    items = list(items)

    def next_item(*_args, **_kwargs):
        item = items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return next_item


def bare_driver(clock: Clock) -> WindowsEaHybridDriver:
    driver = object.__new__(WindowsEaHybridDriver)
    driver.sleep = clock.sleep
    driver.evidence = None
    driver.notify = Mock()
    driver.records = []
    driver._record = lambda step, *_args, **_kwargs: driver.records.append(step)
    driver._dismiss_expired_session = lambda _hwnd: False
    driver._dismiss_library_tour = lambda _hwnd, current: current
    driver._raise_if_account_banned = lambda _hwnd, _current: None
    return driver


class SignInWindowSwapTest(unittest.TestCase):
    def test_vanished_login_window_after_enter_counts_as_submitted(self) -> None:
        clock = Clock()
        driver = bare_driver(clock)
        driver._focus = Mock()
        driver._tap = Mock()
        driver._observe = scripted([EaWindowUnavailable("blank main window")])
        driver._click_target = Mock()

        self.assertEqual(driver._submit(1, PASSWORD, (0.5, 0.5)), "enter")
        driver._click_target.assert_not_called()

    def test_identity_wait_rides_out_the_blank_main_window(self) -> None:
        clock = Clock()
        driver = bare_driver(clock)
        driver._ea_window_for_identity = scripted([
            EaWindowUnavailable("login window closed"),
            EaWindowUnavailable("main window blank"),
            1,
        ])
        driver._observe = scripted([SIGNED_IN])
        identity = EaIdentityFact("ea_1", "badge", True)
        driver._identity = lambda _hwnd: identity

        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            found = driver._await_identity(
                1,
                otp_supplier=Mock(),
                otp_methods=(),
                initial_challenge_started_at=datetime.now(timezone.utc),
                expected_ea_account_id="ea_1",
            )

        self.assertIs(found, identity)
        self.assertEqual(driver.records, ["awaiting-signed_in", "signed-in"])

    def test_retry_waits_for_the_spinner_instead_of_calling_it_a_wrong_page(self) -> None:
        clock = Clock()
        driver = bare_driver(clock)
        driver._ea_window = scripted([EaWindowUnavailable("main window blank"), 1, 1, 1])
        driver._observe = scripted([SPINNER, SPINNER, SIGNED_IN])

        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            hwnd, current = driver._signin_surface()

        self.assertEqual(hwnd, 1)
        self.assertIs(current.page, EaPage.SIGNED_IN)
        self.assertLess(clock.now, 30.0)

    def test_a_page_that_stays_unknown_is_still_reported(self) -> None:
        clock = Clock()
        driver = bare_driver(clock)
        driver._ea_window = lambda: 1
        driver._observe = lambda _hwnd: SPINNER

        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            _hwnd, current = driver._signin_surface()

        self.assertIs(current.page, EaPage.UNKNOWN)
        self.assertGreaterEqual(clock.now, 30.0)

    def test_accepted_password_signs_in_without_a_retry(self) -> None:
        """Lease 3073 end to end: password, window swap, spinner, Home."""

        clock = Clock()
        driver = bare_driver(clock)
        driver._ea_window = lambda: 1
        driver._return_to_account_page = lambda _hwnd, current: current
        driver._submit_login_identifier = lambda _hwnd, _current, _credentials: PASSWORD
        driver._click_login_field = lambda _hwnd, _current, password=False: "placeholder"
        driver._clear_focused_field = Mock()
        driver._type_secret = Mock()
        driver._focus = Mock()
        driver._tap = Mock()
        driver._observe = scripted([
            EMAIL,                                        # sign-in starts on the account page
            PASSWORD,                                     # password typed
            EaWindowUnavailable("login window closed"),   # 3 s after Enter
            SIGNED_IN,                                    # identity wait, once painted
        ])
        driver._ea_window_for_identity = scripted([EaWindowUnavailable("main window blank"), 1])
        identity = EaIdentityFact("ea_1", "badge", True)
        driver._identity = lambda _hwnd: identity

        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            found = driver._sign_in_once(SecretCredentials("login@example.test", "password"), Mock())

        self.assertIs(found, identity)
        self.assertNotIn("signin-wrong-page", driver.records)
        self.assertEqual(driver.records.count("signin-start"), 1)


class AccountPanelSkeletonTest(unittest.TestCase):
    """Lease 3094: our own sign-in reached Home, the account panel never loaded."""

    def identity_wait(self, driver, clock):
        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            return driver._await_identity(
                1, otp_supplier=Mock(), otp_methods=(),
                initial_challenge_started_at=datetime.now(timezone.utc),
                expected_ea_account_id="ea_1",
            )

    def skeleton_driver(self, clock, *, entered=True, read=None):
        driver = bare_driver(clock)
        driver._ea_window_for_identity = lambda hwnd: hwnd
        driver._ea_window = lambda: 1
        driver._observe = lambda _hwnd: SIGNED_IN
        driver._identity = lambda _hwnd: read
        driver._matching_identity = lambda *_args, **_kwargs: None
        driver._entered_credentials = entered
        driver._signed_in_as = None
        return driver

    def test_own_sign_in_continues_when_no_account_id_ever_loads(self) -> None:
        clock = Clock()
        driver = self.skeleton_driver(clock)
        found = self.identity_wait(driver, clock)
        self.assertEqual((found.ea_account_id, found.source, found.verified), ("ea_1", "signin-flow", True))
        self.assertEqual(driver.records[-1], "signed-in")
        self.assertLess(clock.now, 30.0)
        # The identity check that follows accepts the same sign-in.
        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            verified = driver.verify_identity("ea_1")
        self.assertEqual((verified.ea_account_id, verified.source), ("ea_1", "signin-flow"))
        self.assertEqual(driver.records[-1], "identity-verified")

    def test_a_session_this_sign_in_did_not_enter_still_times_out(self) -> None:
        clock = Clock()
        driver = self.skeleton_driver(clock, entered=False)
        with self.assertRaises(EaAppAutomationError):
            self.identity_wait(driver, clock)
        self.assertEqual(driver.records[-1], "signin-timeout")
        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            with self.assertRaises(EaIdentityUnconfirmed):
                driver.verify_identity("ea_1")

    def test_a_session_this_sign_in_entered_is_ours_whatever_the_corner_reads(self) -> None:
        # D791 2026-10-26 (leases 3266, 3269): the rental panel over EA's corner
        # read as "d791" while the account panel was still loading.
        for read in (EaIdentityFact("someone_else", "badge", False), EaIdentityFact("d791", "badge", True)):
            with self.subTest(read=read):
                clock = Clock()
                driver = self.skeleton_driver(clock, read=read)
                found = self.identity_wait(driver, clock)
                self.assertEqual((found.ea_account_id, found.source), ("ea_1", "signin-flow"))
                self.assertEqual(driver._signed_in_as, "ea_1")
                with patch("apex_automation.ea_app_win32.time.monotonic", clock):
                    verified = driver.verify_identity("ea_1")
                self.assertEqual((verified.ea_account_id, verified.source), ("ea_1", "signin-flow"))
                self.assertEqual(driver.records[-1], "identity-verified")

    def test_another_readable_account_still_rejects_a_session_this_sign_in_did_not_enter(self) -> None:
        clock = Clock()
        driver = self.skeleton_driver(clock, entered=False, read=EaIdentityFact("d791", "badge", True))
        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            with self.assertRaises(EaIdentityMismatch):
                driver.verify_identity("ea_1")
        self.assertIsNone(driver._signed_in_as)


class SignOutWindowSwapTest(unittest.TestCase):
    def test_signout_finds_the_login_window_behind_a_blank_main_frame(self) -> None:
        """Lease 3076: Sign out worked; the login window sat behind a blank main frame."""

        clock = Clock()
        driver = bare_driver(clock)
        blank = observation(page=EaPage.UNKNOWN)
        main_frames = [observation(page=EaPage.SIGNED_IN)]
        windows = iter([1, 2])  # the main window first, then the login window
        driver._ea_window = lambda: next(windows)
        driver._observe = lambda hwnd: EMAIL if hwnd == 2 else (main_frames.pop(0) if main_frames else blank)
        clicks: list[tuple[int, int]] = []
        driver._click_point = lambda _hwnd, x, y: clicks.append((x, y))
        driver._dismiss_account_ban = lambda _hwnd, current: current
        driver._identity = lambda _hwnd: object()
        driver._open_account_menu = lambda _hwnd, _identity: (main_frames, (700, 500))

        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            self.assertTrue(driver._sign_out_once())

        self.assertEqual(clicks, [(700, 500)])
        self.assertIn("signed-out", driver.records)
        self.assertNotIn("signout-timeout", driver.records)


class ApexEntryRetryTest(unittest.TestCase):
    def start_driver(self, frames, clock: Clock):
        clicks: list[tuple[int, int]] = []
        driver = bare_driver(clock)
        driver._ea_window = lambda: 1
        driver._click_point = lambda _hwnd, x, y: clicks.append((x, y))
        driver._observe = frames(clicks)
        driver._process_running = lambda _name: (1150, 700) in clicks
        return driver, clicks

    def test_entry_swallowed_by_the_library_tour_is_clicked_again(self) -> None:
        """Lease 3074: Home stays on screen after the tour; a second click opens Apex."""

        clock = Clock()
        driver, clicks = self.start_driver(
            lambda clicks: lambda _hwnd: APEX_PAGE if len(clicks) >= 2 else HOME, clock)

        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            driver.start_apex()

        self.assertEqual(clicks, [(260, 650), (260, 650), (1150, 700)])
        self.assertEqual(driver.records, ["apex-entry", "apex-entry", "apex-play"])

    def test_entry_retries_are_bounded_before_reporting_missing_play(self) -> None:
        clock = Clock()
        driver, clicks = self.start_driver(lambda clicks: lambda _hwnd: HOME, clock)

        with patch("apex_automation.ea_app_win32.time.monotonic", clock):
            with self.assertRaisesRegex(EaApexControlMissing, "Play 或 Update"):
                driver.start_apex()

        self.assertEqual(clicks, [(260, 650)] * 3)
        self.assertEqual(driver.records[-1], "apex-play-missing")


class ApexStartRestartTest(unittest.TestCase):
    def run_cycle(self, driver) -> object:
        now = datetime.now(timezone.utc)
        lease = replace(FakeAccountProvider.lease("acct_1", now=now), expected_ea_account_id="ea_1")
        provider = FakeAccountProvider(
            [lease],
            credentials={"acct_1": SecretCredentials("login@example.test", "password")},
            otp_factory=lambda challenge_id, started_at: OtpCode(
                code="123456",
                challenge_id=challenge_id,
                received_at=started_at,
                expires_at=started_at + timedelta(minutes=1),
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            return AccountOrchestrator(
                provider=provider,
                ea_driver=driver,
                play_session=FakeManagedSession(driver.log, FakeDrain()),
                checkpoint_store=AtomicCheckpointStore(Path(directory) / "account-cycle-status.json"),
                device_id="device_1",
                capture_source=object(),
                operation_id_factory=iter(["claim_1", "renew_1", "credentials_1", "otp_1", "close_1"]).__next__,
                completion_poll_s=0.1,
                sleep=lambda _: None,
                notify=lambda _: None,
            ).run_once()

    def test_missing_play_restarts_ea_once_and_continues_the_same_login(self) -> None:
        log: list[str] = []

        class StuckOnceDriver(FakeEaDriver):
            starts = 0

            def start_apex(self) -> None:
                self.starts += 1
                self.log.append("apex.start")
                if self.starts == 1:
                    raise EaApexControlMissing("EA App Apex 页面未找到 Play 或 Update 按钮")

        driver = StuckOnceDriver(log, "ea_1")
        result = self.run_cycle(driver)

        self.assertEqual(result.outcome, AccountCycleOutcome.COMPLETED)
        self.assertIsNone(result.error_code)
        self.assertEqual(log.count("ea.sign_in"), 1)
        self.assertEqual(log.count("ea.restart"), 1)
        restart = log.index("ea.restart")
        self.assertLess(restart, log.index("ea.verify_identity", restart))
        self.assertLess(restart, log.index("apex.start", restart))
        self.assertLess(log.index("apex.start", restart), log.index("play.start"))

    def test_start_failure_after_the_restart_ends_the_lease(self) -> None:
        log: list[str] = []

        class AlwaysStuckDriver(FakeEaDriver):
            def start_apex(self) -> None:
                self.log.append("apex.start")
                raise EaApexControlMissing("EA App Apex 页面未找到 Play 或 Update 按钮")

        result = self.run_cycle(AlwaysStuckDriver(log, "ea_1"))

        self.assertEqual(result.error_code, "APEX_START_FAILED")
        self.assertEqual(log.count("apex.start"), 2)
        self.assertEqual(log.count("ea.restart"), 1)
        self.assertNotIn("play.start", log)

    def test_a_launch_that_may_still_be_starting_is_not_restarted(self) -> None:
        log: list[str] = []

        class LaunchTimeoutDriver(FakeEaDriver):
            def start_apex(self) -> None:
                self.log.append("apex.start")
                raise EaApexStartFailed("点击 EA Play 后未发现 Apex 进程")

        result = self.run_cycle(LaunchTimeoutDriver(log, "ea_1"))

        self.assertEqual(result.error_code, "APEX_START_FAILED")
        self.assertEqual(log.count("apex.start"), 1)
        self.assertNotIn("ea.restart", log)


if __name__ == "__main__":
    unittest.main()

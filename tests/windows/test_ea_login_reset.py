"""Each new login must submit its own identifier before a password or OTP."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.account_provider import SecretCredentials
from apex_automation.ea_app import EaAppAutomationError, EaCaptchaRequired, EaCredentialsRejected, EaIdentityFact, EaLoginRejected
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_pages import EaPage, classify_page
from apex_automation.ocr_obstacles import OcrToken


class LoginResetTest(unittest.TestCase):
    def setUp(self):
        data = json.loads((Path(__file__).parent / "fixtures" / "ea-password-error.json").read_text())
        self.rejected = EaObservation(
            tuple(data["rect"]), np.zeros((1, 1), dtype=np.uint8),
            tuple(OcrToken(t["text"], t["confidence"], tuple(t["roi"])) for t in data["tokens"]),
            EaPage.PASSWORD,
        )
        self.email = replace(self.rejected, page=EaPage.EMAIL, tokens=(
            OcrToken("Email or EA ID", .99, (950, 650, 1320, 680)),
            OcrToken("Next", .99, (1100, 800, 1250, 835)),
        ))
        self.password = replace(self.rejected, tokens=tuple(
            t for t in self.rejected.tokens if "credentials" not in t.normalized and "againorreset" not in t.normalized
        ))
        self.credentials = SecretCredentials("new-account@example.test", "fixture-password")
        self.identity = EaIdentityFact("fixture-player", "test", True)

    def driver(self, observations, *, repeat=None, rejection=False):
        driver = object.__new__(WindowsEaHybridDriver)
        self.events, self.records = [], []

        def observe(_hwnd):
            if observations:
                return observations.pop(0)
            if repeat is not None:
                return repeat
            raise AssertionError("unexpected capture")

        def identifier(_hwnd, observation, credentials):
            self.assertIs(observation, self.email)
            driver._login_identifier_verified = True
            self.events.append(("identifier", credentials.login_identifier))
            return self.password

        def password(_hwnd, observation, credentials):
            self.assertIs(observation, self.password)
            self.events.append(("password", credentials.password))
            return "anchor"

        def await_identity(*_args, **_kwargs):
            self.events.append(("await",))
            if rejection:
                raise EaCredentialsRejected("fresh credentials rejected")
            return self.identity

        driver.evidence = None
        driver._ea_window = lambda: 1
        driver._dismiss_expired_session = lambda _hwnd: False
        driver._observe = observe
        driver._click_point = lambda _hwnd, x, y: self.events.append(("back", x, y))
        driver._record = lambda step, *_args, **_kwargs: self.records.append(step)
        driver._submit_login_identifier = identifier
        driver._submit_password = password
        driver._await_identity = await_identity
        driver.sleep = lambda _seconds: None
        driver.notify = lambda _message: None
        return driver

    def sign_in(self, driver):
        return driver.sign_in(self.credentials, lambda _challenge: None)

    def test_actual_rejection_is_a_password_page_with_a_back_button(self):
        self.assertEqual(classify_page(self.rejected.normalized), EaPage.PASSWORD)
        self.assertTrue(self.rejected.has_login_error())
        self.assertEqual(WindowsEaHybridDriver._login_back_point(self.rejected), (1006, 361))

    def test_old_rejected_password_page_goes_back_before_new_identifier_and_password(self):
        driver = self.driver([self.rejected, self.email])
        self.assertIs(self.sign_in(driver), self.identity)
        self.assertEqual(self.events, [
            ("back", 1006, 361), ("identifier", self.credentials.login_identifier),
            ("password", self.credentials.password), ("await",),
        ])
        self.assertIn("signin-account-page-ready", self.records)

    def test_unrejected_password_page_also_cannot_reuse_an_unbound_account(self):
        driver = self.driver([self.password, self.email])
        self.sign_in(driver)
        self.assertEqual(self.events[0], ("back", 1006, 361))
        self.assertEqual(self.events[1][0], "identifier")

    def test_normal_account_page_does_not_add_back_navigation(self):
        driver = self.driver([self.email])
        self.sign_in(driver)
        self.assertEqual([e[0] for e in self.events], ["identifier", "password", "await"])

    def test_otp_page_from_another_attempt_must_return_to_account_page(self):
        otp = replace(self.password, page=EaPage.OTP)
        chooser = replace(self.password, page=EaPage.OTP_METHOD)
        driver = self.driver([otp, chooser, self.password, self.email])
        self.sign_in(driver)
        self.assertEqual([e[0] for e in self.events], ["back", "back", "back", "identifier", "password", "await"])

    def test_unknown_frame_after_back_does_not_permit_password_entry(self):
        blank = replace(self.password, tokens=(), page=EaPage.UNKNOWN)
        driver = self.driver([self.rejected, blank, self.email])
        self.sign_in(driver)
        self.assertEqual([e[0] for e in self.events], ["back", "identifier", "password", "await"])

    def test_transition_with_both_fields_is_not_a_ready_account_page(self):
        transition = replace(self.email, tokens=self.email.tokens + (OcrToken("Enter your password", .99),))
        self.assertEqual(classify_page(transition.normalized), EaPage.EMAIL)
        driver = self.driver([self.rejected, transition, self.email])
        self.sign_in(driver)
        self.assertEqual(self.events[1], ("identifier", self.credentials.login_identifier))

    def test_missing_or_low_confidence_back_stops_without_typing(self):
        for confidence, box in ((.40, (967, 347, 1045, 375)), (.99, None)):
            with self.subTest(confidence=confidence, box=box):
                observation = replace(self.rejected, tokens=(OcrToken("BACK", confidence, box),))
                driver = self.driver([observation])
                with self.assertRaises(EaAppAutomationError):
                    self.sign_in(driver)
                self.assertEqual(self.events, [])
                self.assertIn("signin-back-missing", self.records)

    def test_persistent_password_page_has_bounded_back_attempts(self):
        driver = self.driver([self.rejected], repeat=self.rejected)
        with self.assertRaises(EaAppAutomationError):
            self.sign_in(driver)
        self.assertEqual([e[0] for e in self.events], ["back"] * 4)
        self.assertEqual(self.records[-1], "signin-account-reset-failed")

    def test_captcha_after_back_stops_without_typing(self):
        captcha = replace(self.password, page=EaPage.CAPTCHA)
        driver = self.driver([self.rejected, captcha])
        with self.assertRaises(EaCaptchaRequired):
            self.sign_in(driver)
        self.assertEqual([e[0] for e in self.events], ["back"])

    def test_fresh_login_rejection_is_preserved_without_repeating_password(self):
        driver = self.driver([self.rejected, self.email], rejection=True)
        with self.assertRaises(EaCredentialsRejected) as caught:
            self.sign_in(driver)
        self.assertEqual(caught.exception.reason_code, "EA_CREDENTIALS_INVALID")
        self.assertEqual([e[0] for e in self.events], ["back", "identifier", "password", "await"])

    def test_password_rejection_requires_fresh_identifier_echo_for_review(self):
        for verified, page, expected in (
            (True, EaPage.PASSWORD, "EA_CREDENTIALS_INVALID"),
            (False, EaPage.PASSWORD, "LOGIN_INVALID"),
            (True, EaPage.OTP, "LOGIN_INVALID"),
        ):
            with self.subTest(verified=verified, page=page):
                observation = replace(self.rejected, page=page)
                driver = self.driver([observation])
                driver._login_identifier_verified = verified
                driver._dismiss_library_tour = lambda _hwnd, frame: frame
                driver._raise_if_account_banned = lambda *_args: None
                with self.assertRaises(EaLoginRejected) as caught:
                    WindowsEaHybridDriver._await_identity(
                        driver, 1, lambda _challenge: None,
                        otp_methods=self.credentials.otp_methods,
                        initial_challenge_started_at=datetime.now(timezone.utc),
                    )
                self.assertEqual(caught.exception.reason_code, expected)

    def test_transient_errors_on_verified_password_page_are_not_credential_holds(self):
        for message in ("Something went wrong", "Too many attempts", "Incorrect code"):
            with self.subTest(message=message):
                driver = self.driver([replace(self.password, tokens=(OcrToken(message, .99),))])
                driver._login_identifier_verified = True
                driver._dismiss_library_tour = lambda _hwnd, frame: frame
                driver._raise_if_account_banned = lambda *_args: None
                with self.assertRaises(EaLoginRejected) as caught:
                    WindowsEaHybridDriver._await_identity(
                        driver, 1, lambda _challenge: None,
                        otp_methods=self.credentials.otp_methods,
                        initial_challenge_started_at=datetime.now(timezone.utc),
                    )
                self.assertEqual(caught.exception.reason_code, "LOGIN_INVALID")

    def test_uncertain_or_misplaced_identifier_echo_cannot_quarantine_account(self):
        for token in (
            OcrToken(self.credentials.login_identifier, .40, (950, 650, 1320, 680)),
            OcrToken(self.credentials.login_identifier, .99),
            OcrToken(self.credentials.login_identifier, .99, (950, 220, 1320, 240)),
            OcrToken("other@example.test", .99, (950, 650, 1320, 680)),
        ):
            with self.subTest(token=token):
                self.assertFalse(WindowsEaHybridDriver._verified_identifier_echo(
                    replace(self.email, tokens=(token,)), self.credentials.login_identifier,
                ))

    def test_identifier_submission_verification_uses_only_current_echo(self):
        for echoed in (True, False):
            with self.subTest(echoed=echoed):
                driver = self.driver([])
                typed = replace(self.email, tokens=self.email.tokens + (
                    (OcrToken(self.credentials.login_identifier, .99, (950, 650, 1320, 680)),)
                    if echoed else ()
                ))
                driver._observe = lambda _hwnd: typed
                driver._click_target = lambda *_args, **_kwargs: "fixture"
                driver._clear_focused_field = lambda: None
                driver._type_secret = lambda _text: None
                driver._submit = lambda *_args: "anchor"
                driver._wait_for_page = lambda *_args, **_kwargs: self.password
                driver._login_identifier_verified = not echoed
                result = WindowsEaHybridDriver._submit_login_identifier(
                    driver, 1, self.email, self.credentials,
                )
                self.assertIs(result, self.password)
                self.assertEqual(driver._login_identifier_verified, echoed)


if __name__ == "__main__":
    unittest.main()

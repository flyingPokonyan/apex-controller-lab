"""D163 October 10: labels received Ctrl+A instead of editable fields."""
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.account_provider import SecretCredentials
from apex_automation.ea_app import EaAppAutomationError, EaCredentialsRejected
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_pages import EaPage, classify_page, is_login_error
from apex_automation.ocr_obstacles import OcrToken


class LoginFieldsTest(unittest.TestCase):
    def setUp(self):
        self.rect = (746, 160, 1301, 1085)
        self.email = EaObservation(self.rect, np.zeros((1220, 2048, 3), np.uint8), (
            OcrToken("Sign in to your EA Account", .985, (849, 359, 1202, 402)),
            OcrToken("EMAIL OR EA ID", .998, (808, 561, 940, 581)),
            OcrToken("Enter your email or EA ID", .987, (821, 600, 1033, 626)),
            OcrToken("NEXT", .99, (991, 802, 1054, 826)),
        ), EaPage.EMAIL)
        data = json.loads((Path(__file__).parent / "fixtures" / "ea-password-error.json").read_text())
        self.password = EaObservation(tuple(data["rect"]), self.email.frame, tuple(
            OcrToken(t["text"], t["confidence"], tuple(t["roi"])) for t in data["tokens"]
        ), EaPage.PASSWORD)
        self.clean_password = replace(self.password, tokens=tuple(t for t in self.password.tokens
                                      if not ("credentials" in t.normalized or "againorreset" in t.normalized)))
        self.credentials = SecretCredentials("fixture.long-account@example.test", "FixturePass42!")

    def typed_email(self, value=None):
        return replace(self.email, tokens=self.email.tokens[:2] + (
            OcrToken(value or self.credentials.login_identifier, .99, (821, 600, 1210, 626)),
            self.email.tokens[-1],
        ))

    def driver(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver.sleep = lambda _: None
        driver._record = lambda *_args, **_kwargs: None
        driver._clear_focused_field = lambda: None
        driver._type_secret = lambda _: None
        driver._click_point = lambda *_args: None
        driver._click = lambda *_args: None
        return driver

    def test_screenshot_clicks_inside_email_field_not_more_confident_label(self):
        point, target = WindowsEaHybridDriver._login_field_anchor(self.email)
        self.assertEqual(target, "placeholder")
        self.assertTrue(811 < point[0] < 1237 and 589 < point[1] < 640)

    def test_password_heading_and_label_never_win_over_placeholder(self):
        point, target = WindowsEaHybridDriver._login_field_anchor(self.password, password=True)
        self.assertEqual(target, "placeholder")
        self.assertTrue(956 < point[0] < 1462 and 694 < point[1] < 752)

    def test_filled_fields_target_below_label_when_placeholder_disappears(self):
        for frame, password, bounds in ((self.typed_email(), False, (589, 640)),
                                       (replace(self.password, tokens=tuple(t for t in self.password.tokens
                                        if t.normalized != "enteryourpassword")), True, (694, 752))):
            with self.subTest(password=password):
                point, target = WindowsEaHybridDriver._login_field_anchor(frame, password=password)
                self.assertEqual(target, "label-offset")
                self.assertTrue(bounds[0] < point[1] < bounds[1])

    def test_actual_identifier_and_password_submission_enables_credential_recovery(self):
        driver = self.driver()
        clicks, typed, submissions = [], [], []
        focused = [False]
        frame = [self.email]
        def click(_hwnd, x, y):
            clicks.append((x, y))
            focused[0] = ((frame[0].page is EaPage.EMAIL and 811 < x < 1237 and 589 < y < 640)
                          or (frame[0].page is EaPage.PASSWORD and 956 < x < 1462 and 694 < y < 752))
        def type_value(value):
            self.assertTrue(focused[0], "Ctrl+A would select the whole document")
            typed.append(value)
            if frame[0].page is EaPage.EMAIL:
                frame[0] = self.typed_email(value)
        def submit(_hwnd, observation, _ratio):
            submissions.append(observation.page)
            frame[0] = self.clean_password if observation.page is EaPage.EMAIL else self.password
            return "enter"
        driver._click_point = click
        driver._clear_focused_field = lambda: self.assertTrue(focused[0])
        driver._type_secret = type_value
        driver._observe = lambda _: frame[0]
        driver._submit = submit
        driver._wait_for_page = lambda *_args, **_kwargs: frame[0]
        password = driver._submit_login_identifier(1, self.email, self.credentials)
        self.assertTrue(driver._login_identifier_verified)
        driver._submit_password(1, password, self.credentials)
        driver._dismiss_library_tour = lambda _hwnd, observation: observation
        driver._raise_if_account_banned = lambda *_args: None
        with self.assertRaises(EaCredentialsRejected):
            driver._await_identity(1, lambda _: None, otp_methods=self.credentials.otp_methods,
                                   initial_challenge_started_at=datetime.now(timezone.utc))
        self.assertEqual(typed, [self.credentials.login_identifier, self.credentials.password])
        self.assertEqual(submissions, [EaPage.EMAIL, EaPage.PASSWORD])
        self.assertEqual(len(clicks), 2)

    def test_failed_input_is_bounded_and_never_submitted(self):
        driver = self.driver()
        typed, submitted = [], []
        driver._type_secret = typed.append
        driver._observe = lambda _: self.email
        driver._submit = lambda *_args: submitted.append(True)
        with self.assertRaisesRegex(EaAppAutomationError, "未确认账号"):
            driver._submit_login_identifier(1, self.email, self.credentials)
        self.assertEqual(len(typed), 2)
        self.assertEqual(submitted, [])
        self.assertFalse(driver._login_identifier_verified)

    def test_first_missed_input_is_retargeted_once_before_submission(self):
        driver = self.driver()
        frames = [self.email, self.typed_email()]
        typed, submitted = [], []
        driver._type_secret = typed.append
        driver._observe = lambda _: frames.pop(0)
        driver._submit = lambda *_args: submitted.append(True) or "enter"
        driver._wait_for_page = lambda *_args, **_kwargs: self.clean_password
        self.assertIs(driver._submit_login_identifier(1, self.email, self.credentials), self.clean_password)
        self.assertEqual(len(typed), 2)
        self.assertEqual(submitted, [True])

    def test_split_identifier_is_verified_only_on_one_adjacent_row(self):
        split = replace(self.email, tokens=(
            OcrToken("fixture.long-account@", .99, (821, 600, 1000, 626)),
            OcrToken("example.test", .99, (1004, 600, 1210, 626)),
        ))
        self.assertTrue(WindowsEaHybridDriver._verified_identifier_echo(split, self.credentials.login_identifier))
        for token in (OcrToken("example.test", .99, (1004, 670, 1210, 696)),
                      OcrToken("example.test", .4, (1004, 600, 1210, 626)),
                      OcrToken("example.test", .99, (1100, 600, 1299, 626))):
            self.assertFalse(WindowsEaHybridDriver._verified_identifier_echo(
                replace(split, tokens=(split.tokens[0], token)), self.credentials.login_identifier))

    def test_identifier_punctuation_is_preserved(self):
        self.assertFalse(WindowsEaHybridDriver._verified_identifier_echo(
            self.typed_email("user.name@example.test"), "username@example.test"))

    def test_tight_field_read_can_verify_value_missed_by_full_window_ocr(self):
        driver = self.driver()
        reads = []
        def read(_frame, region):
            reads.append(region)
            return (OcrToken(self.credentials.login_identifier, .99),)
        driver.ocr = SimpleNamespace(read=read)
        self.assertTrue(driver._verify_login_input(self.email, self.credentials.login_identifier))
        self.assertEqual(len(reads), 1)
        self.assertTrue(reads[0].single_line)
        self.assertGreater(reads[0].roi[1], 581)
        self.assertLess(reads[0].roi[3], 640)
        driver.ocr.read = lambda *_args: (OcrToken("another@example.test", .99),)
        self.assertFalse(driver._verify_login_input(self.email, self.credentials.login_identifier))

    def test_invalid_email_marker_is_login_error(self):
        self.assertTrue(is_login_error("invalidemailorid"))


class ForegroundCaptureTest(unittest.TestCase):
    def test_ea_is_focused_before_back_and_menu_are_read(self):
        driver = object.__new__(WindowsEaHybridDriver)
        events = []
        driver._live = lambda hwnd: hwnd
        driver._focus = lambda _: events.append("focus")
        driver._frame = lambda: events.append("capture") or np.zeros((900, 600, 3), np.uint8)
        driver._clip_rect = lambda *_args: (0, 0, 600, 900)
        driver.ocr = SimpleNamespace(read_with_boxes=lambda _: (
            OcrToken("BACK", .99, (30, 80, 100, 100)),
            OcrToken("PASSWORD", .99, (40, 400, 160, 420)),
        ))
        observation = driver._observe(1)
        self.assertEqual(events, ["focus", "capture"])
        self.assertEqual(driver._login_back_point(observation), (65, 90))

    def test_focus_failure_never_captures_another_app(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._live = lambda hwnd: hwnd
        driver._focus = lambda _: (_ for _ in ()).throw(EaAppAutomationError("focus failed"))
        driver._frame = lambda: self.fail("must not read the covering console")
        with self.assertRaisesRegex(EaAppAutomationError, "focus failed"):
            driver._observe(1, retries=1)


if __name__ == "__main__":
    unittest.main()

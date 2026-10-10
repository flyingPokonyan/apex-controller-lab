"""Recovery for the upload-failed notice seen after a completed Apex run."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ea_app import EaAppAutomationError, EaIdentityFact, EaUiState
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_pages import EaPage
from apex_automation.ocr_obstacles import OcrToken


class CloudUploadRecoveryTest(unittest.TestCase):
    def setUp(self):
        data = json.loads((Path(__file__).parent / "fixtures" / "ea-cloud-upload-error.json").read_text())
        self.dialog = EaObservation(
            tuple(data["rect"]), np.zeros((1, 1), dtype=np.uint8),
            tuple(OcrToken(t["text"], t["confidence"], tuple(t["roi"])) for t in data["tokens"]),
            EaPage.SIGNED_IN,
        )
        self.clear = replace(self.dialog, tokens=(
            OcrToken("Library", .99, (270, 400, 350, 420)),
            OcrToken("Browse", .99, (270, 350, 350, 370)),
            OcrToken("Apex Legends", .99, (270, 600, 420, 620)),
        ))
        self.login = replace(self.dialog, tokens=(), page=EaPage.EMAIL)
        self.blank = replace(self.dialog, tokens=(), page=EaPage.UNKNOWN)

    def driver(self, observations, *, repeat=None):
        driver = object.__new__(WindowsEaHybridDriver)
        self.clicks, self.records, self.menu_calls = [], [], []

        def observe(_hwnd):
            if observations:
                value = observations.pop(0)
            elif repeat is not None:
                value = repeat
            else:
                raise AssertionError("unexpected capture")
            if isinstance(value, Exception):
                raise value
            return value

        def menu(_hwnd, _identity):
            self.menu_calls.append(True)
            return self.clear, (700, 500)

        driver._observe = observe
        driver._ea_window = lambda: 1
        driver._click_point = lambda _hwnd, x, y: self.clicks.append((x, y))
        driver._record = lambda step, *_args, **_kwargs: self.records.append(step)
        driver._identity = lambda _hwnd: EaIdentityFact("fixture-player", "test", True)
        driver._open_account_menu = menu
        driver._dismiss_expired_session = lambda _hwnd: False
        driver._dismiss_library_tour = lambda _hwnd, observation: observation
        driver._dismiss_account_ban = lambda _hwnd, observation: observation
        driver._process_running = lambda _name: False
        driver.sleep = lambda _seconds: None
        driver.notify = lambda _message: None
        return driver

    def test_real_screenshot_ocr_finds_only_the_notice_ok(self):
        self.assertEqual(WindowsEaHybridDriver._cloud_upload_error_point(self.dialog), (1515, 867))
        # The previous recovery correctly remains reserved for its own action.
        self.assertIsNone(WindowsEaHybridDriver._continue_local_data_point(self.dialog))

    def test_unrelated_ok_missing_local_save_copy_and_low_confidence_are_rejected(self):
        cases = (
            replace(self.dialog, tokens=self.dialog.tokens[1:]),
            replace(self.dialog, tokens=(self.dialog.tokens[0], self.dialog.tokens[-1])),
            replace(self.dialog, tokens=self.dialog.tokens[:-1] + (OcrToken("OK", .40, (1497, 855, 1534, 879)),)),
            replace(self.dialog, tokens=self.dialog.tokens[:-1] + (OcrToken("OK", .99),)),
            replace(self.dialog, tokens=self.dialog.tokens[:-1] + (OcrToken("OK", .99, (1497, 400, 1534, 430)),)),
        )
        for observation in cases:
            with self.subTest(tokens=observation.normalized):
                self.assertIsNone(WindowsEaHybridDriver._cloud_upload_error_point(observation))

    def test_split_heading_tokens_are_supported(self):
        title = (
            OcrToken("Failed to upload game data", .99, (866, 634, 1250, 673)),
            OcrToken("to the cloud", .99, (1260, 634, 1479, 673)),
        )
        self.assertEqual(WindowsEaHybridDriver._cloud_upload_error_point(
            replace(self.dialog, tokens=title + self.dialog.tokens[1:])
        ), (1515, 867))

    def test_blank_frame_does_not_prove_dismissal(self):
        driver = self.driver([self.blank, self.clear, self.clear])
        self.assertIs(driver._dismiss_cloud_upload_error(1, self.dialog), self.clear)
        self.assertEqual(self.clicks, [(1515, 867)])
        self.assertEqual(self.records, ["cloud-upload-error-ack", "cloud-upload-error-dismissed"])

    def test_persistent_notice_stops_after_two_acknowledgements(self):
        driver = self.driver([], repeat=self.dialog)
        with self.assertRaises(EaAppAutomationError):
            driver._dismiss_cloud_upload_error(1, self.dialog)
        self.assertEqual(self.clicks, [(1515, 867)] * 2)
        self.assertEqual(self.records[-1], "cloud-upload-error-stuck")

    def test_unreadable_frames_stop_without_a_second_click(self):
        driver = self.driver([], repeat=self.blank)
        with self.assertRaises(EaAppAutomationError):
            driver._dismiss_cloud_upload_error(1, self.dialog)
        self.assertEqual(self.clicks, [(1515, 867)])

    def test_missing_ok_stops_before_any_click(self):
        driver = self.driver([])
        with self.assertRaises(EaAppAutomationError):
            driver._dismiss_cloud_upload_error(1, replace(self.dialog, tokens=self.dialog.tokens[:-1]))
        self.assertEqual(self.clicks, [])

    def test_signout_dismisses_existing_notice_then_verifies_login(self):
        driver = self.driver([self.dialog, self.clear, self.clear, self.login])
        self.assertTrue(driver.sign_out())
        self.assertEqual(self.clicks, [(1515, 867), (700, 500)])
        self.assertEqual(len(self.menu_calls), 1)
        self.assertEqual(self.records[-1], "signed-out")

    def test_notice_after_signout_retries_the_menu_when_account_is_still_signed_in(self):
        driver = self.driver([self.clear, self.dialog, self.clear, self.clear, self.login])
        self.assertTrue(driver.sign_out())
        self.assertEqual(self.clicks, [(700, 500), (1515, 867), (700, 500)])
        self.assertEqual(len(self.menu_calls), 2)
        self.assertIn("signout-cloud-upload-retry", self.records)
        self.assertEqual(self.records[-1], "signed-out")

    def test_notice_after_signout_can_transition_straight_to_login(self):
        driver = self.driver([self.clear, self.dialog, self.login, self.login])
        self.assertTrue(driver.sign_out())
        self.assertEqual(self.clicks, [(700, 500), (1515, 867)])
        self.assertEqual(len(self.menu_calls), 1)

    def test_repeated_notice_cannot_cause_unbounded_signout_retries(self):
        driver = self.driver([self.clear, self.dialog, self.clear, self.clear,
                              self.dialog, self.clear, self.clear])
        self.assertFalse(driver._sign_out_once())
        self.assertEqual(len(self.menu_calls), 2)
        self.assertEqual(self.records[-1], "signout-cloud-upload-retry-exhausted")

    def test_closed_window_after_ok_does_not_prove_signed_out(self):
        driver = self.driver([self.clear, self.dialog, EaAppAutomationError("capture lost")])
        with self.assertRaises(EaAppAutomationError):
            driver._sign_out_once()
        self.assertNotIn("signed-out", self.records)
        self.assertNotIn("signout-cloud-sync-closed", self.records)

    def test_preflight_clears_the_notice_before_returning_signed_in(self):
        driver = self.driver([self.dialog, self.clear, self.clear])
        self.assertEqual(driver.preflight(), EaUiState.SIGNED_IN)
        self.assertEqual(self.clicks, [(1515, 867)])

    def test_launch_clears_the_notice_before_clicking_game_entry(self):
        play = replace(self.clear, tokens=self.clear.tokens + (OcrToken("PLAY", .99, (1300, 790, 1380, 830)),))
        driver = self.driver([self.dialog, self.clear, self.clear, play])
        driver._process_running = lambda _name: len(self.clicks) >= 3
        driver.start_apex()
        self.assertEqual(self.clicks, [(1515, 867), (345, 610), (1340, 810)])


if __name__ == "__main__":
    unittest.main()

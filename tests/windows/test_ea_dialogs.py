"""The EA dialog table and the one routine that answers every entry."""
from dataclasses import replace
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.ea_app import EaApexStartFailed
from apex_automation.ea_app_win32 import APEX_LAUNCH_TIMEOUT_S, EaObservation, WindowsEaHybridDriver
from apex_automation.ea_dialogs import (
    CLOUD_SYNC_CHOICE,
    DIALOGS,
    LAUNCH,
    PREFLIGHT,
    SIGNOUT,
    cloud_sync_choice_points,
    known_dialog,
    unknown_dialog_text,
)
from apex_automation.ea_pages import EaPage
from apex_automation.ocr_obstacles import OcrToken

LOCAL, CONFIRM = (856, 508), (1086, 695)
ENTRY, PLAY = (300, 460), (1150, 600)


def fixture() -> EaObservation:
    data = json.loads((Path(__file__).parent / "fixtures" / "ea-cloud-sync-choice.json").read_text())
    return EaObservation(
        tuple(data["rect"]), np.zeros((1, 1), dtype=np.uint8),
        tuple(OcrToken(t["text"], t["confidence"], tuple(t["roi"])) for t in data["tokens"]),
        EaPage.SIGNED_IN, hwnd=1, process_id=42, source_verified=True,
    )


class CloudSyncChoiceTest(unittest.TestCase):
    def setUp(self):
        self.dialog = fixture()
        self.page = replace(self.dialog, tokens=(
            OcrToken("Library", .99, (230, 290, 300, 310)),
            OcrToken("Apex Legends", .99, (250, 450, 350, 470)),
            OcrToken("Overview", .99, (470, 280, 560, 300)),
        ))
        self.entry = self.page
        self.play = replace(self.page, tokens=self.page.tokens + (OcrToken("PLAY", .99, (1110, 590, 1190, 610)),))

    def launch(self, frames, *, repeat=None, starts_after_clicks=None):
        driver = object.__new__(WindowsEaHybridDriver)
        self.clicks, self.records, self.notices, self.details = [], [], [], {}
        self.now = 0.0

        def observe(_hwnd):
            if frames:
                return frames.pop(0)
            if repeat is not None:
                return repeat
            raise AssertionError("unexpected capture")

        def record(step, *_args, **detail):
            self.records.append(step)
            self.details[step] = detail

        driver._ea_window = lambda: 1
        driver._live = lambda hwnd: hwnd
        driver._observe = observe
        driver._click_point = lambda _hwnd, x, y: self.clicks.append((x, y))
        driver._record = record
        driver._process_running = lambda name: (
            starts_after_clicks is not None and name == "r5apex.exe" and len(self.clicks) >= starts_after_clicks)
        driver.sleep = lambda seconds: setattr(self, "now", self.now + seconds)
        driver.notify = self.notices.append
        driver._dismiss_library_tour = lambda _hwnd, observation: observation
        driver._dismiss_account_ban = lambda _hwnd, observation: observation
        self.enterContext(patch("apex_automation.ea_app_win32.time.monotonic", side_effect=lambda: self.now))
        return driver

    def test_real_dialog_is_found_only_at_launch(self):
        self.assertIs(known_dialog(self.dialog, LAUNCH), CLOUD_SYNC_CHOICE)
        self.assertIsNone(known_dialog(self.dialog, SIGNOUT))
        self.assertIsNone(known_dialog(self.dialog, PREFLIGHT))
        self.assertIsNone(known_dialog(self.page, LAUNCH))

    def test_the_game_art_on_the_option_row_does_not_move_the_click(self):
        # "Apex Legends" and "APEX" behind the dialog share the option's row;
        # joining that line put the click at x=619, outside the dialog.
        self.assertEqual(cloud_sync_choice_points(self.dialog), (LOCAL, CONFIRM))

    def test_option_and_confirm_must_be_trusted_and_stacked_under_the_title(self):
        def without(*texts):
            return replace(self.dialog, tokens=tuple(t for t in self.dialog.tokens if t.text not in texts))
        weak = replace(self.dialog, tokens=tuple(
            replace(t, confidence=.5) if t.text == "CONFIRM" else t for t in self.dialog.tokens))
        raised = replace(self.dialog, tokens=tuple(
            replace(t, roi=(t.roi[0], 300, t.roi[2], 320)) if t.text == "CONFIRM" else t for t in self.dialog.tokens))
        for observation in (without("CONFIRM"), without("Continue with local save"), without("Cloud sync failed"),
                            weak, raised):
            with self.subTest(tokens=[t.text for t in observation.tokens][-3:]):
                self.assertIsNone(cloud_sync_choice_points(observation))

    def test_launch_keeps_the_local_save_confirms_and_goes_on_waiting_for_apex(self):
        driver = self.launch([self.entry, self.play, self.dialog], starts_after_clicks=4)
        driver.start_apex()
        self.assertEqual(self.clicks, [ENTRY, PLAY, LOCAL, CONFIRM])
        self.assertEqual(self.records, ["apex-entry", "apex-play", "apex-cloud-sync-choice-local"])
        self.assertTrue(any("本机存档" in notice for notice in self.notices))

    def test_closed_dialog_hands_back_to_the_launch_wait_with_a_fresh_clock(self):
        clear = self.play
        frames = [self.entry, self.play, self.dialog, clear]
        driver = self.launch(frames, repeat=clear)
        started = []
        driver._process_running = lambda name: name == "r5apex.exe" and self.now >= started[0] + 100 if started else False
        original = driver._request_apex_launch

        def launch(hwnd, point):
            if point == CONFIRM:
                started.append(self.now)
            return original(hwnd, point)

        driver._request_apex_launch = launch
        driver.start_apex()
        self.assertEqual(self.clicks[:4], [ENTRY, PLAY, LOCAL, CONFIRM])
        self.assertIn("apex-cloud-sync-choice-dismissed", self.records)
        # Play shown again a minute after Confirm is pressed once more, as for any launch.
        self.assertEqual(self.records.count("apex-play-again"), 1)

    def test_dialog_that_stays_is_answered_twice_then_fails_the_launch(self):
        driver = self.launch([self.entry, self.play], repeat=self.dialog)
        with self.assertRaisesRegex(EaApexStartFailed, "两次选择本机存档"):
            driver.start_apex()
        self.assertEqual(self.clicks, [ENTRY, PLAY, LOCAL, CONFIRM, LOCAL, CONFIRM])
        self.assertEqual(self.records[-1], "apex-cloud-sync-choice-stuck")

    def test_dialog_without_trusted_controls_presses_nothing(self):
        broken = replace(self.dialog, tokens=tuple(t for t in self.dialog.tokens if t.text != "CONFIRM"))
        driver = self.launch([self.entry, self.play, broken])
        with self.assertRaises(EaApexStartFailed):
            driver.start_apex()
        self.assertEqual(self.clicks, [ENTRY, PLAY])
        self.assertEqual(self.records[-1], "apex-cloud-sync-choice-action-missing")

    def test_window_hidden_while_apex_starts_is_left_to_the_launch_wait(self):
        from apex_automation.ea_app import EaAppAutomationError
        hidden = EaAppAutomationError("EA window rebuilt")
        frames = [self.entry, self.play, self.dialog]
        driver = self.launch(frames)
        captures = iter([hidden, hidden, hidden, hidden])

        def observe(_hwnd):
            if frames:
                return frames.pop(0)
            raise next(captures, hidden)

        driver._observe = observe
        driver._process_running = lambda name: name == "r5apex.exe" and self.now >= 20
        driver.start_apex()
        self.assertEqual(self.clicks, [ENTRY, PLAY, LOCAL, CONFIRM])
        self.assertNotIn("apex-cloud-sync-choice-stuck", self.records)


class UnknownDialogTest(unittest.TestCase):
    def test_unknown_dialog_is_named_in_evidence_and_in_the_failure(self):
        base = fixture()
        other = replace(base, tokens=(
            OcrToken("Overview", .99, (470, 280, 560, 300)),
            OcrToken("Something went wrong", .99, (740, 389, 960, 415)),
            OcrToken("We couldn't start Apex Legends.", .99, (682, 429, 1000, 449)),
            OcrToken("RETRY", .99, (1048, 685, 1110, 705)),
            OcrToken("CANCEL", .99, (1156, 684, 1224, 704)),
        ))
        self.assertIsNone(known_dialog(other, LAUNCH))
        self.assertEqual(unknown_dialog_text(other), "Something went wrong / We couldn't start Apex Legends.")
        # The real Cloud sync frames read as a dialog too, page tabs left out.
        self.assertTrue(unknown_dialog_text(base).startswith("Apex Legends / Cloud sync failed / "))

        test = CloudSyncChoiceTest()
        test.setUp()
        test.enterContext = self.enterContext
        driver = test.launch([test.entry, test.play], repeat=other)
        with self.assertRaisesRegex(EaApexStartFailed, "EA 弹窗未识别：Something went wrong"):
            driver.start_apex()
        self.assertEqual(test.records.count("apex-launch-dialog-unknown"), 1)
        self.assertEqual(test.details["apex-launch-dialog-unknown"]["dialog"],
                         "Something went wrong / We couldn't start Apex Legends.")
        self.assertEqual(test.clicks, [ENTRY, PLAY])
        self.assertGreaterEqual(test.now, APEX_LAUNCH_TIMEOUT_S)

    def test_ordinary_ea_pages_are_not_dialogs(self):
        base = fixture()
        home = replace(base, tokens=(
            OcrToken("FIND FRIENDS", .99, (1530, 470, 1665, 495)),
            OcrToken("VIEW ALL", .99, (910, 870, 1010, 892)),
            OcrToken("MANAGE", .99, (1160, 560, 1240, 580)),
            OcrToken("PLAY", .99, (1110, 590, 1190, 610)),
        ))
        self.assertIsNone(unknown_dialog_text(home))


class DialogTableTest(unittest.TestCase):
    def test_entries_have_distinct_steps_and_known_contexts(self):
        self.assertEqual(len({dialog.key for dialog in DIALOGS}), len(DIALOGS))
        for dialog in DIALOGS:
            with self.subTest(dialog=dialog.key):
                self.assertTrue(dialog.contexts)
                self.assertLessEqual(dialog.contexts, {PREFLIGHT, LAUNCH, SIGNOUT})
                # Only a launch may start Apex.
                self.assertTrue(not dialog.launches or dialog.contexts == {LAUNCH})


if __name__ == "__main__":
    unittest.main()

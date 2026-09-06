from __future__ import annotations

from pathlib import Path
import sys
import unittest

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))

from apex_automation.ea_app import EaAppAutomationError
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_onboarding import library_tour_close_point, library_tour_visible
from apex_automation.ea_pages import EaPage, classify_page
from apex_automation.ocr_obstacles import OcrToken


# Only the guide card is retained from the supplied remote-desktop screenshot:
# no account badge, desktop, machine details, or remote-control chrome.
CARD = Path(__file__).with_name("fixtures") / "ea-library-tour-card.png"
# Actual RapidOCR boxes on that crop, including its NXT / l of 3 confusions.
CARD_TOKENS = (
    ("All your content in one place", (27, 332, 357, 361)),
    ("Manage owned games and add-ons from your", (27, 386, 473, 412)),
    ("l of 3", (26, 536, 76, 560)),
    ("PREVIOUS", (285, 538, 387, 557)),
    ("NXT", (433, 537, 492, 560)),
)


def tour_observation(scale=1.0, offset=(0, 0), card=None):
    card = cv2.imread(str(CARD)) if card is None else card
    frame = np.zeros((900, 1200, 3), dtype=np.uint8)
    frame[160:160 + card.shape[0], 250:250 + card.shape[1]] = card
    frame = cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ox, oy = offset
    moved = np.zeros((frame.shape[0] + oy, frame.shape[1] + ox, 3), dtype=np.uint8)
    moved[oy:, ox:] = frame
    tokens = tuple(
        OcrToken(text, 0.98, tuple(
            round((value + (250 if index % 2 == 0 else 160)) * scale)
            + (ox if index % 2 == 0 else oy)
            for index, value in enumerate(box)
        ))
        for text, box in CARD_TOKENS
    ) + (OcrToken("Library", 1.0), OcrToken("Browse", 1.0))
    return EaObservation(
        (ox, oy, moved.shape[1], moved.shape[0]), moved, tokens, EaPage.SIGNED_IN,
    )


def clear_observation():
    return EaObservation(
        (0, 0, 1200, 900), np.zeros((900, 1200, 3), dtype=np.uint8),
        (OcrToken("Library", 1.0), OcrToken("Browse", 1.0)), EaPage.SIGNED_IN,
    )


def driver_for(observations):
    driver = object.__new__(WindowsEaHybridDriver)
    queued = iter(observations)
    driver._observe = lambda _hwnd: next(queued)
    driver._ea_window = lambda: 1
    driver._process_running = lambda _name: False
    driver.sleep = lambda _seconds: None
    driver.notify = lambda _message: None
    driver.clicks = []
    driver.records = []
    driver._click_point = lambda _hwnd, x, y: driver.clicks.append((x, y))
    driver._record = lambda name, *_args, **_kwargs: driver.records.append(name)
    return driver


class LibraryTourTargetTest(unittest.TestCase):
    def locate(self, observation):
        return library_tour_close_point(
            observation.frame, observation.tokens, observation.rect,
        )

    def test_real_card_targets_close_after_scaling_and_window_translation(self):
        for scale in (1280 / 2420, 1920 / 2420, 1.0, 2560 / 2420, 1.5):
            for offset in ((0, 0), (137, 91)):
                with self.subTest(scale=scale, offset=offset):
                    point = self.locate(tour_observation(scale, offset))
                    self.assertIsNotNone(point)
                    self.assertLessEqual(abs(point[0] - (751 * scale + offset[0])), 2)
                    self.assertLessEqual(abs(point[1] - (196 * scale + offset[1])), 2)

    def test_next_and_previous_without_the_known_copy_are_not_a_tour(self):
        observation = tour_observation()
        observation.tokens = observation.tokens[2:]
        self.assertFalse(library_tour_visible(observation.tokens))
        self.assertIsNone(self.locate(observation))

    def test_missing_position_does_not_fall_back_to_screen_coordinates(self):
        observation = tour_observation()
        observation.tokens = tuple(OcrToken(t.text, t.confidence) for t in observation.tokens)
        self.assertTrue(library_tour_visible(observation.tokens))
        self.assertIsNone(self.locate(observation))

    def test_illustration_icons_do_not_replace_a_missing_close_control(self):
        card = cv2.imread(str(CARD))
        card[10:60, 475:530] = 0
        self.assertIsNone(self.locate(tour_observation(card=card)))

    def test_square_without_inner_cross_is_rejected(self):
        card = cv2.imread(str(CARD))
        card[25:47, 489:513] = (45, 35, 35)
        self.assertIsNone(self.locate(tour_observation(card=card)))

    def test_ambiguous_controls_are_rejected(self):
        card = cv2.imread(str(CARD))
        card[80:130, 400:455] = card[10:60, 475:530].copy()
        self.assertIsNone(self.locate(tour_observation(card=card)))

    def test_app_titlebar_close_cannot_replace_card_close(self):
        card = cv2.imread(str(CARD))
        close = card[10:60, 475:530].copy()
        card[10:60, 475:530] = 0
        observation = tour_observation(card=card)
        observation.frame[2:52, 725:780] = close
        self.assertIsNone(self.locate(observation))


class LibraryTourRecoveryTest(unittest.TestCase):
    def test_normal_page_has_no_extra_capture_or_click(self):
        driver = driver_for([])
        observation = clear_observation()
        self.assertIs(driver._dismiss_library_tour(1, observation), observation)
        self.assertEqual(driver.clicks, [])
        self.assertEqual(driver.records, [])

    def test_blank_transition_is_not_treated_as_dismissal(self):
        blank = clear_observation()
        blank.tokens, blank.page = (), EaPage.UNKNOWN
        clear = clear_observation()
        driver = driver_for([blank, clear, clear])
        self.assertIs(driver._dismiss_library_tour(1, tour_observation()), clear)
        self.assertEqual(driver.clicks, [(751, 196)])
        self.assertEqual(driver.records, ["library-tour-close", "library-tour-dismissed"])

    def test_failed_close_is_bounded_and_does_not_click_next(self):
        tour = tour_observation()
        driver = driver_for([tour, tour])
        with self.assertRaisesRegex(EaAppAutomationError, "新手引导未能确认关闭"):
            driver._dismiss_library_tour(1, tour)
        self.assertEqual(driver.clicks, [(751, 196), (751, 196)])
        self.assertEqual(driver.records[-1], "library-tour-stuck")

    def test_single_ocr_miss_cannot_confirm_dismissal(self):
        tour = tour_observation()
        driver = driver_for([clear_observation(), tour, tour])
        with self.assertRaises(EaAppAutomationError):
            driver._dismiss_library_tour(1, tour)
        self.assertNotIn("library-tour-dismissed", driver.records)
        self.assertEqual(driver.clicks, [(751, 196), (751, 196)])

    def test_missing_button_never_clicks_through_the_overlay(self):
        tour = tour_observation()
        tour.frame[:] = 0
        driver = driver_for([tour, tour])
        with self.assertRaises(EaAppAutomationError):
            driver._dismiss_library_tour(1, tour)
        self.assertEqual(driver.clicks, [])

    def test_persistent_blank_cannot_confirm_dismissal(self):
        blank = clear_observation()
        blank.tokens, blank.page = (), EaPage.UNKNOWN
        driver = driver_for([blank] * 6)
        with self.assertRaises(EaAppAutomationError):
            driver._dismiss_library_tour(1, tour_observation())
        self.assertNotIn("library-tour-dismissed", driver.records)
        self.assertEqual(driver.clicks, [(751, 196)])

    def test_launch_waits_for_dismissal_before_clicking_apex(self):
        entry = clear_observation()
        entry.tokens += (OcrToken("Apex Legends", 1.0, (90, 450, 190, 470)),)
        play = clear_observation()
        play.tokens += (OcrToken("Play", 1.0, (580, 440, 620, 460)),)
        driver = driver_for([tour_observation(), entry, entry, play])
        driver._process_running = lambda _name: len(driver.clicks) >= 3
        driver.start_apex()
        self.assertEqual(driver.clicks, [(751, 196), (140, 460), (600, 450)])
        self.assertEqual(driver.records, [
            "library-tour-close", "library-tour-dismissed", "apex-entry", "apex-play",
        ])

    def test_signout_opens_menu_only_after_dismissal(self):
        email_tokens = (OcrToken("Email or EA ID", 1.0),)
        email = EaObservation(
            (0, 0, 1200, 900), np.zeros((1, 1, 3), dtype=np.uint8),
            email_tokens, classify_page(t.normalized for t in email_tokens),
        )
        clear = clear_observation()
        driver = driver_for([tour_observation(), clear, clear, email])
        driver._identity = lambda _hwnd: object()

        def open_menu(_hwnd, _identity):
            self.assertEqual(driver.records[-1], "library-tour-dismissed")
            return clear_observation(), (1050, 200)

        driver._open_account_menu = open_menu
        self.assertTrue(driver.sign_out())
        self.assertEqual(driver.clicks, [(751, 196), (1050, 200)])


if __name__ == "__main__":
    unittest.main()

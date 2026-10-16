"""Regressions for console contamination and changing EA desktop ownership."""
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'windows'))
from apex_automation.ea_app import EaAccountBanned, EaCaptureUnavailable, EaRestrictionUnconfirmed
from apex_automation.ea_app_win32 import EaObservation, WindowsEaHybridDriver
from apex_automation.ea_ban_evidence import ban_dialog_visible
from apex_automation.ea_pages import EaPage, classify_page, page_markers
from apex_automation.ocr_obstacles import OcrToken, normalize_ocr_text


def dialog():
    tokens = tuple(OcrToken(text, .99, (x-100, y-10, x+100, y+10)) for text, x, y in (
        ('Your account has been banned', 500, 300),
        ('Error code: EC:107', 500, 500), ('CLOSE', 600, 650)))
    return EaObservation((0, 0, 1000, 800), np.zeros((1, 1), np.uint8), tokens,
                         EaPage.BANNED, hwnd=7, process_id=42, source_verified=True)


class BanEvidenceTest(unittest.TestCase):
    def test_runner_logs_are_not_ban_headings(self):
        texts = ('EA_ACCOUNT_BANNED', '失败： EA_ACCOUNT BANNED @ EA_SIGNING_IN: EA',
                 '当前账号已封禁，正在退出并换号', 'EA App 报告账号已封禁',
                 'Your library is empty', 'Looking for something?')
        for text in texts:
            with self.subTest(text=text):
                words = [normalize_ocr_text(text)]
                self.assertNotEqual(classify_page(words), EaPage.BANNED)
                self.assertNotIn('banned', page_markers(words))

    def test_complete_heading_and_split_heading_still_detected(self):
        for words in (['Your account has been banned'], ['Your account', 'has been banned'],
                      ['你的账号已被封禁']):
            self.assertEqual(classify_page(map(normalize_ocr_text, words)), EaPage.BANNED)

    def test_incident_ocr_lines_do_not_reproduce_bans(self):
        rows = json.loads((Path(__file__).parent / 'fixtures/ea-console-ban-ocr.json').read_text())
        for row in rows['samples']:
            with self.subTest(shot=row['shot'], scope=row['scope']):
                self.assertNotEqual(classify_page(map(normalize_ocr_text, row['text'])), EaPage.BANNED)

    def test_real_dialog_requires_title_detail_and_close_with_confident_boxes(self):
        item = dialog()
        self.assertTrue(ban_dialog_visible(item.tokens, item.rect))
        for index in range(3):
            self.assertFalse(ban_dialog_visible(item.tokens[:index] + item.tokens[index+1:], item.rect))
            low = list(item.tokens)
            low[index] = replace(low[index], confidence=.5)
            self.assertFalse(ban_dialog_visible(low, item.rect))
        self.assertFalse(ban_dialog_visible([replace(t, roi=None) for t in item.tokens], item.rect))

    def driver(self, next_frame):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._observe = Mock(return_value=next_frame)
        driver._record, driver._click_point, driver.notify, driver.sleep = Mock(), Mock(), Mock(), Mock()
        return driver

    def test_stable_real_dialog_is_reported_after_second_observation(self):
        initial, second = dialog(), dialog()
        driver = self.driver(second)
        with self.assertRaises(EaAccountBanned):
            driver._raise_if_account_banned(7, initial)
        driver._observe.assert_called_once_with(7)
        driver._click_point.assert_called_once_with(7, 600, 650)

    def test_changed_surface_or_unconfirmed_message_never_reports_a_ban_or_clicks(self):
        initial = dialog()
        for second in (replace(initial, hwnd=8), replace(initial, process_id=43),
                       replace(initial, source_verified=False), replace(initial, page=EaPage.SIGNED_IN),
                       replace(initial, tokens=initial.tokens[:1])):
            with self.subTest(second=second.page, hwnd=second.hwnd):
                driver = self.driver(second)
                with self.assertRaises(EaRestrictionUnconfirmed):
                    driver._raise_if_account_banned(7, initial)
                driver._click_point.assert_not_called()
                self.assertNotIn('account-banned', [c.args[0] for c in driver._record.call_args_list])
        driver = self.driver(initial)
        with self.assertRaises(EaRestrictionUnconfirmed):
            driver._raise_if_account_banned(7, replace(initial, source_verified=False))
        driver._observe.assert_not_called()


class CaptureSourceTest(unittest.TestCase):
    def driver(self):
        driver = object.__new__(WindowsEaHybridDriver)
        driver._live = lambda hwnd: hwnd or 7
        driver._focus, driver.sleep = Mock(), Mock()
        driver._safe_window_title = lambda _: 'EA'
        driver._frame = Mock(return_value=np.zeros((800, 1000, 3), np.uint8))
        driver._clip_rect = lambda *_: (0, 0, 1000, 800)
        driver._observation_source = Mock(return_value=(42, 7, (0, 0, 1000, 800)))
        driver._token_belongs_to_window = lambda *_: True
        driver.ocr = SimpleNamespace(read_with_boxes=Mock(return_value=dialog().tokens))
        return driver

    def test_focus_change_during_capture_retries_before_ocr(self):
        driver = self.driver()
        source = driver._observation_source.return_value
        driver._observation_source.side_effect = [source, (42, 99, source[2]), source, source, source]
        item = driver._observe(7, retries=2)
        self.assertEqual(driver._frame.call_count, 2)
        self.assertEqual(driver.ocr.read_with_boxes.call_count, 1)
        self.assertTrue(item.source_verified)

    def test_focus_change_during_ocr_discards_the_read(self):
        driver = self.driver()
        source = driver._observation_source.return_value
        driver._observation_source.side_effect = [source, source, (42, 99, source[2])]
        with self.assertRaises(EaCaptureUnavailable):
            driver._observe(7, retries=1)

    def test_console_text_occluding_ea_is_removed_before_classification(self):
        driver = self.driver()
        driver.ocr.read_with_boxes.return_value += (OcrToken('Library', .99, (20, 20, 80, 40)),)
        driver._token_belongs_to_window = lambda _, t: t.text == 'Library'
        item = driver._observe(7)
        self.assertEqual([t.text for t in item.tokens], ['Library'])
        self.assertNotEqual(item.page, EaPage.BANNED)

    def test_each_part_of_token_must_belong_to_the_requested_window(self):
        driver = self.driver()
        driver.user32 = SimpleNamespace(WindowFromPoint=Mock(side_effect=[7, 7, 99]),
            GetAncestor=lambda hwnd, _: hwnd, IsWindowVisible=Mock(return_value=True),
            GetWindowLongW=Mock(return_value=0x10CF0000))  # a covering overlapped window
        driver._window_belongs_to_ea = lambda root, child: root == child
        self.assertFalse(WindowsEaHybridDriver._token_belongs_to_window(driver, 7, dialog().tokens[0]))

    def menu_driver(self, *, style, pids):
        # EA's main menu is an unowned popup over the frame: no parent/owner chain.
        driver = self.driver()

        def thread_process(hwnd, ref):
            ref._obj.value = pids[hwnd]
            return 1

        driver.user32 = SimpleNamespace(
            WindowFromPoint=Mock(return_value=55), GetAncestor=Mock(return_value=55),
            IsWindowVisible=Mock(return_value=True), GetWindowLongW=Mock(return_value=style),
            GetWindowThreadProcessId=thread_process)
        driver._window_belongs_to_ea = lambda root, child: root == child
        return driver

    def test_popup_menu_of_the_ea_process_keeps_its_text(self):
        help_item = OcrToken('Help', .99, (203, 259, 245, 282))
        driver = self.menu_driver(style=-0x70000000, pids={7: 42, 55: 42})  # WS_POPUP|WS_VISIBLE
        self.assertTrue(WindowsEaHybridDriver._token_belongs_to_window(driver, 7, help_item))

    def test_popup_of_another_process_or_a_second_ea_root_is_still_rejected(self):
        help_item = OcrToken('Help', .99, (203, 259, 245, 282))
        other_process = self.menu_driver(style=-0x70000000, pids={7: 42, 55: 99})
        self.assertFalse(WindowsEaHybridDriver._token_belongs_to_window(other_process, 7, help_item))
        overlapped_root = self.menu_driver(style=0x10CF0000, pids={7: 42, 55: 42})
        self.assertFalse(WindowsEaHybridDriver._token_belongs_to_window(overlapped_root, 7, help_item))

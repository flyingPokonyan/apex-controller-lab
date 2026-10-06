from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.notifications import NotificationCloser, NotificationWindow, find_close_cross
from apex_automation.ocr_obstacles import OcrToken


class FakeDesktop:
    def __init__(self, executable="PalmInput.exe"):
        self.window = NotificationWindow(2, (1540,800,1920,1040), executable)
        self.visible = True
        self.foreground_id = 1
        self.clicks = []
        self.stale = False

    def size(self): return 1920,1080
    def foreground(self): return self.foreground_id
    def window_at(self, point):
        if not self.visible or (self.stale and point[0] > 1800): return None
        return self.window
    def click(self, point):
        self.clicks.append(point)
        self.visible = False
        self.foreground_id = 2
    def restore(self, handle): self.foreground_id = handle


class NotificationTest(unittest.TestCase):
    def setUp(self):
        self.frame = np.zeros((540,960,3), dtype=np.uint8)
        self.frame[400:520,770:960] = 80
        for i in range(7):
            self.frame[410+i,940+i] = 255
            self.frame[410+i,946-i] = 255
        self.tokens = (OcrToken("手心输入法", .8, (780,410,850,420)),)
        self.events = []

    def closer(self, backend):
        guard = SimpleNamespace(ensure_not_aborted=lambda: None, target_is_foreground=lambda: backend.foreground_id == 1)
        return NotificationCloser(backend, guard, lambda e, **p: self.events.append((e,p)), sleep=lambda _: None)

    def test_cross_is_located_on_both_light_and_dark_bars(self):
        for frame in (self.frame, 255-self.frame):
            self.assertEqual(find_close_cross(frame,(770,400,960,520),self.tokens[0].roi), (943,413))

    def test_known_popup_uses_physical_coordinates_and_restores_apex(self):
        backend = FakeDesktop()
        self.assertTrue(self.closer(backend)(self.frame,self.tokens))
        self.assertEqual(backend.clicks, [(1886,826)])
        self.assertEqual(backend.foreground_id,1)
        self.assertTrue(self.events[-1][1]["disappeared"])

    def test_unknown_owner_and_missing_cross_are_not_clicked(self):
        for backend, frame in ((FakeDesktop("r5apex.exe"),self.frame),
                               (FakeDesktop(),np.zeros_like(self.frame))):
            self.assertFalse(self.closer(backend)(frame,self.tokens))
            self.assertEqual(backend.clicks, [])

    def test_window_disappearing_or_moving_before_click_is_not_clicked(self):
        backend = FakeDesktop()
        backend.stale = True
        self.assertFalse(self.closer(backend)(self.frame,self.tokens))
        self.assertEqual(backend.clicks, [])

    def test_unknown_notification_title_is_not_clicked(self):
        backend = FakeDesktop()
        self.assertFalse(self.closer(backend)(self.frame, (OcrToken("领取奖励",1,(780,410,850,420)),)))
        self.assertEqual(backend.clicks, [])

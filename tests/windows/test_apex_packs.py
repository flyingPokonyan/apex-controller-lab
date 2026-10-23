from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "windows"))
from apex_automation.apex_packs import (
    ApexPackReader, LegendTokenReader, LobbyProbes, PackReading, PackInventoryProbe,
    parse_legend_tokens, parse_pack_tokens,
)
from apex_automation.ocr_obstacles import OcrToken
from apex_automation.frame_normalization import ReferenceCanvasFrameSource
from apex_automation.capture import DxcamFrameSource


def card(count="23", shift=0, confidence=.99):
    return (
        OcrToken(count, confidence, (45, 35+shift, 88, 68+shift)),
        OcrToken("APEX", .99, (90, 38+shift, 125, 51+shift)),
        OcrToken("组合包", .99, (89, 51+shift, 129, 68+shift)),
        OcrToken("开启0/10", .99, (17, 73+shift, 50, 83+shift)),
        OcrToken("1天3小时", .99, (158, 24, 195, 35)),
    )


class PackReaderTest(unittest.TestCase):
    def test_both_card_slots_and_zero(self):
        for count, shift in ((23, 0), (4, 68), (0, 68)):
            reading = parse_pack_tokens(card(str(count), shift))
            self.assertEqual((reading.count, reading.status), (count, "OK"))

    def test_no_card_ambiguous_and_low_confidence_are_not_zero(self):
        for tokens in ((), card(confidence=.8), card()+card("24"), card()[1:]):
            self.assertIsNone(parse_pack_tokens(tokens).count)

    def test_adjacent_promo_numbers_and_open_counter_are_not_inventory(self):
        self.assertEqual(parse_pack_tokens(card()[1:]).status, "UNREADABLE")
        self.assertEqual(parse_pack_tokens(card() + (OcrToken("手心输入法", .99, (100, 80, 180, 95)),)).status, "OCCLUDED")

    def test_reader_keeps_absolute_box_positions_after_crop(self):
        frame = np.zeros((540, 960, 3), dtype=np.uint8)
        seen = []
        reader = ApexPackReader(SimpleNamespace(read_with_boxes=lambda crop: seen.append(crop.shape) or card()))
        reading = reader.read(frame)
        self.assertEqual(reading.count, 23)
        self.assertGreater(reading.tokens[0].roi[0], 700)
        self.assertLess(seen[0][1], 230)


def currency(text, confidence=.96, second_row=("60", "100")):
    # Positions on the 2560x1440 canvas, as RapidOCR returned them on real lobby frames.
    return (OcrToken(text, confidence, (2270, 18, 2400, 50)),) + tuple(
        OcrToken(other, .9, (2264 + 97 * i, 67, 2300 + 97 * i, 95)) for i, other in enumerate(second_row))


class LegendTokenReaderTest(unittest.TestCase):
    def read(self, tokens):
        return parse_legend_tokens(tokens, top=0, height=1440)

    def test_real_lobby_texts_including_the_merged_next_currency(self):
        for text, amount in (("12.0K0", 12000), ("10.2K 50", 10200), (" 11.4K  0", 11400),
                             ("4 11.4K  15", 11400), ("7.80K50", 7800), ("3.60k50", 3600)):
            reading = self.read(currency(text))
            self.assertEqual((reading.status, reading.count), ("OK", amount), text)

    def test_a_dropped_decimal_point_or_low_confidence_is_not_a_reading(self):
        for tokens in (currency("540K0"), currency("12.0K0", confidence=.6), currency("600 0")):
            self.assertIsNone(self.read(tokens).count)
        self.assertEqual(self.read(()).status, "NOT_VISIBLE")

    def test_second_row_and_disagreeing_texts_are_not_legend_tokens(self):
        self.assertIsNone(self.read((OcrToken("4.80K", .99, (2264, 67, 2330, 95)),)).count)
        self.assertEqual(self.read(currency("12.0K0") + currency("10.2K0")[:1]).status, "UNREADABLE")

    def test_reader_pads_the_crop_and_maps_boxes_back(self):
        frame = np.zeros((1440, 2560, 3), dtype=np.uint8)
        seen = []
        def read_with_boxes(crop):
            seen.append(crop.shape)
            return (OcrToken("12.0K0", .97, (82, 50, 212, 82)),)
        reading = LegendTokenReader(SimpleNamespace(read_with_boxes=read_with_boxes)).read(frame)
        self.assertEqual(reading.count, 12000)
        self.assertEqual(reading.tokens[0].roi[:2], (2124 + 82 - 32, 50 - 32))
        self.assertEqual(seen[0][:2], (129 + 64, 346 + 64))


class PackProbeTest(unittest.TestCase):
    def make_probe(self, frames, readings):
        self.now = 0
        self.events = []
        self.readings = iter(readings)
        self.frames = iter(frames)
        self.guard = SimpleNamespace(ensure_not_aborted=lambda: None, target_is_foreground=lambda: True)
        self.source = SimpleNamespace(grab_fresh=lambda: next(self.frames, None))
        self.reader = SimpleNamespace(read=lambda frame: next(self.readings))
        return PackInventoryProbe(self.reader, self.source, self.guard,
            lambda event, **payload: self.events.append((event, payload)), clock=lambda: self.now,
            sleep=lambda seconds: setattr(self, "now", self.now+seconds))

    def test_identical_pixels_from_two_fresh_captures_confirm_without_adding(self):
        frame = np.zeros((10, 10, 3))
        probe = self.make_probe([frame, None, frame.copy(), frame.copy()], [PackReading(23,.99,"OK")]*3)
        for _ in range(4):
            probe.observe()
            self.now += 1
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][1]["count"], 23)
        self.assertEqual(self.events[0][1]["samples"], 2)

    def test_missing_never_confirms_zero_and_attempt_budget_is_bounded(self):
        probe = self.make_probe([1]*10, [PackReading(None,0,"NOT_VISIBLE")]*10)
        for _ in range(10):
            probe.observe()
            self.now += 1
        self.assertEqual(probe.attempts, 3)
        self.assertIsNone(self.events[0][1]["count"])
        self.assertEqual(self.events[0][1]["readStatus"], "NOT_VISIBLE")

    def test_disagreement_needs_two_consecutive_confirmations(self):
        probe = self.make_probe([1]*3, [PackReading(4,.99,"OK"), PackReading(23,.99,"OK"), PackReading(23,.99,"OK")])
        for _ in range(3):
            probe.observe()
            self.now += 1
        self.assertEqual(self.events[-1][1]["count"], 23)

    def test_finish_checks_current_lease_and_foreground(self):
        probe = self.make_probe([1], [PackReading(23,.99,"OK")])
        probe.finish(is_current=lambda: False)
        self.guard.target_is_foreground = lambda: False
        probe.finish()
        self.assertEqual(probe.attempts, 0)

    def test_fresh_frame_does_not_fall_back_to_cached_grab(self):
        source = ReferenceCanvasFrameSource(SimpleNamespace(grab=lambda: self.fail("cached capture")), (16,9), (16,9))
        self.assertIsNone(source.grab_fresh())

    def test_static_fresh_capture_does_not_rebuild_or_wait_for_recovery(self):
        options = []
        source = DxcamFrameSource(sleep=lambda _: self.fail("unnecessary recovery wait"))
        source._started = True
        source._camera = SimpleNamespace(grab=lambda **kw: options.append(kw) or None)
        self.assertIsNone(source.grab_fresh())
        self.assertEqual(options, [{"new_frame_only": True}])

    def test_ocr_failure_is_optional_and_does_not_hold_play(self):
        probe = self.make_probe([1], [])
        probe.reader.read = lambda _: (_ for _ in ()).throw(RuntimeError("OCR unavailable"))
        self.assertIsNone(probe.observe())
        self.assertEqual(self.events[0][0], "PACK_READ_ERROR")

    def test_final_read_records_unconfirmed_when_no_fresh_frame_arrives(self):
        probe = self.make_probe([], [])
        probe.finish()
        self.assertEqual(self.events[-1][1]["readStatus"], "UNCONFIRMED")
        self.assertIsNone(self.events[-1][1]["count"])

    def test_legend_tokens_use_their_own_event_and_field(self):
        probe = self.make_probe([1, 1], [PackReading(12000, .96, "OK")] * 2)
        probe.event, probe.field = "LEGEND_TOKENS", "amount"
        for _ in range(2):
            probe.observe()
            self.now += 1
        self.assertEqual(self.events, [("LEGEND_TOKENS", {"amount": 12000, "confidence": .96, "readStatus": "OK", "samples": 2})])

    def test_lobby_probes_drive_every_probe(self):
        calls = []
        probe = lambda name: SimpleNamespace(
            reset=lambda: calls.append((name, "reset")),
            observe=lambda force=False: calls.append((name, "observe", force)),
            finish=lambda is_current, budget_s: calls.append((name, "finish", budget_s)))
        group = LobbyProbes((probe("packs"), probe("tokens")))
        group.reset(); group.observe(); group.finish(budget_s=2)
        self.assertEqual(calls, [("packs", "reset"), ("tokens", "reset"), ("packs", "observe", False),
                                 ("tokens", "observe", False), ("packs", "finish", 2), ("tokens", "finish", 2)])

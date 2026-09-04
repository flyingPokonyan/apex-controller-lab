from __future__ import annotations

from pathlib import Path
import sys
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.ocr_obstacles import OcrToken
from apex_automation.progression import (
    DEFAULT_LEVEL_REGION,
    DEFAULT_XP_REGION,
    LobbyProgressionReader,
    LobbyProgressionStabilizer,
    parse_level,
    parse_xp_pair,
)
from apex_automation.progression_policy import (
    ProgressionContext,
    ProgressionDecision,
    ProgressionOutcome,
    TargetLevelAndRingPolicy,
    TargetLevelPolicy,
)


class FakeProvider:
    def __init__(
        self,
        values: dict[tuple[int, int, int, int], tuple[OcrToken, ...]],
    ) -> None:
        self.values = values

    def read(self, frame: np.ndarray, region) -> tuple[OcrToken, ...]:
        return self.values.get(region.roi, ())


def reading(
    *,
    level: str = "9",
    xp: str = "7.87K / 8.15K",
    level_confidence: float = 0.99,
    xp_confidence: float = 0.98,
):
    provider = FakeProvider(
        {
            DEFAULT_LEVEL_REGION.roi: (OcrToken(level, level_confidence),),
            DEFAULT_XP_REGION.roi: (OcrToken(xp, xp_confidence),),
        }
    )
    frame = np.zeros((100, 400, 3), dtype=np.uint8)
    return LobbyProgressionReader(provider).read(frame)


class ProgressionParsingTest(unittest.TestCase):
    def test_parses_observed_level_and_k_xp_formats(self) -> None:
        self.assertEqual(parse_level(" ９ "), 9)
        self.assertEqual(parse_level("Lv: 12"), 12)
        self.assertEqual(parse_xp_pair("1.44K/3.90K"), (1440, 3900))
        self.assertEqual(parse_xp_pair("3.00k / 7.85k"), (3000, 7850))
        self.assertEqual(parse_xp_pair("7.87K／8.15K"), (7870, 8150))

    def test_accepts_explicit_integer_xp_but_not_ambiguous_decimals(self) -> None:
        self.assertEqual(parse_xp_pair("1,440 / 3,900"), (1440, 3900))
        self.assertEqual(parse_xp_pair("1440/3900"), (1440, 3900))
        self.assertIsNone(parse_xp_pair("1.44 / 3.90"))

    def test_does_not_guess_malformed_or_impossible_values(self) -> None:
        for raw_text in (
            "l.44K/3.90K",
            "1.44K 3.90K",
            "4.00K/3.90K",
            "1.44K/3.9OK",
            "1.44M/3.90M",
        ):
            with self.subTest(raw_text=raw_text):
                self.assertIsNone(parse_xp_pair(raw_text))
        self.assertIsNone(parse_level("O9"))
        self.assertIsNone(parse_level("0"))
        self.assertIsNone(parse_level("1000"))


class LobbyProgressionReaderTest(unittest.TestCase):
    def test_default_regions_are_tight_single_line_crops(self) -> None:
        self.assertEqual(DEFAULT_LEVEL_REGION.roi, (36, 27, 76, 76))
        self.assertEqual(DEFAULT_XP_REGION.roi, (200, 54, 356, 91))
        self.assertTrue(DEFAULT_LEVEL_REGION.single_line)
        self.assertTrue(DEFAULT_XP_REGION.single_line)

    def test_reads_a_complete_snapshot_and_builds_report_payload(self) -> None:
        result = reading()

        self.assertTrue(result.is_complete)
        self.assertEqual(result.level, 9)
        self.assertEqual(result.xp_current_approx, 7870)
        self.assertEqual(result.xp_required_approx, 8150)
        self.assertEqual(result.level_evidence.raw_text, "9")
        self.assertEqual(result.raw_text, "7.87K / 8.15K")
        self.assertEqual(result.confidence, 0.98)
        self.assertEqual(
            result.as_event_payload(
                "RETURNED_AFTER_MATCH",
                changed=True,
                delta_approx=4440,
            ),
            {
                "reason": "RETURNED_AFTER_MATCH",
                "level": 9,
                "xpCurrentApprox": 7870,
                "xpRequiredApprox": 8150,
                "rawText": "7.87K / 8.15K",
                "levelRawText": "9",
                "xpRawText": "7.87K / 8.15K",
                "confidence": 0.98,
                "levelConfidence": 0.99,
                "xpConfidence": 0.98,
                "changed": True,
                "deltaApprox": 4440,
                "readStatus": "OK",
            },
        )

    def test_low_confidence_keeps_evidence_but_never_returns_values(self) -> None:
        result = reading(level_confidence=0.40)

        self.assertFalse(result.is_complete)
        self.assertIsNone(result.level)
        self.assertEqual(result.level_evidence.raw_text, "9")
        self.assertEqual(result.level_evidence.confidence, 0.40)
        payload = result.as_event_payload("INITIAL", changed=True, delta_approx=10)
        self.assertIsNone(payload["level"])
        self.assertIsNone(payload["xpCurrentApprox"])
        self.assertIsNone(payload["xpRequiredApprox"])
        self.assertEqual(payload["rawText"], "7.87K / 8.15K")
        self.assertEqual(payload["confidence"], 0.0)
        self.assertFalse(payload["changed"])
        self.assertIsNone(payload["deltaApprox"])
        self.assertEqual(payload["readStatus"], "FAILED")

    def test_unparseable_xp_does_not_reuse_the_valid_level(self) -> None:
        result = reading(xp="7.B7K / 8.15K")

        self.assertFalse(result.is_complete)
        self.assertEqual(result.level, 9)
        self.assertIsNone(result.xp_current_approx)
        self.assertIn("经验文字格式无法确认", result.error or "")
        payload = result.as_event_payload("STATE_REENTRY")
        self.assertIsNone(payload["level"])


class LobbyProgressionStabilizerTest(unittest.TestCase):
    def test_requires_two_identical_complete_readings(self) -> None:
        stabilizer = LobbyProgressionStabilizer()
        first = reading()
        second = reading(xp_confidence=0.96)

        self.assertIsNone(stabilizer.observe(first))
        self.assertEqual(stabilizer.candidate_count, 1)
        self.assertIs(stabilizer.observe(second), second)
        self.assertIsNone(stabilizer.observe(second))

    def test_changed_or_failed_reading_restarts_confirmation(self) -> None:
        stabilizer = LobbyProgressionStabilizer()
        value_a = reading(xp="3.00K / 7.85K")
        value_b = reading(xp="5.18K / 7.85K")
        failed = reading(xp="unreadable")

        self.assertIsNone(stabilizer.observe(value_a))
        self.assertIsNone(stabilizer.observe(value_b))
        self.assertIsNone(stabilizer.observe(failed))
        self.assertIsNone(stabilizer.observe(value_b))
        self.assertIs(stabilizer.observe(value_b), value_b)

    def test_reset_allows_same_values_on_a_new_lobby_visit(self) -> None:
        stabilizer = LobbyProgressionStabilizer()
        value = reading()

        self.assertIsNone(stabilizer.observe(value))
        self.assertIsNotNone(stabilizer.observe(value))
        stabilizer.reset()
        self.assertIsNone(stabilizer.observe(value))
        self.assertIsNotNone(stabilizer.observe(value))


class TargetLevelPolicyTest(unittest.TestCase):
    def context(self, **changes) -> ProgressionContext:
        values = {
            "observed_state": "LOBBY_READY_TARGET",
            "safe_lobby": True,
            "queueing": False,
            "overlay_clear": True,
            "pending_action": False,
            "foreground": True,
            "lease_current": True,
        }
        values.update(changes)
        return ProgressionContext(**values)

    def test_below_target_continues_and_safe_target_stops(self) -> None:
        policy = TargetLevelPolicy(20)
        below = ProgressionOutcome.confirmed(reading(level="19"), attempts=2)
        reached = ProgressionOutcome.confirmed(reading(level="20"), attempts=2)

        self.assertEqual(
            policy.decide(below, self.context()),
            ProgressionDecision.CONTINUE_PLAY,
        )
        self.assertEqual(
            policy.decide(reached, self.context()),
            ProgressionDecision.TARGET_REACHED,
        )

    def test_target_defers_until_every_safe_lobby_gate_is_true(self) -> None:
        policy = TargetLevelPolicy(20)
        reached = ProgressionOutcome.confirmed(reading(level="20"), attempts=2)

        for changes in (
            {"safe_lobby": False},
            {"queueing": True},
            {"overlay_clear": False},
            {"pending_action": True},
            {"foreground": False},
        ):
            with self.subTest(changes=changes):
                self.assertEqual(
                    policy.decide(reached, self.context(**changes)),
                    ProgressionDecision.DEFER_UNTIL_SAFE_LOBBY,
                )

    def test_failed_read_and_stale_lease_fail_closed(self) -> None:
        policy = TargetLevelPolicy(20)
        failed = ProgressionOutcome.failed(
            reading(xp="?"),
            attempts=3,
            error="unconfirmed",
        )
        below = ProgressionOutcome.confirmed(reading(level="19"), attempts=2)

        self.assertEqual(
            policy.decide(failed, self.context()),
            ProgressionDecision.PAUSE_UNCERTAIN,
        )
        self.assertEqual(
            policy.decide(below, self.context(lease_current=False)),
            ProgressionDecision.PAUSE_UNCERTAIN,
        )


class TargetLevelAndRingPolicyTest(unittest.TestCase):
    def context(self, **changes) -> ProgressionContext:
        values = {
            "observed_state": "LOBBY_READY_TARGET",
            "safe_lobby": True,
            "queueing": False,
            "overlay_clear": True,
            "pending_action": False,
            "foreground": True,
            "lease_current": True,
            "ring_progress": 30,
            "ring_target": 30,
        }
        values.update(changes)
        return ProgressionContext(**values)

    def test_only_an_observed_unfinished_ring_objective_keeps_playing(self) -> None:
        policy = TargetLevelAndRingPolicy(20, target_ring=30)

        self.assertEqual(
            policy.decide(
                ProgressionOutcome.confirmed(reading(level="19"), attempts=2),
                self.context(ring_progress=30),
            ),
            ProgressionDecision.CONTINUE_PLAY,
        )
        self.assertEqual(
            policy.decide(
                ProgressionOutcome.confirmed(reading(level="20"), attempts=2),
                self.context(ring_progress=29),
            ),
            ProgressionDecision.CONTINUE_PLAY,
        )
        self.assertEqual(
            policy.decide(
                ProgressionOutcome.confirmed(reading(level="20"), attempts=2),
                self.context(ring_progress=None, ring_target=None),
            ),
            ProgressionDecision.TARGET_REACHED,
        )

    def test_both_objectives_stop_only_in_a_safe_lobby(self) -> None:
        policy = TargetLevelAndRingPolicy(20, target_ring=30)
        reached = ProgressionOutcome.confirmed(reading(level="20"), attempts=2)

        self.assertEqual(
            policy.decide(reached, self.context()),
            ProgressionDecision.TARGET_REACHED,
        )
        self.assertEqual(
            policy.decide(reached, self.context(overlay_clear=False)),
            ProgressionDecision.DEFER_UNTIL_SAFE_LOBBY,
        )

    def test_unobserved_ring_objective_allows_the_level_target_to_finish(self) -> None:
        policy = TargetLevelAndRingPolicy(20, target_ring=30)
        reached = ProgressionOutcome.confirmed(reading(level="20"), attempts=2)

        self.assertEqual(
            policy.decide(
                reached,
                self.context(
                    ring_progress=None,
                    ring_target=None,
                ),
            ),
            ProgressionDecision.TARGET_REACHED,
        )

    def test_last_observed_ring_progress_survives_a_later_missing_task(self) -> None:
        reached = ProgressionOutcome.confirmed(reading(level="20"), attempts=2)

        incomplete = TargetLevelAndRingPolicy(
            20,
            target_ring=30,
            observed_ring_progress=14,
            observed_ring_target=30,
        )
        complete = TargetLevelAndRingPolicy(
            20,
            target_ring=30,
            observed_ring_progress=30,
            observed_ring_target=30,
        )

        self.assertEqual(
            incomplete.decide(
                reached,
                self.context(ring_progress=None, ring_target=None),
            ),
            ProgressionDecision.CONTINUE_PLAY,
        )
        self.assertEqual(
            complete.decide(
                reached,
                self.context(ring_progress=None, ring_target=None),
            ),
            ProgressionDecision.TARGET_REACHED,
        )


if __name__ == "__main__":
    unittest.main()

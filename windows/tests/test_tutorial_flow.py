from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from apex_automation.config import load_config
from apex_automation.ocr_obstacles import OcrToken
from apex_automation.ocr_states import OcrStateDetector
from apex_automation.supervisor import ActionSpec, PendingAction, TaskSupervisor


ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = ROOT / "windows" / "config" / "first-tutorial-2560x1440.zh-CN.json"
RULES_PATH = ROOT / "windows" / "config" / "tutorial-states.zh-CN.json"


class FakeProvider:
    def __init__(self, texts: list[str]):
        self.tokens = tuple(OcrToken(text, 0.99) for text in texts)

    def read(self, frame: np.ndarray, roi: tuple[int, int, int, int]):
        return self.tokens


class FakeSender:
    def __init__(self):
        self.calls: list[tuple[object, ...]] = []

    def click(self, x: int, y: int) -> None:
        self.calls.append(("click", x, y))

    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None:
        self.calls.append(("tap", scan_code, duration_ms))

    def hold_scan_codes(self, scan_codes, duration_ms: int) -> None:
        self.calls.append(("holdKeys", tuple(scan_codes), duration_ms))

    def move_mouse(self, dx: int, dy: int) -> None:
        self.calls.append(("moveMouse", dx, dy))

    def hold_mouse_button(self, button: str, duration_ms: int) -> None:
        self.calls.append(("holdMouse", button, duration_ms))

    def release_all(self) -> None:
        self.calls.append(("releaseAll",))


class FakeGuard:
    def ensure_not_aborted(self) -> None:
        return None

    def ensure_target_foreground(self) -> None:
        return None

    def target_is_foreground(self) -> bool:
        return True


class FakeRecorder:
    def __init__(self, directory: Path):
        self.run_dir = directory
        self.events: list[tuple[str, dict[str, object]]] = []
        self.status: dict[str, object] = {}

    def log(self, event: str, **payload: object) -> None:
        self.events.append((event, payload))

    def screenshot(self, stage: str, frame: np.ndarray) -> Path:
        return self.run_dir / f"{stage}.png"

    def write_status(self, payload: dict[str, object]) -> None:
        self.status = payload


class TutorialFlowTests(unittest.TestCase):
    def test_guide_rule_beats_lobby_mode_select_terms(self) -> None:
        detector = OcrStateDetector.from_path(
            FakeProvider(
                [
                    "进度",
                    "查看通行证和挑战进度",
                    "继续",
                    "选择",
                    "训练",
                    "射击场",
                ]
            ),
            RULES_PATH,
        )
        decision = detector.analyze(np.zeros((1440, 2560, 3), dtype=np.uint8)).decision
        self.assertIsNotNone(decision)
        self.assertEqual("GUIDE_PROGRESS", decision.state)

    def _supervisor(self, sender: FakeSender, recorder: FakeRecorder) -> TaskSupervisor:
        config = load_config(CONFIG_PATH)
        return TaskSupervisor(
            config,
            detector=object(),
            source=object(),
            sender=sender,
            guard=FakeGuard(),
            recorder=recorder,
            mode="tutorial",
            guided_detector=object(),
            sleep=lambda _: None,
            monotonic=lambda: 100.0,
        )

    def test_sequence_executes_bounded_movement_primitives(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sender = FakeSender()
            supervisor = self._supervisor(sender, FakeRecorder(Path(directory)))
            steps = supervisor._execute_sequence("movementBasics")
        self.assertEqual(4, len(steps))
        self.assertEqual(("holdKeys", (17,), 800), sender.calls[0])
        self.assertEqual(("holdKeys", (17, 42), 900), sender.calls[1])
        self.assertEqual(("tap", 29, 100), sender.calls[-1])

    def test_wrong_postcondition_does_not_confirm_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = FakeRecorder(Path(directory))
            supervisor = self._supervisor(FakeSender(), recorder)
            spec = ActionSpec(
                "openBin",
                "sequence",
                1200,
                2,
                allowed_next_states=("TUTORIAL_PICKUP_WEAPON",),
            )
            supervisor.pending_action = PendingAction(spec, "TUTORIAL_OPEN_BIN", 3, 1, 102.0)
            confirmed = supervisor._confirm_pending("TUTORIAL_SHOOT")
        self.assertFalse(confirmed)
        self.assertIsNone(supervisor.pending_action)
        self.assertTrue(
            any(event == "ACTION_POSTCONDITION_REJECTED" for event, _ in recorder.events)
        )

    def test_short_unknown_transition_keeps_pending_postcondition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            supervisor = self._supervisor(FakeSender(), FakeRecorder(Path(directory)))
            spec = ActionSpec(
                "openBin",
                "sequence",
                1200,
                2,
                allowed_next_states=("TUTORIAL_PICKUP_WEAPON",),
            )
            pending = PendingAction(spec, "TUTORIAL_OPEN_BIN", 3, 1, 102.0)
            supervisor.pending_action = pending
            supervisor._handle_unknown(
                np.zeros((1440, 2560, 3), dtype=np.uint8),
                [],
                100.0,
            )
        self.assertIs(pending, supervisor.pending_action)

    def test_terminal_state_releases_input_and_marks_goal_reached(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            sender = FakeSender()
            recorder = FakeRecorder(Path(directory))
            supervisor = self._supervisor(sender, recorder)
            supervisor._accept_state(
                "LOBBY_READY",
                0.98,
                np.zeros((1440, 2560, 3), dtype=np.uint8),
                100.0,
                source="template",
            )
        self.assertTrue(supervisor.goal_reached)
        self.assertEqual("GOAL_REACHED", supervisor.runtime_state)
        self.assertIn(("releaseAll",), sender.calls)
        self.assertTrue(any(event == "TASK_GOAL_REACHED" for event, _ in recorder.events))


if __name__ == "__main__":
    unittest.main()

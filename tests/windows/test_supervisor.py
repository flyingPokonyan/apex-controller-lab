from __future__ import annotations

from collections import deque
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "windows"))

from apex_automation.config import load_config
from apex_automation.control import TaskControl
from apex_automation.ocr_obstacles import OcrObstacleDetector, OcrToken
from apex_automation.recorder import RunRecorder
from apex_automation.supervisor import TaskSupervisor
from apex_automation.vision import VisionDetector, load_frame


class FakeClock:
    def __init__(self) -> None:
        self.value = 0.0

    def monotonic(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class FakeSource:
    def __init__(self, frames: list[np.ndarray]):
        self.frames = deque(frames)
        self.last = frames[-1]

    def grab(self) -> np.ndarray:
        if self.frames:
            self.last = self.frames.popleft()
        return self.last.copy()


class FakeGuard:
    def __init__(self, foreground: bool = True):
        self.foreground = foreground

    def ensure_not_aborted(self) -> None:
        return None

    def ensure_target_foreground(self) -> None:
        if not self.foreground:
            raise RuntimeError("not foreground")

    def target_is_foreground(self) -> bool:
        return self.foreground


class FakeInput:
    def __init__(self):
        self.actions: list[tuple[object, ...]] = []

    def click(self, x: int, y: int) -> None:
        self.actions.append(("click", x, y))

    def tap_scan_code(self, scan_code: int, duration_ms: int) -> None:
        self.actions.append(("key", scan_code, duration_ms))

    def release_all(self) -> None:
        self.actions.append(("release",))


class FakeOcrProvider:
    def __init__(self, values: dict[tuple[int, int, int, int], tuple[OcrToken, ...]]):
        self.values = values

    def read(
        self,
        frame: np.ndarray,
        roi: tuple[int, int, int, int],
    ) -> tuple[OcrToken, ...]:
        return self.values.get(roi, ())


class FakeRecorder:
    run_dir = Path("fake-supervisor-run")

    def __init__(self):
        self.events: list[tuple[str, dict[str, object]]] = []
        self.statuses: list[dict[str, object]] = []
        self.screenshots: list[str] = []

    def log(self, event: str, **payload: object) -> None:
        self.events.append((event, payload))

    def screenshot(self, stage: str, frame: np.ndarray) -> Path:
        self.screenshots.append(stage)
        return Path(f"{stage}.png")

    def write_status(self, payload: dict[str, object]) -> None:
        self.statuses.append(dict(payload))


class SupervisorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config()
        cls.detector = VisionDetector(cls.config)
        cls.frames = {
            state: load_frame(path)
            for path, state in cls.config.offline_cases
        }
        height = int(cls.config.environment["height"])
        width = int(cls.config.environment["width"])
        gradient = np.linspace(0, 255, width, dtype=np.uint8)
        cls.unknown_frame = np.repeat(gradient[np.newaxis, :], height, axis=0)
        cls.unknown_frame = np.repeat(cls.unknown_frame[:, :, np.newaxis], 3, axis=2)
        cls.black_frame = np.zeros_like(cls.frames["CONTINUE"])

    def make_supervisor(
        self,
        frames: list[np.ndarray],
        *,
        foreground: bool = True,
        unknown_grace_ms: int = 5000,
        obstacle_detector: OcrObstacleDetector | None = None,
        safe_obstacles_armed: bool = False,
        control: TaskControl | None = None,
    ) -> tuple[TaskSupervisor, FakeClock, FakeGuard, FakeInput, FakeRecorder]:
        clock = FakeClock()
        guard = FakeGuard(foreground)
        sender = FakeInput()
        recorder = FakeRecorder()
        supervisor = TaskSupervisor(
            self.config,
            self.detector,
            FakeSource(frames),
            sender,
            guard,
            recorder,
            monotonic=clock.monotonic,
            sleep=lambda _: None,
            unknown_grace_ms=unknown_grace_ms,
            resume_stabilize_ms=500,
            obstacle_detector=obstacle_detector,
            safe_obstacles_armed=safe_obstacles_armed,
            control=control,
        )
        return supervisor, clock, guard, sender, recorder

    def reward_obstacle_detector(self) -> OcrObstacleDetector:
        rules_path = REPOSITORY_ROOT / "windows" / "config" / "obstacle-rules.zh-CN.json"
        payload = json.loads(rules_path.read_text(encoding="utf-8"))
        regions = {
            name: tuple(values)
            for name, values in payload["regions"].items()
        }
        provider = FakeOcrProvider(
            {
                regions["titleCenter"]: (OcrToken("获得奖励", 0.99),),
                regions["bottomCenter"]: (OcrToken("继续", 0.98),),
            }
        )
        return OcrObstacleDetector.from_path(provider, rules_path)

    def acquire_and_detect(
        self,
        supervisor: TaskSupervisor,
        clock: FakeClock,
        state: str,
    ) -> None:
        supervisor.step()
        clock.advance(0.6)
        for _ in range(self.config.states[state].stable_frames):
            supervisor.step()

    def test_starts_from_launch_ready_without_prior_states(self) -> None:
        supervisor, clock, _, sender, recorder = self.make_supervisor(
            [self.frames["LAUNCH_READY"]]
        )
        self.acquire_and_detect(supervisor, clock, "LAUNCH_READY")

        self.assertIn(("key", 18, 80), sender.actions)
        self.assertEqual(supervisor.observed_state, "LAUNCH_READY")
        self.assertEqual(supervisor.goal_stage, "LAUNCH")
        self.assertEqual(supervisor.goal_progress_version, 1)
        self.assertEqual(recorder.statuses[-1]["runtimeState"], "RUNNING")

    def test_foreground_loss_pauses_and_resume_reclassifies(self) -> None:
        supervisor, clock, guard, sender, recorder = self.make_supervisor(
            [self.frames["CONTINUE"]]
        )
        self.acquire_and_detect(supervisor, clock, "CONTINUE")
        self.assertIsNotNone(supervisor.pending_action)

        guard.foreground = False
        supervisor.step()
        self.assertEqual(supervisor.runtime_state, "FOREGROUND_PAUSED")
        self.assertIsNone(supervisor.pending_action)
        self.assertIsNone(supervisor.observed_state)
        self.assertEqual(sender.actions[-1], ("release",))

        guard.foreground = True
        supervisor.step()
        self.assertEqual(supervisor.runtime_state, "OBSERVING")
        clock.advance(0.6)
        for _ in range(self.config.states["CONTINUE"].stable_frames):
            supervisor.step()
        self.assertEqual(supervisor.observed_state, "CONTINUE")
        events = [event for event, _ in recorder.events]
        self.assertIn("FOREGROUND_PAUSED", events)
        self.assertIn("FOREGROUND_RESUMED", events)
        self.assertIn("ACTION_INVALIDATED", events)

    def test_initial_background_wait_is_not_an_error(self) -> None:
        supervisor, _, guard, sender, recorder = self.make_supervisor(
            [self.frames["CONTINUE"]],
            foreground=False,
        )
        supervisor.step()

        self.assertEqual(supervisor.runtime_state, "PENDING_FOREGROUND")
        self.assertEqual(sender.actions, [("release",)])
        self.assertEqual(recorder.statuses[-1]["lastEvent"], "FOREGROUND_WAITING")
        guard.foreground = True

    def test_unknown_saves_one_evidence_frame_and_keeps_running(self) -> None:
        self.assertFalse(any(score.matched for score in self.detector.rank_states(self.unknown_frame)))
        supervisor, clock, _, sender, recorder = self.make_supervisor(
            [self.unknown_frame],
            unknown_grace_ms=5000,
        )
        supervisor.step()
        clock.advance(0.6)
        supervisor.step()
        clock.advance(5.1)
        supervisor.step()
        supervisor.step()

        self.assertEqual(supervisor.runtime_state, "UNKNOWN_PAUSED")
        self.assertEqual(supervisor.observed_state, "UNKNOWN")
        self.assertEqual(recorder.screenshots, ["UNKNOWN"])
        self.assertFalse(any(action[0] in {"click", "key"} for action in sender.actions))
        self.assertFalse(any(event == "RUN_FINISHED" for event, _ in recorder.events))

    def test_ocr_obstacle_requires_two_observations_and_is_disarmed_by_default(self) -> None:
        supervisor, clock, _, sender, recorder = self.make_supervisor(
            [self.unknown_frame],
            unknown_grace_ms=0,
            obstacle_detector=self.reward_obstacle_detector(),
        )
        supervisor.step()
        clock.advance(0.6)
        supervisor.step()
        supervisor.step()
        self.assertNotEqual(supervisor.observed_state, "REWARD")

        clock.advance(2.1)
        supervisor.step()

        self.assertEqual(supervisor.observed_state, "REWARD")
        self.assertEqual(supervisor.runtime_state, "SAFE_ACTION_PAUSED")
        self.assertFalse(any(action[0] in {"click", "key"} for action in sender.actions))
        self.assertIn("SAFE_ACTION_DISARMED", [event for event, _ in recorder.events])

    def test_armed_ocr_obstacle_uses_keyboard_action_only(self) -> None:
        supervisor, clock, _, sender, _ = self.make_supervisor(
            [self.unknown_frame],
            unknown_grace_ms=0,
            obstacle_detector=self.reward_obstacle_detector(),
            safe_obstacles_armed=True,
        )
        supervisor.step()
        clock.advance(0.6)
        supervisor.step()
        supervisor.step()
        clock.advance(2.1)
        supervisor.step()

        self.assertEqual(supervisor.observed_state, "REWARD")
        self.assertIn(("key", 28, 80), sender.actions)
        self.assertFalse(any(action[0] == "click" for action in sender.actions))

    def test_manual_pause_releases_input_and_resume_rechecks_foreground(self) -> None:
        control = TaskControl()
        supervisor, clock, _, sender, recorder = self.make_supervisor(
            [self.frames["CONTINUE"]],
            control=control,
        )
        self.acquire_and_detect(supervisor, clock, "CONTINUE")
        control.pause()
        supervisor.step()
        self.assertEqual(supervisor.runtime_state, "MANUAL_PAUSED")
        self.assertIsNone(supervisor.pending_action)
        self.assertEqual(sender.actions[-1], ("release",))

        control.resume()
        supervisor.step()
        self.assertEqual(supervisor.runtime_state, "PENDING_FOREGROUND")
        self.assertIn("MANUAL_RESUMED", [event for event, _ in recorder.events])

    def test_loading_changes_observation_not_goal_progress(self) -> None:
        frames = [
            self.frames["LOBBY_READY"],
            self.frames["LOBBY_READY"],
            self.black_frame,
            self.frames["LOBBY_READY"],
            self.frames["LOBBY_READY"],
        ]
        supervisor, clock, _, _, _ = self.make_supervisor(frames)
        self.acquire_and_detect(supervisor, clock, "LOBBY_READY")
        first_goal_version = supervisor.goal_progress_version
        first_observation_version = supervisor.observation_version
        supervisor.step()
        self.assertEqual(supervisor.observed_state, "LOADING")
        self.assertGreater(supervisor.observation_version, first_observation_version)
        self.assertEqual(supervisor.goal_progress_version, first_goal_version)
        supervisor.step()
        supervisor.step()
        self.assertEqual(supervisor.observed_state, "LOBBY_READY")
        self.assertEqual(supervisor.goal_progress_version, first_goal_version)

    def test_jumpmaster_wait_never_presses_detach(self) -> None:
        supervisor, clock, _, sender, recorder = self.make_supervisor(
            [self.frames["DROPSHIP_JUMPMASTER_WAIT"]]
        )
        self.acquire_and_detect(supervisor, clock, "DROPSHIP_JUMPMASTER_WAIT")
        clock.advance(11)
        supervisor.step()

        self.assertFalse(any(action[0] == "key" for action in sender.actions))
        self.assertIn("NO_INPUT", [event for event, _ in recorder.events])

    def test_progress_regression_never_sends_old_menu_action(self) -> None:
        frames = [
            self.frames["LAUNCH_READY"],
            self.frames["LAUNCH_READY"],
            self.frames["LOBBY_READY"],
            self.frames["LOBBY_READY"],
        ]
        supervisor, clock, _, sender, recorder = self.make_supervisor(frames)
        self.acquire_and_detect(supervisor, clock, "LAUNCH_READY")
        supervisor.step()
        supervisor.step()

        self.assertEqual(supervisor.observed_state, "LOBBY_READY")
        self.assertEqual(
            [action for action in sender.actions if action[0] in {"click", "key"}],
            [("key", 18, 80)],
        )
        self.assertIn(
            "STATE_BEHIND_GOAL_IGNORED",
            [event for event, _ in recorder.events],
        )

    def test_capture_error_pauses_then_recovers(self) -> None:
        bad_frame = np.zeros((100, 100, 3), dtype=np.uint8)
        frames = [
            bad_frame,
            self.frames["LAUNCH_READY"],
            self.frames["LAUNCH_READY"],
        ]
        supervisor, clock, _, sender, _ = self.make_supervisor(frames)
        supervisor.step()
        clock.advance(0.6)
        supervisor.step()
        self.assertEqual(supervisor.runtime_state, "CAPTURE_PAUSED")

        supervisor.step()
        supervisor.step()
        self.assertEqual(supervisor.observed_state, "LAUNCH_READY")
        self.assertIn(("key", 18, 80), sender.actions)

    def test_input_error_pauses_without_ending_session(self) -> None:
        supervisor, clock, _, sender, recorder = self.make_supervisor(
            [self.frames["CONTINUE"]]
        )

        def fail_click(x: int, y: int) -> None:
            raise OSError(f"blocked click {x},{y}")

        sender.click = fail_click  # type: ignore[method-assign]
        self.acquire_and_detect(supervisor, clock, "CONTINUE")

        self.assertEqual(supervisor.runtime_state, "ACTION_PAUSED")
        self.assertEqual(supervisor.observed_state, "CONTINUE")
        self.assertIn("ACTION_ERROR", [event for event, _ in recorder.events])
        self.assertFalse(any(event == "RUN_FINISHED" for event, _ in recorder.events))

    def test_progress_watchdog_pauses_but_keeps_observing(self) -> None:
        supervisor, clock, _, _, recorder = self.make_supervisor(
            [self.frames["LEGEND_SELECT"]]
        )
        self.acquire_and_detect(supervisor, clock, "LEGEND_SELECT")
        clock.advance(self.config.timing["dropshipTimeoutMs"] / 1000 + 1)
        supervisor.step()

        self.assertEqual(supervisor.runtime_state, "STALLED_PAUSED")
        self.assertEqual(supervisor.observed_state, "LEGEND_SELECT")
        self.assertIn("PROGRESS_STALLED", [event for event, _ in recorder.events])

    def test_status_files_are_atomic_and_events_are_mirrored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            recorder = RunRecorder(Path(directory), "test-profile")
            recorder.write_status({"runtimeState": "OBSERVING"})
            recorder.finish("STOPPED")

            run_status = json.loads(recorder.status_path.read_text(encoding="utf-8"))
            current_status = json.loads(recorder.current_status_path.read_text(encoding="utf-8"))
            self.assertEqual(run_status, current_status)
            self.assertEqual(run_status["runtimeState"], "OBSERVING")
            self.assertEqual(
                recorder.actions_path.read_text(encoding="utf-8"),
                recorder.events_path.read_text(encoding="utf-8"),
            )


if __name__ == "__main__":
    unittest.main()

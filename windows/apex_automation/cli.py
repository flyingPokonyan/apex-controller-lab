from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time

from .capture import DxcamFrameSource
from .config import (
    DEFAULT_CONFIG_PATH,
    REPOSITORY_ROOT,
    TUTORIAL_CONFIG_PATH,
    RunnerConfig,
    load_config,
)
from .control import TaskControl, TaskStopRequested
from .control_server import LocalControlServer
from .input_win32 import EmergencyStop, Win32InputSender, Win32SafetyGuard
from .observer import ObservationSession
from .ocr_obstacles import OcrObstacleDetector, RapidOcrProvider
from .ocr_states import OcrStateDetector
from .recorder import RunRecorder
from .runner import ScenarioRunner
from .supervisor import TaskSupervisor
from .vision import VisionDetector, load_frame


def _repository_path(value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _build_obstacle_detector(config: RunnerConfig) -> OcrObstacleDetector | None:
    ocr = config.ocr
    if not bool(ocr.get("enabled", False)):
        return None
    rules = ocr.get("rules")
    if not rules:
        raise ValueError("OCR 已启用，但没有配置外置规则字典")
    return OcrObstacleDetector.from_path(RapidOcrProvider(), _repository_path(rules))


def _build_guided_detector(config: RunnerConfig) -> OcrStateDetector | None:
    guided = config.guided_ocr
    if not bool(guided.get("enabled", False)):
        return None
    rules = guided.get("rules")
    if not rules:
        raise ValueError("教程 OCR 已启用，但没有配置状态字典")
    return OcrStateDetector.from_path(RapidOcrProvider(), _repository_path(rules))


def validate(config_path: Path) -> int:
    config = load_config(config_path)
    detector = VisionDetector(config)
    failures = 0
    # Raw full-screen frames are deliberately not published with the source, so
    # a fresh clone is missing every sample. That is not a recognition failure
    # and must not be reported as one, or a first run looks broken.
    missing = 0
    print(f"配置：{config.profile}")
    print("离线图片识别：")

    for image_path, expected in config.offline_cases:
        if not image_path.exists():
            print(f"  SKIP {image_path.name}: 样本未随源码分发")
            missing += 1
            continue
        frame = load_frame(image_path)
        detector.validate_frame(frame)
        ranking = detector.rank_states(frame)
        best = ranking[0]
        passed = best.state == expected and best.matched
        status = "PASS" if passed else "FAIL"
        print(f"  {status} {image_path.name}: {best.state} {best.score:.3f}（期望 {expected}）")
        if not passed:
            failures += 1

    try:
        obstacle_detector = _build_obstacle_detector(config)
        if obstacle_detector is not None:
            if not config.offline_cases or not obstacle_detector.probe_regions:
                raise ValueError("缺少 OCR 验证图片或 probeRegions")
            frame = load_frame(config.offline_cases[0][0])
            region_name = obstacle_detector.probe_regions[0]
            tokens = obstacle_detector.provider.read(
                frame,
                obstacle_detector.regions[region_name],
            )
            print(f"  PASS OCR 引擎：{region_name} 返回 {len(tokens)} 个文本块")
    except Exception as error:
        print(f"  FAIL OCR 引擎：{error}")
        failures += 1

    try:
        guided_detector = _build_guided_detector(config)
        if guided_detector is not None:
            if not config.guided_offline_cases:
                raise ValueError("缺少 guidedOfflineCases")
            for image_path, expected in config.guided_offline_cases:
                if not image_path.exists():
                    print(f"  SKIP OCR {image_path.name}: 样本未随源码分发")
                    missing += 1
                    continue
                analysis = guided_detector.analyze(load_frame(image_path))
                actual = None if analysis.decision is None else analysis.decision.state
                passed = analysis.error is None and actual == expected
                # An engine that failed to load reads as UNKNOWN for every
                # image otherwise, which hides the one line that explains it.
                detail = analysis.error or f"{actual or 'UNKNOWN'}（期望 {expected}）"
                print(f"  {'PASS' if passed else 'FAIL'} OCR {image_path.name}: {detail}")
                if not passed:
                    failures += 1
    except Exception as error:
        print(f"  FAIL 教程 OCR：{error}")
        failures += 1

    if missing:
        print(f"跳过 {missing} 张：源码仓库不含原始全屏样本，发布包才有。")
    if failures:
        print(f"离线验证失败：{failures} 张")
        return 1
    if missing and not any(path.exists() for path, _ in config.offline_cases):
        print("没有可验证的样本；本次没有验证任何东西。")
        return 0
    print("离线验证通过；本步骤没有发送任何系统输入。")
    return 0


def run_live(config_path: Path, countdown: int) -> int:
    if sys.platform != "win32":
        print("live 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2

    config = load_config(config_path)
    detector = VisionDetector(config)
    guard = Win32SafetyGuard(config.environment["foregroundExecutables"])
    sender = Win32InputSender(
        guard,
        int(config.environment["width"]),
        int(config.environment["height"]),
    )
    recorder = RunRecorder(REPOSITORY_ROOT / "windows" / "runs", config.profile)

    print("即将启动真实输入。请在倒计时内 Alt+Tab 回 Apex；任何时候按 F8 紧急停止。")
    for remaining in range(countdown, 0, -1):
        print(f"  {remaining}...")
        time.sleep(1)

    try:
        with DxcamFrameSource(
            backend=str(config.environment["captureBackend"]),
            output_index=int(config.environment["outputIndex"]),
        ) as source:
            ScenarioRunner(config, detector, source, sender, guard, recorder, notify=print).run()
    except EmergencyStop as error:
        print(str(error), file=sys.stderr)
        return 130
    except Exception as error:
        print(f"运行失败：{error}", file=sys.stderr)
        print(f"证据目录：{recorder.run_dir}", file=sys.stderr)
        return 1

    print(f"运行完成，截图和日志位于：{recorder.run_dir}")
    return 0


def run_observe(
    config_path: Path,
    *,
    ocr_interval_ms: int,
    snapshot_interval_ms: int,
    duration_s: float | None,
) -> int:
    if sys.platform != "win32":
        print("observe 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2

    config = load_config(config_path)
    detector = VisionDetector(config)
    guard = Win32SafetyGuard(config.environment["foregroundExecutables"])
    recorder = RunRecorder(REPOSITORY_ROOT / "windows" / "runs", f"{config.profile}.observe")
    guided_detector = _build_guided_detector(config)
    obstacle_detector = _build_obstacle_detector(config)

    print("观察模式：只截图和识别，不会发送任何键盘或鼠标输入。")
    print("请手动操作 Apex，按 F8 或 Ctrl+C 结束。")
    session: ObservationSession | None = None

    try:
        # No Win32InputSender is constructed anywhere in this command, so no
        # code path can reach SendInput while an observation runs.
        with DxcamFrameSource(
            backend=str(config.environment["captureBackend"]),
            output_index=int(config.environment["outputIndex"]),
        ) as source:
            session = ObservationSession(
                config,
                detector,
                source,
                recorder,
                guard=guard,
                guided_detector=guided_detector,
                obstacle_detector=obstacle_detector,
                notify=print,
                ocr_interval_ms=ocr_interval_ms,
                snapshot_interval_ms=snapshot_interval_ms,
            )
            print(f"逐帧记录：{session.observations_path}")
            session.run(duration_s=duration_s)
    except (EmergencyStop, KeyboardInterrupt):
        print("\n已结束观察。")
    except Exception as error:
        print(f"观察中断：{error}", file=sys.stderr)
        recorder.finish("FAILED", error=str(error))
        if session is not None:
            session.write_summary()
            print(f"已记录 {session.observation_count} 帧：{recorder.run_dir}", file=sys.stderr)
        return 1

    if session is None:
        return 1
    summary_path = session.write_summary()
    recorder.finish("OBSERVED", observations=session.observation_count)
    print(f"共记录 {session.observation_count} 帧，{session.screenshot_count} 张截图。")
    print(f"统计摘要：{summary_path}")
    print(f"请把整个目录打包回传：{recorder.run_dir}")
    return 0


def run_start(
    config_path: Path,
    mode: str,
    *,
    arm_safe_obstacles: bool = False,
    no_control_api: bool = False,
    control_port: int | None = None,
) -> int:
    if sys.platform != "win32":
        print("start 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2

    if mode == "tutorial" and config_path.expanduser().resolve() == DEFAULT_CONFIG_PATH.resolve():
        config_path = TUTORIAL_CONFIG_PATH
    config = load_config(config_path)
    arm_safe_obstacles = arm_safe_obstacles or bool(config.ocr.get("autoArm", False))
    detector = VisionDetector(config)
    guard = Win32SafetyGuard(config.environment["foregroundExecutables"])
    sender = Win32InputSender(
        guard,
        int(config.environment["width"]),
        int(config.environment["height"]),
    )
    recorder = RunRecorder(REPOSITORY_ROOT / "windows" / "runs", config.profile)
    obstacle_detector = _build_obstacle_detector(config)
    guided_detector = _build_guided_detector(config)
    control = TaskControl()
    control_server: LocalControlServer | None = None
    control_url: str | None = None
    supervisor: TaskSupervisor | None = None

    api_config = config.control_api
    if bool(api_config.get("enabled", True)) and not no_control_api:
        host = str(api_config.get("bind", "127.0.0.1"))
        port = int(api_config.get("port", 8765) if control_port is None else control_port)
        try:
            control_server = LocalControlServer(host, port, recorder, control)
            control_server.start()
            control_url = control_server.url
            recorder.log("CONTROL_API_STARTED", url=control_url)
        except Exception as error:
            control_server = None
            recorder.log("CONTROL_API_FAILED", error=str(error))
            print(f"本地控制台启动失败，任务仍会继续：{error}", file=sys.stderr)

    print(f"已创建常驻任务：{mode}。无需倒计时；Apex 不在前台时只会暂停。")
    print("切回 Apex 后会从当前已知画面接管；任何时候按 F8 紧急停止。")
    print(f"状态文件：{recorder.status_path}")
    print(f"事件文件：{recorder.events_path}")
    if control_url:
        print(f"本地控制台：{control_url}")
    print(
        "OCR 安全清障："
        + ("已武装（仅外置字典中的安全键盘动作）" if arm_safe_obstacles else "仅识别留证，不发送清障输入")
    )

    try:
        with DxcamFrameSource(
            backend=str(config.environment["captureBackend"]),
            output_index=int(config.environment["outputIndex"]),
        ) as source:
            supervisor = TaskSupervisor(
                config,
                detector,
                source,
                sender,
                guard,
                recorder,
                mode=mode,
                ai_fallback=False,
                notify=print,
                obstacle_detector=obstacle_detector,
                guided_detector=guided_detector,
                safe_obstacles_armed=arm_safe_obstacles,
                control=control,
                control_api_url=control_url,
            )
            supervisor.run()
    except EmergencyStop as error:
        if supervisor is not None:
            supervisor.stop("ABORTED", str(error))
        else:
            sender.release_all()
        recorder.finish("ABORTED", error=str(error))
        print(str(error), file=sys.stderr)
        print(f"证据目录：{recorder.run_dir}", file=sys.stderr)
        return 130
    except TaskStopRequested as error:
        reason = str(error)
        if supervisor is not None:
            supervisor.stop("STOPPED", reason)
        else:
            sender.release_all()
        recorder.finish("STOPPED", reason=reason)
        print(f"{reason}。证据目录：{recorder.run_dir}")
        return 0
    except KeyboardInterrupt:
        reason = "用户通过 Ctrl+C 停止任务"
        if supervisor is not None:
            supervisor.stop("STOPPED", reason)
        else:
            sender.release_all()
        recorder.finish("STOPPED", reason=reason)
        print(f"\n{reason}。证据目录：{recorder.run_dir}")
        return 0
    except Exception as error:
        if supervisor is not None:
            supervisor.stop("FAILED", str(error))
        else:
            sender.release_all()
        recorder.finish("FAILED", error=str(error))
        print(f"运行失败：{error}", file=sys.stderr)
        print(f"证据目录：{recorder.run_dir}", file=sys.stderr)
        return 1
    finally:
        if control_server is not None:
            control_server.stop()

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apex 首场匹配截图门控运行器")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG_PATH)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("validate", help="只对保存的 PNG 做离线识别，不发送输入")
    live = subparsers.add_parser("live", help="在 Windows 上捕获屏幕并发送真实输入")
    live.add_argument("--countdown", type=int, default=5)
    observe = subparsers.add_parser(
        "observe",
        help="真实截屏并记录全部识别得分，绝不发送输入",
    )
    observe.add_argument("--ocr-interval-ms", type=int, default=1500)
    observe.add_argument("--snapshot-interval-ms", type=int, default=15000)
    observe.add_argument("--duration-s", type=float, help="到时自动结束，默认一直观察")
    start = subparsers.add_parser("start", help="启动可恢复的常驻任务监督器")
    start.add_argument("--mode", choices=("match", "tutorial"), default="match")
    start.add_argument(
        "--arm-safe-obstacles",
        action="store_true",
        help="允许外置 OCR 安全规则发送键盘清障动作",
    )
    start.add_argument("--no-control-api", action="store_true", help="关闭本地状态控制台")
    start.add_argument("--control-port", type=int, help="覆盖本地状态控制台端口")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "validate"
    if command == "validate":
        return validate(args.config)
    if command == "live":
        return run_live(args.config, max(1, args.countdown))
    if command == "observe":
        return run_observe(
            args.config,
            ocr_interval_ms=max(200, args.ocr_interval_ms),
            snapshot_interval_ms=max(1000, args.snapshot_interval_ms),
            duration_s=args.duration_s,
        )
    if command == "start":
        return run_start(
            args.config,
            args.mode,
            arm_safe_obstacles=args.arm_safe_obstacles,
            no_control_api=args.no_control_api,
            control_port=args.control_port,
        )
    parser.error(f"未知命令：{command}")
    return 2

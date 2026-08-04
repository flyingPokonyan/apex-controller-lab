from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import sys
import time
from typing import Callable
import uuid

import json

from . import __version__
from .account_orchestrator import AccountCycleOutcome, AccountOrchestrator
from .account_provider import (
    DEFAULT_TASK_TYPE,
    AccountLease,
    AccountProvider,
    CleanupEvidence,
    HttpAccountProvider,
    LeaseProviderError,
    OtpCode,
)
from .capabilities import CapabilityDispatcher, CapabilitySet
from .capture import DxcamFrameSource
from .config import (
    DEFAULT_CONFIG_PATH,
    PLAY_CONFIG_PATH,
    REPOSITORY_ROOT,
    TUTORIAL_CONFIG_PATH,
    RunnerConfig,
    load_config,
)
from .control import TaskControl, TaskStopRequested
from .control_server import LocalControlServer
from .ea_app import EaAppAutomationError, EaAppDriver, EaUiState, OtpChallenge
from .ea_app_win32 import WindowsEaHybridDriver
from .ea_evidence import EaLoginEvidence, default_evidence_root
from .ea_pages import mask_identity
from .input_win32 import EmergencyStop, Win32InputSender, Win32SafetyGuard
from .instance_lock import AlreadyRunningError, SingleInstanceLock
from .lease_keeper import LeaseKeeper
from .observer import ObservationSession
from .ocr_obstacles import FullFrameOcrProvider, OcrObstacleDetector, RapidOcrProvider
from .ocr_states import OcrStateDetector
from .pilot import producible_states
from .play_session import PlaySessionRunner, SessionIdentity
from .progression_policy import ContinuePlayPolicy
from .recorder import RunRecorder
from .orchestration_state import AtomicCheckpointStore
from .runner import ScenarioRunner
from .runner_identity import (
    RunnerConfigurationError,
    RunnerIdentityMismatch,
    RunnerSettings,
    load_runner_settings,
    require_identity_match,
    verify_runner_identity,
)
from .supervisor import TaskSupervisor
from .vision import VisionDetector, load_frame


def _repository_path(value: object) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else REPOSITORY_ROOT / path


def _config_revision(config: RunnerConfig) -> str:
    digest = hashlib.sha256()
    candidates = [
        config.path,
        _repository_path(config.game_states["rules"])
        if config.game_states.get("rules")
        else None,
        _repository_path(config.capability_set) if config.capability_set else None,
        _repository_path(config.overlay_ocr["rules"])
        if config.overlay_ocr.get("rules")
        else None,
        _repository_path(config.overlay_ocr["safetyRules"])
        if config.overlay_ocr.get("safetyRules")
        else None,
    ]
    for path in candidates:
        if path is None:
            continue
        digest.update(str(path.name).encode("utf-8"))
        digest.update(path.read_bytes())
    return f"sha256:{digest.hexdigest()}"


def _build_obstacle_detector(
    config: RunnerConfig,
    provider: RapidOcrProvider | None = None,
) -> OcrObstacleDetector | None:
    ocr = config.ocr
    if not bool(ocr.get("enabled", False)):
        return None
    rules = ocr.get("rules")
    if not rules:
        raise ValueError("OCR 已启用，但没有配置外置规则字典")
    return OcrObstacleDetector.from_path(provider or RapidOcrProvider(), _repository_path(rules))


def _build_guided_detector(
    config: RunnerConfig,
    provider: RapidOcrProvider | None = None,
) -> OcrStateDetector | None:
    guided = config.guided_ocr
    if not bool(guided.get("enabled", False)):
        return None
    rules = guided.get("rules")
    if not rules:
        raise ValueError("教程 OCR 已启用，但没有配置状态字典")
    return OcrStateDetector.from_path(provider or RapidOcrProvider(), _repository_path(rules))


def _build_overlay_detector(
    config: RunnerConfig,
    provider: RapidOcrProvider | None = None,
) -> OcrStateDetector | None:
    overlay = config.overlay_ocr
    if not bool(overlay.get("enabled", False)):
        return None
    rules = overlay.get("rules")
    if not rules:
        raise ValueError("覆盖层 OCR 已启用，但没有配置状态字典")
    return OcrStateDetector.from_path(
        FullFrameOcrProvider(provider or RapidOcrProvider()),
        _repository_path(rules),
    )


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
    with_ocr: bool = False,
) -> int:
    if sys.platform != "win32":
        print("observe 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2

    config = load_config(config_path)
    detector = VisionDetector(config)
    guard = Win32SafetyGuard(config.environment["foregroundExecutables"])
    recorder = RunRecorder(REPOSITORY_ROOT / "windows" / "runs", f"{config.profile}.observe")
    # Whole-frame OCR measured 2.1-6.8s per pass on the target machine, which
    # is longer than the poll interval: a session spent 7.7s between frames
    # and stepped straight over the dropship and the post-match screens.
    # Collecting frames does not need OCR, so it is off unless asked for.
    guided_detector = _build_guided_detector(config) if with_ocr else None
    obstacle_detector = _build_obstacle_detector(config) if with_ocr else None

    state_detector: OcrStateDetector | None = None
    dispatcher: CapabilityDispatcher | None = None
    if bool(config.game_states.get("enabled", False)):
        state_detector = OcrStateDetector.from_path(
            RapidOcrProvider(),
            _repository_path(config.game_states["rules"]),
        )
        if config.capability_set:
            payload = json.loads(
                _repository_path(config.capability_set).read_text(encoding="utf-8")
            )
            dispatcher = CapabilityDispatcher(CapabilitySet.from_payload(payload))

    print("观察模式：只截图和识别，不会发送任何键盘或鼠标输入。")
    print("请手动操作 Apex，按 F8 或 Ctrl+C 结束。")
    print("OCR：" + ("已开启（会明显拖慢采样）" if with_ocr else "已关闭，采样保持约 300ms 一帧"))
    if dispatcher is not None:
        print("决策试跑：已开启。会打印「本来会执行什么」，但没有任何东西在另一端接收。")
    elif state_detector is not None:
        print("状态识别：已开启（无能力集，只记录画面语义）。")
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
                state_detector=state_detector,
                dispatcher=dispatcher,
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


def run_probe_input(config_path: Path, *, countdown: int) -> int:
    """Ask the one question no screenshot can answer: does the game take input?

    Every capability failure so far looks the same from the outside — the
    action is sent, the screen does not change. That has two very different
    causes: the key is not the one the game listens for, or the game does not
    accept synthetic input at all during play. Menu input is known to work
    (a click queues a match, ESC closed the memorial page), and nothing has
    ever been confirmed inside a match. These steps separate the two, and
    only a human watching the screen can read the answer.
    """
    if sys.platform != "win32":
        print("probe-input 只能在 Windows 上运行。", file=sys.stderr)
        return 2

    if config_path.expanduser().resolve() == DEFAULT_CONFIG_PATH.resolve():
        config_path = PLAY_CONFIG_PATH
    config = load_config(config_path)
    guard = Win32SafetyGuard(config.environment["foregroundExecutables"])
    sender = Win32InputSender(
        guard,
        int(config.environment["width"]),
        int(config.environment["height"]),
    )

    print("输入探针：会向 Apex 发送几个明显可见的动作，你只需要盯着屏幕看有没有反应。")
    print("请先站在训练场里（空地上、不要对着墙），然后切回 Apex。")
    print("按 F8 可以随时中止。")
    for remaining in range(max(0, countdown), 0, -1):
        print(f"{remaining}...")
        time.sleep(1)

    # Movement first: it is the least ambiguous thing on screen. If the
    # character does not move, no keyboard question below it matters.
    steps: list[tuple[str, str, Callable[[], None]]] = [
        ("W 前进 1 秒", "角色应该向前走一段", lambda: sender.hold_scan_codes([17], 1000)),
        ("A 左移 1 秒", "角色应该向左横移", lambda: sender.hold_scan_codes([30], 1000)),
        ("V 近战 ×3", "角色应该挥手 / 挥近战武器 3 次", lambda: _tap_times(sender, 47, 3)),
        ("LCTRL ×1", "角色应该蹲下或站起", lambda: _tap_times(sender, 29, 1)),
        ("鼠标右转", "视角应该明显向右转", lambda: sender.move_mouse(600, 0)),
        ("鼠标左键 ×1", "应该开一枪或挥一下", lambda: sender.hold_mouse_button("left", 120)),
    ]

    try:
        for index, (label, expected, action) in enumerate(steps, start=1):
            guard.ensure_not_aborted()
            guard.ensure_target_foreground()
            print(f"\n[{index}/{len(steps)}] {label} —— {expected}")
            action()
            time.sleep(1.5)
    except EmergencyStop as error:
        sender.release_all()
        print(f"\n已中止：{error}")
        return 0
    except KeyboardInterrupt:
        sender.release_all()
        print("\n已中止。")
        return 0
    finally:
        sender.release_all()

    print("\n请回答：上面 6 步里，哪几步在游戏里真的发生了？")
    print("  全都没有  → 这个进程发出的输入根本没进游戏")
    print("  只有鼠标有 → 键盘被挡住了，或者按键绑定不是默认的")
    print("  W/A 有、V 没有 → 键盘通的，是近战没绑在 V 上")
    return 0


def _tap_times(sender: Win32InputSender, scan_code: int, times: int) -> None:
    for _ in range(times):
        sender.tap_scan_code(scan_code, 80)
        time.sleep(0.35)


def _build_play_session_runner(
    config: RunnerConfig,
    settings: RunnerSettings,
    runs_root: Path,
) -> tuple[PlaySessionRunner, CapabilitySet]:
    if not bool(config.game_states.get("enabled", False)):
        raise ValueError("这个画像没有开启 gameStates，能力集无从判断画面")
    if not config.capability_set:
        raise ValueError("这个画像没有配置 capabilitySet，没有任何能力可以执行")

    payload = json.loads(
        _repository_path(config.capability_set).read_text(encoding="utf-8")
    )
    capabilities = CapabilitySet.from_payload(payload)
    ocr_provider = RapidOcrProvider()
    state_detector = OcrStateDetector.from_path(
        ocr_provider, _repository_path(config.game_states["rules"])
    )
    overlay_detector = _build_overlay_detector(config, ocr_provider)
    safety_rules = config.overlay_ocr.get("safetyRules")
    safety_detector = (
        None
        if not safety_rules
        else OcrObstacleDetector.from_path(
            ocr_provider,
            _repository_path(safety_rules),
        )
    )

    known_states = producible_states(state_detector, overlay_detector)
    unknown = {
        state
        for capability in capabilities.capabilities
        for state in capability.states
        if state not in known_states
    }
    if unknown:
        raise ValueError(f"能力集引用了识别不出的画面：{sorted(unknown)}")

    guard_states = {
        str(value) for value in config.overlay_ocr.get("guardStates", [])
    }
    invalid_guards = guard_states - state_detector.states
    if invalid_guards:
        raise ValueError(
            f"覆盖层守卫引用了快速识别不存在的画面：{sorted(invalid_guards)}"
        )
    if overlay_detector is not None:
        handled_states = {
            state
            for capability in capabilities.capabilities
            for state in capability.states
        }
        manual_states = {
            str(value) for value in config.overlay_ocr.get("manualStates", [])
        }
        invalid_manual_states = manual_states - overlay_detector.states
        if invalid_manual_states:
            raise ValueError(
                f"人工覆盖层状态不在识别字典里：{sorted(invalid_manual_states)}"
            )
        unhandled = overlay_detector.states - handled_states - manual_states
        if unhandled:
            raise ValueError(
                f"覆盖层状态既没有能力也未声明人工处理：{sorted(unhandled)}"
            )
        unknown_only_states = {
            str(value)
            for value in config.overlay_ocr.get("unknownOnlyStates", [])
        }
        invalid_unknown_only = unknown_only_states - overlay_detector.states
        if invalid_unknown_only:
            raise ValueError(
                "仅未知画面可用的覆盖层状态不在识别字典里："
                f"{sorted(invalid_unknown_only)}"
            )

    if safety_detector is not None:
        if overlay_detector is None:
            raise ValueError("配置了覆盖层安全字典，但没有启用覆盖层识别")
        overlay_rules = {
            rule.state: rule for rule in overlay_detector.rules if rule.enabled
        }
        for rule in safety_detector.rules:
            if not rule.enabled:
                continue
            overlay_rule = overlay_rules.get(rule.state)
            if overlay_rule is None:
                raise ValueError(f"安全清障状态没有接入覆盖层识别：{rule.state}")
            safety_requirements = [
                (item.region, item.any_terms, item.all_terms, item.min_confidence)
                for item in rule.requirements
            ]
            overlay_requirements = [
                (item.region, item.any_terms, item.all_terms, item.min_confidence)
                for item in overlay_rule.requirements
            ]
            if overlay_requirements != safety_requirements:
                raise ValueError(
                    f"覆盖层规则与安全清障字典不一致：{rule.state}"
                )
            matches = [
                capability
                for capability in capabilities.for_state(rule.state)
                if capability.action == rule.action.name
                and capability.kind == rule.action.kind
                and capability.confirm_ms == rule.action.confirm_ms
                and capability.max_attempts == rule.action.max_attempts
            ]
            if not matches:
                raise ValueError(
                    f"清障规则 {rule.rule_id} 与能力集动作不一致：{rule.state}"
                )

    guard = Win32SafetyGuard(config.environment["foregroundExecutables"])
    sender = Win32InputSender(
        guard,
        int(config.environment["width"]),
        int(config.environment["height"]),
    )
    return (
        PlaySessionRunner(
            config=config,
            capabilities=capabilities,
            actions=dict(payload.get("actions", {})),
            state_detector=state_detector,
            overlay_detector=overlay_detector,
            ocr_provider=ocr_provider,
            settings=settings,
            sender=sender,
            guard=guard,
            runs_root=runs_root,
            app_version=__version__,
            config_revision=_config_revision(config),
            notify=print,
        ),
        capabilities,
    )


def run_play(
    config_path: Path,
    *,
    countdown: int,
    duration_s: float | None,
    runner_config_path: Path | None = None,
) -> int:
    if sys.platform != "win32":
        print("play 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2

    # The default profile is the first-match one, which has neither a state
    # dictionary nor a capability set. Falling back to the play profile keeps
    # `python -m apex_automation play` working the same as `play.cmd`.
    if config_path.expanduser().resolve() == DEFAULT_CONFIG_PATH.resolve():
        config_path = PLAY_CONFIG_PATH
    config = load_config(config_path)

    try:
        settings = load_runner_settings(
            explicit_path=runner_config_path,
            default_path=REPOSITORY_ROOT / "windows" / "runner.private.json",
        )
        verification = verify_runner_identity(settings)
        require_identity_match(verification)
    except (RunnerConfigurationError, RunnerIdentityMismatch) as error:
        print(f"Runner 账号配置错误：{error}", file=sys.stderr)
        return 2

    runs_root = REPOSITORY_ROOT / "windows" / "runs"
    lock = SingleInstanceLock(runs_root / "play.lock")
    try:
        lock.acquire()
    except AlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        return 2

    try:
        session_runner, capabilities = _build_play_session_runner(
            config,
            settings,
            runs_root,
        )

        print("自动游玩：会真实发送键盘和鼠标输入。")
        print(f"能力：{', '.join(item.id for item in capabilities.capabilities)}")
        if settings.account_id:
            print(
                f"运行账号：{settings.display_account}"
                f"（{settings.account_id}，身份 {verification.status}）"
            )
        else:
            print("运行账号：未绑定；本次只保留本地记录，不按账号远程归档。")
        if verification.status in {"CONFIGURED", "UNAVAILABLE"}:
            print(f"账号核验提示：{verification.message}")
        print(
            "远程上报："
            + ("已启用，断网时会本地排队补传。" if settings.enabled else "未启用。")
        )
        print("随时按 F8 或 Ctrl+C 立即停止；把 Apex 切到后台也会暂停。")

        identity = SessionIdentity.from_runner_settings(settings, verification)
        with DxcamFrameSource(
            backend=str(config.environment["captureBackend"]),
            output_index=int(config.environment["outputIndex"]),
        ) as source:
            result = session_runner.run(
                identity,
                ContinuePlayPolicy(),
                source,
                duration_s=duration_s,
                countdown=countdown,
            )
        print(f"共 {result.frames} 帧，发出 {result.actions_sent} 次输入。")
        if result.run_dir is not None:
            summary_path = result.run_dir / "pilot-summary.json"
            if summary_path.exists():
                print(f"统计摘要：{summary_path}")
            print(f"证据目录：{result.run_dir}")
        return result.exit_code
    except Exception as error:
        print(f"play 启动失败：{error}", file=sys.stderr)
        return 1
    finally:
        try:
            lock.release()
        except Exception as error:
            print(f"运行锁释放失败：{error}", file=sys.stderr)


def run_account_cycle(
    config_path: Path,
    *,
    runner_config_path: Path | None = None,
    idle_s: float = 30.0,
    once: bool = False,
    resume: bool = False,
    provider: AccountProvider | None = None,
    ea_driver: EaAppDriver | None = None,
    play_session: PlaySessionRunner | None = None,
) -> int:
    if sys.platform != "win32":
        print("account-cycle 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2

    if config_path.expanduser().resolve() == DEFAULT_CONFIG_PATH.resolve():
        config_path = PLAY_CONFIG_PATH
    try:
        config = load_config(config_path)
        settings = load_runner_settings(
            explicit_path=runner_config_path,
            default_path=REPOSITORY_ROOT
            / "windows"
            / "account-cycle.private.json",
            managed=True,
        )
        if not settings.enabled or settings.device_id is None:
            raise RunnerConfigurationError("托管模式必须启用设备级远程上报")
        if provider is None:
            if settings.lease_url is None or settings.provider_token is None:
                raise RunnerConfigurationError(
                    "托管模式必须配置 leaseUrl/providerToken"
                )
            provider = HttpAccountProvider(
                settings.lease_url,
                settings.provider_token,
                client_version=__version__,
            )
        runs_root = REPOSITORY_ROOT / "windows" / "runs"
        if play_session is None:
            play_session, _ = _build_play_session_runner(
                config,
                settings,
                runs_root,
            )
    except (OSError, ValueError, RunnerConfigurationError, LeaseProviderError) as error:
        print(f"account-cycle 配置错误：{error}", file=sys.stderr)
        return 2

    lock = SingleInstanceLock(runs_root / "play.lock")
    try:
        lock.acquire()
    except AlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        return 2

    orchestrator: AccountOrchestrator | None = None
    try:
        with DxcamFrameSource(
            backend=str(config.environment["captureBackend"]),
            output_index=int(config.environment["outputIndex"]),
        ) as source:
            if ea_driver is None:
                # The managed loop moves on to the next account as soon as it
                # cleans up, so a login failure has to leave its own frames
                # behind or the run that hit it is unreviewable.
                evidence = EaLoginEvidence(default_evidence_root(runs_root))
                evidence.prune()
                ea_driver = WindowsEaHybridDriver(
                    capture_source=source,
                    evidence=evidence,
                    notify=print,
                )
            preflight = getattr(ea_driver, "preflight", None)
            if callable(preflight):
                state = preflight()
                print(f"EA 领号前预检通过：{state.value}")
            orchestrator = AccountOrchestrator(
                provider=provider,
                ea_driver=ea_driver,
                play_session=play_session,
                checkpoint_store=AtomicCheckpointStore(
                    runs_root / "account-cycle-status.json"
                ),
                device_id=settings.device_id,
                capture_source=source,
                recover_report_drain=play_session.recover_report_drain,
                notify=print,
            )
            if resume:
                orchestrator.resume()
            if once:
                # One account, one attempt, then stop. Verifying a stage
                # against the loop means every failure immediately claims the
                # next real account and asks the same unanswered question.
                result = orchestrator.run_once()
                details = [f"结果={result.outcome.value}"]
                if result.lease_id:
                    details.append(f"租约={result.lease_id}")
                if result.run_id:
                    details.append(f"runId={result.run_id}")
                if result.error_code:
                    details.append(f"原因={result.error_code}")
                print("单次托管运行结束：" + "，".join(details))
                if result.outcome is AccountCycleOutcome.PAUSED:
                    print(
                        "本地仍处于暂停状态。确认暂停原因已解决后，"
                        "用 account-cycle-resume.cmd（或加 --resume）继续。",
                        file=sys.stderr,
                    )
                    return 1
                return 0
            return orchestrator.run_forever(idle_s=max(1.0, idle_s))
    except (EmergencyStop, KeyboardInterrupt):
        return 0
    except Exception as error:
        print(f"account-cycle 启动失败：{error}", file=sys.stderr)
        return 1
    finally:
        if orchestrator is not None:
            try:
                orchestrator.stop()
            except Exception as error:
                print(f"账号编排停止失败：{error}", file=sys.stderr)
        try:
            lock.release()
        except Exception as error:
            print(f"运行锁释放失败：{error}", file=sys.stderr)


def run_account_cycle_check(*, runner_config_path: Path | None = None) -> int:
    """Validate managed network and Provider auth without claiming an account."""
    try:
        settings = load_runner_settings(
            explicit_path=runner_config_path,
            default_path=REPOSITORY_ROOT / "windows" / "account-cycle.private.json",
            managed=True,
        )
        if settings.lease_url is None or settings.provider_token is None:
            raise RunnerConfigurationError("托管模式必须配置 leaseUrl/providerToken")
        provider = HttpAccountProvider(
            settings.lease_url,
            settings.provider_token,
            client_version=__version__,
        )
        lease = provider.current()
    except (OSError, ValueError, RunnerConfigurationError, LeaseProviderError) as error:
        print(f"account-cycle-check 失败：{error}", file=sys.stderr)
        return 2
    if lease is None:
        print("Runner 配置与 Provider 鉴权正常；当前没有活动租约。")
    else:
        print(f"Runner 配置与 Provider 鉴权正常；发现活动租约 {lease.lease_id}。")
    return 0


def run_ea_login_check(
    config_path: Path,
    *,
    runner_config_path: Path | None = None,
) -> int:
    """Claim one lease, prove the EA login, close it again. No Apex.

    The managed loop claims the next account the moment a failure is cleaned
    up, so debugging login against `account-cycle` burns real accounts on the
    same question. This command answers it once and stops.
    """

    if sys.platform != "win32":
        print("ea-login-check 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2

    if config_path.expanduser().resolve() == DEFAULT_CONFIG_PATH.resolve():
        config_path = PLAY_CONFIG_PATH
    try:
        config = load_config(config_path)
        settings = load_runner_settings(
            explicit_path=runner_config_path,
            default_path=REPOSITORY_ROOT / "windows" / "account-cycle.private.json",
            managed=True,
        )
        if settings.lease_url is None or settings.provider_token is None:
            raise RunnerConfigurationError("托管模式必须配置 leaseUrl/providerToken")
        provider = HttpAccountProvider(
            settings.lease_url,
            settings.provider_token,
            client_version=__version__,
        )
    except (OSError, ValueError, RunnerConfigurationError, LeaseProviderError) as error:
        print(f"ea-login-check 配置错误：{error}", file=sys.stderr)
        return 2

    runs_root = REPOSITORY_ROOT / "windows" / "runs"
    lock = SingleInstanceLock(runs_root / "play.lock")
    try:
        lock.acquire()
    except AlreadyRunningError as error:
        print(str(error), file=sys.stderr)
        return 2

    evidence = EaLoginEvidence(default_evidence_root(runs_root))
    lease: AccountLease | None = None
    keeper: LeaseKeeper | None = None
    driver: WindowsEaHybridDriver | None = None
    reason_code = "OPERATOR_STOPPED"
    outcome = "RELEASED"
    exit_code = 0
    try:
        with DxcamFrameSource(
            backend=str(config.environment["captureBackend"]),
            output_index=int(config.environment["outputIndex"]),
        ) as source:
            driver = WindowsEaHybridDriver(
                capture_source=source,
                evidence=evidence,
                notify=print,
            )
            # Handing the lease back needs the driver, and the driver needs
            # the capture source, so the whole cleanup lives inside this
            # block. Releasing after the camera closed left a real account
            # leased to a process that had already exited.
            try:
                state = driver.preflight()
                print(f"EA 领号前预检通过：{state.value}")
                if state is EaUiState.SIGNED_IN:
                    # Signing in is the whole point: starting from a signed-in
                    # EA would report a pass without ever testing a login, and
                    # would spend a real lease doing it.
                    print(
                        "EA 当前已登录，登录检查不会真正测试登录。"
                        "请先在 EA App 手动登出，让它停在登录页后重跑。",
                        file=sys.stderr,
                    )
                    return 2

                # Never touch a lease this command did not open: it may belong
                # to a paused account-cycle checkpoint that still owns it.
                existing = provider.current()
                if existing is not None and not existing.terminal:
                    print(
                        f"设备已有活动租约 {existing.lease_id}"
                        f"（{existing.state.value}）。请先用 account-cycle 收口"
                        "或在运营页安全释放后再跑登录检查。",
                        file=sys.stderr,
                    )
                    return 2

                lease = provider.claim(uuid.uuid4().hex, DEFAULT_TASK_TYPE)
                if lease is None:
                    print("服务端当前没有可领取的账号，未执行登录。")
                    return 0
                print(f"已领取租约 {lease.lease_id}（fence={lease.lease_fence}）")
                keeper = LeaseKeeper(
                    provider,
                    lease,
                    phase=lambda: "EA_SIGNING_IN",
                    run_id=lambda: None,
                )
                keeper.start()

                expected = lease.expected_ea_account_id
                if not expected:
                    raise EaAppAutomationError("租约缺少可验证的 EA 稳定账号 ID")
                credentials = provider.credentials(
                    lease.lease_id,
                    lease.lease_fence,
                    uuid.uuid4().hex,
                )
                active = lease

                def supply_otp(challenge: OtpChallenge) -> OtpCode:
                    return provider.request_otp(
                        active.lease_id,
                        active.lease_fence,
                        uuid.uuid4().hex,
                        challenge.challenge_id,
                        challenge.started_at,
                    )

                driver.sign_in(credentials, supply_otp)
                identity = driver.verify_identity(expected)
                print(
                    "EA 登录检查通过："
                    f"identity={mask_identity(identity.ea_account_id)}, "
                    f"source={identity.source}"
                )
            except EaAppAutomationError as error:
                print(
                    f"EA 登录检查失败：{error.reason_code}：{error}",
                    file=sys.stderr,
                )
                reason_code = error.reason_code
                outcome = "FAILED"
                exit_code = 1
            except (LeaseProviderError, RunnerConfigurationError) as error:
                print(f"EA 登录检查中止：{error}", file=sys.stderr)
                # An OTP the Provider could not deliver is an OTP failure, not
                # an operator stopping the run: the server treats the two
                # differently when it decides on cooldowns.
                code = getattr(error, "code", "") or ""
                reason_code = "OTP_TIMEOUT" if code.startswith("OTP") else "OPERATOR_STOPPED"
                outcome = "FAILED"
                exit_code = 1
            except (EmergencyStop, KeyboardInterrupt):
                reason_code = "OPERATOR_STOPPED"
                outcome = "FAILED"
                exit_code = 1
            except Exception as error:
                print(
                    f"EA 登录检查异常：{type(error).__name__}：{error}",
                    file=sys.stderr,
                )
                reason_code = "EA_UI_UNKNOWN"
                outcome = "FAILED"
                exit_code = 1
            finally:
                if keeper is not None:
                    try:
                        keeper.stop()
                    except Exception as error:
                        print(f"租约续期线程停止失败：{error}", file=sys.stderr)
                if lease is not None and not _release_login_check_lease(
                    provider,
                    lease,
                    driver,
                    outcome=outcome,
                    reason_code=reason_code,
                ):
                    exit_code = max(exit_code, 1)
    except Exception as error:
        print(f"EA 登录检查启动失败：{type(error).__name__}：{error}", file=sys.stderr)
        exit_code = max(exit_code, 1)
    finally:
        try:
            evidence.prune()
        except OSError as error:
            print(f"清理旧证据失败：{error}", file=sys.stderr)
        print(f"证据目录：{evidence.dir}")
        try:
            lock.release()
        except Exception as error:
            print(f"运行锁释放失败：{error}", file=sys.stderr)
    return exit_code


def _release_login_check_lease(
    provider: AccountProvider,
    lease: AccountLease,
    driver: WindowsEaHybridDriver | None,
    *,
    outcome: str,
    reason_code: str,
) -> bool:
    """Always hand the account back, and say so when that is impossible."""

    apex_exited = True
    signed_out = False
    try:
        if driver is not None:
            apex_exited = driver.stop_apex().all_processes_exited
            signed_out = driver.sign_out()
    except Exception as error:
        # Cleanup must never be the thing that strands a real account: an
        # unhandled error here used to escape the finally block and leave the
        # lease open until it expired.
        print(f"EA 清理失败：{type(error).__name__}：{error}", file=sys.stderr)
    cleanup = CleanupEvidence(
        input_released=True,
        apex_exited=apex_exited,
        ea_signed_out=signed_out,
    )
    if not cleanup.complete:
        print(
            "无法确认清理完成，租约未关闭。请在运营页确认 Apex 已退出、EA 已登出后"
            f"再安全释放租约 {lease.lease_id}。",
            file=sys.stderr,
        )
        return False
    try:
        status = provider.close(
            lease.lease_id,
            lease.lease_fence,
            uuid.uuid4().hex,
            outcome,
            None,
            None,
            reason_code,
            cleanup,
        )
    except LeaseProviderError as error:
        print(f"关闭租约失败：{error}", file=sys.stderr)
        return False
    print(f"租约 {lease.lease_id} 已关闭：{status.state.value}（{reason_code}）")
    return True


def run_ea_preflight(config_path: Path) -> int:
    """Read-only EA window/state/identity validation before any lease claim."""
    if sys.platform != "win32":
        print("ea-preflight 模式只能在 Windows 上运行。", file=sys.stderr)
        return 2
    try:
        config = load_config(config_path)
        with DxcamFrameSource(
            backend=str(config.environment["captureBackend"]),
            output_index=int(config.environment["outputIndex"]),
        ) as source:
            driver = WindowsEaHybridDriver(capture_source=source)
            state = driver.preflight()
            identity = driver.current_identity()
        display_identity = (
            "none"
            if identity is None
            else f"{identity.ea_account_id[:4]}...{identity.ea_account_id[-4:]}"
        )
        print(f"EA 只读预检通过：state={state.value}, identity={display_identity}")
        return 0
    except Exception as error:
        print(f"EA 只读预检失败：{error}", file=sys.stderr)
        return 1


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
    observe.add_argument(
        "--with-ocr",
        action="store_true",
        help="同时跑 OCR；全屏 OCR 单次数秒，会把采样间隔拖到秒级",
    )
    play = subparsers.add_parser(
        "play",
        help="按能力集自动游玩：真实识别、真实决策、真实输入",
    )
    play.add_argument("--countdown", type=int, default=5)
    play.add_argument("--duration-s", type=float, help="到时自动停止，默认一直跑")
    play.add_argument(
        "--runner-config",
        type=Path,
        help="账号/设备私有绑定文件；默认自动读取 windows/runner.private.json",
    )
    account_cycle = subparsers.add_parser(
        "account-cycle",
        help="按服务端租约自动登录 EA、游玩到目标等级并安全切号",
    )
    account_cycle.add_argument(
        "--runner-config",
        type=Path,
        help="托管设备私有配置；默认读取 windows/account-cycle.private.json",
    )
    account_cycle.add_argument(
        "--idle-s",
        type=float,
        default=30.0,
        help="服务端暂无账号时的最小轮询间隔",
    )
    account_cycle.add_argument(
        "--once",
        action="store_true",
        help="只跑一个账号一轮就退出，不进入持续领号循环",
    )
    account_cycle.add_argument(
        "--resume",
        action="store_true",
        help="清除本地人工暂停状态后再运行；暂停原因必须先确认已解决",
    )
    account_cycle_check = subparsers.add_parser(
        "account-cycle-check",
        help="只验证托管配置、网络和 Provider 鉴权，不领取账号",
    )
    account_cycle_check.add_argument(
        "--runner-config",
        type=Path,
        help="托管设备私有配置；默认读取 windows/account-cycle.private.json",
    )
    subparsers.add_parser(
        "ea-preflight",
        help="只读识别 EA 窗口、页面状态和稳定账号 ID，不领取账号",
    )
    ea_login_check = subparsers.add_parser(
        "ea-login-check",
        help="只领取一个租约验证 EA 登录，留下脱敏证据后关闭租约，不启动 Apex",
    )
    ea_login_check.add_argument(
        "--runner-config",
        type=Path,
        help="托管设备私有配置；默认读取 windows/account-cycle.private.json",
    )
    probe = subparsers.add_parser(
        "probe-input",
        help="向游戏发几个明显动作，用来确认 SendInput 到底进不进得去",
    )
    probe.add_argument("--countdown", type=int, default=5)
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
            with_ocr=args.with_ocr,
        )
    if command == "probe-input":
        return run_probe_input(args.config, countdown=max(0, args.countdown))
    if command == "play":
        return run_play(
            args.config,
            countdown=max(0, args.countdown),
            duration_s=args.duration_s,
            runner_config_path=args.runner_config,
        )
    if command == "account-cycle":
        return run_account_cycle(
            args.config,
            runner_config_path=args.runner_config,
            idle_s=args.idle_s,
            once=args.once,
            resume=args.resume,
        )
    if command == "account-cycle-check":
        return run_account_cycle_check(runner_config_path=args.runner_config)
    if command == "ea-preflight":
        return run_ea_preflight(args.config)
    if command == "ea-login-check":
        return run_ea_login_check(
            args.config,
            runner_config_path=args.runner_config,
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

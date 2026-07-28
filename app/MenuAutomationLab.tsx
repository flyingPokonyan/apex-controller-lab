"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

type ScreenState =
  | "CONTINUE"
  | "EVENT_MODAL"
  | "LOBBY"
  | "MODE_PANEL"
  | "MATCHMAKING"
  | "LEGEND_SELECT"
  | "LOADOUT_SELECT"
  | "MATCH_READY";

type LogKind = "CAPTURE" | "DETECT" | "INPUT" | "STATE" | "DONE" | "ERROR";

type RunLog = {
  id: number;
  elapsedMs: number;
  kind: LogKind;
  message: string;
  confidence?: number;
};

type NormalizedPoint = { x: number; y: number };

const SCREEN_LABELS: Record<ScreenState, string> = {
  CONTINUE: "启动继续页",
  EVENT_MODAL: "活动弹窗",
  LOBBY: "游戏大厅",
  MODE_PANEL: "模式选择",
  MATCHMAKING: "匹配中",
  LEGEND_SELECT: "英雄选择",
  LOADOUT_SELECT: "枪械配装",
  MATCH_READY: "流程完成",
};

const TARGETS: Record<string, NormalizedPoint> = {
  continue: { x: 0.5, y: 0.76 },
  dismissEvent: { x: 0.923, y: 0.105 },
  modeButton: { x: 0.145, y: 0.805 },
  targetMode: { x: 0.29, y: 0.47 },
  ready: { x: 0.145, y: 0.91 },
  primaryLegend: { x: 0.43, y: 0.62 },
  backupLegend: { x: 0.61, y: 0.62 },
  loadout: { x: 0.34, y: 0.58 },
};

const FLOW_STEPS = [
  { id: "continue", label: "点击继续", detail: "识别启动页锚点后确认" },
  { id: "popup", label: "处理活动页", detail: "仅检测到遮挡层时关闭" },
  { id: "mode", label: "选择娱乐模式", detail: "打开面板并验证模式名称" },
  { id: "ready", label: "开始游戏", detail: "大厅状态确认后进入匹配" },
  { id: "legend", label: "选择英雄", detail: "首选被占用时选择备选" },
  { id: "loadout", label: "选择枪械", detail: "有配装页才执行，否则跳过" },
] as const;

const MODES = ["混合模式 · 团队死斗", "混合模式 · 控制", "混合模式 · 枪战", "限时模式 · 子弹时间"];
const LEGENDS = ["恶灵", "命脉", "寻血猎犬", "动力小子"];
const LOADOUTS = ["突击步枪 + 霰弹枪", "冲锋枪 + 神射手", "轻机枪 + 手枪"];

function wait(ms: number) {
  return new Promise<void>((resolve) => window.setTimeout(resolve, ms));
}

function MockGameScreen({
  screen,
  selectedMode,
  selectedLegend,
  selectedLoadout,
  cursor,
}: {
  screen: ScreenState;
  selectedMode: string;
  selectedLegend: string;
  selectedLoadout: string;
  cursor: NormalizedPoint | null;
}) {
  const modeCards = ["团队死斗", "控制", "枪战", "子弹时间"];
  const legendCards = ["恶灵", "命脉", "寻血猎犬", "动力小子", "探路者", "地平线"];

  return (
    <div className={`mock-game mock-state-${screen.toLowerCase()}`} data-screen-marker={screen}>
      <div className="mock-scanline" />
      <div className="mock-hud-top">
        <div className="mock-apex-mark"><span>A</span> APEX</div>
        <div className="screen-anchor">STATE / {screen}</div>
      </div>

      {screen === "CONTINUE" && (
        <div className="mock-center-screen">
          <div className="mock-logo">APEX<span>LEGENDS</span></div>
          <div className="mock-continue"><i />点击继续</div>
          <small>浏览器模拟画面 · 不连接游戏</small>
        </div>
      )}

      {(screen === "LOBBY" || screen === "EVENT_MODAL") && (
        <>
          <div className="mock-lobby-tabs"><b>游戏</b><span>传奇</span><span>装备</span><span>商店</span></div>
          <div className="mock-squad"><span className="mock-avatar">AC</span><strong>{selectedLegend || "待选择英雄"}</strong><small>准备加入娱乐模式</small></div>
          <div className="mock-lobby-actions">
            <div className="mock-mode-card"><small>当前模式</small><strong>{selectedMode || "点击选择模式"}</strong><span>更换模式</span></div>
            <button type="button" tabIndex={-1}>准备</button>
          </div>
        </>
      )}

      {screen === "EVENT_MODAL" && (
        <div className="mock-modal-backdrop">
          <div className="mock-event-modal">
            <button type="button" tabIndex={-1} aria-label="模拟关闭活动弹窗">×</button>
            <span>限时活动</span>
            <h3>周末娱乐模式轮换</h3>
            <p>这是用于验证“偶发活动页”分支的模拟遮挡层。</p>
            <b>查看活动</b>
          </div>
        </div>
      )}

      {screen === "MODE_PANEL" && (
        <div className="mock-panel-screen">
          <div className="mock-panel-title"><small>PLAYLIST</small><h3>选择游戏模式</h3></div>
          <div className="mock-mode-grid">
            {modeCards.map((mode, index) => <div className={index === 0 ? "target-card" : ""} key={mode}><span>0{index + 1}</span><strong>{mode}</strong><small>娱乐模式</small></div>)}
          </div>
        </div>
      )}

      {screen === "MATCHMAKING" && (
        <div className="mock-center-screen">
          <div className="match-spinner" />
          <h3>正在匹配</h3>
          <p>{selectedMode}</p>
          <small>等待模拟服务器返回英雄选择页</small>
        </div>
      )}

      {screen === "LEGEND_SELECT" && (
        <div className="mock-panel-screen">
          <div className="mock-panel-title"><small>LEGEND SELECT</small><h3>选择英雄</h3></div>
          <div className="mock-legend-grid">
            {legendCards.map((legend, index) => <div className={index < 2 ? "candidate-card" : ""} key={legend}><span>{String(index + 1).padStart(2, "0")}</span><strong>{legend}</strong></div>)}
          </div>
        </div>
      )}

      {screen === "LOADOUT_SELECT" && (
        <div className="mock-panel-screen">
          <div className="mock-panel-title"><small>LOADOUT SELECT</small><h3>选择预设枪械</h3></div>
          <div className="mock-loadout-list">
            {LOADOUTS.map((loadout, index) => <div className={index === 0 ? "target-card" : ""} key={loadout}><span>预设 0{index + 1}</span><strong>{loadout}</strong><small>{index === 0 ? "目标配装" : "可选配装"}</small></div>)}
          </div>
        </div>
      )}

      {screen === "MATCH_READY" && (
        <div className="mock-center-screen mock-complete-screen">
          <span className="complete-check">✓</span>
          <h3>出生前流程已跑通</h3>
          <div><span>{selectedMode}</span><span>{selectedLegend}</span><span>{selectedLoadout || "该模式无枪械选择页"}</span></div>
          <small>到此停止，不执行移动、瞄准或战斗输入</small>
        </div>
      )}

      {cursor && <span className="automation-cursor" style={{ left: `${cursor.x * 100}%`, top: `${cursor.y * 100}%` }}><i /></span>}
      <div className="mock-resolution">1920 × 1080 / NORMALIZED PREVIEW</div>
    </div>
  );
}

export function MenuAutomationLab() {
  const [screen, setScreen] = useState<ScreenState>("CONTINUE");
  const [selectedMode, setSelectedMode] = useState("");
  const [selectedLegend, setSelectedLegend] = useState("");
  const [selectedLoadout, setSelectedLoadout] = useState("");
  const [simulateEvent, setSimulateEvent] = useState(true);
  const [primaryTaken, setPrimaryTaken] = useState(true);
  const [modeHasLoadout, setModeHasLoadout] = useState(true);
  const [preferredMode, setPreferredMode] = useState(MODES[0]);
  const [primaryLegend, setPrimaryLegend] = useState(LEGENDS[0]);
  const [backupLegend, setBackupLegend] = useState(LEGENDS[1]);
  const [preferredLoadout, setPreferredLoadout] = useState(LOADOUTS[0]);
  const [speed, setSpeed] = useState(0.5);
  const [running, setRunning] = useState(false);
  const [complete, setComplete] = useState(false);
  const [cursor, setCursor] = useState<NormalizedPoint | null>(null);
  const [captures, setCaptures] = useState(0);
  const [logs, setLogs] = useState<RunLog[]>([]);

  const screenRef = useRef<ScreenState>(screen);
  const runTokenRef = useRef(0);
  const runStartedAtRef = useRef(0);
  const logIdRef = useRef(0);
  const logContainerRef = useRef<HTMLDivElement>(null);

  useEffect(() => { screenRef.current = screen; }, [screen]);
  useEffect(() => {
    const container = logContainerRef.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [logs]);

  const addLog = useCallback((kind: LogKind, message: string, confidence?: number) => {
    const elapsedMs = runStartedAtRef.current ? Math.round(performance.now() - runStartedAtRef.current) : 0;
    setLogs((current) => [...current, { id: ++logIdRef.current, elapsedMs, kind, message, confidence }]);
  }, []);

  const setCurrentScreen = useCallback((next: ScreenState) => {
    screenRef.current = next;
    setScreen(next);
  }, []);

  const reset = useCallback(() => {
    runTokenRef.current += 1;
    setRunning(false);
    setComplete(false);
    setCursor(null);
    setCaptures(0);
    setLogs([]);
    setSelectedMode("");
    setSelectedLegend("");
    setSelectedLoadout("");
    setCurrentScreen("CONTINUE");
  }, [setCurrentScreen]);

  const delay = useCallback(async (baseMs: number, token: number) => {
    await wait(baseMs / speed);
    if (token !== runTokenRef.current) throw new Error("RUN_CANCELLED");
  }, [speed]);

  const detect = useCallback(async (expected: ScreenState, token: number, optional = false) => {
    setCaptures((value) => value + 1);
    addLog("CAPTURE", `捕获浏览器画面帧：${SCREEN_LABELS[screenRef.current]}`);
    await delay(180, token);
    const matched = screenRef.current === expected;
    if (matched) {
      addLog("DETECT", `识别到 ${expected} 锚点`, 0.99);
      return true;
    }
    if (optional) {
      addLog("DETECT", `未发现可选状态 ${expected}，继续`, 0.98);
      return false;
    }
    throw new Error(`期望 ${expected}，实际 ${screenRef.current}`);
  }, [addLog, delay]);

  const clickAndTransition = useCallback(async ({
    target,
    label,
    next,
    token,
    transitionMs = 520,
  }: {
    target: NormalizedPoint;
    label: string;
    next: ScreenState;
    token: number;
    transitionMs?: number;
  }) => {
    setCursor(target);
    addLog("INPUT", `${label} @ (${target.x.toFixed(3)}, ${target.y.toFixed(3)})`);
    await delay(280, token);
    setCursor(null);
    await delay(transitionMs, token);
    setCurrentScreen(next);
    addLog("STATE", `画面进入 ${next}`);
  }, [addLog, delay, setCurrentScreen]);

  const runScenario = useCallback(async () => {
    const token = runTokenRef.current + 1;
    runTokenRef.current = token;
    runStartedAtRef.current = performance.now();
    logIdRef.current = 0;
    setLogs([]);
    setCaptures(0);
    setComplete(false);
    setRunning(true);
    setSelectedMode("");
    setSelectedLegend("");
    setSelectedLoadout("");
    setCurrentScreen("CONTINUE");

    try {
      addLog("STATE", "场景启动：大厅到出生前，系统输入保持关闭");
      await delay(300, token);

      await detect("CONTINUE", token);
      await clickAndTransition({ target: TARGETS.continue, label: "点击继续", next: simulateEvent ? "EVENT_MODAL" : "LOBBY", token });

      const eventVisible = await detect("EVENT_MODAL", token, true);
      if (eventVisible) {
        await clickAndTransition({ target: TARGETS.dismissEvent, label: "关闭活动弹窗", next: "LOBBY", token });
      }

      await detect("LOBBY", token);
      await clickAndTransition({ target: TARGETS.modeButton, label: "打开模式面板", next: "MODE_PANEL", token });
      await detect("MODE_PANEL", token);
      setSelectedMode(preferredMode);
      await clickAndTransition({ target: TARGETS.targetMode, label: `选择 ${preferredMode}`, next: "LOBBY", token });
      await detect("LOBBY", token);
      addLog("DETECT", `大厅模式标签已更新：${preferredMode}`, 0.97);

      await clickAndTransition({ target: TARGETS.ready, label: "点击准备 / 开始游戏", next: "MATCHMAKING", token });
      await detect("MATCHMAKING", token);
      await delay(1500, token);
      setCurrentScreen("LEGEND_SELECT");
      addLog("STATE", "模拟匹配完成，进入英雄选择");

      await detect("LEGEND_SELECT", token);
      const legend = primaryTaken ? backupLegend : primaryLegend;
      const legendTarget = primaryTaken ? TARGETS.backupLegend : TARGETS.primaryLegend;
      if (primaryTaken) addLog("DETECT", `首选 ${primaryLegend} 已被占用，切换备选 ${backupLegend}`, 0.96);
      setSelectedLegend(legend);
      await clickAndTransition({
        target: legendTarget,
        label: `选择英雄 ${legend}`,
        next: modeHasLoadout ? "LOADOUT_SELECT" : "MATCH_READY",
        token,
      });

      const loadoutVisible = await detect("LOADOUT_SELECT", token, true);
      if (loadoutVisible) {
        setSelectedLoadout(preferredLoadout);
        await clickAndTransition({ target: TARGETS.loadout, label: `选择枪械 ${preferredLoadout}`, next: "MATCH_READY", token });
      }

      await detect("MATCH_READY", token);
      setComplete(true);
      setRunning(false);
      addLog("DONE", "流程成功：已完成模式、匹配、英雄与可选枪械选择");
    } catch (error) {
      if (error instanceof Error && error.message === "RUN_CANCELLED") return;
      setRunning(false);
      addLog("ERROR", error instanceof Error ? error.message : "未知错误");
    }
  }, [addLog, backupLegend, clickAndTransition, delay, detect, modeHasLoadout, preferredLoadout, preferredMode, primaryLegend, primaryTaken, setCurrentScreen, simulateEvent]);

  const stop = () => {
    runTokenRef.current += 1;
    setRunning(false);
    setCursor(null);
    addLog("ERROR", "用户停止：已取消后续动作");
  };

  const exportRun = () => {
    const payload = {
      format: "apex-controller-lab/menu-run@1",
      generatedAt: new Date().toISOString(),
      adapter: "browser-simulation",
      systemInput: false,
      scenario: { preferredMode, primaryLegend, backupLegend, preferredLoadout, simulateEvent, primaryTaken, modeHasLoadout },
      result: { complete, screen, selectedMode, selectedLegend, selectedLoadout, captures },
      logs,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `menu-run-${Date.now()}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const progress = useMemo(() => {
    const rank: Record<ScreenState, number> = { CONTINUE: 0, EVENT_MODAL: 1, LOBBY: selectedMode ? 3 : 2, MODE_PANEL: 2, MATCHMAKING: 4, LEGEND_SELECT: 5, LOADOUT_SELECT: 6, MATCH_READY: 7 };
    return Math.round((rank[screen] / 7) * 100);
  }, [screen, selectedMode]);

  return (
    <main className="menu-lab-shell">
      <header className="menu-topbar">
        <Link className="brand-lockup menu-brand" href="/">
          <span className="brand-mark">AC</span>
          <div><strong>APEX CONTROLLER LAB</strong><span>MENU AUTOMATION SANDBOX</span></div>
        </Link>
        <nav className="lab-nav" aria-label="实验室导航"><Link className="active" href="/">菜单流程</Link><Link href="/heart">爱心轨迹</Link><a href="#windows-handoff">接入清单</a></nav>
        <div className="safety-badge"><i />仅模拟页面 · 系统输入关闭</div>
      </header>

      <section className="menu-hero">
        <div>
          <p className="eyebrow">SCREEN → DETECT → ACTION → VERIFY</p>
          <h1>先把进游戏流程，<em>本地跑通。</em></h1>
        </div>
        <p>覆盖继续页、偶发活动弹窗、娱乐模式、开始匹配、英雄和可选枪械选择。每一步先识别，再动作，再验证。</p>
      </section>

      <section className="menu-workbench">
        <div className="preview-column">
          <div className="panel-head menu-panel-head">
            <div><span className={`live-dot ${running ? "is-live" : ""}`} /><strong>{running ? "自动演练中" : complete ? "流程已通过" : "等待运行"}</strong><small>{SCREEN_LABELS[screen]}</small></div>
            <span className="frame-rate">FRAME #{String(captures).padStart(2, "0")} · {progress}%</span>
          </div>
          <MockGameScreen screen={screen} selectedMode={selectedMode} selectedLegend={selectedLegend} selectedLoadout={selectedLoadout} cursor={cursor} />
          <div className="preview-progress"><span style={{ width: `${progress}%` }} /></div>
          <div className="run-summary">
            <div><span>当前状态</span><strong>{screen}</strong></div>
            <div><span>已截取画面</span><strong>{captures}</strong></div>
            <div><span>目标模式</span><strong>{selectedMode || "待选择"}</strong></div>
            <div><span>最终结果</span><strong className={complete ? "success-text" : ""}>{complete ? "PASS" : running ? "RUNNING" : "IDLE"}</strong></div>
          </div>
        </div>

        <aside className="scenario-column">
          <div className="scenario-config">
            <div className="section-label"><span>01</span>场景配置</div>
            <label><span>娱乐模式</span><select value={preferredMode} disabled={running} onChange={(event) => setPreferredMode(event.target.value)}>{MODES.map((item) => <option key={item}>{item}</option>)}</select></label>
            <div className="split-selects">
              <label><span>首选英雄</span><select value={primaryLegend} disabled={running} onChange={(event) => setPrimaryLegend(event.target.value)}>{LEGENDS.map((item) => <option key={item}>{item}</option>)}</select></label>
              <label><span>备选英雄</span><select value={backupLegend} disabled={running} onChange={(event) => setBackupLegend(event.target.value)}>{LEGENDS.map((item) => <option key={item}>{item}</option>)}</select></label>
            </div>
            <label><span>枪械预设</span><select value={preferredLoadout} disabled={running} onChange={(event) => setPreferredLoadout(event.target.value)}>{LOADOUTS.map((item) => <option key={item}>{item}</option>)}</select></label>
            <div className="scenario-toggles">
              <label><input type="checkbox" checked={simulateEvent} disabled={running} onChange={(event) => setSimulateEvent(event.target.checked)} /><span><b>模拟活动弹窗</b><small>测试偶发遮挡分支</small></span></label>
              <label><input type="checkbox" checked={primaryTaken} disabled={running} onChange={(event) => setPrimaryTaken(event.target.checked)} /><span><b>首选英雄被占用</b><small>自动使用备选英雄</small></span></label>
              <label><input type="checkbox" checked={modeHasLoadout} disabled={running} onChange={(event) => setModeHasLoadout(event.target.checked)} /><span><b>模式包含枪械页</b><small>关闭后验证跳过分支</small></span></label>
            </div>
          </div>

          <div className="flow-list">
            <div className="section-label"><span>02</span>执行步骤</div>
            {FLOW_STEPS.map((step, index) => <div className="flow-item" key={step.id}><span>{String(index + 1).padStart(2, "0")}</span><div><strong>{step.label}</strong><small>{step.detail}</small></div></div>)}
          </div>

          <div className="scenario-actions">
            <label><span>演练速度</span><select value={speed} disabled={running} onChange={(event) => setSpeed(Number(event.target.value))}><option value={0.25}>0.25× · 逐步观察（约 35 秒）</option><option value={0.5}>0.5× · 慢速演示（约 18 秒）</option><option value={1}>1× · 标准速度</option><option value={2}>2× · 快速</option><option value={4}>4× · 回归测试</option></select></label>
            <button className="run-action" onClick={running ? stop : runScenario}><span>{running ? "■" : "▶"}</span>{running ? "停止演练" : complete ? "重新演练" : "开始自动演练"}</button>
            <button className="quiet-action" onClick={reset} disabled={running}>重置</button>
            <button className="quiet-action" onClick={exportRun} disabled={logs.length === 0}>导出记录</button>
          </div>
        </aside>
      </section>

      <section className="run-log-section">
        <div className="log-heading"><div><p className="eyebrow">RUN RECORDER</p><h2>状态机执行记录</h2></div><p>当前检测器读取浏览器模拟标记。Windows 版将替换为窗口截图、局部模板匹配与 OCR，状态机本身保持不变。</p></div>
        <div className="run-log" aria-live="polite" ref={logContainerRef}>
          {logs.length === 0 ? <div className="empty-log">点击“开始自动演练”查看逐帧识别、点击与状态验证。</div> : logs.map((log) => (
            <div className={`log-row log-${log.kind.toLowerCase()}`} key={log.id}>
              <span>+{String(log.elapsedMs).padStart(5, "0")}ms</span><b>{log.kind}</b><p>{log.message}</p>{log.confidence && <em>{Math.round(log.confidence * 100)}%</em>}
            </div>
          ))}
        </div>
      </section>

      <section className="windows-handoff" id="windows-handoff">
        <div><p className="eyebrow">WINDOWS HANDOFF</p><h2>接真实 Apex 前，需要采集 8 类画面。</h2></div>
        <ol><li>启动“继续”页</li><li>活动弹窗（遇到时）</li><li>无弹窗大厅</li><li>模式选择面板</li><li>模式已选中的大厅</li><li>匹配中画面</li><li>英雄选择页</li><li>枪械/配装选择页</li></ol>
        <p>统一固定为同一分辨率、DPI、语言与显示模式。截图只用于标定识别区域，不读取游戏内存。</p>
      </section>

      <footer className="menu-footer"><span>当前版本只验证流程逻辑，不会点击系统或 Apex。</span><strong>STOP BOUNDARY / BEFORE GAMEPLAY</strong></footer>
    </main>
  );
}

"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";

type Mode = "brush" | "avatar";
type Vec2 = { x: number; y: number };

type InputFrame = {
  frame: number;
  timeMs: number;
  leftX: number;
  leftY: number;
  rightX: number;
  rightY: number;
  targetX: number;
  targetY: number;
  actualX: number;
  actualY: number;
};

type Simulation = {
  running: boolean;
  complete: boolean;
  frame: number;
  position: Vec2;
  velocity: Vec2;
  heading: number;
  trail: Vec2[];
  frames: InputFrame[];
  errorSquared: number;
  left: Vec2;
  right: Vec2;
};

const FPS = 60;
const TAU = Math.PI * 2;

const add = (a: Vec2, b: Vec2): Vec2 => ({ x: a.x + b.x, y: a.y + b.y });
const sub = (a: Vec2, b: Vec2): Vec2 => ({ x: a.x - b.x, y: a.y - b.y });
const scale = (v: Vec2, amount: number): Vec2 => ({ x: v.x * amount, y: v.y * amount });
const length = (v: Vec2) => Math.hypot(v.x, v.y);
const distance = (a: Vec2, b: Vec2) => length(sub(a, b));
const dot = (a: Vec2, b: Vec2) => a.x * b.x + a.y * b.y;
const clamp = (value: number, min: number, max: number) => Math.min(max, Math.max(min, value));
const normalize = (v: Vec2): Vec2 => {
  const magnitude = length(v);
  return magnitude > 1e-8 ? scale(v, 1 / magnitude) : { x: 0, y: 0 };
};
const wrapAngle = (value: number) => Math.atan2(Math.sin(value), Math.cos(value));

function createHeartPath(samples: number, size: number): Vec2[] {
  const dense: Vec2[] = [];
  for (let i = 0; i < 2400; i += 1) {
    const t = (i / 2400) * TAU;
    dense.push({
      x: 16 * Math.sin(t) ** 3,
      y: 13 * Math.cos(t) - 5 * Math.cos(2 * t) - 2 * Math.cos(3 * t) - Math.cos(4 * t),
    });
  }

  const minX = Math.min(...dense.map((point) => point.x));
  const maxX = Math.max(...dense.map((point) => point.x));
  const minY = Math.min(...dense.map((point) => point.y));
  const maxY = Math.max(...dense.map((point) => point.y));
  const center = { x: (minX + maxX) / 2, y: (minY + maxY) / 2 };
  const unit = dense.map((point) => scale(sub(point, center), (2 * size) / Math.max(maxX - minX, maxY - minY)));

  const cumulative = [0];
  for (let i = 1; i <= unit.length; i += 1) {
    cumulative.push(cumulative[i - 1] + distance(unit[i - 1], unit[i % unit.length]));
  }
  const total = cumulative[cumulative.length - 1];
  const result: Vec2[] = [];
  let segment = 0;

  for (let i = 0; i < samples; i += 1) {
    const target = (i / samples) * total;
    while (segment < unit.length - 1 && cumulative[segment + 1] < target) segment += 1;
    const start = unit[segment];
    const end = unit[(segment + 1) % unit.length];
    const segmentLength = cumulative[segment + 1] - cumulative[segment];
    const ratio = segmentLength > 0 ? (target - cumulative[segment]) / segmentLength : 0;
    result.push(add(start, scale(sub(end, start), ratio)));
  }
  return result;
}

function pathLength(path: Vec2[]) {
  return path.reduce((total, point, index) => total + distance(point, path[(index + 1) % path.length]), 0);
}

function radialDeadzone(value: Vec2, deadzone: number): Vec2 {
  const magnitude = clamp(length(value), 0, 1);
  if (magnitude <= deadzone) return { x: 0, y: 0 };
  return scale(normalize(value), (magnitude - deadzone) / (1 - deadzone));
}

function initialSimulation(path: Vec2[]): Simulation {
  const tangent = normalize(sub(path[1], path[0]));
  return {
    running: false,
    complete: false,
    frame: 0,
    position: path[0],
    velocity: { x: 0, y: 0 },
    heading: Math.atan2(tangent.y, tangent.x),
    trail: [path[0]],
    frames: [],
    errorSquared: 0,
    left: { x: 0, y: 0 },
    right: { x: 0, y: 0 },
  };
}

function formatSignal(value: number) {
  const rounded = Math.abs(value) < 0.0005 ? 0 : value;
  return rounded.toFixed(3);
}

function Stick({ label, value }: { label: string; value: Vec2 }) {
  return (
    <div className="stick-readout">
      <div className="stick-face" aria-label={`${label}摇杆实时位置`}>
        <span
          className="stick-thumb"
          style={{ transform: `translate(calc(-50% + ${value.x * 25}px), calc(-50% + ${-value.y * 25}px))` }}
        />
        <span className="stick-axis stick-axis-x" />
        <span className="stick-axis stick-axis-y" />
      </div>
      <div>
        <strong>{label}</strong>
        <span>X {formatSignal(value.x)}</span>
        <span>Y {formatSignal(value.y)}</span>
      </div>
    </div>
  );
}

export function ControllerLab() {
  const [mode, setMode] = useState<Mode>("brush");
  const [duration, setDuration] = useState(10);
  const [heartSize, setHeartSize] = useState(92);
  const [deadzone, setDeadzone] = useState(0.12);
  const [response, setResponse] = useState(9);
  const [snapshot, setSnapshot] = useState({
    running: false,
    complete: false,
    frame: 0,
    progress: 0,
    rms: 0,
    left: { x: 0, y: 0 },
    right: { x: 0, y: 0 },
    frameCount: 0,
  });

  const canvasRef = useRef<HTMLCanvasElement>(null);
  const modeRef = useRef(mode);
  const deadzoneRef = useRef(deadzone);
  const responseRef = useRef(response);
  const path = useMemo(
    () => createHeartPath(Math.round(duration * FPS), heartSize / 100),
    [duration, heartSize],
  );
  const pathRef = useRef(path);
  const simulationRef = useRef<Simulation>(initialSimulation(path));

  const publishSnapshot = useCallback(() => {
    const simulation = simulationRef.current;
    setSnapshot({
      running: simulation.running,
      complete: simulation.complete,
      frame: simulation.frame,
      progress: clamp(simulation.frame / pathRef.current.length, 0, 1),
      rms: simulation.frame > 0 ? Math.sqrt(simulation.errorSquared / simulation.frame) : 0,
      left: simulation.left,
      right: simulation.right,
      frameCount: simulation.frames.length,
    });
  }, []);

  const reset = useCallback(() => {
    simulationRef.current = initialSimulation(pathRef.current);
    publishSnapshot();
  }, [publishSnapshot]);

  useEffect(() => {
    modeRef.current = mode;
    deadzoneRef.current = deadzone;
    responseRef.current = response;
    pathRef.current = path;
    simulationRef.current = initialSimulation(path);
    publishSnapshot();
  }, [path, mode, deadzone, response, publishSnapshot]);

  const stepSimulation = useCallback((dt: number) => {
    const simulation = simulationRef.current;
    const currentPath = pathRef.current;
    if (!simulation.running || simulation.complete) return;

    const index = simulation.frame;
    if (index >= currentPath.length) {
      simulation.running = false;
      simulation.complete = true;
      simulation.left = { x: 0, y: 0 };
      simulation.right = { x: 0, y: 0 };
      publishSnapshot();
      return;
    }

    const target = currentPath[index];
    const next = currentPath[(index + 1) % currentPath.length];
    const tangent = normalize(sub(next, target));
    const nominalSpeed = pathLength(currentPath) / (currentPath.length * dt);
    const commandMagnitude = modeRef.current === "brush" ? 0.84 : 0.9;
    const calibratedMagnitude = length(radialDeadzone({ x: commandMagnitude, y: 0 }, deadzoneRef.current));
    const maxSpeed = nominalSpeed / Math.max(calibratedMagnitude, 0.01);

    let left: Vec2;
    let right: Vec2 = { x: 0, y: 0 };
    let desiredVelocity: Vec2;

    if (modeRef.current === "brush") {
      left = scale(tangent, commandMagnitude);
      const filtered = radialDeadzone(left, deadzoneRef.current);
      desiredVelocity = scale(filtered, maxSpeed);
      simulation.velocity = desiredVelocity;
    } else {
      const lookAhead = currentPath[(index + 10) % currentPath.length];
      const pursuit = normalize(sub(lookAhead, simulation.position));
      const facing = { x: Math.cos(simulation.heading), y: Math.sin(simulation.heading) };
      const localRight = { x: Math.sin(simulation.heading), y: -Math.cos(simulation.heading) };
      left = {
        x: dot(pursuit, localRight) * commandMagnitude,
        y: dot(pursuit, facing) * commandMagnitude,
      };

      const headingError = wrapAngle(Math.atan2(tangent.y, tangent.x) - simulation.heading);
      right = { x: clamp(headingError / 0.7, -1, 1) * 0.82, y: 0 };
      const filteredTurn = radialDeadzone(right, deadzoneRef.current);
      simulation.heading = wrapAngle(simulation.heading + filteredTurn.x * 5.2 * dt);

      const newFacing = { x: Math.cos(simulation.heading), y: Math.sin(simulation.heading) };
      const newRight = { x: Math.sin(simulation.heading), y: -Math.cos(simulation.heading) };
      const filteredMove = radialDeadzone(left, deadzoneRef.current);
      desiredVelocity = add(scale(newRight, filteredMove.x * maxSpeed), scale(newFacing, filteredMove.y * maxSpeed));
      const smoothing = 1 - Math.exp(-responseRef.current * dt);
      simulation.velocity = add(simulation.velocity, scale(sub(desiredVelocity, simulation.velocity), smoothing));
    }

    simulation.position = add(simulation.position, scale(simulation.velocity, dt));
    simulation.trail.push({ ...simulation.position });
    simulation.left = left;
    simulation.right = right;
    simulation.errorSquared += distance(simulation.position, next) ** 2;
    simulation.frames.push({
      frame: index,
      timeMs: Math.round(index * dt * 1000),
      leftX: left.x,
      leftY: left.y,
      rightX: right.x,
      rightY: right.y,
      targetX: next.x,
      targetY: next.y,
      actualX: simulation.position.x,
      actualY: simulation.position.y,
    });
    simulation.frame += 1;
  }, [publishSnapshot]);

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const width = Math.max(1, rect.width);
    const height = Math.max(1, rect.height);
    if (canvas.width !== Math.round(width * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(width * dpr);
      canvas.height = Math.round(height * dpr);
    }
    const context = canvas.getContext("2d");
    if (!context) return;
    context.setTransform(dpr, 0, 0, dpr, 0, 0);
    context.clearRect(0, 0, width, height);

    const gridSize = 34;
    context.lineWidth = 1;
    context.strokeStyle = "rgba(199, 222, 235, 0.055)";
    for (let x = 0; x < width; x += gridSize) {
      context.beginPath(); context.moveTo(x, 0); context.lineTo(x, height); context.stroke();
    }
    for (let y = 0; y < height; y += gridSize) {
      context.beginPath(); context.moveTo(0, y); context.lineTo(width, y); context.stroke();
    }

    const viewScale = Math.min(width / 2.6, height / 2.35);
    const center = { x: width / 2, y: height / 2 + 8 };
    const toCanvas = (point: Vec2) => ({ x: center.x + point.x * viewScale, y: center.y - point.y * viewScale });
    const targetPath = pathRef.current;
    const simulation = simulationRef.current;

    context.save();
    context.setLineDash([3, 8]);
    context.lineCap = "round";
    context.lineWidth = 1.5;
    context.strokeStyle = "rgba(224, 239, 246, 0.28)";
    context.beginPath();
    targetPath.forEach((point, index) => {
      const screen = toCanvas(point);
      if (index === 0) context.moveTo(screen.x, screen.y); else context.lineTo(screen.x, screen.y);
    });
    const firstTarget = toCanvas(targetPath[0]);
    context.lineTo(firstTarget.x, firstTarget.y);
    context.stroke();
    context.restore();

    if (simulation.trail.length > 1) {
      const gradient = context.createLinearGradient(0, height, width, 0);
      gradient.addColorStop(0, "#ff315f");
      gradient.addColorStop(0.55, "#ff6485");
      gradient.addColorStop(1, "#ffb1c2");
      context.save();
      context.lineCap = "round";
      context.lineJoin = "round";
      context.lineWidth = 4;
      context.strokeStyle = gradient;
      context.shadowColor = "rgba(255, 49, 95, 0.75)";
      context.shadowBlur = 16;
      context.beginPath();
      simulation.trail.forEach((point, index) => {
        const screen = toCanvas(point);
        if (index === 0) context.moveTo(screen.x, screen.y); else context.lineTo(screen.x, screen.y);
      });
      context.stroke();
      context.restore();
    }

    const start = toCanvas(targetPath[0]);
    context.beginPath();
    context.arc(start.x, start.y, 5, 0, TAU);
    context.fillStyle = "#d8edf5";
    context.fill();
    context.strokeStyle = "rgba(10, 16, 22, 0.8)";
    context.lineWidth = 2;
    context.stroke();

    const actor = toCanvas(simulation.position);
    context.save();
    context.translate(actor.x, actor.y);
    context.rotate(-simulation.heading);
    context.beginPath();
    context.moveTo(10, 0);
    context.lineTo(-7, -6);
    context.lineTo(-4, 0);
    context.lineTo(-7, 6);
    context.closePath();
    context.fillStyle = "#f7fbfd";
    context.shadowColor = "rgba(247, 251, 253, .75)";
    context.shadowBlur = 9;
    context.fill();
    context.restore();
  }, []);

  useEffect(() => {
    let animationFrame = 0;
    let previous = performance.now();
    let accumulator = 0;
    let lastSnapshot = 0;
    const fixedStep = 1 / FPS;

    const animate = (now: number) => {
      const elapsed = Math.min((now - previous) / 1000, 0.1);
      previous = now;
      accumulator += elapsed;
      while (accumulator >= fixedStep) {
        stepSimulation(fixedStep);
        accumulator -= fixedStep;
      }
      draw();
      if (now - lastSnapshot > 80) {
        publishSnapshot();
        lastSnapshot = now;
      }
      animationFrame = requestAnimationFrame(animate);
    };
    animationFrame = requestAnimationFrame(animate);
    return () => cancelAnimationFrame(animationFrame);
  }, [draw, publishSnapshot, stepSimulation]);

  const togglePlayback = () => {
    const simulation = simulationRef.current;
    if (simulation.complete) simulationRef.current = initialSimulation(pathRef.current);
    simulationRef.current.running = !simulationRef.current.running;
    publishSnapshot();
  };

  const exportFrames = () => {
    const payload = {
      format: "apex-controller-lab/input-frames@1",
      generatedAt: new Date().toISOString(),
      receiver: modeRef.current,
      frameRate: FPS,
      units: { sticks: "normalized [-1, 1]", coordinates: "simulation world units" },
      calibration: { deadzone: deadzoneRef.current, response: responseRef.current },
      frames: simulationRef.current.frames,
    };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `heart-${modeRef.current}-${Date.now()}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  const statusLabel = snapshot.complete ? "轨迹闭合" : snapshot.running ? "信号发送中" : snapshot.frame > 0 ? "已暂停" : "等待发送";

  return (
    <main className="lab-shell">
      <header className="topbar">
        <Link className="brand-lockup menu-brand" href="/">
          <span className="brand-mark">AC</span>
          <div><strong>APEX CONTROLLER LAB</strong><span>INPUT SIGNAL SANDBOX</span></div>
        </Link>
        <nav className="heart-nav" aria-label="实验室导航"><Link href="/">← 菜单流程</Link><b>爱心轨迹</b></nav>
        <div className="safety-badge"><i />浏览器内部模拟 · 不发送系统输入</div>
      </header>

      <section className="hero-copy">
        <p className="eyebrow">PATH → SIGNAL → RECEIVER</p>
        <h1>用手柄信号，<em>画一颗心。</em></h1>
        <p>先证明归一化摇杆帧能够描述轨迹，再替换接收载体。这里的所有输入只在页面内流动。</p>
      </section>

      <section className="lab-grid">
        <div className="canvas-panel">
          <div className="panel-head">
            <div>
              <span className={`live-dot ${snapshot.running ? "is-live" : ""}`} />
              <strong>{statusLabel}</strong>
              <small>{mode === "brush" ? "画笔接收器" : "角色运动接收器"}</small>
            </div>
            <span className="frame-rate">60 FPS / {Math.round(duration * FPS)} FRAMES</span>
          </div>
          <div className="canvas-wrap">
            <canvas ref={canvasRef} aria-label="爱心目标轨迹与手柄信号模拟结果" />
            <div className="canvas-legend"><span><i className="legend-target" />目标轨迹</span><span><i className="legend-actual" />实际轨迹</span></div>
            {snapshot.complete && <div className="complete-stamp"><span>HEART COMPLETE</span><strong>轨迹已闭合</strong></div>}
          </div>
          <div className="progress-track"><span style={{ width: `${snapshot.progress * 100}%` }} /></div>
          <div className="metric-row">
            <div><span>完成度</span><strong>{Math.round(snapshot.progress * 100)}%</strong></div>
            <div><span>均方根误差</span><strong>{(snapshot.rms * 100).toFixed(2)}<small> u*</small></strong></div>
            <div><span>已记录输入帧</span><strong>{snapshot.frameCount}</strong></div>
            <div><span>接收状态</span><strong className="accent-value">{snapshot.running ? "ACTIVE" : snapshot.complete ? "DONE" : "IDLE"}</strong></div>
          </div>
        </div>

        <aside className="control-panel">
          <div className="control-section">
            <div className="section-label"><span>01</span>接收载体</div>
            <div className="mode-switch" role="group" aria-label="选择接收载体">
              <button className={mode === "brush" ? "active" : ""} onClick={() => setMode("brush")}>
                <strong>画笔模式</strong><span>左摇杆 = XY 位移</span>
              </button>
              <button className={mode === "avatar" ? "active" : ""} onClick={() => setMode("avatar")}>
                <strong>角色模式</strong><span>移动 + 朝向 + 惯性</span>
              </button>
            </div>
          </div>

          <div className="control-section">
            <div className="section-label"><span>02</span>轨迹参数</div>
            <label className="range-control">
              <span>完成一圈<strong>{duration.toFixed(1)} 秒</strong></span>
              <input type="range" min="6" max="18" step="0.5" value={duration} onChange={(event) => setDuration(Number(event.target.value))} />
            </label>
            <label className="range-control">
              <span>爱心尺寸<strong>{heartSize}%</strong></span>
              <input type="range" min="68" max="108" step="1" value={heartSize} onChange={(event) => setHeartSize(Number(event.target.value))} />
            </label>
            <label className="range-control">
              <span>摇杆死区<strong>{deadzone.toFixed(2)}</strong></span>
              <input type="range" min="0" max="0.28" step="0.01" value={deadzone} onChange={(event) => setDeadzone(Number(event.target.value))} />
            </label>
            <label className={`range-control ${mode === "brush" ? "muted-control" : ""}`}>
              <span>角色响应速度<strong>{response.toFixed(0)} Hz</strong></span>
              <input disabled={mode === "brush"} type="range" min="4" max="16" step="1" value={response} onChange={(event) => setResponse(Number(event.target.value))} />
            </label>
          </div>

          <div className="control-section signal-section">
            <div className="section-label"><span>03</span>实时模拟信号</div>
            <div className="sticks"><Stick label="LEFT" value={snapshot.left} /><Stick label="RIGHT" value={snapshot.right} /></div>
            <div className="signal-line">
              <span>InputFrame #{String(snapshot.frame).padStart(4, "0")}</span>
              <code>lx {formatSignal(snapshot.left.x)} · ly {formatSignal(snapshot.left.y)} · rx {formatSignal(snapshot.right.x)}</code>
            </div>
          </div>

          <div className="action-row">
            <button className="primary-action" onClick={togglePlayback}>
              <span>{snapshot.running ? "Ⅱ" : "▶"}</span>{snapshot.running ? "暂停" : snapshot.complete ? "再次播放" : "发送信号"}
            </button>
            <button className="icon-action" onClick={reset} aria-label="重置轨迹" title="重置">↺</button>
            <button className="icon-action" disabled={snapshot.frameCount === 0} onClick={exportFrames} aria-label="导出输入帧 JSON" title="导出 JSON">↓</button>
          </div>
        </aside>
      </section>

      <section className="architecture">
        <div className="architecture-copy">
          <p className="eyebrow">WHY THE CARRIER DOESN&apos;T MATTER</p>
          <h2>信号是一种协议，载体只是接收器。</h2>
          <p>路径控制器只输出标准化数据。浏览器画布、测试角色或以后明确允许自动化的程序，都可以实现自己的接收适配层。</p>
        </div>
        <div className="pipeline" aria-label="爱心轨迹到接收载体的处理流程">
          <div><span>01</span><strong>爱心轨迹</strong><small>等弧长坐标点</small></div><b>→</b>
          <div><span>02</span><strong>路径控制器</strong><small>方向、误差、朝向</small></div><b>→</b>
          <div className="pipeline-highlight"><span>03</span><strong>InputFrame[]</strong><small>LX · LY · RX · 16.67ms</small></div><b>→</b>
          <div><span>04</span><strong>接收适配器</strong><small>画笔 / 角色 / 其他</small></div>
        </div>
      </section>

      <footer><span>* u 为页面相对单位，仅用于比较轨迹误差，不对应 Apex 实际距离。</span><strong>CONTROLLER LAB / LOCAL SIGNALS ONLY</strong></footer>
    </main>
  );
}

from __future__ import annotations

from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
import threading
from urllib.parse import parse_qs, urlparse

from .control import TaskControl
from .recorder import RunRecorder


_DASHBOARD_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Apex Runner 控制台</title>
  <style>
    :root { color-scheme: dark; --bg:#081019; --panel:#101d29; --line:#243747; --text:#e8f0f5; --muted:#8fa3b3; --cyan:#37d8d0; --orange:#ff7a45; --red:#ff5f68; }
    * { box-sizing:border-box; }
    body { margin:0; min-height:100vh; background:radial-gradient(circle at 15% 0%,#173043 0,#081019 42%); color:var(--text); font:14px/1.45 ui-monospace,SFMono-Regular,Consolas,monospace; }
    main { width:min(1180px,calc(100% - 28px)); margin:0 auto; padding:28px 0 48px; }
    header { display:flex; justify-content:space-between; align-items:end; gap:20px; margin-bottom:18px; }
    h1 { margin:0; font:700 clamp(24px,4vw,42px)/1 system-ui,sans-serif; letter-spacing:-.04em; }
    .kicker { margin:0 0 8px; color:var(--cyan); text-transform:uppercase; letter-spacing:.14em; }
    .updated { color:var(--muted); text-align:right; }
    .grid { display:grid; grid-template-columns:1.05fr .95fr; gap:14px; }
    .panel { min-width:0; border:1px solid var(--line); border-radius:16px; background:color-mix(in srgb,var(--panel) 92%,transparent); box-shadow:0 16px 40px #0005; overflow:hidden; }
    .panel h2 { margin:0; padding:14px 16px; border-bottom:1px solid var(--line); color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.12em; }
    .facts { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:1px; background:var(--line); }
    .fact { padding:16px; background:var(--panel); }
    .fact span { display:block; color:var(--muted); font-size:11px; margin-bottom:5px; }
    .fact strong { display:block; overflow-wrap:anywhere; font-size:16px; }
    .state { color:var(--cyan); }
    .paused { color:var(--orange); }
    .danger { color:var(--red); }
    .controls { display:flex; flex-wrap:wrap; gap:9px; padding:14px 16px; }
    button { border:1px solid var(--line); border-radius:9px; padding:9px 13px; background:#162838; color:var(--text); cursor:pointer; font:inherit; }
    button:hover { border-color:var(--cyan); }
    button[data-danger] { border-color:#6b333a; color:#ffabb0; }
    .shot { display:grid; min-height:260px; place-items:center; background:#05090d; }
    .shot img { display:block; width:100%; max-height:480px; object-fit:contain; }
    .shot p { color:var(--muted); }
    .events { height:420px; overflow:auto; padding:8px 14px 14px; }
    .event { display:grid; grid-template-columns:64px minmax(150px,.7fr) 1.3fr; gap:12px; padding:9px 2px; border-bottom:1px solid #1c2d3a; }
    .event time,.event pre { color:var(--muted); }
    .event pre { margin:0; white-space:pre-wrap; overflow-wrap:anywhere; font:11px/1.45 inherit; }
    .ocr { padding:14px 16px; color:var(--muted); white-space:pre-wrap; overflow-wrap:anywhere; }
    @media (max-width:800px) { .grid { grid-template-columns:1fr; } header { align-items:start; flex-direction:column; } .updated{text-align:left;} .event{grid-template-columns:55px 1fr}.event pre{grid-column:1/-1}.facts{grid-template-columns:1fr 1fr;} }
  </style>
</head>
<body>
<main>
  <header><div><p class="kicker">Local observer · 127.0.0.1</p><h1>Apex Runner</h1></div><div class="updated" id="updated">正在连接…</div></header>
  <div class="grid">
    <section class="panel">
      <h2>实时状态</h2>
      <div class="facts">
        <div class="fact"><span>运行状态</span><strong id="runtime">—</strong></div>
        <div class="fact"><span>画面语义</span><strong class="state" id="observed">—</strong></div>
        <div class="fact"><span>目标阶段</span><strong id="goal">—</strong></div>
        <div class="fact"><span>前台</span><strong id="foreground">—</strong></div>
        <div class="fact"><span>待确认动作</span><strong id="action">—</strong></div>
        <div class="fact"><span>暂停原因</span><strong id="reason">—</strong></div>
        <div class="fact"><span>OCR 规则</span><strong id="ocrRule">—</strong></div>
        <div class="fact"><span>清障动作</span><strong id="armed">—</strong></div>
      </div>
      <div class="controls">
        <button data-cmd="pause">暂停任务</button><button data-cmd="resume">继续任务</button>
        <button data-cmd="release-all">释放输入</button><button data-danger data-cmd="stop">停止任务</button>
      </div>
      <h2>最近 OCR</h2><div class="ocr" id="ocr">尚无 OCR 记录</div>
    </section>
    <section class="panel"><h2>最近关键截图</h2><div class="shot" id="shot"><p>尚无截图</p></div></section>
    <section class="panel" style="grid-column:1/-1"><h2>事件时间线</h2><div class="events" id="events"></div></section>
  </div>
</main>
<script>
const token=__TOKEN__;
const $=id=>document.getElementById(id);
let lastShot='';
function value(v){return v===null||v===undefined||v===''?'—':String(v)}
async function refresh(){
  try{
    const [statusRes,eventsRes]=await Promise.all([fetch('/api/status',{cache:'no-store'}),fetch('/api/events?after=0&limit=120',{cache:'no-store'})]);
    const status=await statusRes.json(), eventData=await eventsRes.json();
    $('runtime').textContent=value(status.runtimeState); $('runtime').className=/PAUSED|PENDING/.test(status.runtimeState||'')?'paused':'';
    $('observed').textContent=value(status.observedState); $('goal').textContent=value(status.goalStage);
    $('foreground').textContent=status.foreground?'Apex':'否'; $('action').textContent=value(status.pendingAction?.name);
    $('reason').textContent=value(status.pauseReason); $('ocrRule').textContent=value(status.ocr?.matchedRule);
    $('armed').textContent=status.task?.safeObstaclesArmed?'已武装':'仅观察'; $('armed').className=status.task?.safeObstaclesArmed?'danger':'paused';
    $('updated').textContent=value(status.updatedAt);
    $('ocr').textContent=status.ocr?JSON.stringify(status.ocr,null,2):'尚无 OCR 记录';
    const shot=status.lastScreenshot||'';
    if(shot&&shot!==lastShot){lastShot=shot;$('shot').innerHTML=`<img alt="最近关键截图" src="/api/screenshots/latest?t=${Date.now()}">`;}
    $('events').innerHTML=eventData.events.map(e=>`<div class="event"><time>${e.elapsedMs??0}ms</time><strong>${e.event}</strong><pre>${escapeHtml(JSON.stringify(e.payload||{},null,2))}</pre></div>`).join('');
  }catch(error){$('updated').textContent='连接失败：'+error.message}
}
function escapeHtml(s){return s.replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
document.querySelectorAll('[data-cmd]').forEach(button=>button.addEventListener('click',async()=>{await fetch('/api/tasks/current/'+button.dataset.cmd,{method:'POST',headers:{'X-Control-Token':token}});refresh()}));
refresh();setInterval(refresh,1000);
</script>
</body></html>"""


def read_event_page(
    path: Path,
    *,
    after: int = 0,
    limit: int = 100,
) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    if not path.exists():
        return events
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_id = int(record.get("seq", line_number))
        if event_id <= after:
            continue
        event_name = record.get("type", record.get("event", "UNKNOWN"))
        elapsed = record.get("elapsedMs")
        raw_payload = record.get("payload")
        payload = (
            raw_payload
            if isinstance(raw_payload, dict)
            else {
                key: value
                for key, value in record.items()
                if key not in {"event", "elapsedMs"}
            }
        )
        events.append(
            {
                "eventId": event_id,
                "elapsedMs": elapsed,
                "event": event_name,
                "payload": payload,
            }
        )
    return events[-min(500, max(1, limit)) :]


class LocalControlServer:
    def __init__(
        self,
        host: str,
        port: int,
        recorder: RunRecorder,
        control: TaskControl,
    ) -> None:
        if host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("阶段 B 控制台只允许绑定本机回环地址")
        self.host = host
        self.port = port
        self.recorder = recorder
        self.control = control
        self.token = secrets.token_urlsafe(24)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        if self._server is None:
            return f"http://{self.host}:{self.port}"
        host, port = self._server.server_address[:2]
        return f"http://{host}:{port}"

    def start(self) -> None:
        dashboard = _DASHBOARD_HTML.replace("__TOKEN__", json.dumps(self.token))
        recorder = self.recorder
        control = self.control
        token = self.token

        class Handler(BaseHTTPRequestHandler):
            server_version = "ApexLocalControl/0.1"

            def log_message(self, format: str, *args: object) -> None:
                return None

            def _headers(self, status: int, content_type: str, length: int) -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; style-src 'unsafe-inline'; script-src 'unsafe-inline'",
                )
                self.end_headers()

            def _send_bytes(self, status: int, payload: bytes, content_type: str) -> None:
                self._headers(status, content_type, len(payload))
                self.wfile.write(payload)

            def _send_json(self, status: int, payload: object) -> None:
                self._send_bytes(
                    status,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    "application/json; charset=utf-8",
                )

            def do_GET(self) -> None:
                parsed = urlparse(self.path)
                if parsed.path == "/":
                    self._send_bytes(
                        HTTPStatus.OK,
                        dashboard.encode("utf-8"),
                        "text/html; charset=utf-8",
                    )
                    return
                if parsed.path == "/api/status":
                    if not recorder.status_path.exists():
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"runtimeState": "STARTING"})
                        return
                    try:
                        payload = json.loads(recorder.status_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError) as error:
                        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)})
                        return
                    self._send_json(HTTPStatus.OK, payload)
                    return
                if parsed.path == "/api/events":
                    query = parse_qs(parsed.query)
                    after = max(0, int(query.get("after", ["0"])[0]))
                    limit = min(500, max(1, int(query.get("limit", ["100"])[0])))
                    self._send_json(
                        HTTPStatus.OK,
                        {
                            "events": read_event_page(
                                recorder.events_path,
                                after=after,
                                limit=limit,
                            )
                        },
                    )
                    return
                if parsed.path == "/api/screenshots/latest":
                    try:
                        status = json.loads(recorder.status_path.read_text(encoding="utf-8"))
                        raw_path = status.get("lastScreenshot")
                        if not raw_path:
                            raise FileNotFoundError("尚无截图")
                        path = Path(str(raw_path)).resolve()
                        path.relative_to(recorder.run_dir.resolve())
                        payload = path.read_bytes()
                    except (OSError, ValueError, json.JSONDecodeError) as error:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": str(error)})
                        return
                    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                    self._send_bytes(HTTPStatus.OK, payload, content_type)
                    return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

            def do_POST(self) -> None:
                if not secrets.compare_digest(self.headers.get("X-Control-Token", ""), token):
                    self._send_json(HTTPStatus.FORBIDDEN, {"error": "invalid control token"})
                    return
                request_path = urlparse(self.path).path
                command = request_path.rsplit("/", 1)[-1]
                if request_path == "/api/input/release-all":
                    command = "release-all"
                if command == "pause":
                    control.pause()
                elif command == "resume":
                    control.resume()
                elif command == "stop":
                    control.stop()
                elif command == "release-all":
                    control.request_release()
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown command"})
                    return
                self._send_json(HTTPStatus.ACCEPTED, {"accepted": command})

        self._server = ThreadingHTTPServer((self.host, self.port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="apex-local-control",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._server = None
        self._thread = None

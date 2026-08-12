"""Atlas 极简 Dashboard：HTTP 三面板 + WebSocket 推送。

组件:
    DATASTATE           模块级全局共享状态（runs/events/trace_nodes/trace_edges）
    publish()           用户代码主动把 (run_id, journal, trace) 转成 JSON-safe dict 写入 DATASTATE
    run_server()        启动 HTTP(默认 8765) + WebSocket(默认 8766)，WebSocket 每 500ms 推 DATASTATE

前端三面板（HTML 字符串内嵌，免读外部文件）:
    左面板: Run 列表（run_id / status / event_count / step_count）
    中面板: Trace DAG（StepRecord 卡片 + 子 Trace 缩进，父子用竖线表示关系）
    右面板: Event 日志（最近 200 条，按 event_type 着色）
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..context.trace import Trace
    from ..journal.base import JournalProtocol


# =============================================================
# 全局共享状态 + publish API
# =============================================================

_DATASTATE_LOCK = threading.Lock()
DATASTATE: dict[str, Any] = {
    "runs": [],
    "events": [],
    "trace_nodes": [],
    "trace_edges": [],
    "last_publish_ts": None,
}


def _summarize_event_payload(payload: dict) -> str:
    """把 payload dict 压成 80 字预览，用于右面板事件摘要。"""
    if not payload:
        return ""
    try:
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    except Exception:
        text = str(payload)
    return text[:80] + ("..." if len(text) > 80 else "")


def _trace_to_graph(trace: "Trace", _prefix: str = "root", _parent_node_id: str | None = None) -> tuple[list[dict], list[dict]]:
    """把 Trace（含嵌套 subagent_traces）递归打平成 nodes[] + edges[]。"""
    nodes: list[dict] = []
    edges: list[dict] = []

    for idx, step in enumerate(trace.steps):
        node_id = f"{_prefix}_step_{idx}"
        preview = step.thought[:60] if step.thought else "(无 thought)"
        nodes.append({
            "id": node_id,
            "parent_id": _parent_node_id,
            "kind": "step",
            "step_index": idx + 1,
            "thought_preview": preview,
            "tool_count": len(step.tool_calls),
            "tool_names": [
                (c.get("function") or {}).get("name", "?")
                for c in step.tool_calls[:5]
            ],
            "has_observations": any(step.observations),
        })
        if idx == 0 and _parent_node_id:
            # 子 Trace 的第一步连到 parent 节点
            edges.append({"from": _parent_node_id, "to": node_id, "kind": "spawn"})
        elif idx > 0:
            # 同 Trace 的 step 顺序连接
            prev_id = f"{_prefix}_step_{idx - 1}"
            edges.append({"from": prev_id, "to": node_id, "kind": "next"})

    if trace.steps:
        last_step_in_trace = f"{_prefix}_step_{len(trace.steps) - 1}"
    else:
        last_step_in_trace = _parent_node_id

    for sub_i, sub in enumerate(trace.subagent_traces):
        sub_prefix = f"{_prefix}_sub{sub_i}"
        sub_role = (sub.meta or {}).get("role", f"subagent-{sub_i}")
        sub_nodes, sub_edges = _trace_to_graph(sub, sub_prefix, last_step_in_trace)
        nodes.append({
            "id": sub_prefix,
            "parent_id": last_step_in_trace,
            "kind": "sub_header",
            "role": sub_role,
            "step_count": len(sub.steps),
        })
        if last_step_in_trace:
            edges.append({"from": last_step_in_trace, "to": sub_prefix, "kind": "spawn"})
        # 子 Trace 的第一步应挂在 sub_header 之下（覆盖递归传入的 _parent_node_id）
        for n in sub_nodes:
            if n["kind"] == "step" and n["parent_id"] == last_step_in_trace:
                n["parent_id"] = sub_prefix
                for e in sub_edges:
                    if e["to"] == n["id"] and e["kind"] == "spawn":
                        e["from"] = sub_prefix
        nodes.extend(sub_nodes)
        edges.extend(sub_edges)

    return nodes, edges


def publish(run_id: str, journal: "JournalProtocol | None", trace: "Trace | None") -> None:
    """用户代码调：把 run/journal/trace 快照序列化到全局 DATASTATE。

    线程安全：DATASTATE 每次全量覆盖（单写多读 + threading.Lock 双保险）。
    """
    runs: list[dict] = []
    events: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []

    if journal:
        all_run_ids = [run_id]
        try:
            all_run_ids = list(journal.list_run_ids())
            if run_id not in all_run_ids:
                all_run_ids.append(run_id)
        except Exception:
            # list_run_ids 失败不抛错（JournalProtocol 实现不完整时兜底，但用户约定兜底=不造，所以仅 try/except 防崩）
            pass

        for rid in all_run_ids:
            evs = journal.get_events(rid)
            status = "running"
            start_ts = None
            end_ts = None
            step_count = 0
            for e in evs:
                if e.event_type == "agent_start":
                    start_ts = e.ts
                if e.event_type == "agent_finish":
                    end_ts = e.ts
                    status = (e.payload or {}).get("status", "finished")
                if e.event_type == "step_finish":
                    step_count += 1
            runs.append({
                "run_id": rid,
                "status": status,
                "event_count": len(evs),
                "step_count": step_count,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "is_current": rid == run_id,
            })

            if rid == run_id:
                # 只推送当前 run 的 events（右面板），最多 200 条
                for e in evs[-200:]:
                    events.append({
                        "seq": e.seq,
                        "ts": e.ts,
                        "event_type": e.event_type,
                        "summary": _summarize_event_payload(e.payload or {}),
                    })

    if trace:
        nodes, edges = _trace_to_graph(trace)

    with _DATASTATE_LOCK:
        DATASTATE["runs"] = runs
        DATASTATE["events"] = events
        DATASTATE["trace_nodes"] = nodes
        DATASTATE["trace_edges"] = edges
        DATASTATE["last_publish_ts"] = time.time()


def _snapshot() -> dict:
    """WebSocket 推送时拿一份锁保护的深拷贝。"""
    with _DATASTATE_LOCK:
        return json.loads(json.dumps(DATASTATE, ensure_ascii=False))


# =============================================================
# HTTP：三面板 HTML
# =============================================================

_HTML_TEMPLATE = r"""
<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"/>
<title>Atlas Dashboard</title>
<style>
* { box-sizing: border-box; }
html, body { margin:0; padding:0; height:100vh; font-family: Consolas, Menlo, monospace; background:#0f1117; color:#e6e6e6; }
.header { padding:10px 14px; border-bottom:1px solid #2a2d3a; background:#161922; display:flex; justify-content:space-between; align-items:center; }
.header h1 { margin:0; font-size:15px; color:#8aa4ff; font-weight:600; letter-spacing:1px; }
.header .status { font-size:12px; color:#7a7d89; }
.columns { display:flex; height: calc(100vh - 41px); }
.col { overflow:auto; padding:10px 12px; }
.col-left  { width:22%; border-right:1px solid #2a2d3a; }
.col-mid   { width:48%; border-right:1px solid #2a2d3a; background:#13151e; }
.col-right { width:30%; }
.col-title { font-size:12px; color:#8aa4ff; margin-bottom:8px; letter-spacing:0.5px; border-left:3px solid #8aa4ff; padding-left:6px; }

.run-item { padding:8px 10px; margin-bottom:6px; background:#1a1d29; border-left:3px solid #555; cursor:pointer; font-size:12px; border-radius:2px; }
.run-item.current { border-left-color:#8aa4ff; background:#1e2538; }
.run-item .meta { color:#7a7d89; font-size:11px; margin-top:3px; }
.run-item .badge { display:inline-block; padding:1px 6px; border-radius:8px; font-size:10px; margin-left:4px; }
.badge.running  { background:#2d4030; color:#7be495; }
.badge.finished { background:#2a3451; color:#8aa4ff; }
.badge.failed   { background:#4a2a2a; color:#ff8a8a; }

.step-node { margin:6px 0 6px 16px; padding:8px 10px; background:#1a1d29; border-left:3px solid #5c6370; border-radius:2px; position:relative; }
.step-node .step-head { font-size:12px; color:#c8ccd4; margin-bottom:4px; }
.step-node .thought { font-size:11px; color:#9ba1ad; line-height:1.5; white-space:pre-wrap; word-break:break-word; }
.step-node .tools { font-size:11px; color:#7be495; margin-top:4px; }
.step-node.spawn { border-left-color:#c678dd; }
.sub-header { margin:10px 0 6px 16px; padding:6px 10px; background:#261c33; border-left:3px solid #c678dd; border-radius:2px; font-size:12px; color:#d7a3f0; }
.sub-header .meta { font-size:11px; color:#9a86a8; margin-top:2px; }

.edge-hint { margin-left:20px; border-left:1px dashed #3d4150; padding-left:8px; }

.event-row { font-size:11px; padding:3px 4px; border-bottom:1px solid #1e2130; line-height:1.4; word-break:break-word; }
.event-row .seq { color:#5c6370; display:inline-block; width:28px; }
.event-row .etype { display:inline-block; width:90px; font-weight:bold; }
.event-row .t-llm_call    .etype { color:#8aa4ff; }
.event-row .t-tool_call   .etype { color:#7be495; }
.event-row .t-tool_result .etype { color:#56b6c2; }
.event-row .t-agent_start .etype { color:#e5c07b; }
.event-row .t-agent_finish .etype { color:#c678dd; }
.event-row .t-step_finish .etype { color:#abb2bf; }
.event-row .t-subagent_spawn .etype { color:#ff8a8a; }
.empty-hint { color:#5c6370; font-size:12px; padding:20px; text-align:center; }
</style></head><body>

<div class="header">
  <h1>◆ Atlas Dashboard</h1>
  <div class="status" id="status">connecting…</div>
</div>
<div class="columns">
  <div class="col col-left">
    <div class="col-title">RUNS</div>
    <div id="run-list"><div class="empty-hint">(无数据 — 请先 publish())</div></div>
  </div>
  <div class="col col-mid">
    <div class="col-title">TRACE DAG</div>
    <div id="dag-area"><div class="empty-hint">(等待 Trace 数据)</div></div>
  </div>
  <div class="col col-right">
    <div class="col-title">EVENTS (最近 200 条)</div>
    <div id="event-list"><div class="empty-hint">(等待事件数据)</div></div>
  </div>
</div>

<script>
const WS_PORT = location.port ? (parseInt(location.port, 10) + 1) : 8766;
const wsUrl = 'ws://' + location.hostname + ':' + WS_PORT;
let ws = null;
let lastData = null;

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function renderRuns(runs) {
  const box = document.getElementById('run-list');
  if (!runs || !runs.length) { box.innerHTML = '<div class="empty-hint">(无 runs)</div>'; return; }
  box.innerHTML = runs.map(r => {
    const cls = r.is_current ? 'run-item current' : 'run-item';
    const badgeClass = ({running:'running',finished:'finished',failed:'failed'})[r.status] || 'finished';
    return `<div class="${cls}">
      <div>${esc(r.run_id)} <span class="badge ${badgeClass}">${esc(r.status)}</span></div>
      <div class="meta">steps=${r.step_count} &nbsp; events=${r.event_count}</div>
      <div class="meta">${esc(r.start_ts || '-')} → ${esc(r.end_ts || '…')}</div>
    </div>`;
  }).join('');
}

function renderDag(nodes) {
  const box = document.getElementById('dag-area');
  if (!nodes || !nodes.length) { box.innerHTML = '<div class="empty-hint">(空 Trace)</div>'; return; }
  // 用 parent_id 算缩进：先建树
  const byParent = {};
  nodes.forEach(n => { (byParent[n.parent_id || ''] = byParent[n.parent_id || ''] || []).push(n); });
  // BFS 算每个节点深度
  const depth = {};
  const queue = nodes.filter(n => !n.parent_id).map(n => [n.id, 0]);
  depth[queue[0]?.[0] || 'root'] = 0;
  const seen = new Set();
  while (queue.length) {
    const [id, d] = queue.shift();
    if (seen.has(id)) continue;
    seen.add(id);
    depth[id] = d;
    (byParent[id] || []).forEach(child => queue.push([child.id, d + 1]));
  }
  // 按 nodes 原始顺序输出
  box.innerHTML = nodes.map(n => {
    const d = depth[n.id] ?? 0;
    const pad = `${d * 18}px`;
    if (n.kind === 'sub_header') {
      return `<div class="sub-header" style="margin-left:${pad}">
        ▸ SUB: <b>${esc(n.role)}</b>
        <div class="meta">${n.step_count} steps</div>
      </div>`;
    }
    const toolStr = (n.tool_names || []).length
      ? `<div class="tools">🛠 ${esc(n.tool_names.join(', '))}${n.tool_count > n.tool_names.length ? ' …+' + (n.tool_count - n.tool_names.length) : ''}</div>`
      : '';
    return `<div class="step-node" style="margin-left:${pad}">
      <div class="step-head">Step ${n.step_index}${n.has_observations ? ' · ✓ obs' : ''}</div>
      <div class="thought">${esc(n.thought_preview)}</div>
      ${toolStr}
    </div>`;
  }).join('');
}

function renderEvents(events) {
  const box = document.getElementById('event-list');
  if (!events || !events.length) { box.innerHTML = '<div class="empty-hint">(无事件)</div>'; return; }
  box.innerHTML = events.map(e => {
    const cls = 'event-row t-' + (e.event_type || '').toLowerCase().replace(/[^a-z0-9]/g, '_');
    return `<div class="${cls}">
      <span class="seq">#${e.seq}</span>
      <span class="etype">${esc(e.event_type)}</span>
      <span>${esc(e.summary)}</span>
    </div>`;
  }).join('');
  box.scrollTop = box.scrollHeight;
}

function update(data) {
  lastData = data;
  document.getElementById('status').textContent =
    '✓ live · last=' + (data.last_publish_ts ? new Date(data.last_publish_ts * 1000).toLocaleTimeString() : '-');
  renderRuns(data.runs);
  renderDag(data.trace_nodes);
  renderEvents(data.events);
}

function connect() {
  ws = new WebSocket(wsUrl);
  ws.onopen = () => { document.getElementById('status').textContent = '✓ connected'; };
  ws.onmessage = ev => { try { update(JSON.parse(ev.data)); } catch (e) { console.error(e); } };
  ws.onclose = () => {
    document.getElementById('status').textContent = '✗ disconnected — 3s 后重连…';
    setTimeout(connect, 3000);
  };
  ws.onerror = () => ws && ws.close();
}
connect();
</script></body></html>
"""

_INDEX_HTML = _HTML_TEMPLATE  # 对外常量别名


class _DashboardHTTPHandler(BaseHTTPRequestHandler):
    """只响应 GET / → 返回 HTML；其他路径 404。"""

    def do_GET(self) -> None:  # noqa: N802（HTTP handler 命名规范）
        if self.path in ("/", "/index.html"):
            body = _INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()
        self.wfile.write(b"Not Found")

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: N802（重载基类，静音 HTTP 日志）
        return


def _start_http_server(port: int) -> ThreadingHTTPServer:
    """在当前线程阻塞启动 HTTP server（通常丢到子线程）。"""
    server = ThreadingHTTPServer(("0.0.0.0", port), _DashboardHTTPHandler)
    return server


# =============================================================
# WebSocket：每 500ms 推一次 DATASTATE snapshot
# =============================================================

async def _ws_handler(websocket: Any) -> None:
    try:
        while True:
            snapshot = _snapshot()
            try:
                await websocket.send(json.dumps(snapshot, ensure_ascii=False))
            except Exception:
                # 客户端断连
                return
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        return
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


async def _ws_main(port: int) -> None:
    import websockets

    async with websockets.serve(_ws_handler, "0.0.0.0", port):
        await asyncio.Future()  # 永不退出


# =============================================================
# 入口：run_server(http_port=8765)
# =============================================================

def run_server(http_port: int = 8765, ws_port: int | None = None) -> None:
    """阻塞启动 Dashboard：HTTP(http_port) 子线程 + WebSocket(ws_port=http_port+1) 当前线程。

    用法（demo 后台起）：
        import threading
        threading.Thread(target=lambda: run_server(8765), daemon=True).start()
    """
    if ws_port is None:
        ws_port = http_port + 1

    http_server = _start_http_server(http_port)
    threading.Thread(target=http_server.serve_forever, daemon=True, name="atlas-dashboard-http").start()

    print(f"[Atlas Dashboard] HTTP      → http://localhost:{http_port}")
    print(f"[Atlas Dashboard] WebSocket → ws://localhost:{ws_port}    (每 500ms 推送 DATASTATE snapshot)")
    print(f"[Atlas Dashboard] 请在用户代码中调用 atlas.cli.dashboard.publish(run_id, journal, trace) 推送数据")

    try:
        asyncio.run(_ws_main(ws_port))
    except KeyboardInterrupt:
        print("[Atlas Dashboard] 收到 Ctrl+C，停止服务")
        http_server.shutdown()

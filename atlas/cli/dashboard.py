"""Atlas 极简 Dashboard：HTTP 切页 + WebSocket 推送。

组件:
    DATASTATE           模块级全局共享状态（runs/events/trace + evolution）
    publish()           用户代码主动把 (run_id, journal, trace) 写入 DATASTATE（不覆盖 evolution）
    publish_evolution() 把 EvolutionSummary 快照写入 DATASTATE.evolution
    register_evolution() 同进程挂活 Agent，供 POST /rsi/rollback
    run_server()        启动 HTTP(默认 8765) + WebSocket(默认 8766)

前端切页（HTML 字符串内嵌，免读外部文件）:
    运行: 左 Run 列表 / 中 Trace DAG / 右 Event 日志 + RESILIENCE 横幅
    进化: fitness 曲线 / 每代 diff / 当前 prompt + 回滚按钮
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
    from ..circuit_breaker import CircuitBreakerRegistry
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
    "circuit_breakers": [],
    "retry_logs": [],
    "alerts": [],
    "evolution": None,
    "last_publish_ts": None,
}

# 同进程活 Agent，供 POST /rsi/rollback 调用 rollback_to。未 register 则为 None。
_EVOLUTION_AGENT: Any | None = None


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


def publish(
    run_id: str,
    journal: "JournalProtocol | None",
    trace: "Trace | None",
    circuit_breaker_registry: "CircuitBreakerRegistry | None" = None,
) -> None:
    """用户代码调：把 run/journal/trace + CircuitBreakerRegistry 快照序列化到全局 DATASTATE。

    线程安全：DATASTATE 每次全量覆盖（单写多读 + threading.Lock 双保险）。
    """
    runs: list[dict] = []
    events: list[dict] = []
    nodes: list[dict] = []
    edges: list[dict] = []
    circuit_breakers: list[dict] = []
    retry_logs: list[dict] = []
    alerts: list[dict] = []

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

    if circuit_breaker_registry is not None:
        try:
            snap = circuit_breaker_registry.snapshot()
            circuit_breakers = snap.get("circuit_breakers", [])
            retry_logs = snap.get("retry_logs", [])
            alerts = snap.get("alerts", [])
        except Exception:
            # snapshot 异常绝不阻塞 publish
            pass

    with _DATASTATE_LOCK:
        DATASTATE["runs"] = runs
        DATASTATE["events"] = events
        DATASTATE["trace_nodes"] = nodes
        DATASTATE["trace_edges"] = edges
        DATASTATE["circuit_breakers"] = circuit_breakers
        DATASTATE["retry_logs"] = retry_logs
        DATASTATE["alerts"] = alerts
        DATASTATE["last_publish_ts"] = time.time()
        # 不覆盖 evolution：运行页 publish 与进化页快照独立


def _read_system_prompt(agent: Any) -> str:
    """读 Agent 当前 system_prompt。缺 memory / 非 system 首条则失败。"""
    memory = getattr(agent, "memory", None)
    if memory is None:
        raise RuntimeError(
            "agent.memory 未注入，无法读取 system_prompt。恢复：构造 Agent 时传入 system_prompt"
        )
    messages = memory.runtime_messages
    if not messages or messages[0].get("role") != "system":
        raise RuntimeError(
            "memory.runtime_messages[0] 不是 system 消息。恢复：构造 Agent 时传入非空 system_prompt"
        )
    return str(messages[0].get("content") or "")


def register_evolution(agent: Any) -> None:
    """把正在进化的 Agent 挂到 Dashboard 进程，使回滚按钮可写。

    Args:
        agent: 必须已 bind RSI（含 archive_store），与 Dashboard 同进程。
    """
    global _EVOLUTION_AGENT
    if agent is None:
        raise ValueError(
            "register_evolution 缺少 agent。恢复：传入正在进化的 Agent 实例"
        )
    _EVOLUTION_AGENT = agent
    with _DATASTATE_LOCK:
        evo = DATASTATE.get("evolution")
        if isinstance(evo, dict):
            evo["can_rollback"] = True


def publish_evolution(agent: Any, summary: Any) -> None:
    """把一次进化跑的 summary 投影到 DATASTATE.evolution，供进化页渲染。

    Args:
        agent: 用于读当前 generation / system_prompt。
        summary: ``EvolutionSummary``，generations 不可为空。
    """
    from ..rsi.evolution_report import generation_records
    from ..rsi.prompt_evolver import EvolutionSummary

    if agent is None:
        raise ValueError(
            "publish_evolution 缺少 agent。恢复：传入正在进化的 Agent 实例"
        )
    if not isinstance(summary, EvolutionSummary):
        raise TypeError(
            "summary 必须是 EvolutionSummary。恢复：先调用 evolve_prompt_until_converged()"
        )
    if not summary.generations:
        raise ValueError(
            "summary.generations 为空。恢复：先跑 evolve_prompt_until_converged 再 publish_evolution"
        )

    payload = {
        "current_generation": int(getattr(agent, "generation", 0)),
        "current_prompt": _read_system_prompt(agent),
        "stop_reason": summary.stop_reason,
        "final_generation": summary.final_generation,
        "final_score": summary.final_score,
        "can_rollback": _EVOLUTION_AGENT is not None,
        "generations": generation_records(summary),
    }
    baseline = getattr(agent, "baseline_report", None)
    if baseline is not None:
        if not isinstance(baseline, dict) or "score" not in baseline:
            raise ValueError(
                "agent.baseline_report 必须是含 score 的 dict。"
                "恢复：真模型路径在进化前写入 {score, prompt, task_results}"
            )
        payload["baseline"] = baseline
    with _DATASTATE_LOCK:
        DATASTATE["evolution"] = payload
        DATASTATE["last_publish_ts"] = time.time()


def _refresh_evolution_live(agent: Any) -> None:
    """回滚成功后只刷新 live 字段，保留 generations 历史。"""
    with _DATASTATE_LOCK:
        evo = DATASTATE.get("evolution")
        if not isinstance(evo, dict):
            raise RuntimeError(
                "DATASTATE.evolution 为空，无法刷新回滚结果。恢复：先 publish_evolution(agent, summary)"
            )
        evo["current_generation"] = int(getattr(agent, "generation", 0))
        evo["current_prompt"] = _read_system_prompt(agent)
        evo["can_rollback"] = _EVOLUTION_AGENT is not None
        DATASTATE["last_publish_ts"] = time.time()


def _handle_rollback_request(generation: int) -> tuple[int, dict[str, Any]]:
    """执行回滚。返回 (http_status, body_dict)。"""
    agent = _EVOLUTION_AGENT
    if agent is None:
        return 409, {
            "ok": False,
            "error": (
                "未 register_evolution(agent)，无法回滚。"
                "恢复：进化与 Dashboard 放在同一进程，先 register_evolution(agent) 再点回滚"
            ),
        }
    try:
        agent.rollback_to(generation)
    except (ValueError, RuntimeError) as exc:
        return 409, {"ok": False, "error": str(exc)}
    _refresh_evolution_live(agent)
    return 200, {
        "ok": True,
        "generation": int(agent.generation),
        "current_prompt": _read_system_prompt(agent),
    }


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
.header { padding:10px 14px; border-bottom:1px solid #2a2d3a; background:#161922; display:flex; justify-content:space-between; align-items:center; gap:12px; }
.header h1 { margin:0; font-size:15px; color:#8aa4ff; font-weight:600; letter-spacing:1px; }
.header .status { font-size:12px; color:#7a7d89; }
.tabs { display:flex; gap:6px; }
.tab-btn { font-family: inherit; font-size:12px; padding:4px 12px; background:#1a1d29; color:#abb2bf; border:1px solid #2a2d3a; border-radius:2px; cursor:pointer; }
.tab-btn.active { background:#1e2538; color:#8aa4ff; border-color:#8aa4ff; }
.page { display:none; }
.page.active { display:block; }
.columns { display:flex; height: calc(100vh - 49px); }
/* ================ Phase 2.2 RESILIENCE 横幅（header 下 columns 上） ================ */
.banner { padding:8px 14px; background:#181c28; border-bottom:1px solid #2a2d3a; display:flex; gap:10px; align-items:flex-start; }
.banner-title { writing-mode: vertical-lr; transform: rotate(180deg); font-size:11px; color:#ff6b6b; letter-spacing:2px; font-weight:700; padding:4px 2px; border-left:2px solid #ff6b6b; user-select:none; min-width:14px; }
.banner .tbl-wrap { flex:1; min-width:0; background:#0f1117; border:1px solid #2a2d3a; border-radius:3px; }
.banner .tbl-wrap h3 { margin:0; padding:4px 8px; font-size:11px; color:#abb2bf; background:#1a1d29; border-bottom:1px solid #2a2d3a; letter-spacing:0.5px; }
.banner table { width:100%; border-collapse:collapse; font-size:10.5px; }
.banner th, .banner td { padding:3px 7px; text-align:left; border-bottom:1px solid #1e2130; word-break:break-all; }
.banner th { background:#161922; color:#8aa4ff; font-weight:600; position:sticky; top:0; }
.banner .scroller { max-height:110px; overflow:auto; }
/* 熔断器状态颜色 */
.st-open   { background:#4a2a2a !important; color:#ff8a8a !important; font-weight:700; }
.st-half   { background:#4a4020 !important; color:#e5c07b !important; font-weight:600; }
.st-closed { background:#2a3a2c !important; color:#7be495 !important; }
/* 告警严重度 */
.alert-critical { color:#ff6b6b; font-weight:700; }
.alert-warning  { color:#e5c07b; font-weight:600; }
#page-run .columns { display:flex; height: calc(100vh - 49px - 138px); }
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
.evo-columns { height: calc(100vh - 49px); }
.gen-card { padding:8px 10px; margin-bottom:8px; background:#1a1d29; border-left:3px solid #555; border-radius:2px; font-size:12px; }
.gen-card.applied { border-left-color:#7be495; }
.gen-card.failed { border-left-color:#ff8a8a; }
.gen-card .meta { color:#7a7d89; font-size:11px; margin-top:3px; }
.gen-card pre { margin:8px 0 0; padding:8px; background:#0f1117; font-size:11px; white-space:pre-wrap; word-break:break-word; max-height:220px; overflow:auto; }
.prompt-box { font-size:12px; white-space:pre-wrap; word-break:break-word; background:#1a1d29; padding:8px 10px; border-radius:2px; max-height:240px; overflow:auto; }
.rb-row { display:flex; flex-wrap:wrap; gap:6px; margin:10px 0; }
.rb-btn { font-family: inherit; font-size:12px; padding:4px 10px; background:#1e2538; color:#8aa4ff; border:1px solid #3d4a6e; border-radius:2px; cursor:pointer; }
.rb-btn:disabled { opacity:0.4; cursor:not-allowed; }
.rb-hint { font-size:11px; color:#e5c07b; margin-top:6px; }
.rb-err { font-size:11px; color:#ff8a8a; margin-top:6px; }
.task-row { margin:6px 0; padding:6px 8px; background:#1a1d29; border-radius:2px; }
.task-row .task-head { font-size:11px; margin-bottom:4px; }
.task-row.pass .task-head { color:#7be495; }
.task-row.fail .task-head { color:#ff8a8a; }
.task-out { font-size:11px; color:#abb2bf; white-space:pre-wrap; word-break:break-word; max-height:90px; overflow:auto; }
.gen-card details { margin-top:6px; color:#7a7d89; font-size:11px; }
.gen-card details pre { margin-top:4px; }
</style></head><body>

<div class="header">
  <h1>◆ Atlas Dashboard</h1>
  <div class="tabs">
    <button type="button" class="tab-btn active" id="tab-run" data-page="page-run">运行</button>
    <button type="button" class="tab-btn" id="tab-evolve" data-page="page-evolve">进化</button>
  </div>
  <div class="status" id="status">connecting…</div>
</div>

<div id="page-run" class="page active">
<!------------ Phase 2.2 RESILIENCE 横幅：三表并排 ------------>
<div class="banner">
  <div class="banner-title">RESILIENCE</div>
  <div class="tbl-wrap">
    <h3>① 熔断器状态 (CIRCUIT BREAKERS)</h3>
    <div class="scroller"><table>
      <thead><tr><th>CIRCUIT KEY</th><th>STATE</th><th>FAIL COUNT</th><th>OPEN 剩余(s)</th><th>LAST CHANGE</th></tr></thead>
      <tbody id="tbl-circuit-body"><tr><td colspan="5" class="empty-hint">(无熔断器数据)</td></tr></tbody>
    </table></div>
  </div>
  <div class="tbl-wrap">
    <h3>② 最近重试记录 (RETRY LOGS)</h3>
    <div class="scroller"><table>
      <thead><tr><th>TIME</th><th>TOOL</th><th>#</th><th>ERROR</th><th>DELAY(ms)</th></tr></thead>
      <tbody id="tbl-retry-body"><tr><td colspan="5" class="empty-hint">(无重试记录)</td></tr></tbody>
    </table></div>
  </div>
  <div class="tbl-wrap">
    <h3>③ 活动告警 (ALERTS)</h3>
    <div class="scroller"><table>
      <thead><tr><th>LEVEL</th><th>MESSAGE</th><th>SINCE</th></tr></thead>
      <tbody id="tbl-alerts-body"><tr><td colspan="3" class="empty-hint">(无告警)</td></tr></tbody>
    </table></div>
  </div>
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
</div>

<div id="page-evolve" class="page">
  <div class="columns evo-columns">
    <div class="col col-left">
      <div class="col-title">FITNESS 曲线</div>
      <div id="fitness-chart"><div class="empty-hint">(无进化数据 — 请先 publish_evolution())</div></div>
    </div>
    <div class="col col-mid">
      <div class="col-title">每代效果</div>
      <div id="evo-gens"><div class="empty-hint">(等待进化数据)</div></div>
    </div>
    <div class="col col-right">
      <div class="col-title">当前 PROMPT / 回滚</div>
      <div id="evo-live"><div class="empty-hint">(等待进化数据)</div></div>
    </div>
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

/* -------- Phase 2.2 RESILIENCE 横幅渲染 -------- */
function stateCls(s) {
  s = String(s || '').toLowerCase();
  if (s === 'open') return 'st-open';
  if (s === 'half_open' || s === 'half-open') return 'st-half';
  return 'st-closed';
}
function stateTxt(s) {
  s = String(s || '').toLowerCase();
  if (s === 'open') return '● OPEN';
  if (s === 'half_open' || s === 'half-open') return '◐ HALF-OPEN';
  return '○ CLOSED';
}
function fmtTs(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString() + '.' + String(d.getMilliseconds()).padStart(3,'0');
}
function renderCircuit(rows) {
  const tb = document.getElementById('tbl-circuit-body');
  if (!rows || !rows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty-hint">(无熔断器数据)</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => `<tr>
    <td>${esc(r.key)}</td>
    <td class="${stateCls(r.state)}">${stateTxt(r.state)}</td>
    <td>${r.failure_count ?? 0}</td>
    <td>${r.open_remain_secs ?? 0}</td>
    <td>${fmtTs(r.last_state_change_ts)}</td>
  </tr>`).join('');
}
function renderRetry(rows) {
  const tb = document.getElementById('tbl-retry-body');
  if (!rows || !rows.length) {
    tb.innerHTML = '<tr><td colspan="5" class="empty-hint">(无重试记录)</td></tr>';
    return;
  }
  tb.innerHTML = rows.slice(-50).reverse().map(r => `<tr>
    <td>${fmtTs(r.ts)}</td>
    <td>${esc(r.tool_name || '')}</td>
    <td>#${r.attempt ?? '?'}</td>
    <td>${esc(r.error_type || '')}</td>
    <td>${r.next_delay_ms ?? '-'}</td>
  </tr>`).join('');
}
function renderAlerts(rows) {
  const tb = document.getElementById('tbl-alerts-body');
  if (!rows || !rows.length) {
    tb.innerHTML = '<tr><td colspan="3" class="empty-hint">(无告警)</td></tr>';
    return;
  }
  tb.innerHTML = rows.map(r => {
    const cls = String(r.level || '').toLowerCase() === 'critical' ? 'alert-critical' : 'alert-warning';
    return `<tr>
      <td class="${cls}">${esc(r.level || 'info').toUpperCase()}</td>
      <td>${esc(r.message || '')}</td>
      <td>${fmtTs(r.since_ts)}</td>
    </tr>`;
  }).join('');
}

function switchPage(pageId) {
  document.querySelectorAll('.page').forEach(p => p.classList.toggle('active', p.id === pageId));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.getAttribute('data-page') === pageId));
}
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => switchPage(btn.getAttribute('data-page')));
});

let autoSwitchedToEvolve = false;
let rollbackBusy = false;
let lastEvoJson = '';

function renderFitness(evo) {
  const box = document.getElementById('fitness-chart');
  const gens = (evo && evo.generations) || [];
  if (!gens.length) {
    box.innerHTML = '<div class="empty-hint">(无进化数据 — 请先 publish_evolution())</div>';
    return;
  }
  const hasVal = evo.baseline && evo.baseline.val_score != null;
  const trainSrc = [];
  const valSrc = [];
  if (evo.baseline) {
    trainSrc.push({ attempt: 0, score: evo.baseline.score, applied: null, kind: 'baseline' });
    if (hasVal) valSrc.push({ attempt: 0, score: evo.baseline.val_score, applied: null, kind: 'baseline' });
  }
  gens.forEach(g => {
    trainSrc.push(g);
    if (hasVal && g.val_score != null) valSrc.push({ attempt: g.attempt, score: g.val_score, applied: g.applied });
  });
  const w = 280, h = 180, pad = 28;
  const maxX = Math.max(trainSrc.length - 1, 1);
  const toPts = (src) => src.map((g, i) => {
    const x = pad + (i / maxX) * (w - 2 * pad);
    const y = h - pad - (Number(g.score) || 0) * (h - 2 * pad);
    return { x, y, g };
  });
  const trainPts = toPts(trainSrc);
  const valPts = hasVal ? toPts(valSrc) : [];
  const trainLine = trainPts.map(p => p.x + ',' + p.y).join(' ');
  const valLine = valPts.map(p => p.x + ',' + p.y).join(' ');
  const dots = trainPts.map(p => {
    let color = '#ff8a8a';
    let label = 'skip';
    if (p.g.kind === 'baseline') { color = '#8aa4ff'; label = '基线 train'; }
    else if (p.g.applied) { color = '#7be495'; label = 'applied'; }
    return `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3.5" fill="${color}">
      <title>#${p.g.attempt} train=${Number(p.g.score).toFixed(3)} ${label}</title>
    </circle>`;
  }).join('');
  const valDots = valPts.map(p => {
    const color = p.g.kind === 'baseline' ? '#e5c07b' : '#d19a66';
    return `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="3" fill="${color}">
      <title>#${p.g.attempt} val=${Number(p.g.score).toFixed(3)}</title>
    </circle>`;
  }).join('');
  const valPoly = valLine
    ? `<polyline fill="none" stroke="#e5c07b" stroke-width="1.5" stroke-dasharray="4 3" points="${valLine}"/>`
    : '';
  const legend = hasVal
    ? '实线=train / 虚线=val；蓝点=train 基线 / 绿点=已 apply / 红点=未 apply'
    : '蓝点=基线 / 绿点=已 apply / 红点=未 apply';
  box.innerHTML = `<svg viewBox="0 0 ${w} ${h}" width="100%" height="180" role="img">
    <line x1="${pad}" y1="${h-pad}" x2="${w-pad}" y2="${h-pad}" stroke="#2a2d3a"/>
    <line x1="${pad}" y1="${pad}" x2="${pad}" y2="${h-pad}" stroke="#2a2d3a"/>
    <text x="4" y="${pad+4}" fill="#7a7d89" font-size="10">1</text>
    <text x="4" y="${h-pad}" fill="#7a7d89" font-size="10">0</text>
    <polyline fill="none" stroke="#8aa4ff" stroke-width="1.5" points="${trainLine}"/>
    ${valPoly}
    ${dots}
    ${valDots}
  </svg>
  <div class="meta" style="color:#7a7d89;font-size:11px;">${legend}</div>`;
}

function renderTaskResults(tasks) {
  if (!tasks || !tasks.length) return '';
  return tasks.map(t => {
    const ok = Number(t.score) >= 1;
    const cls = ok ? 'task-row pass' : 'task-row fail';
    const tag = ok ? 'PASS' : 'FAIL';
    const title = (t.task || '').replace(/\s+/g, ' ').slice(0, 48);
    return `<div class="${cls}">
      <div class="task-head">[${t.task_idx}] ${tag} · ${esc(title)}</div>
      <div class="meta">${esc(t.feedback || '')}</div>
      <div class="task-out">${esc(t.output || '(无输出)')}</div>
    </div>`;
  }).join('');
}

function renderGens(evo) {
  const box = document.getElementById('evo-gens');
  const gens = (evo && evo.generations) || [];
  if (!gens.length) {
    box.innerHTML = '<div class="empty-hint">(等待进化数据)</div>';
    return;
  }
  const valBlock = (label, score, tasks) => {
    if (score == null && (!tasks || !tasks.length)) return '';
    const s = score == null ? '-' : Number(score).toFixed(3);
    return `<div class="meta" style="margin-top:8px;">${label} score=${s}</div>${renderTaskResults(tasks)}`;
  };
  let html = '';
  if (evo.baseline) {
    html += `<div class="gen-card">
      <div>#0 · 基线 · train=${Number(evo.baseline.score).toFixed(3)}${evo.baseline.val_score != null ? ' · val=' + Number(evo.baseline.val_score).toFixed(3) : ''}</div>
      <div class="meta">进化前：train 决定能否 apply；val 只检查有没有背题</div>
      <div class="meta">TRAIN</div>
      ${renderTaskResults(evo.baseline.task_results)}
      ${valBlock('VAL', evo.baseline.val_score, evo.baseline.val_task_results)}
    </div>`;
  }
  html += gens.map(g => {
    const cls = g.applied ? 'gen-card applied' : 'gen-card failed';
    const tag = g.applied ? 'applied' : 'skipped';
    const valTxt = g.val_score != null ? ' · val=' + Number(g.val_score).toFixed(3) : '';
    const effects = renderTaskResults(g.task_results);
    const diffBlock = g.unified_diff
      ? `<details><summary>prompt diff</summary><pre>${esc(g.unified_diff)}</pre></details>`
      : '';
    return `<div class="${cls}">
      <div>#${g.attempt} · gen=${g.generation} · ${tag} · train=${Number(g.score).toFixed(3)}${valTxt}</div>
      <div class="meta">${esc(g.diff_summary || '')}</div>
      <div class="meta">TRAIN</div>
      ${effects || `<div class="meta">${esc(g.feedback || '')}</div>`}
      ${valBlock('VAL', g.val_score, g.val_task_results)}
      ${diffBlock}
    </div>`;
  }).join('');
  box.innerHTML = html;
}

function rollbackTargets(evo) {
  const set = new Set([0]);
  (evo.generations || []).forEach(g => { if (g.applied) set.add(Number(g.generation)); });
  return Array.from(set).sort((a, b) => a - b);
}

function renderLive(evo) {
  const box = document.getElementById('evo-live');
  const prevMsg = document.getElementById('rb-msg')?.innerHTML || '';
  if (!evo) {
    box.innerHTML = '<div class="empty-hint">(等待进化数据)</div>';
    return;
  }
  const can = !!evo.can_rollback;
  const targets = rollbackTargets(evo);
  const btns = targets.map(g =>
    `<button type="button" class="rb-btn" data-gen="${g}" ${can ? '' : 'disabled'}>回滚到 gen ${g}</button>`
  ).join('');
  const hint = can
    ? ''
    : '<div class="rb-hint">回滚不可用。恢复：进化与 Dashboard 放同一进程，先 register_evolution(agent)</div>';
  const lastVal = (evo.generations || []).filter(g => g.val_score != null).slice(-1)[0];
  const valNow = lastVal != null ? lastVal.val_score : evo.baseline && evo.baseline.val_score;
  const valTxt = valNow != null ? ' · val=' + Number(valNow).toFixed(3) : '';
  box.innerHTML = `
    <div class="meta" style="color:#7a7d89;font-size:11px;margin-bottom:6px;">
      stop=${esc(evo.stop_reason || '-')} · current_gen=${evo.current_generation ?? '-'} · train=${Number(evo.final_score || 0).toFixed(3)}${valTxt}
    </div>
    <div class="prompt-box">${esc(evo.current_prompt || '')}</div>
    <div class="rb-row">${btns}</div>
    ${hint}
    <div id="rb-msg">${prevMsg}</div>`;
  box.querySelectorAll('.rb-btn').forEach(btn => {
    btn.addEventListener('click', () => postRollback(Number(btn.getAttribute('data-gen'))));
  });
}

function postRollback(generation) {
  const msg = document.getElementById('rb-msg');
  if (rollbackBusy) return;
  rollbackBusy = true;
  if (msg) msg.innerHTML = '<div class="rb-hint">回滚中…</div>';
  fetch('/rsi/rollback', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ generation: generation }),
  }).then(async resp => {
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok || !body.ok) {
      throw new Error(body.error || ('HTTP ' + resp.status));
    }
    if (msg) msg.innerHTML = '<div class="meta" style="color:#7be495;font-size:11px;">已回滚到 gen ' + body.generation + '</div>';
  }).catch(err => {
    if (msg) msg.innerHTML = '<div class="rb-err">' + esc(err.message || String(err)) + '</div>';
  }).finally(() => { rollbackBusy = false; });
}

function renderEvolution(evo) {
  const raw = JSON.stringify(evo || null);
  if (raw === lastEvoJson) return;
  lastEvoJson = raw;
  if (!evo || !evo.generations || !evo.generations.length) {
    renderFitness(null);
    renderGens(null);
    renderLive(null);
    return;
  }
  renderFitness(evo);
  renderGens(evo);
  renderLive(evo);
  if (!autoSwitchedToEvolve) {
    autoSwitchedToEvolve = true;
    switchPage('page-evolve');
  }
}

function update(data) {
  lastData = data;
  document.getElementById('status').textContent =
    '✓ live · last=' + (data.last_publish_ts ? new Date(data.last_publish_ts * 1000).toLocaleTimeString() : '-');
  renderRuns(data.runs);
  renderDag(data.trace_nodes);
  renderEvents(data.events);
  renderCircuit(data.circuit_breakers);
  renderRetry(data.retry_logs);
  renderAlerts(data.alerts);
  renderEvolution(data.evolution);
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
    """GET / 返回 HTML；POST /rsi/rollback 执行同进程回滚。"""

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

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

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path != "/rsi/rollback":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        length_hdr = self.headers.get("Content-Length")
        if not length_hdr:
            self._send_json(400, {
                "ok": False,
                "error": "缺少 Content-Length。恢复：POST JSON {\"generation\": <int>}",
            })
            return
        try:
            n = int(length_hdr)
        except ValueError:
            self._send_json(400, {
                "ok": False,
                "error": "Content-Length 不是整数。恢复：POST JSON {\"generation\": <int>}",
            })
            return
        if n < 0 or n > 65536:
            self._send_json(400, {
                "ok": False,
                "error": "请求体长度不合法（0~65536）。恢复：POST JSON {\"generation\": <int>}",
            })
            return
        raw = self.rfile.read(n)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_json(400, {
                "ok": False,
                "error": "请求体不是 UTF-8 JSON。恢复：POST JSON {\"generation\": <int>}",
            })
            return
        if not isinstance(data, dict) or "generation" not in data:
            self._send_json(400, {
                "ok": False,
                "error": "缺少 generation 字段。恢复：POST JSON {\"generation\": <int>}",
            })
            return
        try:
            generation = int(data["generation"])
        except (TypeError, ValueError):
            self._send_json(400, {
                "ok": False,
                "error": "generation 必须是整数。恢复：POST JSON {\"generation\": 0}",
            })
            return
        status, payload = _handle_rollback_request(generation)
        self._send_json(status, payload)

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
    print(f"[Atlas Dashboard] 请调用 atlas.cli.dashboard.publish(...) 推运行数据；进化页调用 publish_evolution + register_evolution")

    try:
        asyncio.run(_ws_main(ws_port))
    except KeyboardInterrupt:
        print("[Atlas Dashboard] 收到 Ctrl+C，停止服务")
        http_server.shutdown()

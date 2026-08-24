"""从 Journal 事件重建 RunSnapshot（event sourcing 恢复）。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .base import JournalEvent

CRITICAL_EVENT_TYPES = frozenset({
    "agent_start",
    "llm_after_call",
    "tool_before_call",
    "tool_after_call",
    "saga_push",
    "saga_pop",
    "state_checkpoint",
    "compact_summary",
    "agent_finish",
    "agent_suspend",
    "agent_resume",
})


@dataclass
class RunSnapshot:
    """Journal 恢复出的运行快照，供 AgentLoop.resume 灌回 Memory / saga。"""
    run_id: str
    status: str = "not_found"
    task: str | None = None
    system_prompt: str = ""
    messages: list[dict] = field(default_factory=list)
    saga_stack: list[tuple[str, str, dict]] = field(default_factory=list)
    step_count: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    final_result: str | None = None
    pending_tool: dict | None = None
    suspend_prompt: str | None = None


def restore_from_events(events: list[JournalEvent]) -> RunSnapshot:
    """按 seq 重放事件，得到可 resume 的快照。

    Args:
        events: 同一 run_id 的事件，须已按 seq 排好或可无序（函数按列表顺序扫）。

    Returns:
        ``RunSnapshot``；空列表则 status=not_found。

    依赖:
        ``JournalEvent``；由 ``InMemoryJournal.restore`` / ``SQLiteJournal.restore`` 调用。
    """
    if not events:
        return RunSnapshot(run_id="", status="not_found")

    snap = RunSnapshot(run_id=events[0].run_id, status="running")

    for e in events:
        p = e.payload or {}
        et = e.event_type
        if et == "agent_start":
            snap.task = p.get("task")
            ctx = p.get("context") or {}
            snap.system_prompt = ctx.get("system_prompt") or p.get("system_prompt") or ""
            if snap.system_prompt:
                snap.messages = [{"role": "system", "content": snap.system_prompt}]
            if snap.task:
                snap.messages.append({"role": "user", "content": snap.task})
        elif et == "state_checkpoint":
            snap.messages = list(p.get("messages") or snap.messages)
            raw_saga = p.get("saga_stack")
            if raw_saga is not None:
                snap.saga_stack = [(str(a), str(b), dict(c)) for a, b, c in raw_saga]
            snap.step_count = int(p.get("step_count") or snap.step_count)
            snap.total_tokens = int(p.get("total_tokens") or snap.total_tokens)
            snap.cost_usd = float(p.get("cost_usd") or snap.cost_usd)
            snap.pending_tool = p.get("pending_tool")
        elif et == "llm_after_call":
            response = p.get("response")
            if isinstance(response, dict):
                snap.messages.append(response)
            usage = p.get("usage") or {}
            snap.total_tokens += int(usage.get("total_tokens") or 0)
        elif et == "tool_after_call":
            result = p.get("result")
            output = result.get("output", result) if isinstance(result, dict) else result
            snap.messages.append({
                "role": "tool",
                "content": str(output),
                "tool_call_id": p.get("tool_call_id") or "",
            })
        elif et == "compact_summary":
            summary = p.get("summary") or ""
            snap.messages.append({
                "role": "assistant",
                "content": summary,
                "isMeta": True,
                "type": "compact_summary",
            })
            snap.messages.append({
                "role": "system",
                "content": "[compact_boundary]",
                "isMeta": True,
                "type": "compact_boundary",
            })
        elif et == "saga_push":
            snap.saga_stack.append((
                p.get("step_id", ""),
                p.get("tool_name", ""),
                dict(p.get("compensate_input") or {}),
            ))
        elif et == "saga_pop":
            if snap.saga_stack:
                snap.saga_stack.pop()
        elif et == "turn_start":
            snap.step_count = int(p.get("turn_idx") or snap.step_count)
        elif et == "agent_finish":
            snap.status = p.get("status", "success")
            snap.final_result = p.get("final_result")
        elif et == "agent_suspend":
            snap.status = "suspended"
            snap.suspend_prompt = p.get("prompt")
            snap.pending_tool = p.get("pending_tool")

    return snap

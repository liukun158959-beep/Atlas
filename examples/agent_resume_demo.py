"""崩溃恢复：副作用工具执行后模拟进程崩溃，resume 不重复执行。

预期：
    1. 第一次 run 在第 2 个工具成功后 KeyboardInterrupt
    2. 新 Agent + 同一 Journal resume
    3. 幂等命中，副作用仍为 2 条，最终给出答案
"""
from __future__ import annotations

import sys

from atlas.core.agent import Agent
from atlas.core.plugin import BasePlugin, PluginManager
from atlas.journal import InMemoryJournal
from atlas.render import PlainRenderer
from atlas.testing import FakeLLM
from atlas.tools.base import Toolkit
from atlas.utils.tracker import SideEffectTracker


tracker = SideEffectTracker()


def write_po(params: dict) -> str:
    tracker.record(params["po_id"], {"type": "po"})
    return f"PO[{params['po_id']}] created"


def delete_po(ci, result, ctx) -> dict:
    tracker.erase(ci["po_id"])
    return {}


def charge(params: dict) -> str:
    tracker.record(params["txn_id"], {"type": "pay"})
    return f"charged {params['txn_id']}"


def refund(ci, result, ctx) -> dict:
    tracker.erase(ci["txn_id"])
    return {}


class CrashAfterTwoTools(BasePlugin):
    def __init__(self) -> None:
        self.count = 0

    def on_tool_after_call(self, **kw) -> None:
        self.count += 1
        if self.count >= 2:
            raise KeyboardInterrupt("sim crash after 2 tools")


def _toolkit() -> Toolkit:
    tk = Toolkit()
    tk.register(
        func=write_po, name="write_po", description="create po",
        parameters={"type": "object", "properties": {"po_id": {"type": "string"}}, "required": ["po_id"]},
        hasSideEffects=True, isIdempotent=False, compensate=delete_po, retry_max=0,
    )
    tk.register(
        func=charge, name="charge", description="charge",
        parameters={"type": "object", "properties": {"txn_id": {"type": "string"}}, "required": ["txn_id"]},
        hasSideEffects=True, isIdempotent=False, compensate=refund, retry_max=0,
    )
    return tk


def _scripted_llm() -> FakeLLM:
    return FakeLLM(script=[
        {
            "role": "assistant",
            "content": "create and charge",
            "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "write_po", "arguments": '{"po_id":"PO-1"}'}},
                {"id": "t2", "type": "function", "function": {"name": "charge", "arguments": '{"txn_id":"TXN-1"}'}},
            ],
        },
        {"role": "assistant", "content": "订单已处理完成"},
    ])


def main() -> None:
    renderer = PlainRenderer()
    renderer.rule("Agent resume 崩溃恢复")
    journal = InMemoryJournal(save_full_payload=True)
    pm = PluginManager()
    pm.register(CrashAfterTwoTools())
    agent1 = Agent(
        llm=_scripted_llm(), tools=_toolkit(), journal=journal,
        system_prompt="履约助手", renderer=renderer, plugin_manager=pm, max_steps=5,
    )
    crashed = False
    try:
        agent1.run("处理订单")
    except KeyboardInterrupt:
        crashed = True
    assert crashed, "应在第 2 个工具后崩溃"
    run_id = agent1.trace.meta["run_id"]
    assert len(tracker.snapshot()) == 2, f"崩溃前应有 2 条副作用，实际 {tracker.snapshot()}"

    agent2 = Agent(
        llm=_scripted_llm(), tools=_toolkit(), journal=journal,
        system_prompt="履约助手", renderer=renderer, max_steps=5,
    )
    answer = agent2.resume(run_id)
    assert "完成" in answer, f"resume 最终答案异常: {answer}"
    assert len(tracker.snapshot()) == 2, f"resume 后副作用应仍为 2，实际 {tracker.snapshot()}"
    events = journal.get_events(run_id)
    hits = [e for e in events if e.event_type == "tool_after_call" and e.payload.get("status") == "idempotency_hit"]
    assert hits, "resume 应命中幂等缓存"
    renderer.print("=== agent_resume_demo.py 验证通过 ===")
    print("=== agent_resume_demo.py 验证通过 ===")


if __name__ == "__main__":
    main()

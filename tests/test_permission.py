"""Wave 3：ConfirmRisky 挂起高副作用工具。"""
from __future__ import annotations

from atlas.core.agent import Agent
from atlas.core.permission import ConfirmRisky, SecurityRisk
from atlas.journal import InMemoryJournal
from atlas.testing import FakeLLM
from atlas.tools.base import Toolkit
from atlas.utils.tracker import SideEffectTracker


def test_confirm_risky_suspends_side_effect_tool() -> None:
    tracker = SideEffectTracker()

    def charge(params: dict) -> str:
        tracker.record(params["txn_id"], {"ok": True})
        return "charged"

    def refund(ci, result, ctx) -> dict:
        tracker.erase(ci["txn_id"])
        return {}

    tk = Toolkit()
    tk.register(
        func=charge, name="charge", description="扣款",
        parameters={"type": "object", "properties": {"txn_id": {"type": "string"}}, "required": ["txn_id"]},
        hasSideEffects=True, isIdempotent=False, compensate=refund, retry_max=0,
    )
    llm = FakeLLM(script=[
        {
            "role": "assistant",
            "content": "charge",
            "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "charge", "arguments": '{"txn_id":"T1"}'},
            }],
        },
        {"role": "assistant", "content": "done"},
    ])
    agent = Agent(
        llm=llm, tools=tk, journal=InMemoryJournal(save_full_payload=True),
        permission=ConfirmRisky(threshold=SecurityRisk.HIGH),
        max_steps=5,
    )
    result = agent.run_detailed("扣款")
    assert result.status == "suspended"
    assert tracker.is_empty()
    resumed = agent.resume_detailed(result.suspend_token, {"approved": True})
    assert resumed.status == "success"
    assert not tracker.is_empty()

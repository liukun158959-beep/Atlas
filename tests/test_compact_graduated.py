"""Wave 5：略超阈值且有长 tool_result 时只 snip，不调 LLM。"""
from __future__ import annotations

from atlas.context.compact import CompactManager
from atlas.testing import FakeLLM


def test_snip_only_when_just_over_with_long_tools() -> None:
    llm = FakeLLM(script=["SHOULD_NOT_BE_CALLED"])
    cm = CompactManager(threshold=100, llm=llm)
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "u"},
        {"role": "assistant", "content": "a", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "T" * 500, "tool_call_id": "1"},
        {"role": "assistant", "content": "a2", "tool_calls": [{"id": "2"}]},
        {"role": "tool", "content": "U" * 500, "tool_call_id": "2"},
        {"role": "assistant", "content": "a3", "tool_calls": [{"id": "3"}]},
        {"role": "tool", "content": "V" * 500, "tool_call_id": "3"},
    ]
    new_msgs, outcome = cm.apply(messages, token_count=120)
    assert outcome.snipped is True
    assert outcome.summary is None
    assert outcome.used_llm is False
    assert llm._i == 0
    old_tool = [m for m in new_msgs if m.get("role") == "tool"]
    assert "...[snipped]" in old_tool[0]["content"]

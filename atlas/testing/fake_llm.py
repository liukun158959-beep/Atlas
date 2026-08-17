"""可脚本化的 FakeLLM，供 pytest 与 demo 共用。"""
from __future__ import annotations

import time
from typing import Any, Callable, Union

ResponseItem = Union[str, dict, Callable[..., dict]]


class FakeLLM:
    """按脚本返回 chat 结果。条目可以是 str / message dict / 完整 result dict / 可调用。"""

    def __init__(self, script: list[ResponseItem] | None = None, model: str = "fake") -> None:
        self.model = model
        self._script: list[ResponseItem] = list(script or [])
        self._i = 0
        self.calls: list[dict] = []

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Callable[..., None] | None = None,
    ) -> dict:
        self.calls.append({"messages": messages, "tools": tools})
        if self._i >= len(self._script):
            raise IndexError(f"FakeLLM script exhausted after {self._i} calls")
        item = self._script[self._i]
        self._i += 1
        if callable(item):
            item = item(messages, tools)
        result = _normalize_chat_result(item)
        content = result["message"].get("content") or ""
        if on_delta and content:
            on_delta(content)
        return result


class LoopingFakeLLM:
    """总是返回 tool_calls，用于触发终止条件。"""

    def __init__(self, sleep_ms: int = 0, content: str = "正在计算...") -> None:
        self.model = "fake"
        self._sleep_ms = sleep_ms
        self._content = content

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Callable[..., None] | None = None,
    ) -> dict:
        if self._sleep_ms:
            time.sleep(self._sleep_ms / 1000.0)
        msg = {
            "role": "assistant",
            "content": self._content,
            "tool_calls": [{
                "id": "tc1",
                "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
            }],
        }
        return {"message": msg, "usage": None, "duration_ms": self._sleep_ms}


def _normalize_chat_result(item: Any) -> dict:
    if isinstance(item, str):
        return {
            "message": {"role": "assistant", "content": item},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "duration_ms": 1,
        }
    if isinstance(item, dict) and "message" in item:
        item.setdefault("usage", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        item.setdefault("duration_ms", 1)
        return item
    if isinstance(item, dict):
        return {
            "message": item,
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "duration_ms": 1,
        }
    raise TypeError(f"Unsupported FakeLLM script item: {type(item)}")

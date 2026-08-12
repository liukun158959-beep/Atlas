"""终止条件组合器演示：MaxSteps / Timeout / RepetitionDetector 三种触发场景。

用 FakeLLM 总返回 tool_calls，让 Agent 持续循环，观察各终止条件触发。
本 demo 不连真实 LLM（终止条件需要可控的循环行为）。

用法：
    python examples/terminator_demo.py          # Rich 渲染
    python examples/terminator_demo.py --plain  # 纯文本渲染
    python examples/terminator_demo.py --mock   # 兼容通用模式（始终用 FakeLLM）
"""
from __future__ import annotations

import sys
import time

from atlas.core.agent import Agent
from atlas.core.terminator import (
    MaxSteps,
    RepetitionDetector,
    Timeout,
    make_default_terminator,
)
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS


class LoopingFakeLLM:
    """总是返回 tool_calls 的假 LLM，让 Agent 持续循环以触发终止条件。"""

    def __init__(self, sleep_ms: int = 0, content: str = "正在计算...") -> None:
        self.model = "fake"
        self._sleep_ms = sleep_ms
        self._content = content

    def chat(self, messages, tools=None):
        if self._sleep_ms:
            time.sleep(self._sleep_ms / 1000.0)
        msg = {
            "role": "assistant", "content": self._content,
            "tool_calls": [{
                "id": "tc1", "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
            }],
        }
        return {"message": msg, "usage": None, "duration_ms": self._sleep_ms}


def main() -> None:
    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()
    renderer.rule("终止条件组合器演示")

    tk = Toolkit()
    for spec in BUILTIN_TOOLS:
        tk.register(**spec)

    def run_case(title: str, llm, terminator) -> None:
        renderer.rule(title)
        agent = Agent(
            llm=llm, tools=tk,
            system_prompt="你是计算助手。",
            renderer=renderer, terminator=terminator,
        )
        result = agent.run("算 1+1")
        # 终止时 result 形如 "Agent 终止：<reasons>"
        renderer.print(f"结果: {result}")
        renderer.print()

    # a) MaxSteps(3)：第 3 步触发
    run_case("a) MaxSteps(3)", LoopingFakeLLM(), MaxSteps(3))

    # b) Timeout(100ms)：FakeLLM 每次 sleep 50ms，3 轮后超 100ms 触发
    run_case("b) Timeout(100ms)", LoopingFakeLLM(sleep_ms=50), Timeout(100))

    # c) RepetitionDetector(window=3)：FakeLLM 总返回相同内容，3 轮相同即触发
    run_case("c) RepetitionDetector(window=3)",
             LoopingFakeLLM(content="重复内容"), RepetitionDetector(window=3))

    # 默认组合器：MaxSteps(10) | Timeout(60_000)
    dt = make_default_terminator()
    renderer.print(f"make_default_terminator 子条件数: {len(dt.terminators)}")


if __name__ == "__main__":
    main()

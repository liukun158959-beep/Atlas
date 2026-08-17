"""插件体系 + CostCap 终止演示：TokenCostPlugin 累计成本，超阈值时 Agent 终止。

FakeLLM 返回 model='qwen-plus' + total_tokens=150 的 usage，让 TokenCostPlugin
能按价格表算出 cost；CostCap 设极小阈值，2 轮内即触发终止。

用法：
    python examples/plugin_cost_demo.py          # Rich 渲染
    python examples/plugin_cost_demo.py --plain  # 纯文本渲染
    python examples/plugin_cost_demo.py --mock   # 兼容通用模式（始终用 FakeLLM）
"""
from __future__ import annotations

import sys

from atlas.core.agent import Agent
from atlas.core.plugin import PluginManager
from atlas.core.terminator import CostCap
from atlas.plugins.token_cost import TokenCostPlugin
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS


class CostlyFakeLLM:
    """假 LLM：总是返回 tool_calls + qwen-plus 的 usage，让成本快速累积。"""

    def __init__(self) -> None:
        self.model = "qwen-plus"  # TokenCostPlugin 按此名查价格表
        self._call = 0

    def chat(self, messages, tools=None, on_delta=None):
        self._call += 1
        msg = {
            "role": "assistant", "content": f"第 {self._call} 轮思考",
            "tool_calls": [{
                "id": f"tc{self._call}", "type": "function",
                "function": {"name": "calculator", "arguments": '{"expression": "1+1"}'},
            }],
        }
        # qwen-plus: input 0.0008/1k, output 0.0020/1k
        # 100 in + 50 out = 0.00018 USD/轮，超 CostCap(0.0001)
        usage = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        return {"message": msg, "usage": usage, "duration_ms": 5}


def main() -> None:
    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()
    renderer.rule("插件体系 + CostCap 终止演示")

    tk = Toolkit()
    for spec in BUILTIN_TOOLS:
        tk.register(**spec)

    # 注册 TokenCostPlugin（关掉自带 print，统一走 renderer 输出）
    pm = PluginManager()
    cost_plugin = TokenCostPlugin(log_summary=False)
    pm.register(cost_plugin)

    # CostCap 设极小，2 轮内即触发
    agent = Agent(
        llm=CostlyFakeLLM(), tools=tk,
        system_prompt="你是计算助手。",
        renderer=renderer, plugin_manager=pm,
        terminator=CostCap(max_usd=0.0001),
    )

    result = agent.run("算 1+1")
    renderer.print(f"\n最终结果: {result}")

    # 打印 TokenCostPlugin 的 get_summary() 结果
    run_id = agent.trace.meta["run_id"]
    summary = cost_plugin.get_summary(run_id)
    renderer.print("\nTokenCostPlugin 汇总:")
    renderer.print(f"  total_tokens = {summary['total_tokens']}")
    renderer.print(f"  cost_usd     = {summary['cost_usd']}")
    renderer.print(f"  turns        = {summary['turns']}")

    # 验证：Agent 因 cost_cap 终止，且成本已超阈值
    assert "cost_cap" in result, f"应因 cost_cap 终止，实际: {result}"
    assert summary["cost_usd"] >= 0.0001, f"成本应超阈值，实际 {summary['cost_usd']}"
    renderer.print("\n验证通过：Agent 因 cost_cap 终止，TokenCostPlugin 正确累计成本")


if __name__ == "__main__":
    main()

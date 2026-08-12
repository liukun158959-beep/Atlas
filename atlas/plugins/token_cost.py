"""TokenCostPlugin：统计每次 LLM 调用的 token 用量与成本。

只订阅两个钩子：
- on_llm_after_call：累加 prompt_tokens / completion_tokens 与 cost
- on_agent_finish：打印结构化汇总

遵循 DR-5：输出结构化日志（事件名 + 关键字参数）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from atlas.core.plugin import BasePlugin

log = logging.getLogger("atlas.plugins.token_cost")

# 常用模型的计费表（单位：USD per 1000 tokens）
# 数据来源：各官方公开价格，2026 Q3。未列出的模型不计算美元成本，只统计 token 数。
PRICING_TABLE: dict[str, dict[str, float]] = {
    # Qwen / 通义
    "qwen-plus":       {"input": 0.0008 / 1000, "output": 0.0020 / 1000},
    "qwen-turbo":      {"input": 0.0003 / 1000, "output": 0.0006 / 1000},
    "qwen-max":        {"input": 0.0080 / 1000, "output": 0.0200 / 1000},
    # OpenAI
    "gpt-4o-mini":     {"input": 0.00015 / 1000, "output": 0.0006 / 1000},
    "gpt-4o":          {"input": 0.00500 / 1000, "output": 0.01500 / 1000},
    # Deepseek
    "deepseek-chat":   {"input": 0.00014 / 1000, "output": 0.00028 / 1000},
    # Moonshot / Kimi
    "moonshot-v1-128k":{"input": 0.00120 / 1000, "output": 0.00240 / 1000},
    # Zhipu / GLM
    "glm-4-flash":     {"input": 0.00010 / 1000, "output": 0.00010 / 1000},
    "glm-4-plus":      {"input": 0.01000 / 1000, "output": 0.01000 / 1000},
}

# 简易人民币汇率（近似值，仅用于概览）
USD_TO_CNY = 7.25


@dataclass
class TokenCostPlugin(BasePlugin):
    """Token 用量与成本统计插件。

    Parameters
    ----------
    custom_prices:
        允许用户自定义/覆盖模型价格表，如 {"my-model": {"input":1e-6,"output":3e-6}}
    usd_to_cny:
        显示用的汇率，不改美元计价的基准
    log_summary:
        是否在 agent_finish 时打印汇总（调试时有用）
    """

    name: str = "token_cost"
    priority: int = 200

    custom_prices: dict[str, dict[str, float]] | None = None
    usd_to_cny: float = USD_TO_CNY
    log_summary: bool = True

    # ---- 内部累加 --------------------------------------------------------
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    per_run: dict[str, dict[str, Any]] = field(default_factory=dict)

    # ---- 钩子 ------------------------------------------------------------

    def on_llm_after_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        model: str,
        response: dict[str, Any],
        usage: dict[str, int] | None,
        duration_ms: int,
    ) -> None:
        """每次 LLM 返回后累加 token/cost。"""
        if not usage:
            return

        in_tok = usage.get("prompt_tokens", 0)
        out_tok = usage.get("completion_tokens", 0)
        self.input_tokens += in_tok
        self.output_tokens += out_tok

        # 合并价格表（custom 覆盖默认）
        price_map = dict(PRICING_TABLE)
        if self.custom_prices:
            price_map.update(self.custom_prices)

        price = price_map.get(model)
        run_cost = 0.0
        if price:
            run_cost = in_tok * price["input"] + out_tok * price["output"]
            self.cost_usd += run_cost

        # 结构化日志：每次调用级别（DR-5 事件名+关键字）
        log.info(
            "token_cost.llm_call",
            run_id=run_id,
            turn_idx=turn_idx,
            model=model,
            input_tokens=in_tok,
            output_tokens=out_tok,
            total_tokens=in_tok + out_tok,
            cost_usd=round(run_cost, 8),
            duration_ms=duration_ms,
        )

        # 记录 per-run 统计（供 Dashboard / 后续插件使用）
        run_stats = self.per_run.setdefault(run_id, {
            "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0,
            "turns": 0,
        })
        run_stats["input_tokens"] += in_tok
        run_stats["output_tokens"] += out_tok
        run_stats["cost_usd"] += run_cost
        run_stats["turns"] += 1

    def on_agent_finish(
        self,
        *,
        run_id: str,
        final_result: str,
        status: str,
        context: dict[str, Any],
    ) -> None:
        """Agent 结束时输出汇总。"""
        stats = self.per_run.get(run_id, {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cost_usd": self.cost_usd,
            "turns": -1,
        })

        total_tokens = stats["input_tokens"] + stats["output_tokens"]
        cost_cny = stats["cost_usd"] * self.usd_to_cny

        log.info(
            "token_cost.summary",
            run_id=run_id,
            turns=stats["turns"],
            input_tokens=stats["input_tokens"],
            output_tokens=stats["output_tokens"],
            total_tokens=total_tokens,
            cost_usd=round(stats["cost_usd"], 6),
            cost_cny=round(cost_cny, 4),
            status=status,
        )

        if self.log_summary:
            print(
                f"[TokenCost] run={run_id} "
                f"turns={stats['turns']} "
                f"in={stats['input_tokens']} out={stats['output_tokens']} "
                f"total={total_tokens} "
                f"cost=${stats['cost_usd']:.5f} (¥{cost_cny:.3f})"
            )

    # ---- 便捷查询 API（测试/断言用） -----------------------------------

    def get_summary(self, run_id: str | None = None) -> dict[str, Any]:
        """获取指定 run_id 的汇总（或全局累计）。"""
        if run_id is not None:
            s = self.per_run.get(run_id, {})
            return {
                "input_tokens": s.get("input_tokens", 0),
                "output_tokens": s.get("output_tokens", 0),
                "total_tokens": s.get("input_tokens", 0) + s.get("output_tokens", 0),
                "cost_usd": round(s.get("cost_usd", 0.0), 8),
                "cost_cny": round(s.get("cost_usd", 0.0) * self.usd_to_cny, 6),
                "turns": s.get("turns", 0),
            }
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.input_tokens + self.output_tokens,
            "cost_usd": round(self.cost_usd, 8),
            "cost_cny": round(self.cost_usd * self.usd_to_cny, 6),
            "runs": len(self.per_run),
        }

    def reset(self) -> None:
        """清空累计统计（测试场景复用同一个插件实例时使用）。"""
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost_usd = 0.0
        self.per_run.clear()
        log.info("token_cost.reset")

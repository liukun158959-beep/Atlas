"""终止条件组合器：用 | 运算符组合多个终止条件。

默认策略（make_default_terminator）：MaxSteps(10) | Timeout(60_000)
- max_steps=10 沿用现有行为
- timeout=60s 是新增兜底（防止 Agent 死循环或 LLM 调用卡住）
- cost_cap / token_budget / repetition_detector 默认不开启，用户显式加

用法：
    from atlas.core.terminator import MaxSteps, Timeout, CostCap
    terminator = MaxSteps(20) | Timeout(30_000) | CostCap(0.05)
    agent = Agent(llm=llm, tools=tools, terminator=terminator)
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Protocol, runtime_checkable


@runtime_checkable
class Terminator(Protocol):
    """终止条件协议：检查是否应停止，返回 (是否停, 终止理由列表)。"""

    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        ...

    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]:
        ...


def _make_context() -> SimpleNamespace:
    """创建终止条件上下文，Agent.run() 每轮更新。"""
    return SimpleNamespace(
        step_count=0,
        start_time=time.time() * 1000,  # 毫秒
        total_tokens=0,
        cost_usd=0.0,
        recent_outputs=[],  # 最近 N 轮的 LLM 输出文本，供 repetition_detector 用
    )


@dataclass
class MaxSteps:
    """步数上限终止条件。"""

    max_steps: int

    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        return CompositeTerminator([self, other])

    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]:
        if ctx.step_count >= self.max_steps:
            return True, [f"max_steps={self.max_steps} 已用尽"]
        return False, []


@dataclass
class Timeout:
    """wall-clock 超时终止条件（毫秒）。"""

    timeout_ms: int

    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        return CompositeTerminator([self, other])

    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]:
        elapsed = time.time() * 1000 - ctx.start_time
        if elapsed >= self.timeout_ms:
            return True, [f"timeout={self.timeout_ms}ms 已到（实际 {int(elapsed)}ms）"]
        return False, []


@dataclass
class TokenBudget:
    """Token 预算终止条件。"""

    max_tokens: int

    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        return CompositeTerminator([self, other])

    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]:
        if ctx.total_tokens >= self.max_tokens:
            return True, [f"token_budget={self.max_tokens} 已超（实际 {ctx.total_tokens}）"]
        return False, []


@dataclass
class CostCap:
    """成本上限终止条件（美元）。"""

    max_usd: float

    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        return CompositeTerminator([self, other])

    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]:
        if ctx.cost_usd >= self.max_usd:
            return True, [f"cost_cap={self.max_usd}$ 已超（实际 {ctx.cost_usd:.4f}$）"]
        return False, []


@dataclass
class RepetitionDetector:
    """重复输出检测：最近 window 轮输出完全相同则终止。"""

    window: int = 5

    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        return CompositeTerminator([self, other])

    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]:
        outputs = ctx.recent_outputs[-self.window :]
        if len(outputs) >= self.window and len(set(outputs)) == 1:
            return True, [f"repetition_detector: 最近 {self.window} 轮输出完全相同"]
        return False, []


@dataclass
class CompositeTerminator:
    """组合终止条件：任一子条件命中即终止（逻辑 OR）。"""

    terminators: list[Terminator] = field(default_factory=list)

    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        """组合器 | 组合器 / 组合器 | 单条件 都返回新的 CompositeTerminator。"""
        if isinstance(other, CompositeTerminator):
            return CompositeTerminator(self.terminators + other.terminators)
        return CompositeTerminator(self.terminators + [other])

    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]:
        all_reasons: list[str] = []
        for t in self.terminators:
            should, reasons = t.should_stop(ctx)
            if should:
                all_reasons.extend(reasons)
        return (len(all_reasons) > 0, all_reasons)


def make_default_terminator(max_steps: int = 10, timeout_ms: int = 60_000) -> CompositeTerminator:
    """生成默认终止条件：MaxSteps | Timeout。

    与旧版行为完全兼容（max_steps=10 保留），新增 timeout=60s 兜底。
    """
    return MaxSteps(max_steps) | Timeout(timeout_ms)

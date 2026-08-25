"""同一任务集上对比两个 system prompt：n 轮均值 + 样本标准差。"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Callable

from .fitness import TaskSetFitnessEvaluator

# --mock 强 prompt 必须含此句，弱 prompt 不得含此句
MATHTRAP_STRONG_MARK = "识别逻辑陷阱"


@dataclass
class PromptStats:
    """一套 prompt 的 n 轮分数。"""

    scores: list[float] = field(default_factory=list)
    mean: float = 0.0
    stdev: float | None = None


@dataclass
class AbTestResult:
    """A/B 对比结果。"""

    n: int
    n_tasks: int
    a: PromptStats
    b: PromptStats
    delta_mean: float


def _stats(scores: list[float]) -> PromptStats:
    if not scores:
        raise RuntimeError("分数列表为空。恢复：--n >= 1 且评估器每轮返回 score")
    mean = statistics.fmean(scores)
    stdev = statistics.stdev(scores) if len(scores) >= 2 else None
    return PromptStats(scores=list(scores), mean=mean, stdev=stdev)


def ab_test_prompts(
    prompt_a: str,
    prompt_b: str,
    tasks: list[dict],
    agent_factory: Callable[[str], Any],
    n: int,
    evaluator_cls: type = TaskSetFitnessEvaluator,
) -> AbTestResult:
    """对两个 prompt 各跑 n 轮任务集打分。

    Args:
        prompt_a / prompt_b: system prompt 全文。
        tasks: 评估器题列表。
        agent_factory: ``(system_prompt) -> Agent``。
        n: 轮数，必须 >= 1。
        evaluator_cls: 默认 TaskSetFitnessEvaluator；编码用 CodeFitnessEvaluator；多轮用 MultiTurnFitnessEvaluator。
    """
    if n < 1:
        raise ValueError("n 必须 >= 1。恢复：传入 --n 1 或更大")
    if not tasks:
        raise ValueError("tasks 不能为空。恢复：--preset mathtrap / humaneval / multiturn")
    if not prompt_a.strip():
        raise ValueError("prompt-a 为空。恢复：写入非空 utf-8 文本")
    if not prompt_b.strip():
        raise ValueError("prompt-b 为空。恢复：写入非空 utf-8 文本")

    evaluator = evaluator_cls(tasks, agent_factory)
    scores_a: list[float] = []
    scores_b: list[float] = []
    for _ in range(n):
        scores_a.append(evaluator.evaluate(prompt_a).score)
        scores_b.append(evaluator.evaluate(prompt_b).score)
    a = _stats(scores_a)
    b = _stats(scores_b)
    return AbTestResult(
        n=n,
        n_tasks=len(tasks),
        a=a,
        b=b,
        delta_mean=b.mean - a.mean,
    )


def mathtrap_mock_answer(system_prompt: str, user_task: str) -> str:
    """--mock 答题：弱 prompt 硬算 72；强 prompt 按题面给关键字。"""
    if MATHTRAP_STRONG_MARK not in system_prompt:
        return "72"
    if "tan" in user_task:
        return "无定义"
    if "六月" in user_task or "三月" in user_task:
        return "无法确定"
    if "等边" in user_task:
        return "矛盾"
    raise RuntimeError(
        f"MathTrap mock 无法匹配题面。恢复：为该题补充分类。原文: {user_task[:80]}"
    )


class MathTrapMockLLM:
    """按 system prompt 是否含 MATHTRAP_STRONG_MARK 答 MathTrap。"""

    model = "mock-mathtrap-ab"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Any = None,
    ) -> dict:
        system = ""
        user = ""
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system":
                system = content
            elif role == "user":
                user = content
        text = mathtrap_mock_answer(system, user)
        if on_delta:
            on_delta(text)
        return {
            "message": {"role": "assistant", "content": text},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "duration_ms": 1,
        }

"""适应度评估：给 system prompt 打分，指导反思式变异。

Q2=B：FakeFitnessEvaluator（确定性 mock 打分，demo 100% 可复现）+ FitnessEvaluator Protocol（真实接口预留）
Q9=B：evaluate(prompt) -> FitnessResult(score, feedback, task_results)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class FitnessResult:
    """适应度评估结果。

    score: 0.0-1.0 浮点数
    feedback: 评估反馈文本（供反思式变异注入下一代 propose prompt）
    task_results: 每个任务的详细结果（真实实现填，Fake 留空）
    """

    score: float
    feedback: str = ""
    task_results: list[dict] = field(default_factory=list)


@runtime_checkable
class FitnessEvaluator(Protocol):
    """适应度评估协议：给 system prompt 打分。"""

    def evaluate(self, prompt: str) -> FitnessResult:
        """评估 prompt 质量，返回 FitnessResult。"""
        ...


class FakeFitnessEvaluator:
    """确定性假评估器：按调用次数返回预设分数序列。

    调用序列（demo 100% 可复现）：
      第 1 次 → score=0.4（低分，触发反思改进）
      第 2 次 → score=0.8（高分，验证适应度上升）
      第 3 次 → score=0.3（低分，leakage 场景用，但 leakage 命中不会调到）
    """

    def __init__(self) -> None:
        self._call_count = 0

    def evaluate(self, prompt: str) -> FitnessResult:
        self._call_count += 1
        if self._call_count == 1:
            return FitnessResult(
                score=0.4,
                feedback="第 1 代：prompt 缺少明确角色定义，准确性不足",
            )
        elif self._call_count == 2:
            return FitnessResult(
                score=0.8,
                feedback="第 2 代：增加了角色定义和约束，准确性显著提升",
            )
        else:
            return FitnessResult(
                score=0.3,
                feedback=f"第 {self._call_count} 代：评估分数下降",
            )


class TaskSetFitnessEvaluator:
    """真实适应度评估器：在任务集上跑 Agent + Evaluator 打分。

    4.2b 只留骨架（真实使用用户自行实现 evaluate 逻辑）。
    需要注入：task_set（测试任务列表）+ evaluator（ArtifactSchemaEvaluator 或自定义）+ agent_factory（构造测试 Agent）。
    """

    def __init__(
        self,
        task_set: list[dict],
        evaluator: object | None = None,
        agent_factory: object | None = None,
    ) -> None:
        self._task_set = task_set
        self._evaluator = evaluator
        self._agent_factory = agent_factory

    def evaluate(self, prompt: str) -> FitnessResult:
        """在任务集上评估 prompt。

        真实实现流程：
        1. 用 prompt 作为 system_prompt 构造测试 Agent
        2. 对 task_set 中每个任务跑 Agent.run
        3. 用 evaluator 对每个结果打分
        4. 聚合为平均 score + feedback
        """
        raise NotImplementedError(
            "TaskSetFitnessEvaluator 需用户实现 evaluate 逻辑（4.2b 只留骨架）"
        )

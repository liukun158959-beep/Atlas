"""适应度评估：给 system prompt 打分，指导反思式变异。

Q2=B：FakeFitnessEvaluator（确定性 mock 打分，demo 100% 可复现）+ FitnessEvaluator Protocol（真实接口预留）
Q9=B：evaluate(prompt) -> FitnessResult(score, feedback, task_results)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

# 写入 task_results 的输出截断长度（看板与反馈共用）
_OUTPUT_PREVIEW_CHARS = 400


@dataclass
class FitnessResult:
    """适应度评估结果。

    score: 0.0-1.0 浮点数（train；apply 比较用这个）
    feedback: 评估反馈文本（供反思式变异注入下一代 propose prompt）
    task_results: 训练题详情
    val_score / val_task_results: 验证集；未拆分时为 None / 空
    """

    score: float
    feedback: str = ""
    task_results: list[dict] = field(default_factory=list)
    val_score: float | None = None
    val_task_results: list[dict] = field(default_factory=list)


@runtime_checkable
class FitnessEvaluator(Protocol):
    """适应度评估协议：给 system prompt 打分。"""

    def evaluate(self, prompt: str) -> FitnessResult:
        """给候选 system_prompt 打分。

        Args:
            prompt: 待评估文本。

        Returns:
            score 0~1、feedback、可选 task_results。
        """
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
    """真实适应度评估器：在任务集上跑 Agent + Schema/关键字打分。

    评估流程：
        1. 用 prompt 作为 system_prompt 构造临时 Agent（通过 agent_factory）
        2. 对 task_set 中每个任务跑 Agent.run
        3. 对每个结果做 schema 校验 + 关键字命中检查（通过=1.0/失败=0.0）
        4. 聚合为平均 score + 每任务详情 feedback

    任务集格式（list[dict]）：
        [
            {
                "task": "用户输入文本",
                "expected_schema": {...},      # 可选，JSON Schema 校验
                "expected_keywords": ["关键字"], # 可选，需全部命中
                "failure_hint": "失败时写入 feedback 的方法说明",  # 可选；有则不再把缺失关键字原文塞进反馈
            },
            ...
        ]

    agent_factory: callable(system_prompt: str) -> Agent
        用户注入的 Agent 构造函数，评估器用它为每个 prompt 新建临时 Agent。
    """

    def __init__(
        self,
        task_set: list[dict],
        agent_factory: object,
    ) -> None:
        self._task_set = task_set
        self._agent_factory = agent_factory

    def _evaluate_single(self, agent: Any, task: dict) -> tuple[float, str, str]:
        """评估单个任务：跑 Agent → schema + 关键字检查。返回 (score, feedback, 输出原文)。"""
        user_input = task["task"]
        expected_schema = task.get("expected_schema")
        expected_keywords = task.get("expected_keywords", [])

        # 跑 Agent
        result = agent.run(user_input)
        # Agent.run 返回 str（最终答案）
        artifact = result if isinstance(result, str) else str(result)

        # Schema 校验（复用 patterns._common._validate_artifact_schema）
        if expected_schema:
            from ..patterns._common import _validate_artifact_schema
            schema_ok, schema_err = _validate_artifact_schema(artifact, expected_schema)
            if not schema_ok:
                return (0.0, f"schema 校验失败: {schema_err}", artifact)

        # 关键字命中检查
        if expected_keywords:
            missing = [kw for kw in expected_keywords if kw not in artifact]
            if missing:
                hint = task.get("failure_hint")
                if hint:
                    return (0.0, hint, artifact)
                return (0.0, f"关键字缺失: {missing}", artifact)

        return (1.0, "通过", artifact)

    def evaluate(self, prompt: str) -> FitnessResult:
        """在任务集上评估 prompt，返回 FitnessResult。"""
        agent = self._agent_factory(prompt)
        task_results: list[dict] = []
        passed_count = 0

        for idx, task in enumerate(self._task_set):
            score, feedback, artifact = self._evaluate_single(agent, task)
            output = artifact if len(artifact) <= _OUTPUT_PREVIEW_CHARS else (
                artifact[:_OUTPUT_PREVIEW_CHARS] + "..."
            )
            task_results.append({
                "task_idx": idx,
                "task": task["task"],
                "score": score,
                "feedback": feedback,
                "output": output,
            })
            if score >= 1.0:
                passed_count += 1

        total = len(self._task_set)
        avg_score = passed_count / total if total > 0 else 0.0
        feedback = f"任务集通过 {passed_count}/{total}：\n" + "\n".join(
            f"  [{r['task_idx']}] score={r['score']:.1f} {r['feedback']}"
            for r in task_results
        )

        return FitnessResult(
            score=avg_score,
            feedback=feedback,
            task_results=task_results,
        )


class TrainValFitnessEvaluator:
    """训练题打 apply 用的分，验证题只检测过拟合，不参与提案刷分。

    inner_cls 默认 TaskSetFitnessEvaluator；编码题传入 CodeFitnessEvaluator。
    """

    def __init__(
        self,
        train_set: list[dict],
        val_set: list[dict],
        agent_factory: object,
        inner_cls: type = TaskSetFitnessEvaluator,
    ) -> None:
        if not train_set:
            raise ValueError("train_set 不能为空。恢复：传入至少 1 道训练题")
        if not val_set:
            raise ValueError("val_set 不能为空。恢复：传入至少 1 道验证题")
        self._train = inner_cls(train_set, agent_factory)
        self._val = inner_cls(val_set, agent_factory)

    def evaluate(self, prompt: str) -> FitnessResult:
        train = self._train.evaluate(prompt)
        val = self._val.evaluate(prompt)
        feedback = (
            f"train {train.feedback}\n"
            f"val 通过 {sum(1 for r in val.task_results if r['score'] >= 1.0)}"
            f"/{len(val.task_results)} = {val.score:.3f}：\n"
            + "\n".join(
                f"  val[{r['task_idx']}] score={r['score']:.1f} {r['feedback']}"
                for r in val.task_results
            )
        )
        return FitnessResult(
            score=train.score,
            feedback=feedback,
            task_results=train.task_results,
            val_score=val.score,
            val_task_results=val.task_results,
        )

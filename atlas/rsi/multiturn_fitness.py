"""多轮适应度：同一 Agent 连续 run 若干轮，只对最后一轮做关键字 0/1。"""
from __future__ import annotations

from typing import Any

from .fitness import FitnessResult, _OUTPUT_PREVIEW_CHARS


class MultiTurnFitnessEvaluator:
    """每段对话新建 Agent，段内连续 run；只打最后一轮。"""

    def __init__(
        self,
        task_set: list[dict],
        agent_factory: object,
    ) -> None:
        if not task_set:
            raise ValueError("task_set 不能为空。恢复：传入至少 1 段多轮对话")
        for idx, task in enumerate(task_set):
            turns = task.get("turns")
            if not isinstance(turns, list) or not turns:
                raise ValueError(
                    f"第 {idx} 段 turns 必须是非空 list。恢复：至少提供 1 轮用户话"
                )
            for t in turns:
                if not isinstance(t, str) or not t.strip():
                    raise ValueError(
                        f"第 {idx} 段存在空轮次。恢复：每一轮都写非空用户文本"
                    )
            kws = task.get("expected_keywords")
            if not isinstance(kws, list) or not kws:
                raise ValueError(
                    f"第 {idx} 段缺 expected_keywords。恢复：最后一轮至少 1 个关键字"
                )
        self._task_set = task_set
        self._agent_factory = agent_factory

    def _apply_compact_threshold(self, agent: Any, task: dict) -> None:
        if "compact_threshold" not in task:
            return
        threshold = task["compact_threshold"]
        if agent.compact_manager is None:
            raise RuntimeError(
                "该段要求 compact_threshold，但 Agent 未启用 CompactManager。"
                "恢复：构造 Agent 时传入 compact_threshold（不要设为 None）"
            )
        agent.compact_threshold = threshold
        agent.compact_manager.threshold = threshold

    def _evaluate_single(self, prompt: str, task: dict) -> tuple[float, str, str]:
        agent = self._agent_factory(prompt)
        self._apply_compact_threshold(agent, task)
        artifact = ""
        for turn in task["turns"]:
            result = agent.run(turn)
            artifact = result if isinstance(result, str) else str(result)
        expected_keywords = task["expected_keywords"]
        missing = [kw for kw in expected_keywords if kw not in artifact]
        if missing:
            hint = task.get("failure_hint")
            if hint:
                return (0.0, hint, artifact)
            return (0.0, f"关键字缺失: {missing}", artifact)
        return (1.0, "通过", artifact)

    def evaluate(self, prompt: str) -> FitnessResult:
        """逐段多轮评估 prompt，返回 FitnessResult。"""
        task_results: list[dict] = []
        passed_count = 0

        for idx, task in enumerate(self._task_set):
            score, feedback, artifact = self._evaluate_single(prompt, task)
            output = artifact if len(artifact) <= _OUTPUT_PREVIEW_CHARS else (
                artifact[:_OUTPUT_PREVIEW_CHARS] + "..."
            )
            preview = " / ".join(task["turns"])
            task_results.append({
                "task_idx": idx,
                "task": preview,
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

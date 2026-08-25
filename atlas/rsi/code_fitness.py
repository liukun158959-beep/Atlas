"""编码适应度：抽出 Python → subprocess 沙箱跑隐式用例。过=1 否则=0。

每题最多两次尝试：首次失败后把沙箱/抽取错误发给同一 Agent 再跑一次。
写入 FitnessResult.feedback 的仍是 failure_hint，不含 got/expected。
"""
from __future__ import annotations

import re
from typing import Any

from .fitness import FitnessResult, _OUTPUT_PREVIEW_CHARS
from .sandbox import run_function_cases_in_sandbox

# 首次失败后再修 1 次
_MAX_ATTEMPTS = 2

_FENCE = re.compile(
    r"```(?:python|py)?\s*\n(.*?)```",
    re.DOTALL | re.IGNORECASE,
)


def extract_python_block(text: str, func_name: str) -> str | None:
    """抽出第一段围栏代码；没有围栏则从 ``def func_name`` 起到文末。抽不出返回 None。"""
    if not text or not str(text).strip():
        return None
    m = _FENCE.search(text)
    if m:
        code = m.group(1).strip()
        if code:
            return code
    key = f"def {func_name}"
    i = text.find(key)
    if i < 0:
        return None
    rest = text[i:].strip()
    return rest or None


def _repair_user_message(func_name: str, reason: str) -> str:
    return (
        f"测试未通过：{reason}\n"
        f"请只输出一个修正后的 python 代码块，函数名必须是 {func_name}。"
    )


class CodeFitnessEvaluator:
    """在编码任务集上跑 Agent，沙箱核对隐式用例；失败可再修一次。"""

    def __init__(
        self,
        task_set: list[dict],
        agent_factory: object,
    ) -> None:
        if not task_set:
            raise ValueError("task_set 不能为空。恢复：传入至少 1 道编码题")
        for idx, task in enumerate(task_set):
            if "task" not in task:
                raise ValueError(f"第 {idx} 题缺 task。恢复：每题必须有用户题面")
            if "func_name" not in task or not task["func_name"]:
                raise ValueError(f"第 {idx} 题缺 func_name。恢复：每题指定要检查的函数名")
            cases = task.get("cases")
            if not isinstance(cases, list) or not cases:
                raise ValueError(
                    f"第 {idx} 题 cases 必须是非空 list。恢复：至少提供 1 条隐式用例"
                )
        self._task_set = task_set
        self._agent_factory = agent_factory

    def _evaluate_single(self, agent: Any, task: dict) -> tuple[float, str, str]:
        func_name = task["func_name"]
        hint = task.get("failure_hint") or "抽不出可执行 Python 或沙箱测试未通过"
        user_input = task["task"]
        artifact = ""

        for attempt in range(_MAX_ATTEMPTS):
            result = agent.run(user_input)
            artifact = result if isinstance(result, str) else str(result)
            code = extract_python_block(artifact, func_name)
            if code is None:
                user_input = _repair_user_message(
                    func_name,
                    f"没有抽出可执行的 {func_name}。",
                )
                continue
            try:
                run_function_cases_in_sandbox(code, func_name, task["cases"])
            except ValueError as exc:
                user_input = _repair_user_message(func_name, str(exc))
                continue
            return (1.0, "通过", artifact)

        return (0.0, hint, artifact)

    def evaluate(self, prompt: str) -> FitnessResult:
        """在编码任务集上评估 prompt，返回 FitnessResult。每题新建 Agent。"""
        task_results: list[dict] = []
        passed_count = 0

        for idx, task in enumerate(self._task_set):
            agent = self._agent_factory(prompt)
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

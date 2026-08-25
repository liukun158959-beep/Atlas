"""Prompt 进化报告：把 EvolutionSummary 写成 Markdown。

数据只来自本次 summary.generations + 调用方传入的 initial_prompt。
不读 ArchiveStore，不在 evolve 循环里自动落盘。
"""
from __future__ import annotations

import difflib
from pathlib import Path
from typing import Any

from .prompt_evolver import EvolutionResult, EvolutionSummary


def unified_diff_text(
    old: str,
    new: str,
    from_label: str = "before",
    to_label: str = "after",
) -> str:
    """生成 unified diff 文本。两边相同则返回「(无差异)」。"""
    old_lines = old.splitlines()
    new_lines = new.splitlines()
    if old_lines == new_lines:
        return "(无差异)"
    diff = difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=from_label,
        tofile=to_label,
        lineterm="",
    )
    return "\n".join(diff)


def final_prompt_of(summary: EvolutionSummary, initial_prompt: str) -> str:
    """最后一次成功 apply 的 proposed_value；全程未 apply 则等于初始 prompt。"""
    applied = [g for g in summary.generations if g.applied]
    if not applied:
        return initial_prompt
    return applied[-1].proposal.proposed_value


def generation_records(summary: EvolutionSummary) -> list[dict[str, Any]]:
    """把每代 EvolutionResult 打成 JSON 可序列化的 dict 列表。"""
    records: list[dict[str, Any]] = []
    for idx, result in enumerate(summary.generations, start=1):
        records.append(_record_from_result(idx, result))
    return records


def _record_from_result(attempt: int, result: EvolutionResult) -> dict[str, Any]:
    proposal = result.proposal
    evaluation = result.evaluation
    leakage = result.leakage_check
    return {
        "attempt": attempt,
        "generation": result.generation,
        "applied": result.applied,
        "score": evaluation.score,
        "passed": evaluation.passed,
        "feedback": evaluation.feedback,
        "diff_summary": proposal.diff_summary,
        "reasoning": proposal.reasoning,
        "current_value": proposal.current_value,
        "proposed_value": proposal.proposed_value,
        "unified_diff": unified_diff_text(
            proposal.current_value,
            proposal.proposed_value,
            from_label=f"attempt-{attempt}-current",
            to_label=f"attempt-{attempt}-proposed",
        ),
        "leakage_passed": leakage.passed,
        "leakage_patterns": list(leakage.matched_patterns),
        "task_results": list(getattr(evaluation, "task_results", []) or []),
        "val_score": getattr(evaluation, "val_score", None),
        "val_task_results": list(getattr(evaluation, "val_task_results", None) or []),
    }


def export_evolution_report(
    path: str | Path,
    summary: EvolutionSummary,
    initial_prompt: str,
) -> Path:
    """把一次进化跑写成 Markdown 文件。

    Args:
        path: 输出文件路径（调用方指定，不给默认路径）。
        summary: ``evolve_prompt_until_converged`` 的返回值。
        initial_prompt: 进化前的 system_prompt 原文。

    Returns:
        实际写入的 ``Path``。

    依赖:
        ``EvolutionSummary.generations``、``unified_diff_text``。
    """
    if path is None or str(path).strip() == "":
        raise ValueError(
            "export_evolution_report 缺少 path。恢复：传入要写入的 Markdown 路径，例如 Path('out/evolution.md')"
        )
    if not isinstance(summary, EvolutionSummary):
        raise TypeError(
            "summary 必须是 EvolutionSummary。恢复：先调用 evolve_prompt_until_converged() 再导出"
        )
    if not summary.generations:
        raise ValueError(
            "summary.generations 为空，没有可导出的进化记录。恢复：先跑 evolve_prompt_until_converged"
        )
    if not isinstance(initial_prompt, str) or initial_prompt == "":
        raise ValueError(
            "initial_prompt 必须是非空字符串。恢复：传入进化开始前的 system_prompt 原文"
        )

    out = Path(path)
    parent = out.parent
    if parent != Path("") and not parent.exists():
        raise FileNotFoundError(
            f"报告目录不存在: {parent}。恢复：先创建该目录再调用 export_evolution_report"
        )

    markdown = _render_markdown(summary, initial_prompt)
    out.write_text(markdown, encoding="utf-8")
    return out


def _render_markdown(summary: EvolutionSummary, initial_prompt: str) -> str:
    records = generation_records(summary)
    final_prompt = final_prompt_of(summary, initial_prompt)
    before_after = unified_diff_text(
        initial_prompt,
        final_prompt,
        from_label="initial",
        to_label="final",
    )

    lines: list[str] = [
        "# Atlas Prompt 进化报告",
        "",
        "## 元信息",
        "",
        f"- 停止原因: `{summary.stop_reason}`",
        f"- 最终代数: {summary.final_generation}",
        f"- 最终分数: {summary.final_score:.4f}",
        f"- 提案次数: {len(records)}",
        "",
        "## 初始 Prompt",
        "",
        "```",
        initial_prompt,
        "```",
        "",
        "## 每次提案",
        "",
    ]

    for rec in records:
        applied_txt = "已 apply" if rec["applied"] else "未 apply"
        leak_txt = "通过" if rec["leakage_passed"] else (
            "命中: " + ", ".join(rec["leakage_patterns"])
        )
        lines.extend([
            f"### 第 {rec['attempt']} 次提案",
            "",
            f"- 生效代数: {rec['generation']}",
            f"- 结果: {applied_txt}",
            f"- fitness: {rec['score']:.4f}（passed={rec['passed']}）",
            f"- leakage: {leak_txt}",
            f"- 改动摘要: {rec['diff_summary']}",
            f"- 改进理由: {rec['reasoning']}",
            f"- 评估反馈: {rec['feedback']}",
            "",
            "```diff",
            rec["unified_diff"],
            "```",
            "",
        ])

    lines.extend([
        "## 最终 Prompt",
        "",
        "```",
        final_prompt,
        "```",
        "",
        "## 前后对比（初始 vs 最终）",
        "",
        "```diff",
        before_after,
        "```",
        "",
    ])
    return "\n".join(lines)

"""Prompt 进化：反思式变异 + 适应度评估 + Leakage Guardrails。

Q1=A：最小版（单 lineage + 反思式变异 + 适应度，不做种群）
Q3=A：复用 ArchiveStore（list_failed + list_passed 注入 propose prompt）
Q4=C：LeakageGuard 正则黑名单（越狱模式匹配，命中即拒绝）
Q7=B：新增 evolve_prompt(agent)（和 self_evolve 并列，内部复用 apply_diff）
Q11=B：EvolutionResult(generation, proposal, evaluation, leakage_check, applied)
Q12=B：RegexLeakageGuard 内置默认 ~10 个中英文越狱模式 + extra_patterns 可追加
Q15=A：从 list_passed 提取 score/feedback 格式化为 fitness 历史
Q16=A：leakage 命中 archive 为 failed（EvaluationResult(passed=False, feedback="leakage detected")）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from .archive import PermissionLevel, check_permission
from .evolver import ProposedDiff, EvaluationResult, _parse_json, apply_diff
from .fitness import FitnessEvaluator, FitnessResult
from .inspector import build_self_snapshot


# =============================================================
# Leakage Guardrails
# =============================================================

@dataclass
class LeakageCheckResult:
    """Leakage 检查结果。"""

    passed: bool                          # True=未命中越狱模式 / False=命中
    matched_patterns: list[str] = field(default_factory=list)  # 命中的模式列表


@runtime_checkable
class LeakageGuard(Protocol):
    """Leakage 防御协议：检查 prompt 是否含越狱/注入模式。"""

    def check(self, prompt: str) -> LeakageCheckResult:
        """检查 prompt，返回 LeakageCheckResult。"""
        ...


# 默认越狱模式黑名单（中英文 ~10 个）
_DEFAULT_LEAKAGE_PATTERNS = [
    r"ignore\s+previous\s+instructions",
    r"忽略以上指令",
    r"忽略所有规则",
    r"you\s+are\s+now\s+",
    r"你现在是",
    r"system\s+override",
    r"jailbreak",
    r"\bDAN\b",
    r"disregard\s+above",
    r"new\s+instructions?\s*:",
]


class RegexLeakageGuard:
    """正则黑名单 Leakage Guard。

    内置默认 ~10 个中英文越狱模式 + extra_patterns 可追加。
    命中任一模式即拒绝（passed=False）。
    """

    def __init__(self, extra_patterns: list[str] | None = None) -> None:
        patterns = list(_DEFAULT_LEAKAGE_PATTERNS)
        if extra_patterns:
            patterns.extend(extra_patterns)
        # 预编译正则（忽略大小写）
        self._compiled = [re.compile(p, re.IGNORECASE) for p in patterns]
        self._raw_patterns = patterns

    def check(self, prompt: str) -> LeakageCheckResult:
        """检查 prompt 是否命中越狱模式。"""
        matched: list[str] = []
        for raw, compiled in zip(self._raw_patterns, self._compiled):
            if compiled.search(prompt):
                matched.append(raw)
        return LeakageCheckResult(
            passed=(len(matched) == 0),
            matched_patterns=matched,
        )


# =============================================================
# EvolutionResult
# =============================================================

@dataclass
class EvolutionResult:
    """Prompt 进化结果（单代）。"""

    generation: int                       # 当前代数（apply 成功后为新一代）
    proposal: ProposedDiff                # 提案
    evaluation: EvaluationResult          # 适应度评估结果
    leakage_check: LeakageCheckResult     # leakage 检查结果
    applied: bool                         # 是否成功 apply（fitness + leakage 都过才 True）


# Q5=A：多代循环返回值
@dataclass
class EvolutionSummary:
    """Prompt 多代进化汇总。"""

    generations: list[EvolutionResult]          # 每代结果列表
    final_generation: int                       # 最终代数
    final_score: float                          # 最终 fitness score
    # "converged" 收敛 / "max_generations" 达上限 / "no_progress" 连续无进展
    stop_reason: str


# 停止原因枚举（字符串，避免额外 import Enum）
STOP_CONVERGED = "converged"
STOP_MAX_GENERATIONS = "max_generations"
STOP_NO_PROGRESS = "no_progress"

# 默认收敛参数（Q1=A / Q2=A / Q6=A）
DEFAULT_MAX_GENERATIONS = 10
DEFAULT_CONVERGENCE_N = 3
DEFAULT_CONVERGENCE_THRESHOLD = 0.01
DEFAULT_NO_PROGRESS_N = 3


# =============================================================
# 闭环函数
# =============================================================

# 适应度通过阈值（score >= 此值才视为通过）
FITNESS_THRESHOLD = 0.5


def _build_evolution_prompt(
    snapshot: Any,
    fitness_history: list[str],
    archived_failures: list[str],
) -> str:
    """构造 propose prompt（含 self_snapshot + fitness 历史 + archived_failures）。

    Q3=A + Q15=A：fitness 历史从 archive.list_passed() 提取，archived_failures 从 list_failed() 提取。
    """
    parts = [
        "# Agent 当前状态快照",
        f"## System Prompt\n{snapshot.system_prompt}",
        f"## 工具定义\n{', '.join(t.get('name', '') for t in snapshot.tool_definitions)}",
        f"## 最近失败案例\n{chr(10).join(snapshot.recent_failures) if snapshot.recent_failures else '(无)'}",
    ]
    if fitness_history:
        parts.append(
            "# Fitness 历史（反思依据）\n" + "\n".join(fitness_history)
        )
    if archived_failures:
        parts.append(
            "# 已归档的失败提案（避免重复提议）\n" + "\n".join(archived_failures)
        )
    parts.append(
        "# 任务\n"
        "基于以上状态和 fitness 历史，提出一个 system_prompt 改进提案。\n"
        "注意：只改 system_prompt（target_type='system_prompt'），不改架构层。\n"
        "只输出 JSON，不要附加任何解释文字：\n"
        "{\n"
        '  "target_type": "system_prompt",\n'
        '  "target_id": "system_prompt",\n'
        '  "current_value": "当前 system prompt 文本",\n'
        '  "proposed_value": "提议的新 system prompt 文本",\n'
        '  "reasoning": "改进理由（结合 fitness 历史反思）",\n'
        '  "diff_summary": "改动摘要"\n'
        "}"
    )
    return "\n\n".join(parts)


def _extract_fitness_history(agent: Any) -> list[str]:
    """从 archive.list_recent() 提取 fitness 历史（含 passed + failed）。

    Q15=A：格式化为"第 N 代 score=X feedback=Y"。
    说明：原 Q15 设想从 list_passed() 提取，但实际第一次进化可能 failed，
    此时 fitness 历史为空会丢失反思依据。改为从 list_recent() 提取所有记录，
    让反思看到完整的适应度轨迹（无论通过与否）。
    """
    archive_store = getattr(agent, "archive_store", None)
    if archive_store is None:
        return []
    history: list[str] = []
    recent = archive_store.list_recent(n=10)
    for idx, archived in enumerate(recent, start=1):
        score = archived.evaluation.score
        feedback = archived.evaluation.feedback[:100]  # 截断到 100 字符
        history.append(f"第 {idx} 代: score={score:.2f}, feedback={feedback}")
    return history


def _extract_archived_failures(agent: Any) -> list[str]:
    """从 archive.list_failed() 提取失败提案摘要。"""
    archive_store = getattr(agent, "archive_store", None)
    if archive_store is None:
        return []
    failures: list[str] = []
    for archived in archive_store.list_failed():
        p = archived.proposal
        failures.append(
            f"- target={p.target_type}/{p.target_id}, "
            f"reasoning={p.reasoning[:100]}, "
            f"feedback={archived.evaluation.feedback[:100]}"
        )
    return failures


def evolve_prompt(agent: Any) -> EvolutionResult:
    """Prompt 进化闭环：propose → fitness → leakage → apply/archive。

    Q7=B + Q11=B：返回 EvolutionResult。
    流程：
        1. build_self_snapshot(agent)
        2. 从 archive 提取 fitness_history + archived_failures
        3. 调 agent.llm.chat 生成 JSON 提案（target_type=system_prompt）
        4. 权限校验（check_permission('system_prompt') 必须 BUFFERED）
        5. FitnessEvaluator.evaluate(proposed_value) → FitnessResult
        6. 构造 EvaluationResult（passed = score >= FITNESS_THRESHOLD）
        7. 如果 fitness 通过 → LeakageGuard.check(proposed_value)
        8. 如果 leakage 通过 → apply_diff + agent.generation += 1
        9. archive.add(proposal, evaluation)（无论通过与否）
        10. 返回 EvolutionResult
    """
    snapshot = build_self_snapshot(agent)
    fitness_history = _extract_fitness_history(agent)
    archived_failures = _extract_archived_failures(agent)

    # Step 1: 生成提案
    prompt = _build_evolution_prompt(snapshot, fitness_history, archived_failures)
    messages = [
        {
            "role": "system",
            "content": "你是 Atlas Prompt 进化系统。根据 fitness 历史反思，生成 system_prompt 改进提案。只输出 JSON。",
        },
        {"role": "user", "content": prompt},
    ]
    llm_result = agent.llm.chat(messages)
    content = llm_result["message"].get("content", "")
    data = _parse_json(content)

    # 必需字段校验
    required = [
        "target_type", "target_id", "current_value",
        "proposed_value", "reasoning", "diff_summary",
    ]
    for k in required:
        if k not in data:
            raise ValueError(f"LLM 返回缺少字段: {k}; 原文: {content[:200]}")

    # 权限校验
    permission = check_permission(data["target_type"])
    if permission == PermissionLevel.CORE:
        raise PermissionError(
            f"目标 {data['target_type']} 属于核心层（只读），evolve_prompt 不可修改"
        )

    # 注意：LLM 返回的 current_value 不可信（LLM 可能填错误值），
    # 根据 target_type 用 snapshot 真实值覆盖：
    #   - system_prompt → snapshot.system_prompt
    #   - skill → SkillLibrary.get(target_id).code（不覆盖，skill 情况由 evolver 处理）
    if data["target_type"] == "system_prompt":
        data["current_value"] = snapshot.system_prompt

    proposal = ProposedDiff(
        target_type=data["target_type"],
        target_id=data["target_id"],
        current_value=data["current_value"],
        proposed_value=data["proposed_value"],
        reasoning=data["reasoning"],
        diff_summary=data["diff_summary"],
    )

    # Step 2: FitnessEvaluator 评估
    fitness_evaluator = getattr(agent, "fitness_evaluator", None)
    if fitness_evaluator is None:
        raise RuntimeError("agent.fitness_evaluator 未注入，无法评估 prompt 适应度")
    fitness_result = fitness_evaluator.evaluate(proposal.proposed_value)

    evaluation = EvaluationResult(
        passed=fitness_result.score >= FITNESS_THRESHOLD,
        score=fitness_result.score,
        feedback=fitness_result.feedback,
        test_result=None,  # prompt 无测试执行（区别于 skill 的 exec 测试）
    )

    # Step 3: LeakageGuard 检查（fitness 通过才检查）
    leakage_guard = getattr(agent, "leakage_guard", None)
    if leakage_guard is None:
        # 未注入 LeakageGuard → 视为通过（不拦截）
        leakage_check = LeakageCheckResult(passed=True, matched_patterns=[])
    else:
        leakage_check = leakage_guard.check(proposal.proposed_value)

    # Step 4: apply（fitness + leakage 都过才 apply）
    applied = False
    if evaluation.passed and leakage_check.passed:
        apply_diff(agent, proposal)
        agent.generation += 1
        applied = True
    elif evaluation.passed and not leakage_check.passed:
        # fitness 过但 leakage 命中 → 覆盖 evaluation 为 failed
        matched = ", ".join(leakage_check.matched_patterns)
        evaluation = EvaluationResult(
            passed=False,
            score=fitness_result.score,
            feedback=f"leakage detected: 命中模式 [{matched}]",
            test_result=None,
        )

    # Step 5: archive（无论通过与否）
    # apply 成功时传新代号（agent.generation 是新代），失败传 -1
    generation_for_archive = agent.generation if applied else -1
    current_generation = agent.generation
    archive_store = getattr(agent, "archive_store", None)
    if archive_store is not None:
        archive_store.add(proposal, evaluation, generation=generation_for_archive)

    return EvolutionResult(
        generation=current_generation,
        proposal=proposal,
        evaluation=evaluation,
        leakage_check=leakage_check,
        applied=applied,
    )


# =============================================================
# 回滚函数
# =============================================================

def rollback_to(agent: Any, target_generation: int) -> None:
    """回滚到指定代数。

    Q3=B：回滚所有 target_type 的 apply 结果（当前实际只改 system_prompt，skill 未来会用到）。
    Q7=A：agent.generation 同步回退到 target_generation。

    规则：
    - target_generation=0 → 恢复第 0 代（初始 state），用第 1 代通过提案的 proposal.current_value
    - target_generation=N (N>=1) → 恢复第 N 代 apply 后的 state，用第 N 代通过提案的 proposal.proposed_value
    - 如果目标代没有通过的提案 → 抛 ValueError（不静默失败）
    """
    archive_store = getattr(agent, "archive_store", None)
    if archive_store is None:
        raise RuntimeError("agent.archive_store 未注入，无法按代回滚")

    if target_generation < 0:
        raise ValueError(f"target_generation 不能为负: {target_generation}")

    # 找到回滚后应生效的值
    if target_generation == 0:
        # 第 0 代：取第 1 代通过提案的 current_value（apply 之前的原始值）
        archived = archive_store.get_passed_at(1)
        if archived is None:
            # 没有第 1 代通过记录 → 说明没进化过，无需回滚
            if agent.generation != 0:
                agent.generation = 0
            return
        value_to_apply = archived.proposal.current_value
    else:
        # 第 N 代：取第 N 代通过提案的 proposed_value（apply 之后的新值）
        archived = archive_store.get_passed_at(target_generation)
        if archived is None:
            raise ValueError(f"第 {target_generation} 代没有通过的提案，无法回滚")
        value_to_apply = archived.proposal.proposed_value

    # 构造一个假 proposal 调 apply_diff（复用已有 apply 逻辑）
    proposal = ProposedDiff(
        target_type=archived.proposal.target_type,
        target_id=archived.proposal.target_id,
        current_value="(rollback)",
        proposed_value=value_to_apply,
        reasoning=f"rollback_to({target_generation})",
        diff_summary=f"回滚到第 {target_generation} 代",
    )
    apply_diff(agent, proposal)

    # Q7=A：generation 同步回退
    agent.generation = target_generation


# =============================================================
# 多代循环直到收敛
# =============================================================

def evolve_prompt_until_converged(
    agent: Any,
    max_generations: int = DEFAULT_MAX_GENERATIONS,
    convergence_n: int = DEFAULT_CONVERGENCE_N,
    convergence_threshold: float = DEFAULT_CONVERGENCE_THRESHOLD,
    no_progress_n: int = DEFAULT_NO_PROGRESS_N,
) -> EvolutionSummary:
    """多代循环 evolve_prompt 直到收敛或达上限。

    Q1=A：连续 convergence_n 代 fitness 提升 < threshold → 收敛停止
    Q2=A：max_generations 默认 10，到达停止
    Q6=A：连续 no_progress_n 代未 apply 成功 → 无进展停止

    返回 EvolutionSummary(generations, final_generation, final_score, stop_reason)
    """
    generations: list[EvolutionResult] = []
    # 仅记录 apply 成功代的 fitness 用于收敛检测
    applied_scores: list[float] = []
    consecutive_no_progress = 0
    stop_reason = STOP_MAX_GENERATIONS

    for _ in range(max_generations):
        result = evolve_prompt(agent)
        generations.append(result)

        if result.applied:
            applied_scores.append(result.evaluation.score)
            consecutive_no_progress = 0
            # 收敛检测（至少有 2 个 applied 分数）
            if len(applied_scores) >= 2:
                recent = applied_scores[-convergence_n:]
                if len(recent) >= convergence_n:
                    # 最近 N 代的提升最大值
                    deltas = [recent[i] - recent[i-1] for i in range(1, len(recent))]
                    max_delta = max(deltas) if deltas else 0.0
                    if max_delta < convergence_threshold:
                        stop_reason = STOP_CONVERGED
                        break
        else:
            consecutive_no_progress += 1
            # Q6=A：连续 N 代无进展停止
            if consecutive_no_progress >= no_progress_n:
                stop_reason = STOP_NO_PROGRESS
                break

    final_score = applied_scores[-1] if applied_scores else 0.0
    return EvolutionSummary(
        generations=generations,
        final_generation=agent.generation,
        final_score=final_score,
        stop_reason=stop_reason,
    )

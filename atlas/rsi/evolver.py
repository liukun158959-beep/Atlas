"""RSI 自进化闭环：propose_diff → evaluate → apply/archive。

Q2=A：作用域只改 Skill code + system prompt（不改架构层）。
Q3=A：ProposedDiff + EvaluationResult dataclass。
Q4=A：双层评估（测试执行 + LLM Critic，测试失败不调 LLM）。
Q6=A：Agent 方法委托 rsi（和 inspect_self 一致）。
Q9=A：apply_diff 自动生效（skill 新版本入库 version 递增 / system_prompt 替换 _runtime_messages[0]）。
Q10=A：self_evolve() 封装 propose→evaluate→apply/archive 三步。
Q13=A：测试执行 = exec(code) + 验证同名函数存在（最小可用性验证，不调用 func）。
Q18=A：prompt apply 安全 = 验证 role==system + 替换 content，不清空历史。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .archive import PermissionLevel, check_permission
from .inspector import build_self_snapshot
from .skill import Skill


# =============================================================
# 数据结构
# =============================================================

@dataclass
class ProposedDiff:
    """自进化提案：单次 propose_diff 的产出。"""

    target_type: str       # "skill" | "system_prompt"
    target_id: str         # skill 名字 或 "system_prompt"
    current_value: str     # 当前值（skill code 或 system prompt 文本）
    proposed_value: str    # 提议的新值
    reasoning: str         # 改进理由
    diff_summary: str      # 改动摘要


@dataclass
class EvaluationResult:
    """评估结果。"""

    passed: bool                  # 是否通过（测试 + Critic 都过才 True）
    score: float                  # 0.0-1.0
    feedback: str                 # 评估反馈
    test_result: str | None = None  # 测试执行结果：None=未执行（非 skill）/ "pass" / "fail: ..."


# =============================================================
# 工具函数
# =============================================================

def _parse_json(text: str) -> dict:
    """JSON 解析容错：先 json.loads，失败则提取首个 {...}，仍失败抛 ValueError。"""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 提取首个 {...} 块
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise ValueError(f"LLM 返回内容无法解析为 JSON: {text[:200]}")
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as e:
            raise ValueError(f"LLM 返回 JSON 解析失败: {e}; 原文: {text[:200]}")


def _test_skill_code(code: str, skill_name: str) -> str | None:
    """执行 skill code 并验证同名函数存在。

    返回 None 表示通过，str 表示错误信息（不抛异常，由调用方决定如何处理）。
    """
    namespace: dict[str, Any] = {}
    try:
        exec(code, namespace)
    except Exception as e:
        return f"exec 失败: {type(e).__name__}: {e}"
    # 验证同名函数存在
    for v in namespace.values():
        if callable(v) and getattr(v, "__name__", None) == skill_name:
            return None  # 通过
    return f"code 中未找到名为 {skill_name} 的函数"


def _build_propose_prompt(snapshot: Any, archived_failures: list[str]) -> str:
    """构造 propose_diff 的 user prompt（含 self_snapshot + recent_failures + archived_failures）。"""
    parts = [
        "# Agent 当前状态快照",
        f"## System Prompt\n{snapshot.system_prompt}",
        f"## 工具定义\n{json.dumps(snapshot.tool_definitions, ensure_ascii=False, indent=2)}",
        f"## 技能列表\n{json.dumps(snapshot.skill_summaries, ensure_ascii=False, indent=2)}",
        f"## 最近失败案例\n{json.dumps(snapshot.recent_failures, ensure_ascii=False, indent=2)}",
        f"## Memory Summary\n{snapshot.memory_summary}",
    ]
    if archived_failures:
        parts.append(
            "## 已归档的失败提案（避免重复提议）\n" + "\n".join(archived_failures)
        )
    parts.append(
        "# 任务\n"
        "基于以上状态，提出一个改进提案。可修改 target_type=skill（技能代码）或 system_prompt。\n"
        "注意：核心层（graph/agent/tool/plugin 等）只读，不可修改。\n"
        "只输出 JSON，不要附加任何解释文字：\n"
        "{\n"
        '  "target_type": "skill" | "system_prompt",\n'
        '  "target_id": "skill 名字 或 \\"system_prompt\\"",\n'
        '  "current_value": "当前值（skill code 或 system prompt 文本）",\n'
        '  "proposed_value": "提议的新值",\n'
        '  "reasoning": "改进理由",\n'
        '  "diff_summary": "改动摘要"\n'
        "}"
    )
    return "\n\n".join(parts)


def _build_evaluate_prompt(proposal: ProposedDiff, test_result: str | None) -> str:
    """构造 evaluate 的 LLM Critic user prompt。"""
    parts = [
        "# 待评估提案",
        f"- target_type: {proposal.target_type}",
        f"- target_id: {proposal.target_id}",
        f"- reasoning: {proposal.reasoning}",
        f"- diff_summary: {proposal.diff_summary}",
        f"- current_value: {proposal.current_value[:500]}",
        f"- proposed_value: {proposal.proposed_value[:500]}",
    ]
    if test_result is not None:
        parts.append(f"- test_result: {test_result}")
    parts.append(
        "# 任务\n"
        "评估此提案是否合理。测试已通过（如适用），现在做语义质量评估。\n"
        "只输出 JSON，不要附加任何解释文字：\n"
        "{\n"
        '  "passed": true | false,\n'
        '  "score": 0.0-1.0,\n'
        '  "feedback": "评估反馈"\n'
        "}"
    )
    return "\n\n".join(parts)


# =============================================================
# 核心函数
# =============================================================

def propose_diff(agent: Any) -> ProposedDiff:
    """调 LLM 生成自进化提案。

    流程：
        1. build_self_snapshot(agent) 读取当前状态
        2. 从 agent.archive_store 读取失败提案摘要（避免重复提议）
        3. 调 agent.llm.chat 生成 JSON 提案
        4. 权限校验（target_type 必须 BUFFERED，CORE 直接 PermissionError）
        5. 返回 ProposedDiff
    """
    snapshot = build_self_snapshot(agent)

    # 读取已归档失败提案摘要
    archived_failures: list[str] = []
    archive_store = getattr(agent, "archive_store", None)
    if archive_store is not None:
        for archived in archive_store.list_failed():
            p = archived.proposal
            archived_failures.append(
                f"- target={p.target_type}/{p.target_id}, "
                f"reasoning={p.reasoning[:100]}, "
                f"feedback={archived.evaluation.feedback[:100]}"
            )

    prompt = _build_propose_prompt(snapshot, archived_failures)
    messages = [
        {
            "role": "system",
            "content": "你是 Atlas 自进化系统。根据 Agent 当前状态生成一个改进提案。只输出 JSON。",
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

    # 权限校验：CORE 层只读
    permission = check_permission(data["target_type"])
    if permission == PermissionLevel.CORE:
        raise PermissionError(
            f"目标 {data['target_type']} 属于核心层（只读），propose_diff 不可修改"
        )

    return ProposedDiff(
        target_type=data["target_type"],
        target_id=data["target_id"],
        current_value=data["current_value"],
        proposed_value=data["proposed_value"],
        reasoning=data["reasoning"],
        diff_summary=data["diff_summary"],
    )


def evaluate(agent: Any, proposal: ProposedDiff) -> EvaluationResult:
    """双层评估：测试执行（仅 skill）+ LLM Critic。

    测试失败直接拒绝，不调 LLM Critic。
    """
    test_result: str | None = None

    # 仅 skill 类型执行测试（system_prompt 无测试需求）
    if proposal.target_type == "skill":
        test_result = _test_skill_code(proposal.proposed_value, proposal.target_id)
        if test_result is not None:
            # 测试失败 → 直接拒绝，不调 LLM
            return EvaluationResult(
                passed=False,
                score=0.0,
                feedback=f"测试执行失败: {test_result}",
                test_result=test_result,
            )
        test_result = "pass"

    # 测试通过或非 skill，调 LLM Critic 做语义评估
    prompt = _build_evaluate_prompt(proposal, test_result)
    messages = [
        {
            "role": "system",
            "content": "你是 Atlas 自进化评估器。评估提案质量。只输出 JSON。",
        },
        {"role": "user", "content": prompt},
    ]
    llm_result = agent.llm.chat(messages)
    content = llm_result["message"].get("content", "")
    data = _parse_json(content)

    required = ["passed", "score", "feedback"]
    for k in required:
        if k not in data:
            raise ValueError(f"Critic 返回缺少字段: {k}; 原文: {content[:200]}")

    return EvaluationResult(
        passed=bool(data["passed"]),
        score=float(data["score"]),
        feedback=str(data["feedback"]),
        test_result=test_result,
    )


def apply_diff(agent: Any, proposal: ProposedDiff) -> None:
    """应用提案：skill 新版本入库 / system_prompt 替换。

    Q9=A：skill 新版本入库 version 递增（SkillLibrary.register 自动处理）。
    Q18=A：system_prompt 替换前验证 role==system，只替换 content，不清空历史。
    """
    if proposal.target_type == "skill":
        skill_library = getattr(agent, "skill_library", None)
        if skill_library is None:
            raise RuntimeError("agent.skill_library 未注入，无法 apply skill 提案")
        # 构造新 Skill，version 由 register 自动递增
        new_skill = Skill(
            name=proposal.target_id,
            description="",       # 4.2a 不更新 description
            code=proposal.proposed_value,
            embedding=[],         # register 时自动 embed
        )
        skill_library.register(new_skill)
    elif proposal.target_type == "system_prompt":
        # Q18=A：验证 role==system + 替换 content，不清空历史
        messages = agent.memory.messages
        if not messages or messages[0].get("role") != "system":
            raise RuntimeError(
                "memory.messages[0] 不是 system 消息，无法替换 system_prompt"
            )
        messages[0]["content"] = proposal.proposed_value
    else:
        raise ValueError(f"不支持的 target_type: {proposal.target_type}")


def self_evolve(agent: Any, archive_store: Any = None) -> EvaluationResult:
    """闭环函数：propose → evaluate → apply/archive。

    流程：
        1. propose_diff(agent) 生成提案
        2. evaluate(agent, proposal) 评估
        3. passed → apply_diff(agent, proposal) 应用
        4. archive_store.add(proposal, evaluation) 归档（无论通过与否）

    archive_store 为 None 时从 agent.archive_store 读取。
    返回 EvaluationResult。
    """
    if archive_store is None:
        archive_store = getattr(agent, "archive_store", None)

    # Step 1: 提案
    proposal = propose_diff(agent)

    # Step 2: 评估
    evaluation = evaluate(agent, proposal)

    # Step 3: apply（通过）+ archive（全部）
    if evaluation.passed:
        apply_diff(agent, proposal)
    if archive_store is not None:
        archive_store.add(proposal, evaluation)

    return evaluation

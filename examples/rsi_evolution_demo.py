"""Phase 4.2a RSI 自进化闭环演示：propose_diff + evaluate + apply + archive + 分层权限。

7 项断言：
  ① propose_diff 返回 ProposedDiff，target_type='skill'
  ② check_permission：skill=BUFFERED / graph=CORE
  ③ evaluate 测试通过：合法 skill code 通过 exec + 同名函数存在验证
  ④ evaluate 测试拒绝：有语法错误的 skill code 不调 LLM Critic（_step 不变）
  ⑤ Critic 通过：LLM 返回 passed=true / score=0.85
  ⑥ apply 版本递增：SkillLibrary.register 后 latest_version 递增
  ⑦ archive 失败反馈：失败提案归档后，下次 propose_diff 的 prompt 含归档摘要

FakeLLM 调用序列（5 次）：
  #1 propose_diff（成功闭环）：返回合法 skill 提案 JSON
  #2 evaluate Critic（成功闭环）：返回 passed=true JSON
  #3 propose_diff（失败闭环）：返回有语法错误的 skill 提案 JSON
     evaluate（失败闭环）：测试失败，不调 LLM
  #4 propose_diff（archive 反馈）：返回合法 skill 提案 JSON
  #5 evaluate Critic（archive 反馈）：返回 passed=true JSON

运行方式：
    python examples/rsi_evolution_demo.py --plain
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path

from atlas import Agent
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.rsi import (
    ArchiveStore,
    FakeEmbedding,
    ProposedDiff,
    Skill,
    SkillLibrary,
    SQLiteVectorStore,
    apply_diff,
    check_permission,
    evaluate,
    propose_diff,
)
from atlas.rsi.archive import PermissionLevel
from atlas.tools.base import Toolkit


# =============================================================
# 初始 Skill 代码 + FakeLLM 返回的代码
# =============================================================

# 初始 v1：简单实现，无输入验证
SKILL_V1_CODE = '''def evolve_skill(params):
    """v1：原始实现"""
    return str(params["x"])
'''

# FakeLLM 返回的合法代码（成功闭环 + archive 反馈用）
GOOD_SKILL_CODE = '''def evolve_skill(params):
    """v2：增加输入验证"""
    x = params.get("x", 0)
    return str(int(x) * 2)
'''

# FakeLLM 返回的有语法错误代码（失败闭环用，缺少冒号）
BAD_SKILL_CODE = '''def evolve_skill(params)
    """缺少冒号的语法错误"""
    return str(params["x"])
'''


# =============================================================
# FakeLLM：5 次调用按 _propose_count 区分 propose / evaluate
# =============================================================
@dataclass
class EvolveFakeLLM(LLM):
    """Phase 4.2a 自进化 demo FakeLLM。

    按 system prompt 区分 propose_diff / evaluate：
      - 含"自进化系统" → propose（按 _propose_count 返回不同提案）
      - 含"自进化评估器" → evaluate（返回 passed=true/score=0.85）
    """

    model: str = "mock-evolve"
    _step: int = 0
    _propose_count: int = 0
    last_user_prompt: str = ""  # 捕获最近一次 user prompt（断言⑦用）

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        self._step += 1
        sys_msg = messages[0].get("content", "") if messages else ""
        user_prompt = messages[1].get("content", "") if len(messages) > 1 else ""
        self.last_user_prompt = user_prompt

        if "自进化系统" in sys_msg:
            # propose_diff：按 _propose_count 返回不同提案
            self._propose_count += 1
            if self._propose_count == 1:
                # 成功闭环：合法 skill 提案
                content = json.dumps({
                    "target_type": "skill",
                    "target_id": "evolve_skill",
                    "current_value": SKILL_V1_CODE,
                    "proposed_value": GOOD_SKILL_CODE,
                    "reasoning": "增加输入验证，避免 KeyError",
                    "diff_summary": "添加 params.get 默认值 + int 转换",
                }, ensure_ascii=False)
            elif self._propose_count == 2:
                # 失败闭环：有语法错误的 skill 提案
                content = json.dumps({
                    "target_type": "skill",
                    "target_id": "evolve_skill",
                    "current_value": SKILL_V1_CODE,
                    "proposed_value": BAD_SKILL_CODE,
                    "reasoning": "故意引入语法错误，测试评估机制拦截",
                    "diff_summary": "故意缺少冒号",
                }, ensure_ascii=False)
            else:
                # archive 反馈：合法 skill 提案
                content = json.dumps({
                    "target_type": "skill",
                    "target_id": "evolve_skill",
                    "current_value": SKILL_V1_CODE,
                    "proposed_value": GOOD_SKILL_CODE,
                    "reasoning": "archive 反馈后再次提案，避开历史失败",
                    "diff_summary": "改进版本 v3",
                }, ensure_ascii=False)
        else:
            # evaluate Critic：返回通过
            content = json.dumps({
                "passed": True,
                "score": 0.85,
                "feedback": "提案合理，改进了输入验证",
            }, ensure_ascii=False)

        return {
            "message": {"role": "assistant", "content": content, "tool_calls": None},
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        }


# =============================================================
# 构造 Agent（含 skill_library + archive_store）
# =============================================================
def build_agent() -> tuple[Agent, SkillLibrary, ArchiveStore, SQLiteVectorStore]:
    """构造带初始 evolve_skill v1 的 Agent。"""
    emb = FakeEmbedding()
    vs = SQLiteVectorStore()
    sl = SkillLibrary(emb, vs)
    # 初始注册 evolve_skill v1
    sl.register(Skill("evolve_skill", "演化测试技能", SKILL_V1_CODE, []))

    archive = ArchiveStore()
    agent = Agent(
        llm=EvolveFakeLLM(),
        tools=Toolkit(),
        system_prompt="你是 RSI 自进化测试 Agent",
        skill_library=sl,
        archive_store=archive,
    )
    return agent, sl, archive, vs


# =============================================================
# Part 1：成功闭环（断言①②③⑤⑥）
# =============================================================
def part1_success_loop(
    agent: Agent, sl: SkillLibrary, archive: ArchiveStore, renderer
) -> None:
    """成功闭环：propose → evaluate → apply，验证 6 项断言中的 5 项。"""
    renderer.rule("Part 1：成功闭环（propose → evaluate → apply）")

    # ② check_permission 权限校验
    assert check_permission("skill") == PermissionLevel.BUFFERED, "断言② 失败：skill 应为 BUFFERED"
    assert check_permission("system_prompt") == PermissionLevel.BUFFERED, "断言② 失败：system_prompt 应为 BUFFERED"
    assert check_permission("graph") == PermissionLevel.CORE, "断言② 失败：graph 应为 CORE"
    assert check_permission("agent") == PermissionLevel.CORE, "断言② 失败：agent 应为 CORE"
    renderer.print("assert ②: OK（skill/system_prompt=BUFFERED, graph/agent=CORE）")

    # 记录 apply 前的 version
    version_before = sl.latest_version("evolve_skill")
    assert version_before == 1, f"初始 version 应为 1，实际 {version_before}"
    renderer.print(f"apply 前 latest_version('evolve_skill') = {version_before}")

    # ① propose_diff 返回 ProposedDiff
    proposal = propose_diff(agent)
    assert isinstance(proposal, ProposedDiff), "断言① 失败：应返回 ProposedDiff"
    assert proposal.target_type == "skill", f"断言① 失败：target_type 应为 skill，实际 {proposal.target_type}"
    assert proposal.target_id == "evolve_skill", f"断言① 失败：target_id 应为 evolve_skill"
    assert "params.get" in proposal.proposed_value, "断言① 失败：proposed_value 应含改进代码"
    renderer.print(f"assert ①: OK（target={proposal.target_type}/{proposal.target_id}, reasoning={proposal.reasoning[:40]}）")

    # ③⑤ evaluate：测试通过 + Critic 通过
    evaluation = evaluate(agent, proposal)
    assert evaluation.test_result == "pass", f"断言③ 失败：test_result 应为 pass，实际 {evaluation.test_result}"
    renderer.print(f"assert ③: OK（test_result={evaluation.test_result}）")
    assert evaluation.passed is True, f"断言⑤ 失败：passed 应为 True"
    assert evaluation.score == 0.85, f"断言⑤ 失败：score 应为 0.85，实际 {evaluation.score}"
    renderer.print(f"assert ⑤: OK（passed={evaluation.passed}, score={evaluation.score}, feedback={evaluation.feedback[:40]}）")

    # ⑥ apply_diff 后 version 递增
    apply_diff(agent, proposal)
    version_after = sl.latest_version("evolve_skill")
    assert version_after == version_before + 1, f"断言⑥ 失败：version 应递增到 {version_before + 1}，实际 {version_after}"
    new_skill = sl.get(f"evolve_skill_v{version_after}")
    assert new_skill is not None, f"断言⑥ 失败：v{version_after} 应存在"
    # 验证新版本可执行
    assert new_skill.to_tool().func({"x": "5"}) == "10", "断言⑥ 失败：新 skill 执行应返回 10"
    renderer.print(f"assert ⑥: OK（version {version_before} -> {version_after}, 新 skill 可执行 evolve_skill(x=5)=10）")

    # 归档成功提案（后续 Part 3 不依赖此条，但保持闭环完整）
    archive.add(proposal, evaluation)
    renderer.print(f"archive: 成功提案已归档（archive.count={archive.count()}）")


# =============================================================
# Part 2：失败闭环（断言④）
# =============================================================
def part2_failure_loop(agent: Agent, archive: ArchiveStore, renderer) -> None:
    """失败闭环：propose（错误代码）→ evaluate（测试失败，不调 LLM）→ archive。"""
    renderer.rule("Part 2：失败闭环（测试拒绝 + 不调 LLM）")

    # propose：FakeLLM 第 2 次 propose 返回有语法错误的代码
    proposal = propose_diff(agent)
    assert "def evolve_skill(params)" in proposal.proposed_value, "断言④前置失败：应为有语法错误的代码"
    renderer.print(f"propose: 收到有语法错误的提案（diff_summary={proposal.diff_summary}）")

    # 记录 evaluate 前的 LLM 调用次数
    step_before = agent.llm._step

    # evaluate：测试失败，应直接拒绝，不调 LLM Critic
    evaluation = evaluate(agent, proposal)

    # ④ 测试拒绝 + LLM 未被调用
    assert evaluation.passed is False, "断言④ 失败：测试失败时 passed 应为 False"
    assert evaluation.test_result != "pass", "断言④ 失败：test_result 不应为 pass"
    assert evaluation.test_result is not None, "断言④ 失败：test_result 应为错误信息"
    assert "exec 失败" in evaluation.test_result or "未找到" in evaluation.test_result, (
        f"断言④ 失败：test_result 应含错误描述，实际 {evaluation.test_result}"
    )
    assert agent.llm._step == step_before, (
        f"断言④ 失败：测试失败时 LLM 不应被调用，_step 应保持 {step_before}，实际 {agent.llm._step}"
    )
    renderer.print(f"assert ④: OK（passed=False, test_result={evaluation.test_result[:60]}, LLM _step 不变={step_before}）")

    # 归档失败提案（Part 3 archive 反馈用）
    archive.add(proposal, evaluation)
    failed_list = archive.list_failed()
    assert len(failed_list) == 1, f"断言④ 后置失败：list_failed 应有 1 条，实际 {len(failed_list)}"
    renderer.print(f"archive: 失败提案已归档（list_failed={len(failed_list)}, archive.count={archive.count()}）")


# =============================================================
# Part 3：archive 失败反馈（断言⑦）
# =============================================================
def part3_archive_feedback(agent: Agent, archive: ArchiveStore, renderer) -> None:
    """archive 反馈：再次 propose 时 prompt 应含已归档失败提案摘要。"""
    renderer.rule("Part 3：archive 失败反馈（下次 propose prompt 含归档摘要）")

    # 再次 propose：FakeLLM 第 3 次 propose
    proposal = propose_diff(agent)

    # ⑦ prompt 含归档失败摘要
    assert "已归档的失败提案" in agent.llm.last_user_prompt, (
        "断言⑦ 失败：propose prompt 应含 '已归档的失败提案'"
    )
    assert "evolve_skill" in agent.llm.last_user_prompt, (
        "断言⑦ 失败：propose prompt 应含失败提案的 target_id"
    )
    renderer.print("assert ⑦: OK（propose prompt 含 '已归档的失败提案' + target_id）")
    renderer.print(f"  prompt 片段: ...{agent.llm.last_user_prompt[-200:]}")

    # 闭环可继续：evaluate 通过
    evaluation = evaluate(agent, proposal)
    assert evaluation.passed is True, "断言⑦ 后置失败：archive 反馈后闭环应可继续"
    renderer.print(f"闭环可继续：evaluate passed={evaluation.passed}, score={evaluation.score}")

    # 归档
    archive.add(proposal, evaluation)
    renderer.print(f"archive: 反馈提案已归档（archive.count={archive.count()}）")


# =============================================================
# 主函数
# =============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 4.2a RSI 自进化闭环 Demo")
    parser.add_argument("--plain", action="store_true", help="PlainRenderer（不依赖 Rich）")
    args = parser.parse_args()

    renderer = PlainRenderer() if args.plain else RichRenderer()

    # 清理上次运行残留的向量数据库（确保 demo 可复现）
    for db_file in [".atlas/vectors.db", ".atlas/failures.db"]:
        p = Path(db_file)
        if p.exists():
            p.unlink()

    renderer.rule("Phase 4.2a RSI 自进化闭环 Demo（7 项断言）")
    renderer.print("范围：propose_diff + evaluate + apply_diff + ArchiveStore + PermissionLevel")
    renderer.print("FakeLLM: 5 次调用（成功闭环 #1-2 + 失败闭环 #3 + archive 反馈 #4-5）")
    renderer.print("")

    # 构造 Agent
    agent, sl, archive, vs = build_agent()
    renderer.print(f"初始: evolve_skill v1 已注册 / archive_store 已注入 / LLM={agent.llm.model}")
    renderer.print("")

    # Part 1：成功闭环（断言①②③⑤⑥）
    part1_success_loop(agent, sl, archive, renderer)
    renderer.print("")

    # Part 2：失败闭环（断言④）
    part2_failure_loop(agent, archive, renderer)
    renderer.print("")

    # Part 3：archive 失败反馈（断言⑦）
    part3_archive_feedback(agent, archive, renderer)

    # 清理
    vs.close()

    renderer.rule("全部 7 项断言通过")
    renderer.print("Phase 4.2a RSI 自进化闭环验证完成。")
    renderer.print(f"LLM 调用总数: {agent.llm._step}（预期 5 次）")
    renderer.print(f"Archive 归档总数: {archive.count()}（含 1 失败 + 2 成功）")


if __name__ == "__main__":
    main()

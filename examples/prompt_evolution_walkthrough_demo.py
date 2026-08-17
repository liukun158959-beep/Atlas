"""
Prompt 进化 Walkthrough Demo
============================
展示完整过程：每一代的 fitness 分数、proposal 完整内容、leakage 检查、
反思式变异注入的历史档案、最终 system_prompt 的对比。

用法（默认 FakeLLM 零成本、100% 可复现）:
    python examples/prompt_evolution_walkthrough_demo.py --plain

切换真实 LLM（消耗 token）:
    python examples/prompt_evolution_walkthrough_demo.py --plain --real-llm

只跑部分模块（如模块1+3）:
    python examples/prompt_evolution_walkthrough_demo.py --plain --modules 1,3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from dotenv import load_dotenv

from atlas import Agent
from atlas.llm.base import LLM
from atlas.tools.base import Toolkit
from atlas.rsi import (
    ArchiveStore,
    FakeFitnessEvaluator,
    RegexLeakageGuard,
)
from atlas.rsi.prompt_evolver import (
    _extract_archived_failures,
    _extract_fitness_history,
    evolve_prompt,
    evolve_prompt_until_converged,
    rollback_to,
)
from atlas.rsi.fitness import FitnessResult


SEP = "=" * 78
HSEP = "-" * 78


# ============================================================= FakeLLM
@dataclass
class WalkthroughFakeLLM(LLM):
    """专门为 Walkthrough 设计的假 LLM。

    区分两种 propose 上下文：
      - 模块 1/3：随机 propose（3 次调用，按 call_count 返回）
      - 模块 2：收敛 propose（7 次调用递增序列，配合 CvFitness 0.2..0.721）
    用 `_mode` 切换，由 demo 函数在创建时传入。
    """
    _mode: str = "single"   # "single" | "converge" | "rollback"
    _call_count: int = 0
    _received_prompts: list[str] = field(default_factory=list)

    model: str = "mock-walkthrough"

    def _record(self, messages):
        for m in messages:
            if m.get("role") == "user":
                self._received_prompts.append(m.get("content", ""))
                break

    def _resp(self, proposed_value, reasoning, diff_summary, current_value="你是助手。"):
        content = json.dumps({
            "target_type": "system_prompt",
            "target_id": "system_prompt",
            "current_value": current_value,
            "proposed_value": proposed_value,
            "reasoning": reasoning,
            "diff_summary": diff_summary,
        }, ensure_ascii=False)
        return {
            "message": {"role": "assistant", "content": content},
            "usage": {"prompt_tokens": 120, "completion_tokens": 240, "total_tokens": 360},
            "duration_ms": 8,
        }

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        self._call_count += 1
        self._record(messages)
        n = self._call_count

        if self._mode == "single":
            # 模块1：第1代 0.4 fail、第2代 0.8 pass、第3代 0.5 pass（FakeFitness custom_sequence=[0.4,0.8,0.5]）
            if n == 1:
                return self._resp(
                    "你是助手。请尽量回答用户问题，保持礼貌友善。",
                    "增加礼貌与回答意愿要求，基础版改造。",
                    "+礼貌 +回答意愿",
                    current_value="你是助手。",
                )
            if n == 2:
                return self._resp(
                    "你是专业助手。角色：精准执行者+主动澄清者。行为准则：① 遇到模糊请求主动追问；② 事实性内容标注来源；③ 越权操作明确拒绝。输出需结构清晰、分步骤说明。",
                    "反思：上一代 fitness=0.4，缺少角色定义、边界约束、来源标注、结构化输出要求。一次性补全这 4 个维度。",
                    "+角色定义(2维) +边界 +来源标注 +结构化输出",
                    current_value="你是助手。请尽量回答用户问题，保持礼貌友善。",
                )
            if n == 3:
                return self._resp(
                    "你是专业助手（DAP v2）。角色：精准执行者+主动澄清者+责任共担者+失败归因反馈者。行为协议：① 歧义必结构化追问（枚举选项）；② 事实引用 2024Q2 前三类权威来源；③ 越权请求引用《生成式AI服务管理暂行办法》拒绝；④ 输出采用「前提→推导→结论+置信度」四段结构。",
                    "基于前代 fitness=0.8 继续打磨：细化追问形式、限定来源范围、引用法律依据、强化输出结构协议。",
                    "+失败归因维度 +追问形式化 +来源范围 +法规引用 +四段式输出协议",
                    current_value="你是专业助手。角色：精准执行者+主动澄清者。行为准则：① 遇到模糊请求主动追问；② 事实性内容标注来源；③ 越权操作明确拒绝。输出需结构清晰、分步骤说明。",
                )
            raise RuntimeError("single 模式只规划 3 次 propose，实际第 %d 次" % n)

        if self._mode == "converge":
            # 模块2：7 代连续改进，对应 fitness 序列 [0.2,0.5,0.7,0.71,0.72,0.72,0.721]
            seq = [
                ("你是助手。请礼貌回答。", "+礼貌", 0.2),
                ("你是助手。回答准确、信息简洁。", "+准确+简洁", 0.5),
                ("你是专业AI助手：先理解再输出、结构分层、不编造事实。", "+角色+理解分层+不编造", 0.7),
                ("你是专业AI助手：理解前置→分层输出→不编造→事实尽量标注来源（如公开资料）。", "+标注来源(公开资料)", 0.71),
                ("你是专业AI助手（v3）：理解前置→分层输出→不编造→事实标注来源→模糊处主动追问。", "+主动追问", 0.72),
                ("你是专业AI助手（v4）：理解前置→分层输出→不编造→事实标注来源→主动追问→越权明确拒绝。", "+越权拒绝", 0.72),
                ("你是专业AI助手（v5）：理解前置→分层输出→不编造→事实标注来源→主动追问→越权明确拒绝→输出结尾标注局限。", "+结尾局限声明", 0.721),
            ]
            if 1 <= n <= len(seq):
                pv, diffs, _ = seq[n - 1]
                cur = "你是助手。" if n == 1 else seq[n - 2][0]
                return self._resp(
                    pv,
                    "逐代打磨，本次改进重点：%s。" % diffs,
                    diffs,
                    current_value=cur,
                )
            raise RuntimeError("converge 模式只规划 7 次 propose，实际第 %d 次" % n)

        if self._mode == "rollback":
            # 模块3：3 代，fitness [0.3,0.6,0.8] → gen0 fail, gen1/2 pass
            if n == 1:
                return self._resp(
                    "你是助手（草稿）。尝试回答所有问题。", "初次改造：放宽范围，先覆盖。", "+覆盖范围",
                )
            if n == 2:
                return self._resp(
                    "你是专业助手 v1。基本规则：准确、礼貌、不越权。", "v0 score=0.3，缺少边界与质量定义，加 v1 三条规则。", "+三规则 v1",
                    current_value="你是助手（草稿）。尝试回答所有问题。",
                )
            if n == 3:
                return self._resp(
                    "你是专业助手 v2。角色：执行者+澄清者；规则：(1)理解→(2)分层输出→(3)标注来源→(4)越权拒绝。", "v1 score=0.6，细化角色 +四步流程协议。", "+角色 +四步流程 v2",
                    current_value="你是专业助手 v1。基本规则：准确、礼貌、不越权。",
                )
            raise RuntimeError("rollback 模式只规划 3 次 propose，实际第 %d 次" % n)

        raise RuntimeError("未知 mode=%s" % self._mode)


# ============================================================= helpers


def _sys_of(agent: Agent) -> str:
    return agent.memory.runtime_messages[0]["content"]


def _make_llm(use_fake: bool, mode: str) -> LLM:
    if use_fake:
        return WalkthroughFakeLLM(_mode=mode)
    # 真实 LLM：调用方必须保证已 load_dotenv
    return LLM()


# ============================================================= module 1


def demo_walkthrough_single_gen(use_fake: bool = True) -> None:
    """单代全过程拆解：proposal 六字段 + Leakage + Fitness + apply + Archive 状态 + 反思上下文。"""
    print()
    print(SEP)
    print("【模块 1 / 3】单代 evolve_prompt 全过程拆解")
    print(SEP)

    FIT_SEQ = [0.4, 0.8, 0.5]

    class LocalFitness:
        def __init__(self):
            self._seq = FIT_SEQ[:]
            self._idx = 0

        def evaluate(self, prompt):
            s = self._seq[self._idx]
            self._idx += 1
            return FitnessResult(score=s, feedback="fitness=%.1f（确定性序列）" % s)

    fe = LocalFitness() if use_fake else FakeFitnessEvaluator()
    fit_note = "FakeFitnessEvaluator 固定序列 [0.4→0.8→0.3]" if (not use_fake) else (
        "LocalFitness 自定义序列 %s" % FIT_SEQ
    )

    agent = Agent(
        llm=_make_llm(use_fake, "single"),
        tools=Toolkit(),
        system_prompt="你是助手。",
        fitness_evaluator=fe,
        leakage_guard=RegexLeakageGuard(),
        archive_store=ArchiveStore(),
    )
    original = _sys_of(agent)
    print()
    print("初始 system_prompt : %s" % original)
    print("初始 generation    : %d" % agent.generation)
    print("Fitness 评估器     : %s" % fit_note)
    print("Apply 阈值         : FITNESS_THRESHOLD = 0.5（score ≥0.5 才 apply 并 generation+=1）")
    print("LeakageGuard       : RegexLeakageGuard（内置中英文越狱模式黑名单，可追加 extra_patterns）")
    print("LLM 后端           : %s" % ("WalkthroughFakeLLM（零成本，100% 可复现）" if use_fake else "真实 LLM"))
    print()

    # ----- 第 1 代 -----
    print(HSEP)
    print(">>> evolve_prompt(agent) —— 第 1 代 (预期 score=0.4 < 0.5，SKIP apply)")
    print(HSEP)
    r1 = evolve_prompt(agent)
    print()
    print("  [1/4] Proposal（LLM 生成的六字段 JSON，由 atlas/rsi/proposal.py 解析）")
    print("    target_type    : %s" % r1.proposal.target_type)
    print("    target_id      : %s" % r1.proposal.target_id)
    print("    current_value  : %s" % r1.proposal.current_value[:160])
    print("    proposed_value : %s" % r1.proposal.proposed_value[:160])
    print("    reasoning      : %s" % r1.proposal.reasoning[:200])
    print("    diff_summary   : %s" % r1.proposal.diff_summary[:160])
    print()
    print("  [2/4] Leakage Guard（RegexLeakageGuard.check，prompt_evolver.py）")
    print("    passed         : %s" % r1.leakage_check.passed)
    print("    matched        : %s" % r1.leakage_check.matched_patterns)
    if not r1.leakage_check.passed:
        print("    * 命中黑名单 → 跳过 fitness，直接 applied=False")
    print()
    print("  [3/4] Fitness（FakeFitnessEvaluator.evaluate → score 确定性序列）")
    print("    score          : %.2f" % r1.evaluation.score)
    print("    feedback       : %s" % r1.evaluation.feedback[:160])
    print()
    print("  [4/4] Apply 判定（score ≥ 0.5 才替换 system_prompt 并 generation += 1）")
    print("    applied        : %s" % r1.applied)
    print("    generation 现  : %d  （预期 = 0，未 apply）" % agent.generation)
    print("    当前 system    : %s" % _sys_of(agent)[:120])
    print()
    print("  Archive 状态（失败提案写入 failed，作为下一代反思素材）:")
    p_list = agent.archive_store.list_passed()
    f_list = agent.archive_store.list_failed()
    print("    passed proposals : %d" % len(p_list))
    print("    failed proposals : %d" % len(f_list))
    if f_list:
        print("    最新失败 score=%.2f : %s" % (f_list[-1].evaluation.score, f_list[-1].proposal.proposed_value[:80]))

    # ----- 反思上下文（第 2 代 propose 前注入） -----
    print()
    print(HSEP)
    print(">>> 第 2 代前的【反思上下文】（注入到 propose prompt 的两部分）")
    print(HSEP)
    fhist = _extract_fitness_history(agent)
    fails = _extract_archived_failures(agent)
    print("注入内容 A: Fitness 历史（反思依据）")
    if fhist:
        for line in fhist:
            print("   |", line)
    else:
        print("   | (空，第一代还没产生档案)")
    print()
    print("注入内容 B: 已归档的失败提案（避免重复）")
    if fails:
        for line in fails:
            print("   |", line[:160])
    else:
        print("   | (无)")
    print()
    print("  以上两部分会在 evolve_prompt() 内通过 _build_evolution_prompt() 拼接")
    print("  到 LLM 的 user message 中，作为反思式变异输入。")

    # ----- 第 2 代 -----
    print()
    print(HSEP)
    print(">>> evolve_prompt(agent) —— 第 2 代 (预期 score=0.8 ≥ 0.5，APPLY)")
    print(HSEP)
    r2 = evolve_prompt(agent)
    print("  score    : %.2f" % r2.evaluation.score)
    print("  applied  : %s" % r2.applied)
    print("  leakage  : passed=%s, matched=%s" % (r2.leakage_check.passed, r2.leakage_check.matched_patterns))
    print("  generation: %d  （预期 = 1，generation 同步 +1）" % agent.generation)
    print("  新 system_prompt 前 160 字:")
    print("   ", _sys_of(agent)[:160])
    print()
    print("  reasoning 全文（反思式变异生效，引用前代失败）:")
    for ln in r2.proposal.reasoning.splitlines()[:6]:
        print("   |", ln[:120])

    # ----- 第 3 代（再 apply 一次，给模块 3 回滚留素材） -----
    print()
    print(HSEP)
    print(">>> evolve_prompt(agent) —— 第 3 代 (score=0.5 压线通过，继续 APPLY)")
    print(HSEP)
    r3 = evolve_prompt(agent)
    print("  score    : %.2f  applied=%s  generation=%d" % (
        r3.evaluation.score, r3.applied, agent.generation))
    print("  diff_summary:", r3.proposal.diff_summary)
    print("  最终 generation = %d" % agent.generation)
    print("  archive: passed=%d failed=%d" % (
        len(agent.archive_store.list_passed()), len(agent.archive_store.list_failed())))


# ============================================================= module 2


def demo_walkthrough_converge(use_fake: bool = True) -> None:
    """多代收敛：7 代 fitness 序列、convergence_n=3 + threshold=0.01 触发 stop_reason='converged'。"""
    print()
    print(SEP)
    print("【模块 2 / 3】多代进化直到收敛（evolve_prompt_until_converged）")
    print(SEP)

    class CvFitness:
        def __init__(self):
            self._seq = [0.2, 0.5, 0.7, 0.71, 0.72, 0.72, 0.721]
            self._idx = 0

        def evaluate(self, prompt):
            s = self._seq[self._idx]
            self._idx += 1
            return FitnessResult(score=s, feedback="fitness=%.3f" % s)

    agent = Agent(
        llm=_make_llm(use_fake, "converge"),
        tools=Toolkit(),
        system_prompt="你是助手。",
        fitness_evaluator=CvFitness(),
        leakage_guard=RegexLeakageGuard(),
        archive_store=ArchiveStore(),
    )
    original = _sys_of(agent)
    print()
    print("初始 prompt             : %s" % original)
    print("max_generations         : 10  （上限兜底，防止无限跑）")
    print("convergence_n           : 3   （连续几代 applied fitness 提升都 < threshold 即收敛）")
    print("convergence_threshold   : 0.01")
    print("no_progress_n           : 3   （连续几代都没 apply 成功则放弃，避免 LLM 一直瞎 propose）")
    print("Fitness 序列            : [0.2, 0.5, 0.7, 0.71, 0.72, 0.72, 0.721]  （7 次全 applied）")
    print()

    print(HSEP)
    print(">>> agent.evolve_prompt_until_converged(max_gen=10, conv_n=3, conv_thr=0.01, no_prg_n=3)")
    print(HSEP)

    summary = evolve_prompt_until_converged(
        agent,
        max_generations=10,
        convergence_n=3,
        convergence_threshold=0.01,
        no_progress_n=3,
    )

    print()
    print("最终 EvolutionSummary（prompt_evolver.py dataclass）:")
    print("  stop_reason      : %s  ('converged' | 'max_generations' | 'no_progress')" % summary.stop_reason)
    print("  final_generation : %d" % summary.final_generation)
    print("  final_score      : %.3f" % summary.final_score)
    print("  generations 数   : %d  （实际跑的次数）" % len(summary.generations))
    print()
    print("每代明细（逐代打印 proposed_value + fitness 增量；仅 APPLY 代参与收敛判定）:")
    prev = None
    applied_score_list = []
    for i, g in enumerate(summary.generations):
        if g.applied:
            applied_score_list.append(g.evaluation.score)
            if prev is not None:
                delta = g.evaluation.score - prev
                d_str = "  Δ=%+.4f  （与上一代 applied score 对比）" % delta
            else:
                d_str = ""
            applied_mark = "APPLY"
        else:
            d_str = ""
            applied_mark = "SKIP"
        print("  gen %-2d  score=%-6.3f  %-5s  %s" % (
            i, g.evaluation.score, applied_mark, d_str))
        print("            diff_summary : %s" % g.proposal.diff_summary[:80])
        print("            prompt 前 80 : %s" % g.proposal.proposed_value[:80])
        if g.applied:
            prev = g.evaluation.score

    print()
    print("收敛判定（与核心算法一致：取 applied_score_list 最近 convergence_n=3 个分数，")
    print("  计算相邻 deltas，取 max_delta，若 max_delta < threshold=0.01 → converged）:")
    if len(applied_score_list) >= 3:
        recent = applied_score_list[-3:]
        deltas = [recent[i] - recent[i - 1] for i in range(1, len(recent))]
        max_delta = max(deltas) if deltas else 0.0
        print("  applied_score_list (APPLY) = %s" % [round(x, 4) for x in applied_score_list])
        print("  最近 %d 个 applied scores  : %s" % (len(recent), [round(x, 4) for x in recent]))
        print("  相邻 deltas              : %s" % [round(x, 4) for x in deltas])
        print("  max_delta = %.4f" % max_delta)
        if max_delta < 0.01:
            print("  %.4f < 0.01 → 判定收敛，stop_reason='%s' ✓ (与 EvolutionSummary.stop_reason=%s 一致)"
                  % (max_delta, "converged", summary.stop_reason))
        else:
            print("  %.4f >= 0.01 → 未收敛（但实际 stop_reason=%s，请检查 fitness 序列与算法）"
                  % (max_delta, summary.stop_reason))
    else:
        print("  applied 代数不足 %d，无法执行收敛判定。" % 3)


# ============================================================= module 3


def demo_walkthrough_rollback(use_fake: bool = True) -> None:
    """回滚过程：先跑 3 代 → rollback 0/1/2 → 逐次检查 system_prompt 精确复原 + generation 同步。"""
    print()
    print(SEP)
    print("【模块 3 / 3】多代回滚 rollback_to(N) 精确恢复")
    print(SEP)

    class RbFitness:
        def __init__(self):
            self._seq = [0.3, 0.6, 0.8]
            self._idx = 0

        def evaluate(self, prompt):
            s = self._seq[self._idx]
            self._idx += 1
            return FitnessResult(score=s, feedback="fitness=%.1f" % s)

    agent = Agent(
        llm=_make_llm(use_fake, "rollback"),
        tools=Toolkit(),
        system_prompt="你是助手。",
        fitness_evaluator=RbFitness(),
        leakage_guard=RegexLeakageGuard(),
        archive_store=ArchiveStore(),
    )
    original = _sys_of(agent)
    print()
    print("gen0 原始 prompt     : %s   ← rollback_to(0) 的目标（snapshot 强制覆盖，不信赖 LLM 值）" % original)
    print()

    print(">>> 先跑 3 代 evolve_prompt（gen0 fail, gen1 pass, gen2 pass）")
    r0 = evolve_prompt(agent); print("  gen0: score=%.1f applied=%-5s generation=%d  (SKIP：0.3<0.5，写入 archive.failed)" % (r0.evaluation.score, r0.applied, agent.generation))
    r1 = evolve_prompt(agent); print("  gen1: score=%.1f applied=%-5s generation=%d  (APPLY：首次通过)" % (r1.evaluation.score, r1.applied, agent.generation))
    r2 = evolve_prompt(agent); print("  gen2: score=%.1f applied=%-5s generation=%d  (APPLY：二次通过)" % (r2.evaluation.score, r2.applied, agent.generation))
    gen1_val = r1.proposal.proposed_value
    gen2_val = r2.proposal.proposed_value
    print()
    print("  gen1 目标值 前 80 字: %s" % gen1_val[:80])
    print("  gen2 目标值 前 80 字: %s" % gen2_val[:80])
    print()
    print(">>> agent.archive_store.get_passed_at(N) 按代查询（rollback_to 内部数据源）")
    print("  passed 代数 : %s" % [p.generation for p in agent.archive_store.list_passed()])
    print("  get_passed_at(1) 存在: %s  score=%.1f" % (
        agent.archive_store.get_passed_at(1) is not None,
        agent.archive_store.get_passed_at(1).evaluation.score if agent.archive_store.get_passed_at(1) else float("nan"),
    ))
    print("  get_passed_at(2) 存在: %s  score=%.1f" % (
        agent.archive_store.get_passed_at(2) is not None,
        agent.archive_store.get_passed_at(2).evaluation.score if agent.archive_store.get_passed_at(2) else float("nan"),
    ))

    print()
    print(HSEP)
    print(">>> agent.rollback_to(0)  —— 强制恢复到进化前基线（gen1.current_value 的 snapshot）")
    print(HSEP)
    rollback_to(agent, 0)
    match_0 = _sys_of(agent) == original
    print("  sys == gen0 original : %s" % match_0)
    print("  generation           : %d" % agent.generation)
    print("  当前前 80 字         : %s" % _sys_of(agent)[:80])

    print()
    print(HSEP)
    print(">>> agent.rollback_to(1)  —— 恢复 get_passed_at(1).proposal.proposed_value")
    print(HSEP)
    rollback_to(agent, 1)
    match_1 = _sys_of(agent) == gen1_val
    print("  sys == gen1.proposed : %s" % match_1)
    print("  generation           : %d" % agent.generation)
    print("  当前前 80 字         : %s" % _sys_of(agent)[:80])

    print()
    print(HSEP)
    print(">>> agent.rollback_to(2)  —— 恢复 get_passed_at(2).proposal.proposed_value")
    print(HSEP)
    rollback_to(agent, 2)
    match_2 = _sys_of(agent) == gen2_val
    print("  sys == gen2.proposed : %s" % match_2)
    print("  generation           : %d" % agent.generation)
    print("  当前前 80 字         : %s" % _sys_of(agent)[:80])

    print()
    if match_0 and match_1 and match_2:
        print("回滚精确性: rollback_to(0/1/2) 全部匹配 ✓")
    else:
        print("回滚精确性: 异常 ✗ (match0=%s, match1=%s, match2=%s)" % (match_0, match_1, match_2))


# ============================================================= main


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plain", action="store_true", help="纯文本输出（推荐）")
    parser.add_argument("--real-llm", action="store_true",
                        help="使用真实 LLM（需要 .env 已配置 ATLAS_API_KEY / BASE_URL / MODEL），默认使用内置 WalkthroughFakeLLM")
    parser.add_argument("--modules", default="1,2,3",
                        help="要跑哪些模块，逗号分隔：1=单代拆解, 2=多代收敛, 3=多代回滚。默认全部")
    args = parser.parse_args()

    use_fake = not args.real_llm
    try:
        selected = {int(x.strip()) for x in args.modules.split(",") if x.strip()}
    except ValueError:
        print("--modules 参数格式错误，例如 --modules 1,3"); return 2
    if not selected.issubset({1, 2, 3}):
        print("--modules 只能包含 1、2、3"); return 2

    if not use_fake:
        load_dotenv()

    print()
    print(SEP)
    print("Prompt 进化 Walkthrough Demo    （过程逐行打印）")
    print(SEP)
    print("后端 LLM    : %s" % ("WalkthroughFakeLLM（零成本、100% 可复现）" if use_fake else "真实 LLM（读取 .env 配置，消耗 token）"))
    print("模块        : %s" % ", ".join(str(x) for x in sorted(selected)))
    print("数据结构    : Proposal / EvolutionResult / EvolutionSummary → atlas/rsi/ 下对应文件")
    print("核心函数    : evolve_prompt() / evolve_prompt_until_converged() / rollback_to() → atlas/rsi/prompt_evolver.py")
    print("反思上下文  : _extract_fitness_history() + _extract_archived_failures() → _build_evolution_prompt() 拼接注入")
    print("归档回滚    : ArchiveStore.add(generation=) / list_by_generation() / get_passed_at(N) → atlas/rsi/archive.py")

    try:
        if 1 in selected:
            demo_walkthrough_single_gen(use_fake=use_fake)
        if 2 in selected:
            demo_walkthrough_converge(use_fake=use_fake)
        if 3 in selected:
            demo_walkthrough_rollback(use_fake=use_fake)
    except Exception as e:
        print("\n[运行异常] %r" % (e,))
        import traceback; traceback.print_exc()
        return 1

    print()
    print(SEP)
    print("跑完。查看字段定义的代码入口：")
    print("  Proposal            : atlas/rsi/proposal.py（target_type/current_value/proposed_value/reasoning/diff_summary）")
    print("  EvolutionResult     : atlas/rsi/prompt_evolver.py（generation/proposal/leakage_check/evaluation/applied）")
    print("  EvolutionSummary    : atlas/rsi/prompt_evolver.py（generations/final_generation/final_score/stop_reason）")
    print("  LeakageCheckResult  : atlas/rsi/prompt_evolver.py（passed/matched_patterns）")
    print("  FitnessResult       : atlas/rsi/fitness.py（score/feedback/task_results）")
    print("  Rollback 实现       : atlas/rsi/prompt_evolver.py rollback_to() + archive.py ArchiveStore.get_passed_at()")
    print("  Reflect 上下文      : atlas/rsi/prompt_evolver.py _extract_fitness_history() + _extract_archived_failures() → _build_evolution_prompt()")
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())

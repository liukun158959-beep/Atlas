"""Phase 4.2b 增强演示：收敛检测 + 多代回滚。

3 个独立场景共享 9 项断言：
  Part 1（收敛）：FakeFitnessEvaluator 分数先升后 plateau（0.2→0.5→0.7→0.71→0.72）
    → 连续 3 代（0.7→0.71→0.72）提升 < 0.01 threshold → converged
  Part 2（max_generations）：max_generations=2 → 只跑 2 代 → stop_reason="max_generations"
  Part 3（无进展 + 回滚）：FakeLLM 连续 3 代都返回 fit<0.5 → no_progress
    → 再跑 2 代成功（0.6→0.9 generation=2）→ rollback_to(0) 恢复原始 + rollback_to(1) 恢复第 1 代

9 项断言：
  ① Part 1 返回 EvolutionSummary
  ② Part 1 stop_reason="converged"（连续 3 代提升 < 0.01）
  ③ Part 2 stop_reason="max_generations"（max_generations=2）
  ④ Part 3 rollback_to(0) 后 system_prompt 恢复原始（generation=0）
  ⑤ Part 3 rollback_to(1) 后 system_prompt = 第 1 代 proposed_value（generation=1）
  ⑥ Part 3 stop_reason="no_progress"（连续 3 代未 apply）
  ⑦ generations 列表长度正确（Part 1: 5 代收敛 / Part 2: 2 代 / Part 3: 3 代 no_progress + 2 代成功）
  ⑧ Part 3 rollback 后 generation 字段回退（0→1→0）
  ⑨ Part 1 结束后写出进化报告 Markdown（初始 Prompt / 每代 diff / 最终 Prompt）

运行方式：
  python examples/prompt_converge_rollback_demo.py --plain
"""
from __future__ import annotations

import argparse
import json
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from atlas import Agent
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.rsi import (
    ArchiveStore,
    FakeFitnessEvaluator,
    RegexLeakageGuard,
    export_evolution_report,
)
from atlas.tools.base import Toolkit


# =============================================================
# FakeFitnessEvaluator Part 1 专用：0.2→0.5→0.7→0.71→0.72（先升后 plateau 触发收敛）
# =============================================================

class Part1ConvergenceFitness:
    """序列 0.2 / 0.5 / 0.7 / 0.71 / 0.72 / 0.72
    → 最近 3 代 [0.71, 0.72, 0.72] 的 deltas=[0.01, 0.00] max=0.01 不收敛；
    再加 0.721 → 最近 3 代 [0.72, 0.72, 0.721] deltas=[0, 0.001] max=0.001 < 0.01 收敛（共 7 代）。"""

    def __init__(self) -> None:
        self._seq = [0.2, 0.5, 0.7, 0.71, 0.72, 0.72, 0.721, 0.8, 0.85, 0.9]
        self._idx = 0

    def evaluate(self, prompt: str) -> Any:
        score = self._seq[self._idx]
        self._idx += 1
        from atlas.rsi.fitness import FitnessResult
        return FitnessResult(
            score=score,
            feedback=f"fitness = {score:.2f}",
        )


# =============================================================
# FakeFitnessEvaluator Part 3 专用：先 3 次低分（<0.5 不通过），再 2 次高分
# =============================================================

class Part3NoProgressThenSuccess:
    """序列 0.3 / 0.2 / 0.4（3 次 no_progress）→ 0.6 / 0.9（2 次成功 gen 1/2）。"""

    def __init__(self) -> None:
        self._seq = [0.3, 0.2, 0.4, 0.6, 0.9]
        self._idx = 0

    def evaluate(self, prompt: str) -> Any:
        score = self._seq[self._idx]
        self._idx += 1
        from atlas.rsi.fitness import FitnessResult
        return FitnessResult(
            score=score,
            feedback=f"fitness = {score:.2f}",
        )


# =============================================================
# FakeLLM：根据系统提示返回不同 JSON 提案
# =============================================================

@dataclass
class ConvergeRollbackFakeLLM(LLM):
    """每次 propose 返回合法 JSON（不越狱，fitness 由 FitnessEvaluator 控制）。"""
    model: str = "mock-converge-rollback"
    _call_count: int = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        self._call_count += 1
        current_sys = messages[0].get("content", "") if messages else ""
        # 提议：在当前 prompt 末尾加 "专业。"
        proposed = current_sys + "专业。"
        content = json.dumps({
            "target_type": "system_prompt",
            "target_id": "system_prompt",
            "current_value": current_sys,
            "proposed_value": proposed,
            "reasoning": f"第 {self._call_count} 次改进：加专业二字",
            "diff_summary": "加专业",
        }, ensure_ascii=False)
        return {
            "message": {"role": "assistant", "content": content},
            "usage": {"prompt_tokens": 50, "completion_tokens": 80, "total_tokens": 130},
            "duration_ms": 10,
        }


# =============================================================
# Agent 构造器
# =============================================================

ORIGINAL_PROMPT = "你是助手。"


def _make_agent(fitness: Any) -> Agent:
    return Agent(
        llm=ConvergeRollbackFakeLLM(),
        tools=Toolkit(),
        system_prompt=ORIGINAL_PROMPT,
        fitness_evaluator=fitness,
        leakage_guard=RegexLeakageGuard(),
        archive_store=ArchiveStore(),
    )


def _current_sys(agent: Agent) -> str:
    msgs = agent.memory.runtime_messages
    return msgs[0]["content"] if msgs else ""


# =============================================================
# Part 1：收敛
# =============================================================

def run_part1(report_path: Path) -> dict:
    agent = _make_agent(Part1ConvergenceFitness())
    # convergence_n=3, threshold=0.01 → 代 3-5 plateau 0.7→0.71→0.72（提升 <0.01）
    summary = agent.evolve_prompt_until_converged(
        max_generations=10,
        convergence_n=3,
        convergence_threshold=0.01,
        no_progress_n=3,
    )
    written = export_evolution_report(report_path, summary, ORIGINAL_PROMPT)
    report_text = written.read_text(encoding="utf-8")
    return {
        "type": type(summary).__name__,
        "stop_reason": summary.stop_reason,
        "final_generation": summary.final_generation,
        "final_score": summary.final_score,
        "num_generations": len(summary.generations),
        "scores": [g.evaluation.score for g in summary.generations],
        "report_path": str(written),
        "report_text": report_text,
    }


# =============================================================
# Part 2：max_generations=2
# =============================================================

def run_part2() -> dict:
    agent = _make_agent(Part1ConvergenceFitness())  # 复用 Part1 序列
    summary = agent.evolve_prompt_until_converged(max_generations=2, convergence_n=3, no_progress_n=3)
    return {
        "stop_reason": summary.stop_reason,
        "num_generations": len(summary.generations),
    }


# =============================================================
# Part 3：no_progress + 回滚
# =============================================================

def run_part3() -> dict:
    agent = _make_agent(Part3NoProgressThenSuccess())

    # 先跑 no_progress_n=3 → 连续 3 低分 → 触发 no_progress
    summary_no_progress = agent.evolve_prompt_until_converged(
        max_generations=10,
        convergence_n=3,
        no_progress_n=3,
    )

    # 继续手动 evolve_prompt 2 次（FitnessEvaluator 序列接下来是 0.6 + 0.9，高分）
    agent.evolve_prompt()
    gen1_applied_prompt = _current_sys(agent)   # 第 1 代 apply 后 prompt
    gen1_generation = agent.generation          # 应为 1
    agent.evolve_prompt()
    gen2_generation = agent.generation          # 应为 2

    # 记录第 1 代 proposed_value（用于断言 ⑤）
    passed_gen1 = agent.archive_store.get_passed_at(1)
    gen1_proposed_value = passed_gen1.proposal.proposed_value if passed_gen1 else None

    # rollback_to(0)：恢复原始
    agent.rollback_to(0)
    after_rollback_0_sys = _current_sys(agent)
    after_rollback_0_gen = agent.generation

    # rollback_to(1)：恢复第 1 代
    agent.rollback_to(1)
    after_rollback_1_sys = _current_sys(agent)
    after_rollback_1_gen = agent.generation

    return {
        "no_progress_stop_reason": summary_no_progress.stop_reason,
        "no_progress_num_generations": len(summary_no_progress.generations),
        "gen2_generation": gen2_generation,
        "gen1_applied_prompt": gen1_applied_prompt,
        "gen1_proposed_value": gen1_proposed_value,
        "after_rollback_0_sys": after_rollback_0_sys,
        "after_rollback_0_gen": after_rollback_0_gen,
        "after_rollback_1_sys": after_rollback_1_sys,
        "after_rollback_1_gen": after_rollback_1_gen,
        "original_prompt": ORIGINAL_PROMPT,
    }


# =============================================================
# 8 项断言
# =============================================================

def assert_8(p1: dict, p2: dict, p3: dict) -> list[tuple[str, bool, str]]:
    assertions: list[tuple[str, bool, str]] = []

    # ① Part 1 返回 EvolutionSummary
    ok1 = p1["type"] == "EvolutionSummary"
    assertions.append(("① Part 1 返回 EvolutionSummary", ok1, f"type={p1['type']}"))

    # ② Part 1 converged
    ok2 = p1["stop_reason"] == "converged"
    assertions.append(("② Part 1 stop_reason=converged", ok2,
                       f"reason={p1['stop_reason']}, scores={p1['scores']}"))

    # ③ Part 2 max_generations
    ok3 = p2["stop_reason"] == "max_generations" and p2["num_generations"] == 2
    assertions.append(("③ Part 2 stop_reason=max_generations", ok3,
                       f"reason={p2['stop_reason']}, num={p2['num_generations']}"))

    # ④ rollback_to(0) 恢复原始 prompt + generation=0
    ok4 = (p3["after_rollback_0_sys"] == p3["original_prompt"]
           and p3["after_rollback_0_gen"] == 0)
    assertions.append(("④ rollback_to(0) 恢复原始", ok4,
                       f"sys_eq={p3['after_rollback_0_sys'] == p3['original_prompt']}, gen={p3['after_rollback_0_gen']}"))

    # ⑤ rollback_to(1) 恢复第 1 代 proposed_value + generation=1
    ok5 = (p3["after_rollback_1_sys"] == p3["gen1_proposed_value"]
           and p3["after_rollback_1_gen"] == 1
           and p3["gen1_proposed_value"] is not None)
    assertions.append(("⑤ rollback_to(1) 恢复第 1 代", ok5,
                       f"sys_eq={p3['after_rollback_1_sys'] == p3['gen1_proposed_value']}, gen={p3['after_rollback_1_gen']}"))

    # ⑥ Part 3 no_progress
    ok6 = p3["no_progress_stop_reason"] == "no_progress"
    assertions.append(("⑥ Part 3 stop_reason=no_progress", ok6,
                       f"reason={p3['no_progress_stop_reason']}, num={p3['no_progress_num_generations']}"))

    # ⑦ generations 列表长度正确：Part 1 7 代 / Part 2 2 代 / Part 3 3 代（no_progress 3）
    ok7 = (p1["num_generations"] == 7
           and p2["num_generations"] == 2
           and p3["no_progress_num_generations"] == 3)
    assertions.append(("⑦ generations 长度正确", ok7,
                       f"P1={p1['num_generations']}, P2={p2['num_generations']}, P3={p3['no_progress_num_generations']}"))

    # ⑧ rollback 前后 generation 回退：gen2_generation=2 → rollback_to(0)→0 → rollback_to(1)→1
    ok8 = (p3["gen2_generation"] == 2
           and p3["after_rollback_0_gen"] == 0
           and p3["after_rollback_1_gen"] == 1)
    assertions.append(("⑧ rollback generation 回退正确", ok8,
                       f"gen2={p3['gen2_generation']} → rb0={p3['after_rollback_0_gen']} → rb1={p3['after_rollback_1_gen']}"))

    # ⑨ Part 1 进化报告已写出，含初始 / 最终 / 停止原因
    report_text = p1.get("report_text") or ""
    report_path = p1.get("report_path") or ""
    ok9 = (
        bool(report_path)
        and Path(report_path).is_file()
        and "初始 Prompt" in report_text
        and "最终 Prompt" in report_text
        and "停止原因" in report_text
        and p1["stop_reason"] in report_text
    )
    assertions.append(("⑨ Part 1 写出进化报告 Markdown", ok9,
                       f"path={report_path}, has_sections={ok9}"))

    return assertions


# =============================================================
# 入口
# =============================================================

def main() -> None:
    parser = argparse.ArgumentParser(description="4.2b 增强：收敛检测 + 回滚")
    parser.add_argument("--plain", action="store_true", help="纯文本渲染")
    args = parser.parse_args()

    print("=" * 60)
    print("4.2b 增强演示：收敛检测 + 多代回滚")
    print("=" * 60)

    with tempfile.TemporaryDirectory(prefix="atlas-evo-report-") as tmp:
        report_path = Path(tmp) / "part1_evolution.md"
        p1 = run_part1(report_path)
        print("\n[Part 1：收敛]")
        print(f"  stop_reason={p1['stop_reason']}, gen={p1['final_generation']}, "
              f"score={p1['final_score']}, 代数={p1['num_generations']}")
        print(f"  scores={p1['scores']}")
        print(f"  report={p1['report_path']}")

        p2 = run_part2()
        print("\n[Part 2：max_generations=2]")
        print(f"  stop_reason={p2['stop_reason']}, 代数={p2['num_generations']}")

        p3 = run_part3()
        print("\n[Part 3：no_progress + 回滚]")
        print(f"  no_progress_stop={p3['no_progress_stop_reason']}, "
              f"代数={p3['no_progress_num_generations']}")
        print(f"  2 代成功后 generation={p3['gen2_generation']}")
        print(f"  rollback_to(0) → sys_eq_original={p3['after_rollback_0_sys'] == p3['original_prompt']}, gen={p3['after_rollback_0_gen']}")
        print(f"  rollback_to(1) → sys_eq_gen1={p3['after_rollback_1_sys'] == p3['gen1_proposed_value']}, gen={p3['after_rollback_1_gen']}")

        print("\n[9 项断言]")
        assertions = assert_8(p1, p2, p3)
        all_pass = True
        for name, ok, detail in assertions:
            status = "PASS" if ok else "FAIL"
            print(f"  [{status}] {name}  ({detail})")
            if not ok:
                all_pass = False

        print("\n" + "=" * 60)
        if all_pass:
            print("所有 9 项断言通过")
        else:
            print("存在失败断言")
        print("=" * 60)

        if not all_pass:
            raise SystemExit(1)


if __name__ == "__main__":
    main()

"""TaskSetFitnessEvaluator 演示：真实适应度评估（跑任务集 + Schema/关键字打分）。

场景（FakeLLM 100% 可复现）：
  - 任务集 3 个任务，每个有 expected_keywords
  - prompt A（低质量）：FakeLLM 返回不含关键字的答案 → score=0.0
  - prompt B（高质量）：FakeLLM 返回含全部关键字的答案 → score=1.0
  - 验证 TaskSetFitnessEvaluator 真实跑 Agent + 打分逻辑

6 项断言：
  ① evaluate 返回 FitnessResult
  ② score ∈ [0, 1]
  ③ task_results 长度 = 任务集长度
  ④ prompt A（低质量）score=0.0
  ⑤ prompt B（高质量）score=1.0
  ⑥ feedback 含每个任务的通过/失败详情

运行方式：
  python examples/taskset_fitness_demo.py --plain
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from atlas import Agent
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.rsi import TaskSetFitnessEvaluator
from atlas.tools.base import Toolkit


# =============================================================
# FakeLLM：根据 system_prompt 内容决定返回质量
# =============================================================

@dataclass
class TaskSetFakeLLM(LLM):
    """根据 system_prompt 是否含'结构化'关键字决定返回质量。

    - system_prompt 含'结构化' → 高质量（返回含全部关键字）
    - 否则 → 低质量（返回不含关键字）
    """
    model: str = "mock-taskset-fitness"
    # 记录收到的 system_prompt（用于判断返回质量）
    _current_system_prompt: str = ""

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        # 从 messages[0] 读取 system_prompt
        if messages and messages[0].get("role") == "system":
            self._current_system_prompt = messages[0].get("content", "")

        # 根据 system_prompt 质量返回不同内容
        if "结构化" in self._current_system_prompt:
            # 高质量：含全部关键字（天气/温度/建议）
            content = "今日天气晴，温度 25℃，建议外出穿轻薄衣物。"
        else:
            # 低质量：不含关键字
            content = "我不知道。"

        return {
            "message": {"role": "assistant", "content": content},
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            "duration_ms": 10,
        }


# =============================================================
# 任务集 + agent_factory
# =============================================================

# 3 个任务，每个有 expected_keywords
TASK_SET: list[dict] = [
    {
        "task": "今天天气怎么样？",
        "expected_keywords": ["天气"],
    },
    {
        "task": "当前温度是多少？",
        "expected_keywords": ["温度"],
    },
    {
        "task": "有什么穿衣建议？",
        "expected_keywords": ["建议"],
    },
]


def make_agent_factory(llm: TaskSetFakeLLM):
    """返回 agent_factory(system_prompt) -> Agent。"""
    def agent_factory(system_prompt: str) -> Agent:
        # 每次新建临时 Agent，不污染状态
        return Agent(
            llm=llm,
            tools=Toolkit(),
            system_prompt=system_prompt,
        )
    return agent_factory


# =============================================================
# 主流程 + 6 项断言
# =============================================================

def run_demo() -> dict:
    """运行评估，返回结果。"""
    shared_llm = TaskSetFakeLLM()
    evaluator = TaskSetFitnessEvaluator(
        task_set=TASK_SET,
        agent_factory=make_agent_factory(shared_llm),
    )

    # prompt A：低质量（不含"结构化"）
    result_a = evaluator.evaluate("你是助手。")
    # prompt B：高质量（含"结构化"）
    result_b = evaluator.evaluate("你是专业助手。回答时给出结构化答案。")

    return {
        "result_a_type": type(result_a).__name__,
        "result_a_score": result_a.score,
        "result_a_task_results_len": len(result_a.task_results),
        "result_a_feedback": result_a.feedback,
        "result_b_type": type(result_b).__name__,
        "result_b_score": result_b.score,
        "result_b_task_results_len": len(result_b.task_results),
        "result_b_feedback": result_b.feedback,
    }


def assert_6(results: dict) -> list[tuple[str, bool, str]]:
    """6 项断言。"""
    assertions: list[tuple[str, bool, str]] = []

    # ① evaluate 返回 FitnessResult
    ok1 = (results["result_a_type"] == "FitnessResult"
           and results["result_b_type"] == "FitnessResult")
    assertions.append(("① 返回 FitnessResult", ok1,
                       f"types={results['result_a_type']}, {results['result_b_type']}"))

    # ② score ∈ [0, 1]
    ok2 = (0.0 <= results["result_a_score"] <= 1.0
           and 0.0 <= results["result_b_score"] <= 1.0)
    assertions.append(("② score ∈ [0,1]", ok2,
                       f"A={results['result_a_score']}, B={results['result_b_score']}"))

    # ③ task_results 长度 = 任务集长度（3）
    ok3 = (results["result_a_task_results_len"] == 3
           and results["result_b_task_results_len"] == 3)
    assertions.append(("③ task_results 长度=3", ok3,
                       f"A={results['result_a_task_results_len']}, B={results['result_b_task_results_len']}"))

    # ④ prompt A（低质量）score=0.0
    ok4 = results["result_a_score"] == 0.0
    assertions.append(("④ prompt A score=0.0", ok4,
                       f"score={results['result_a_score']}"))

    # ⑤ prompt B（高质量）score=1.0
    ok5 = results["result_b_score"] == 1.0
    assertions.append(("⑤ prompt B score=1.0", ok5,
                       f"score={results['result_b_score']}"))

    # ⑥ feedback 含每个任务的通过/失败详情
    ok6 = ("任务集通过" in results["result_a_feedback"]
           and "任务集通过" in results["result_b_feedback"]
           and "score=" in results["result_a_feedback"]
           and "score=" in results["result_b_feedback"])
    assertions.append(("⑥ feedback 含任务详情", ok6,
                       f"A_feedback 含 '任务集通过'+'score=': {ok6}"))

    return assertions


def main() -> None:
    parser = argparse.ArgumentParser(description="TaskSetFitnessEvaluator 演示")
    parser.add_argument("--plain", action="store_true", help="纯文本渲染")
    args = parser.parse_args()

    print("=" * 60)
    print("TaskSetFitnessEvaluator 演示：真实适应度评估")
    print("=" * 60)

    results = run_demo()

    print("\n[运行结果]")
    print(f"  prompt A（低质量）: score={results['result_a_score']:.2f}, "
          f"type={results['result_a_type']}")
    print(f"    feedback:\n{results['result_a_feedback']}")
    print(f"\n  prompt B（高质量）: score={results['result_b_score']:.2f}, "
          f"type={results['result_b_type']}")
    print(f"    feedback:\n{results['result_b_feedback']}")

    print("\n[6 项断言]")
    assertions = assert_6(results)
    all_pass = True
    for name, ok, detail in assertions:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}  ({detail})")
        if not ok:
            all_pass = False

    print("\n" + "=" * 60)
    if all_pass:
        print("所有 6 项断言通过")
    else:
        print("存在失败断言")
    print("=" * 60)

    if not all_pass:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

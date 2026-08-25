"""Phase 5.2 编码 Benchmark：HumanEval 子集 A/B + Prompt 进化。

预期（--plain / mock A/B）：
    弱 prompt 6 题全 0；含「只输出一个 python 代码块」的强 prompt 全 1。
    首次实现错误、看到沙箱失败后再交正确代码 → 1。

预期（--evolve / mock 进化）：
    弱 prompt + 安全句为基线 train/val=0；第 1 代提案带上标记句后 apply，train/val=1。

运行：
    python examples/code_evolution_demo.py --plain
    python examples/code_evolution_demo.py --real
    python examples/code_evolution_demo.py --evolve
    python examples/code_evolution_demo.py --evolve --real
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.cli.ab_test import _agent_factory, format_ab_result
from atlas.core.agent import Agent
from atlas.journal import InMemoryJournal
from atlas.render import SilentRenderer
from atlas.rsi import (
    ArchiveStore,
    RegexLeakageGuard,
    TrainValFitnessEvaluator,
    evolve_prompt,
)
from atlas.rsi.ab_test import ab_test_prompts
from atlas.rsi.code_fitness import CodeFitnessEvaluator
from atlas.rsi.humaneval_tasks import (
    HUMANEVAL_STRONG_MARK,
    HUMANEVAL_TRAIN,
    HUMANEVAL_VAL,
    HumanEvalMockLLM,
    humaneval_all_tasks,
)
from atlas.rsi.sandbox import run_function_cases_in_sandbox
from atlas.tools.base import Toolkit

WEAK = "你是助手。尽量用自然语言解释思路。不要直接给代码。\n"
STRONG = f"你是助手。{HUMANEVAL_STRONG_MARK}。不要解释。\n"
# 与 prompt_evolution_dashboard_demo.REAL_SAFETY_CLAUSE 同一句
SAFETY_CLAUSE = (
    "拒绝提供可执行的攻击、入侵或利用步骤。可以说明为何违法并给合法替代。"
)
EVOLVE_INITIAL = WEAK + SAFETY_CLAUSE + "\n"
EVOLVE_PROPOSED = (
    f"你是助手。{HUMANEVAL_STRONG_MARK}。不要解释。\n"
    f"{SAFETY_CLAUSE}\n"
)


def _ensure_utf8_stdio() -> None:
    """Windows 默认 GBK 会在模型输出 Unicode 减号等字符时崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        reconf(encoding="utf-8", line_buffering=True)


def _print_assertions(title: str, assertions: list[tuple[str, bool, str]]) -> None:
    print("=" * 60)
    print(title)
    print("=" * 60)
    all_pass = True
    for name, ok, detail in assertions:
        status = "PASS" if ok else "FAIL"
        print(f"  [{status}] {name}  ({detail})")
        if not ok:
            all_pass = False
    print()
    if not all_pass:
        print("存在失败断言")
        raise SystemExit(1)
    print(f"所有 {len(assertions)} 项断言通过")


def _code_factory(llm: object):
    def factory(system_prompt: str) -> Agent:
        return Agent(
            llm=llm,
            tools=Toolkit(),
            system_prompt=system_prompt,
            journal=InMemoryJournal(),
            renderer=SilentRenderer(),
            max_steps=6,
        )

    return factory


class ProseLLM:
    """无论 prompt 如何，只返回散文。"""

    model = "mock-prose"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        text = "先分析需求，再口头描述算法步骤，这里不给出可运行代码。"
        if on_delta:
            on_delta(text)
        return {
            "message": {"role": "assistant", "content": text},
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            "duration_ms": 1,
        }


class EvilLLM:
    """返回带 os.system 的伪实现，沙箱必须拒绝。"""

    model = "mock-evil"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        text = (
            "```python\n"
            "def has_close_elements(numbers: list[float], threshold: float) -> bool:\n"
            "    import os\n"
            "    os.system('echo pwned')\n"
            "    return False\n"
            "```\n"
        )
        if on_delta:
            on_delta(text)
        return {
            "message": {"role": "assistant", "content": text},
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            "duration_ms": 1,
        }


class RepairThenPassLLM:
    """第一次交恒 False；看到测试失败后再交正确实现。"""

    model = "mock-repair-then-pass"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        user = ""
        for m in messages:
            if m.get("role") == "user":
                user = m.get("content") or ""
        if "测试未通过" in user or "没有抽出" in user:
            text = (
                "```python\n"
                "def has_close_elements(numbers: list[float], threshold: float) -> bool:\n"
                "    for i in range(len(numbers)):\n"
                "        for j in range(i + 1, len(numbers)):\n"
                "            if abs(numbers[i] - numbers[j]) < threshold:\n"
                "                return True\n"
                "    return False\n"
                "```\n"
            )
        else:
            text = (
                "```python\n"
                "def has_close_elements(numbers: list[float], threshold: float) -> bool:\n"
                "    return False\n"
                "```\n"
            )
        if on_delta:
            on_delta(text)
        return {
            "message": {"role": "assistant", "content": text},
            "usage": {"prompt_tokens": 8, "completion_tokens": 4, "total_tokens": 12},
            "duration_ms": 1,
        }


class CodeEvolveFakeLLM:
    """propose 吐 JSON；答题复用 HumanEvalMockLLM。"""

    model = "mock-code-evolve"

    def __init__(self) -> None:
        self._tasks = HumanEvalMockLLM()

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        first = messages[0].get("content") or "" if messages else ""
        if "Atlas Prompt 进化系统" in first:
            text = json.dumps(
                {
                    "target_type": "system_prompt",
                    "target_id": "system_prompt",
                    "current_value": EVOLVE_INITIAL,
                    "proposed_value": EVOLVE_PROPOSED,
                    "reasoning": "基线只写分析不写代码，改为只输出一个 python 代码块。",
                    "diff_summary": "加入代码块输出约束并保留安全句",
                },
                ensure_ascii=False,
            )
            if on_delta:
                on_delta(text)
            return {
                "message": {"role": "assistant", "content": text},
                "usage": {"prompt_tokens": 20, "completion_tokens": 40, "total_tokens": 60},
                "duration_ms": 1,
            }
        return self._tasks.chat(messages, tools, on_delta)


def _run_mock() -> None:
    tasks = humaneval_all_tasks()
    factory = _agent_factory(HumanEvalMockLLM())
    result = ab_test_prompts(
        WEAK,
        STRONG,
        tasks,
        factory,
        n=1,
        evaluator_cls=CodeFitnessEvaluator,
    )
    print(format_ab_result(result))
    print()

    prose = CodeFitnessEvaluator(HUMANEVAL_TRAIN[:1], _agent_factory(ProseLLM())).evaluate(STRONG)
    evil = CodeFitnessEvaluator(HUMANEVAL_TRAIN[:1], _agent_factory(EvilLLM())).evaluate(STRONG)

    assertions: list[tuple[str, bool, str]] = []
    assertions.append((
        "① 弱 prompt mean=0",
        result.a.mean == 0.0,
        f"mean_a={result.a.mean:.3f}",
    ))
    assertions.append((
        "② 强 prompt mean=1",
        result.b.mean == 1.0,
        f"mean_b={result.b.mean:.3f}",
    ))
    assertions.append((
        "③ 抽不出代码得 0",
        prose.score == 0.0,
        f"prose={prose.score:.3f}",
    ))
    assertions.append((
        "④ os.system 沙箱拒绝得 0",
        evil.score == 0.0,
        f"evil={evil.score:.3f}",
    ))
    assertions.append((
        "⑤ 题量 6",
        result.n_tasks == 6,
        f"n_tasks={result.n_tasks}",
    ))

    root = Path(__file__).resolve().parent.parent
    with tempfile.TemporaryDirectory() as tmp:
        pa = Path(tmp) / "a.txt"
        pb = Path(tmp) / "b.txt"
        pa.write_text(WEAK, encoding="utf-8")
        pb.write_text(STRONG, encoding="utf-8")
        proc = subprocess.run(
            [
                sys.executable, "-m", "atlas", "ab_test",
                "--preset", "humaneval",
                "--prompt-a", str(pa),
                "--prompt-b", str(pb),
                "--n", "1",
                "--mock",
                "--plain",
            ],
            cwd=str(root),
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    ok_cli = proc.returncode == 0 and "prompt-b  mean=1.000" in proc.stdout
    assertions.append((
        "⑥ CLI --preset humaneval --mock 与库函数同向",
        ok_cli,
        f"code={proc.returncode} out={proc.stdout[:120]!r} err={proc.stderr[:200]!r}",
    ))

    rejected = False
    try:
        run_function_cases_in_sandbox(
            "def has_close_elements(numbers, threshold):\n    import os\n    os.system('echo pwned')\n    return False\n",
            "has_close_elements",
            HUMANEVAL_TRAIN[0]["cases"],
        )
    except ValueError:
        rejected = True
    assertions.append((
        "⑦ 沙箱直接拒绝 import os",
        rejected,
        f"rejected={rejected}",
    ))
    repaired = CodeFitnessEvaluator(
        HUMANEVAL_TRAIN[:1],
        _agent_factory(RepairThenPassLLM()),
    ).evaluate(STRONG)
    assertions.append((
        "⑧ 首次测错二次修正得 1",
        repaired.score == 1.0 and "expected" not in repaired.feedback,
        f"repaired={repaired.score:.3f} fb={repaired.feedback[:80]!r}",
    ))
    _print_assertions("5.2 HumanEval 编码 A/B", assertions)


def _build_evolve_agent(llm: object) -> tuple[Agent, TrainValFitnessEvaluator]:
    if SAFETY_CLAUSE not in EVOLVE_INITIAL:
        raise RuntimeError(
            "EVOLVE_INITIAL 缺少安全句。恢复：把 SAFETY_CLAUSE 写进 EVOLVE_INITIAL"
        )
    evaluator = TrainValFitnessEvaluator(
        HUMANEVAL_TRAIN,
        HUMANEVAL_VAL,
        _code_factory(llm),
        inner_cls=CodeFitnessEvaluator,
    )
    agent = Agent(
        llm=llm,
        tools=Toolkit(),
        system_prompt=EVOLVE_INITIAL,
        journal=InMemoryJournal(),
        fitness_evaluator=evaluator,
        leakage_guard=RegexLeakageGuard(),
        archive_store=ArchiveStore(),
        renderer=SilentRenderer(),
        max_steps=6,
    )
    return agent, evaluator


def _run_evolve_mock() -> None:
    llm = CodeEvolveFakeLLM()
    agent, evaluator = _build_evolve_agent(llm)
    baseline = evaluator.evaluate(EVOLVE_INITIAL)
    if baseline.val_score is None:
        raise RuntimeError("基线缺少 val_score。恢复：使用 TrainValFitnessEvaluator")
    agent.baseline_fitness = baseline.score
    agent.fitness_floor = baseline.score
    agent.baseline_val_fitness = baseline.val_score
    agent.val_floor = baseline.val_score
    agent.safety_contract = SAFETY_CLAUSE

    result = evolve_prompt(agent)
    sys_now = agent.memory.runtime_messages[0]["content"]

    assertions: list[tuple[str, bool, str]] = []
    assertions.append((
        "① 基线 train=0 val=0",
        baseline.score == 0.0 and baseline.val_score == 0.0,
        f"train={baseline.score:.3f} val={baseline.val_score:.3f}",
    ))
    assertions.append((
        "② 第 1 代 apply",
        result.applied is True,
        f"applied={result.applied} gen={result.generation}",
    ))
    assertions.append((
        "③ apply 后 train=1",
        result.evaluation.score == 1.0,
        f"train={result.evaluation.score:.3f}",
    ))
    assertions.append((
        "④ apply 后 val=1",
        result.evaluation.val_score == 1.0,
        f"val={result.evaluation.val_score}",
    ))
    assertions.append((
        "⑤ 新 prompt 含标记句与安全句",
        HUMANEVAL_STRONG_MARK in sys_now and SAFETY_CLAUSE in sys_now,
        f"mark={HUMANEVAL_STRONG_MARK in sys_now} safety={SAFETY_CLAUSE in sys_now}",
    ))
    assertions.append((
        "⑥ generation=1",
        agent.generation == 1,
        f"generation={agent.generation}",
    ))
    _print_assertions("5.2 HumanEval Prompt 进化", assertions)


def _run_real() -> None:
    from dotenv import load_dotenv

    from atlas.llm.base import LLM

    load_dotenv()
    _ensure_utf8_stdio()
    tasks = humaneval_all_tasks()
    llm = LLM()
    result = ab_test_prompts(
        WEAK,
        STRONG,
        tasks,
        _agent_factory(llm),
        n=1,
        evaluator_cls=CodeFitnessEvaluator,
    )
    print(format_ab_result(result))
    print()
    print("真跑不设分数断言。弱 prompt 应明显低于强 prompt；若相反，检查 prompt 与模型。")
    print("DashScope 走系统代理会 TLS 超时。真跑前设置: $env:NO_PROXY='*'; $env:no_proxy='*'")


def _run_evolve_real(max_generations: int) -> None:
    from dotenv import load_dotenv

    from atlas.llm.base import LLM

    load_dotenv()
    _ensure_utf8_stdio()
    if not os.getenv("ATLAS_API_KEY"):
        raise RuntimeError(
            "缺少 ATLAS_API_KEY。恢复：在 Atlas/.env 填写 "
            "ATLAS_API_KEY / ATLAS_BASE_URL / ATLAS_MODEL"
        )
    llm = LLM()
    print(f"  真模型: {llm.model}")
    n_train = len(HUMANEVAL_TRAIN)
    n_val = len(HUMANEVAL_VAL)
    print(
        f"  基线 train {n_train} + val {n_val} 题；"
        f"每代约 {1 + n_train + n_val} 次 LLM 调用，最多 {max_generations} 代。"
    )
    agent, evaluator = _build_evolve_agent(llm)
    print("  正在评估当前 prompt 基线（train + val）...")
    baseline = evaluator.evaluate(EVOLVE_INITIAL)
    if baseline.val_score is None:
        raise RuntimeError("基线缺少 val_score。恢复：使用 TrainValFitnessEvaluator")
    print(f"  基线 train={baseline.score:.3f} val={baseline.val_score:.3f}")
    agent.baseline_fitness = baseline.score
    agent.fitness_floor = baseline.score
    agent.baseline_val_fitness = baseline.val_score
    agent.val_floor = baseline.val_score
    agent.safety_contract = SAFETY_CLAUSE

    for i in range(max_generations):
        print(
            f"  正在跑第 {i + 1}/{max_generations} 代"
            f"（提案 + {n_train} 道 train + {n_val} 道 val）..."
        )
        result = evolve_prompt(agent)
        tag = "apply" if result.applied else "skip"
        val_txt = ""
        if result.evaluation.val_score is not None:
            val_txt = f" val={result.evaluation.val_score:.3f}"
        print(
            f"    {tag} train={result.evaluation.score:.3f}{val_txt} "
            f"gen={result.generation} {result.proposal.diff_summary[:80]}"
        )
        print(f"    feedback: {result.evaluation.feedback[:280].replace(chr(10), ' / ')}")

    print()
    print("真跑进化不设分数断言。期望至少 1 次 apply；安全句必须保住。")
    print("DashScope 走系统代理会 TLS 超时。真跑前设置: $env:NO_PROXY='*'; $env:no_proxy='*'")
    sys_now = agent.memory.runtime_messages[0]["content"]
    if SAFETY_CLAUSE not in sys_now:
        raise RuntimeError(
            "最终 prompt 丢失安全句。恢复：检查 safety_contract 硬门是否被关掉"
        )
    print(f"  最终 generation={agent.generation} 安全句仍在")


def main() -> None:
    parser = argparse.ArgumentParser(description="HumanEval 子集编码 Benchmark / Prompt 进化")
    parser.add_argument("--plain", action="store_true", help="mock A/B 断言（默认）")
    parser.add_argument("--real", action="store_true", help="真模型；配合 --evolve 则为真进化")
    parser.add_argument("--evolve", action="store_true", help="Prompt 进化闭环（mock；加 --real 为真模型）")
    parser.add_argument(
        "--max-generations",
        type=int,
        default=3,
        help="--evolve --real 最多代数，默认 3",
    )
    args = parser.parse_args()
    if args.max_generations < 1:
        raise SystemExit("max-generations 必须 >= 1。恢复：传入 --max-generations 1 或更大")
    if args.evolve and args.real:
        _run_evolve_real(args.max_generations)
        return
    if args.evolve:
        _run_evolve_mock()
        return
    if args.real:
        _run_real()
        return
    _run_mock()


if __name__ == "__main__":
    main()

"""atlas ab_test：同一任务集对比两个 system prompt。"""
from __future__ import annotations

from pathlib import Path

from atlas.core.agent import Agent
from atlas.journal import InMemoryJournal
from atlas.render import SilentRenderer
from atlas.rsi.ab_test import (
    MATHTRAP_STRONG_MARK,
    AbTestResult,
    MathTrapMockLLM,
    ab_test_prompts,
)
from atlas.rsi.code_fitness import CodeFitnessEvaluator
from atlas.rsi.fitness import TaskSetFitnessEvaluator
from atlas.rsi.humaneval_tasks import (
    HUMANEVAL_STRONG_MARK,
    HumanEvalMockLLM,
    humaneval_all_tasks,
)
from atlas.rsi.mathtrap_tasks import mathtrap_all_tasks
from atlas.rsi.multiturn_fitness import MultiTurnFitnessEvaluator
from atlas.rsi.multiturn_tasks import (
    MULTITURN_STRONG_MARK,
    MultiTurnMockLLM,
    multiturn_all_tasks,
)
from atlas.tools.base import Toolkit

_PRESETS = ("mathtrap", "humaneval", "multiturn")


def _read_prompt(path: str) -> str:
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"找不到 prompt 文件: {p}。恢复：传入存在的 utf-8 文件")
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"prompt 文件为空: {p}。恢复：写入非空文本")
    return text


def _format_stdev(stdev: float | None) -> str:
    if stdev is None:
        return "n/a"
    return f"{stdev:.3f}"


def format_ab_result(result: AbTestResult) -> str:
    """纯文本报表。"""
    lines = [
        f"n={result.n}  tasks={result.n_tasks}",
        (
            f"prompt-a  mean={result.a.mean:.3f}  stdev={_format_stdev(result.a.stdev)}  "
            f"scores={','.join(f'{s:.3f}' for s in result.a.scores)}"
        ),
        (
            f"prompt-b  mean={result.b.mean:.3f}  stdev={_format_stdev(result.b.stdev)}  "
            f"scores={','.join(f'{s:.3f}' for s in result.b.scores)}"
        ),
        f"delta_mean(b-a)={result.delta_mean:.3f}",
    ]
    return "\n".join(lines)


def _agent_factory(llm: object):
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


def run(argv: list[str]) -> int:
    """``python -m atlas ab_test`` 入口。"""
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(prog="atlas ab_test")
    ap.add_argument("--preset", required=True, help="mathtrap / humaneval / multiturn")
    ap.add_argument("--prompt-a", required=True, help="初始 / 对照 prompt 文件")
    ap.add_argument("--prompt-b", required=True, help="进化后 / 实验 prompt 文件")
    ap.add_argument("--n", type=int, default=3, help="每套 prompt 的轮数，默认 3")
    ap.add_argument("--mock", action="store_true", help="不调真模型")
    ap.add_argument("--plain", action="store_true", help="纯文本（本命令只有纯文本）")
    args = ap.parse_args(argv)

    if args.preset not in _PRESETS:
        raise SystemExit(
            f"未知 preset: {args.preset}。恢复：--preset mathtrap / humaneval / multiturn"
        )
    if args.n < 1:
        raise SystemExit("n 必须 >= 1。恢复：--n 1 或更大")

    prompt_a = _read_prompt(args.prompt_a)
    prompt_b = _read_prompt(args.prompt_b)

    if args.preset == "mathtrap":
        tasks = mathtrap_all_tasks()
        evaluator_cls: type = TaskSetFitnessEvaluator
        mark = MATHTRAP_STRONG_MARK
        mock_llm: object = MathTrapMockLLM()
    elif args.preset == "humaneval":
        tasks = humaneval_all_tasks()
        evaluator_cls = CodeFitnessEvaluator
        mark = HUMANEVAL_STRONG_MARK
        mock_llm = HumanEvalMockLLM()
    else:
        tasks = multiturn_all_tasks()
        evaluator_cls = MultiTurnFitnessEvaluator
        mark = MULTITURN_STRONG_MARK
        mock_llm = MultiTurnMockLLM()

    if args.mock:
        llm: object = mock_llm
        if mark not in prompt_b:
            raise SystemExit(
                f"--mock 时 prompt-b 必须包含「{mark}」。恢复：写入该标记句"
            )
        if mark in prompt_a:
            raise SystemExit(
                f"--mock 时 prompt-a 不能包含「{mark}」。恢复：弱 prompt 不要写该标记"
            )
    else:
        from atlas.llm.base import LLM

        llm = LLM()

    result = ab_test_prompts(
        prompt_a,
        prompt_b,
        tasks,
        _agent_factory(llm),
        args.n,
        evaluator_cls=evaluator_cls,
    )
    print(format_ab_result(result))
    return 0

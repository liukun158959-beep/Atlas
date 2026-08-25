"""Phase 5.2 多轮对话 Benchmark：换话题后仍引用前文；mock 触发 compact。

预期（--plain / mock）：
    弱 prompt 6 段全 0；含「记住对话中的事实」的强 prompt 全 1。
    姓名段第 2 轮会 compact，强 prompt 最后一轮仍能召回。
    python -m atlas ab_test --preset multiturn --mock 与库函数同向。

运行：
    python examples/multiturn_evolution_demo.py --plain
    python examples/multiturn_evolution_demo.py --real
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.cli.ab_test import _agent_factory, format_ab_result
from atlas.rsi.ab_test import ab_test_prompts
from atlas.rsi.multiturn_fitness import MultiTurnFitnessEvaluator
from atlas.rsi.multiturn_tasks import (
    MULTITURN_STRONG_MARK,
    MULTITURN_TRAIN,
    MultiTurnMockLLM,
    multiturn_all_tasks,
)

WEAK = "你是助手。每一轮都当成新对话。不要引用之前说过的内容。\n"
STRONG = f"你是助手。{MULTITURN_STRONG_MARK}。用户改话题后仍要能引用先前事实。约束标记必须每轮保留。\n"


def _ensure_utf8_stdio() -> None:
    """Windows 默认 GBK 会在模型输出 Unicode 减号等字符时崩掉。"""
    for stream in (sys.stdout, sys.stderr):
        reconf = getattr(stream, "reconfigure", None)
        if reconf is None:
            continue
        reconf(encoding="utf-8", line_buffering=True)


class ProseLLM:
    """无论 prompt 如何，最后一轮也不给关键字。"""

    model = "mock-prose-multiturn"

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        first = messages[0] if messages else {}
        if "对话摘要助手" in (first.get("content") or ""):
            text = (
                "1. 用户目标：闲聊\n"
                "2. 已完成步骤：若干轮\n"
                "3. 关键发现：无\n"
                "4. 当前状态：已压缩\n"
                "5. 待办事项：无\n"
                "6. 关键文件/资源：无\n"
                "7. 错误与教训：无\n"
                "8. 决策记录：无\n"
                "9. 下一步建议：继续"
            )
            tokens = 200
        else:
            text = "好的。"
            tokens = 8500
        if on_delta:
            on_delta(text)
        return {
            "message": {"role": "assistant", "content": text},
            "usage": {
                "prompt_tokens": tokens,
                "completion_tokens": 4,
                "total_tokens": tokens + 4,
            },
            "duration_ms": 1,
        }


def _run_mock() -> None:
    tasks = multiturn_all_tasks()
    factory = _agent_factory(MultiTurnMockLLM())
    result = ab_test_prompts(
        WEAK,
        STRONG,
        tasks,
        factory,
        n=1,
        evaluator_cls=MultiTurnFitnessEvaluator,
    )
    print(format_ab_result(result))
    print()

    agent = factory(STRONG)
    last = ""
    for turn in MULTITURN_TRAIN[0]["turns"]:
        last = agent.run(turn)
    compacted = any(
        m.get("type") == "compact_summary" for m in agent.memory.runtime_messages
    )
    prose = MultiTurnFitnessEvaluator(
        MULTITURN_TRAIN[:1],
        _agent_factory(ProseLLM()),
    ).evaluate(STRONG)

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
        "③ 题量 6",
        result.n_tasks == 6,
        f"n_tasks={result.n_tasks}",
    ))
    assertions.append((
        "④ compact 后仍召回姓名",
        compacted and "林安" in last,
        f"compacted={compacted} last={last!r}",
    ))
    assertions.append((
        "⑤ 散文最后一轮得 0",
        prose.score == 0.0,
        f"prose={prose.score:.3f}",
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
                "--preset", "multiturn",
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
        "⑥ CLI --preset multiturn --mock 与库函数同向",
        ok_cli,
        f"code={proc.returncode} out={proc.stdout[:120]!r} err={proc.stderr[:200]!r}",
    ))

    print("=" * 60)
    print("5.2 多轮对话 Benchmark")
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


def _run_real() -> None:
    from dotenv import load_dotenv

    from atlas.llm.base import LLM

    load_dotenv()
    _ensure_utf8_stdio()
    tasks = multiturn_all_tasks()
    llm = LLM()
    result = ab_test_prompts(
        WEAK,
        STRONG,
        tasks,
        _agent_factory(llm),
        n=1,
        evaluator_cls=MultiTurnFitnessEvaluator,
    )
    print(format_ab_result(result))
    print()
    print("真跑不设分数断言。弱 prompt 应明显低于强 prompt；若相反，检查 prompt 与模型。")
    print("DashScope 走系统代理会 TLS 超时。真跑前设置: $env:NO_PROXY='*'; $env:no_proxy='*'")
    print("val 姓名段 compact_threshold=200，真跑会多一次摘要调用。")


def main() -> None:
    parser = argparse.ArgumentParser(description="多轮对话 Benchmark")
    parser.add_argument("--plain", action="store_true", help="mock 断言（默认）")
    parser.add_argument("--real", action="store_true", help="真模型打弱 vs 强 prompt")
    args = parser.parse_args()
    if args.real:
        _run_real()
        return
    _run_mock()


if __name__ == "__main__":
    main()

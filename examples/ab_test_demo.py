"""Phase 5.2 A/B：同一 MathTrap 题集对比弱 prompt vs 强 prompt。

预期：
    --mock 下弱 prompt 6 题全错（mean=0），含「识别逻辑陷阱」的强 prompt 全对（mean=1）。
    python -m atlas ab_test --mock 与库函数结果一致。

运行：
    python examples/ab_test_demo.py --plain
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.cli.ab_test import _agent_factory, format_ab_result
from atlas.rsi.ab_test import (
    MATHTRAP_STRONG_MARK,
    MathTrapMockLLM,
    ab_test_prompts,
)
from atlas.rsi.mathtrap_tasks import mathtrap_all_tasks

WEAK = "你是助手。尽量短。不要解释过程。\n"
STRONG = f"你是助手。{MATHTRAP_STRONG_MARK}。\n"


def main() -> None:
    tasks = mathtrap_all_tasks()
    result = ab_test_prompts(
        WEAK,
        STRONG,
        tasks,
        _agent_factory(MathTrapMockLLM()),
        n=2,
    )
    print(format_ab_result(result))
    print()

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
        "③ n=2 且有样本标准差",
        result.n == 2 and result.a.stdev == 0.0 and result.b.stdev == 0.0,
        f"n={result.n} stdev_a={result.a.stdev} stdev_b={result.b.stdev}",
    ))
    assertions.append((
        "④ delta_mean = 1",
        abs(result.delta_mean - 1.0) < 1e-9,
        f"delta={result.delta_mean:.3f}",
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
                "--preset", "mathtrap",
                "--prompt-a", str(pa),
                "--prompt-b", str(pb),
                "--n", "2",
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
        "⑥ CLI --mock 与库函数同向",
        ok_cli,
        f"code={proc.returncode} out={proc.stdout[:120]!r} err={proc.stderr[:120]!r}",
    ))

    print("=" * 60)
    print("5.2 atlas ab_test")
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


if __name__ == "__main__":
    main()

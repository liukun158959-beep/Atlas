"""Parallel 并行汇聚演示：ParallelNode 扇出 + and_ 汇聚（Phase 3.1）。

用法：
    python examples/parallel_edge_demo.py --plain   # 纯文本渲染（推荐验证用）
    python examples/parallel_edge_demo.py           # Rich 渲染

验证项（4 项断言，grill-me Q28=A）：
    1. 3 个 Worker 并行执行（start_ts 差值 < 50ms）
    2. 汇聚结果 parallel_results 长度=3
    3. summarizer 收到合并 state（summary 含 3 个子结果）
    4. 总耗时 < 串行耗时（3 x worker_duration）

图结构：
    parallel_1(ParallelNode[worker_a, worker_b, worker_c]) -> summarizer -> END
"""
from __future__ import annotations

import argparse
import sys
import time

from atlas.core.graph import END, Graph, ParallelNode
from atlas.render import PlainRenderer, RichRenderer


# =============================================================
# 3 个 Worker 节点（纯 Python，各 sleep 0.1s 模拟耗时任务）
# =============================================================
def worker_a(state: dict) -> dict:
    """Worker A：计算 input * 2。"""
    start_ts = time.time()
    time.sleep(0.1)  # 模拟耗时
    return {"name": "a", "result": state["input"] * 2, "start_ts": start_ts}


def worker_b(state: dict) -> dict:
    """Worker B：计算 input * 3。"""
    start_ts = time.time()
    time.sleep(0.1)
    return {"name": "b", "result": state["input"] * 3, "start_ts": start_ts}


def worker_c(state: dict) -> dict:
    """Worker C：计算 input + 100。"""
    start_ts = time.time()
    time.sleep(0.1)
    return {"name": "c", "result": state["input"] + 100, "start_ts": start_ts}


def summarizer(state: dict) -> dict:
    """汇聚节点：把 3 个 Worker 的结果格式化汇总。"""
    results = state["parallel_results"]
    return {"summary": [f"{r['name']}:{r['result']}" for r in results]}


# =============================================================
# 主函数
# =============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Parallel 并行汇聚演示")
    parser.add_argument("--plain", action="store_true", help="纯文本渲染（无颜色）")
    args = parser.parse_args()

    renderer = PlainRenderer() if args.plain else RichRenderer()

    renderer.rule("Phase 3.1 Parallel Demo：ParallelNode 扇出 + and_ 汇聚")

    # 构建图
    g = Graph(name="parallel_edge_demo")
    g.add_node("parallel_1", ParallelNode([worker_a, worker_b, worker_c]))
    g.add_node("summarizer", summarizer)
    g.add_edge("parallel_1", "summarizer")
    g.add_edge("summarizer", END)
    g.set_entry_point("parallel_1")

    renderer.markdown("图结构：`parallel_1(3 Worker 并行) -> summarizer -> END`")
    renderer.markdown("3 个 Worker 各 sleep 0.1s，串行需 0.3s，并行应 < 0.2s。")

    # 运行
    t0 = time.time()
    result = g.run({"input": 10})
    elapsed = time.time() - t0
    trace = result.trace

    # 渲染结果
    renderer.rule("执行结果")
    renderer.markdown(f"总耗时：`{elapsed:.3f}s`（串行基准 0.300s）")
    renderer.markdown(f"汇聚结果：`{result.final_state['summary']}`")
    renderer.markdown(f"终止方式：`{trace.terminated_by}`（iterations={trace.iterations}）")

    renderer.rule("节点执行记录")
    for i, ne in enumerate(trace.node_executions):
        renderer.markdown(f"  [{i + 1}] {ne.node_name}  ({ne.duration_ms}ms)")

    # =============================================================
    # 4 项断言（grill-me Q28=A）
    # =============================================================
    renderer.rule("断言验证")

    parallel_results = result.final_state["parallel_results"]

    # 断言 1：3 个 Worker 并行执行（start_ts 差值 < 50ms）
    assert len(parallel_results) == 3, f"断言1前置失败: 结果数 {len(parallel_results)}"
    timestamps = [r["start_ts"] for r in parallel_results]
    max_diff = max(timestamps) - min(timestamps)
    assert max_diff < 0.05, f"断言1失败: Worker start_ts 差值 {max_diff:.3f}s 应 < 0.05s"
    renderer.markdown(f"[1/4] 3 Worker 并行执行（start_ts 差值 {max_diff * 1000:.1f}ms < 50ms） ... OK")

    # 断言 2：汇聚结果 parallel_results 长度=3
    assert len(parallel_results) == 3, f"断言2失败: {len(parallel_results)}"
    names = [r["name"] for r in parallel_results]
    assert names == ["a", "b", "c"], f"断言2失败: 顺序 {names}"
    renderer.markdown("[2/4] 汇聚结果 parallel_results 长度=3（顺序 a,b,c） ... OK")

    # 断言 3：summarizer 收到合并 state（summary 含 3 个子结果）
    summary = result.final_state["summary"]
    assert len(summary) == 3, f"断言3失败: summary {summary}"
    assert summary == ["a:20", "b:30", "c:110"], f"断言3失败: summary 内容 {summary}"
    renderer.markdown("[3/4] summarizer 收到合并 state（summary=['a:20','b:30','c:110']） ... OK")

    # 断言 4：总耗时 < 串行耗时（3 x worker_duration = 0.3s）
    assert elapsed < 0.3, f"断言4失败: 并行耗时 {elapsed:.3f}s 应 < 0.3s（串行基准）"
    renderer.markdown(f"[4/4] 总耗时 {elapsed:.3f}s < 串行基准 0.300s ... OK")

    renderer.rule("全部 4 项断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

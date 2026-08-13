"""Edge 控制流边演示：条件边 + 循环 + state 传递（Phase 3.1）。

用法：
    python examples/edge_demo.py --plain   # 纯文本渲染（推荐验证用）
    python examples/edge_demo.py           # Rich 渲染

验证项（5 项断言，grill-me Q28=B）：
    1. 节点执行顺序 [start, worker, worker]（worker 执行 2 次，第 1 次答案"需要改进"，第 2 次"完善"）
    2. 条件边路由记录 [worker->worker(refine), worker->END(end)]（2 次条件边路由）
    3. 最终 state 含最终答案"完善"
    4. terminated_by == "END"（max_iterations 未触发）
    5. worker 第 2 次输入包含 worker 第 1 次输出（验证 state 在节点间传递）

图结构：
    start -> worker -> (条件边: answer 含"完善" ? end : refine) -> END 或 worker
"""
from __future__ import annotations

import argparse
import sys

from atlas.core.graph import END, Graph
from atlas.render import PlainRenderer, RichRenderer


# =============================================================
# 节点函数（纯 Python，模拟 worker 2 次返回不同答案）
# =============================================================
_call_count = {"worker": 0}


def start_node(state: dict) -> dict:
    """初始化节点：设置任务描述。"""
    return {"task": "解释什么是闭包", "answer": "", "call_count": 0}


def worker_node(state: dict) -> dict:
    """Worker 节点：第 1 次返回"需要改进"，第 2 次返回"完善"。

    用 call_count 计数器模拟 LLM 2 次调用的不同输出。
    """
    _call_count["worker"] += 1
    count = _call_count["worker"]
    if count == 1:
        # 第 1 次：答案不够好
        return {"answer": "闭包是一个函数（需要改进，缺少捕获变量说明）", "call_count": count}
    else:
        # 第 2 次：答案完善
        return {"answer": "闭包是捕获了外部变量的函数（完善）", "call_count": count}


def route_worker(state: dict) -> str:
    """条件边判断函数：answer 含"完善" -> end，否则 -> refine。"""
    if "完善" in state.get("answer", ""):
        return "end"
    return "refine"


# =============================================================
# 主函数
# =============================================================
def main() -> int:
    parser = argparse.ArgumentParser(description="Edge 控制流边演示")
    parser.add_argument("--plain", action="store_true", help="纯文本渲染（无颜色）")
    args = parser.parse_args()

    renderer = PlainRenderer() if args.plain else RichRenderer()

    renderer.rule("Phase 3.1 Edge Demo：条件边 + 循环 + state 传递")

    # 构建图
    g = Graph(name="edge_demo", max_iterations=10)
    g.add_node("start", start_node)
    g.add_node("worker", worker_node)
    g.add_edge("start", "worker")
    g.add_conditional_edges("worker", route_worker, {"refine": "worker", "end": END})
    g.set_entry_point("start")

    renderer.markdown("图结构：`start -> worker -> (条件边) -> worker(loop) / END`")
    renderer.markdown("Worker 第 1 次返回'需要改进'，条件边路由回 worker；第 2 次返回'完善'，路由到 END。")

    # 运行
    result = g.run({"init": True})
    trace = result.trace

    # 渲染结果
    renderer.rule("执行结果")
    renderer.markdown(f"最终答案：`{result.final_state['answer']}`")
    renderer.markdown(f"Worker 调用次数：`{result.final_state['call_count']}`")
    renderer.markdown(f"终止方式：`{trace.terminated_by}`（iterations={trace.iterations}）")

    renderer.rule("节点执行记录")
    for i, ne in enumerate(trace.node_executions):
        renderer.markdown(
            f"  [{i + 1}] {ne.node_name}  ({ne.duration_ms}ms)\n"
            f"      input:  call_count={ne.input_state.get('call_count', '-')}\n"
            f"      output: answer={ne.output_state.get('answer', '-')[:50]}..."
        )

    renderer.rule("边路由记录")
    for i, er in enumerate(trace.edge_routings):
        renderer.markdown(f"  [{i + 1}] {er.from_node} -> {er.to_node}  (key={er.condition_key})")

    # =============================================================
    # 5 项断言（grill-me Q28=B）
    # =============================================================
    renderer.rule("断言验证")

    # 断言 1：节点执行顺序 [start, worker, worker]
    node_seq = [ne.node_name for ne in trace.node_executions]
    assert node_seq == ["start", "worker", "worker"], f"断言1失败: {node_seq}"
    renderer.markdown("[1/5] 节点执行顺序 [start, worker, worker] ... OK")

    # 断言 2：条件边路由记录 [start->worker(普通), worker->worker(refine), worker->END(end)]
    assert len(trace.edge_routings) == 3, f"断言2失败: 路由记录数 {len(trace.edge_routings)}"
    r0 = trace.edge_routings[0]
    r1 = trace.edge_routings[1]
    r2 = trace.edge_routings[2]
    assert r0.from_node == "start" and r0.condition_key is None and r0.to_node == "worker", \
        f"断言2失败: 普通边 {r0}"
    assert r1.from_node == "worker" and r1.condition_key == "refine" and r1.to_node == "worker", \
        f"断言2失败: 第1次条件边 {r1}"
    assert r2.from_node == "worker" and r2.condition_key == "end" and r2.to_node == END, \
        f"断言2失败: 第2次条件边 {r2}"
    renderer.markdown("[2/5] 条件边路由 [start->worker, worker->worker(refine), worker->END(end)] ... OK")

    # 断言 3：最终 state 含最终答案"完善"
    assert "完善" in result.final_state["answer"], f"断言3失败: {result.final_state['answer']}"
    renderer.markdown("[3/5] 最终答案含'完善' ... OK")

    # 断言 4：terminated_by == "END"
    assert trace.terminated_by == "END", f"断言4失败: {trace.terminated_by}"
    renderer.markdown("[4/5] terminated_by == END ... OK")

    # 断言 5：worker 第 2 次输入包含 worker 第 1 次输出（state 传递）
    worker_executions = [ne for ne in trace.node_executions if ne.node_name == "worker"]
    assert len(worker_executions) == 2, f"断言5失败: worker 执行次数 {len(worker_executions)}"
    worker_1_output = worker_executions[0].output_state
    worker_2_input = worker_executions[1].input_state
    # worker 第 1 次输出的 answer 应该在 worker 第 2 次输入的 state 里
    assert worker_1_output["answer"] == worker_2_input["answer"], \
        f"断言5失败: worker1输出 '{worker_1_output['answer'][:30]}' 未传递到 worker2 输入"
    renderer.markdown("[5/5] worker 第 2 次输入包含第 1 次输出（state 传递） ... OK")

    renderer.rule("全部 5 项断言通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())

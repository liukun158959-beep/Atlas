"""Phase 2.4 演示：OrchestratorWorkers 多 Agent 模式包（动态分派 + 并行 Worker）。

场景（--mock 模式全断言可重复）：
  用户任务"查询仓库库存并汇总" ->
    第1轮: Orchestrator dispatch ["查询A库存", "查询B库存"] -> 2 个 Worker 并行执行
    第2轮: Orchestrator dispatch ["计算总库存"] -> 1 个 Worker 执行
    第3轮: Orchestrator synthesize -> 最终答案含"总库存"
  另含 max_iterations 强制汇总实例（1 轮 dispatch 后达上限强制 synthesize）

定位（和 PWC 互补）：
  - pwc_demo.py = 质量闭环（串行 + Critic 打回重做，不求快但求稳）
  - orchestrator_workers_demo.py = 效率优先（并行 + 动态分派，多轮决策循环）

运行方式：
  python examples/orchestrator_workers_demo.py --mock --plain    # 断言模式，100% 可重复
  python examples/orchestrator_workers_demo.py --mock             # 同上，Rich 渲染（若可用）
  python examples/orchestrator_workers_demo.py --real             # 真实 LLM（需 .env 配置）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas import LLM, Toolkit
from atlas.patterns import OrchestratorWorkers, OWResult
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS


# =============================================================
# FakeLLM：内容匹配 + 调用计数（线程安全）
# =============================================================
@dataclass
class OWFakeLLM(LLM):
    """Mock LLM：Orchestrator 按调用计数返回，Worker 按任务内容匹配。

    主场景调用序列（7 次）：
      1  Orchestrator 第1轮 -> dispatch ["查询A库存", "查询B库存"]
      2  Worker "查询A库存"  -> "A库存=100"（并行）
      3  Worker "查询B库存"  -> "B库存=200"（并行）
      4  Orchestrator 第2轮 -> dispatch ["计算总库存"]
      5  Worker "计算总库存"  -> "总库存=300"
      6  Orchestrator 第3轮 -> synthesize "经查询，A库存100，B库存200，总库存300。"

    强制汇总场景（max_iterations=2，单独实例）：
      1  Orchestrator 第1轮 -> dispatch ["查询A库存"]
      2  Worker "查询A库存"  -> "A库存=100"
      3  Orchestrator 第2轮 -> synthesize（forced=True）
    """

    model: str = "mock-ow"
    scenario: str = "main"  # "main" 或 "forced"
    _orch_calls: int = 0
    _orch_messages: list = field(default_factory=list)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        # 从 messages 中提取 system 消息
        system_msg = ""
        for m in messages:
            if m.get("role") == "system":
                system_msg = m.get("content", "")
                break

        if "任务编排者" in system_msg:
            # Orchestrator 调用：按计数器返回（线程安全）
            with self._lock:
                self._orch_calls += 1
                call_num = self._orch_calls
                self._orch_messages.append(system_msg)
            content = self._orchestrator_response(call_num)
        else:
            # Worker 调用：按任务内容匹配（无状态，线程安全）
            content = self._worker_response(system_msg)

        return {
            "message": {"role": "assistant", "content": content},
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "duration_ms": 5,
        }

    def _orchestrator_response(self, call_num: int) -> str:
        if self.scenario == "main":
            if call_num == 1:
                return json.dumps({
                    "action": "dispatch",
                    "tasks": ["查询A库存", "查询B库存"],
                    "reasoning": "需要分别查询两个仓库的库存",
                }, ensure_ascii=False)
            elif call_num == 2:
                return json.dumps({
                    "action": "dispatch",
                    "tasks": ["计算总库存"],
                    "reasoning": "已获取A和B的库存，需要计算总和",
                }, ensure_ascii=False)
            elif call_num == 3:
                return json.dumps({
                    "action": "synthesize",
                    "answer": "经查询，A库存100，B库存200，总库存300。",
                    "reasoning": "信息已充分，可以汇总",
                }, ensure_ascii=False)
            else:
                return json.dumps({
                    "action": "synthesize",
                    "answer": f"(mock 兜底 #{call_num})",
                    "reasoning": "兜底",
                }, ensure_ascii=False)
        else:  # forced 场景
            if call_num == 1:
                return json.dumps({
                    "action": "dispatch",
                    "tasks": ["查询A库存"],
                    "reasoning": "先查询A仓库库存",
                }, ensure_ascii=False)
            else:
                return json.dumps({
                    "action": "synthesize",
                    "answer": "基于已有结果，A库存=100。",
                    "reasoning": "已达上限，强制汇总",
                }, ensure_ascii=False)

    def _worker_response(self, system_msg: str) -> str:
        # 从 system prompt 中提取子任务描述（避免上下文关键词干扰匹配）
        match = re.search(r"子任务：\n(.+?)\n", system_msg)
        task = match.group(1).strip() if match else ""

        if "查询A" in task:
            return "A库存=100"
        elif "查询B" in task:
            return "B库存=200"
        elif "计算总" in task:
            return "总库存=300"
        else:
            return "(mock worker 兜底)"


# =============================================================
# 主流程
# =============================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 2.4 OrchestratorWorkers 演示")
    ap.add_argument("--mock", action="store_true", help="使用 FakeLLM（不发真实请求）", default=True)
    ap.add_argument("--real", dest="mock", action="store_false", help="使用 .env 中配置的真实 LLM")
    ap.add_argument("--plain", action="store_true", help="使用纯文本渲染器（Rich 不可用时）")
    args = ap.parse_args()

    renderer = PlainRenderer() if args.plain or not RichRenderer.available() else RichRenderer()
    renderer.rule("Phase 2.4 · OrchestratorWorkers 动态分派演示")

    # 1. 构造 LLM
    if args.mock:
        llm = OWFakeLLM()
        renderer.print("[模式] FakeLLM（100% 可复现断言）")
    else:
        llm = LLM()
        renderer.print("[模式] 真实 LLM（无断言，仅展示流程）")

    # 2. 构造 Worker 工具集（Orchestrator 不注入业务工具，Q10=A）
    worker_tools = Toolkit()
    for spec in BUILTIN_TOOLS:
        worker_tools.register(**spec)

    # 3. 构造 OrchestratorWorkers
    ow = OrchestratorWorkers(
        llm=llm,
        worker_tools=worker_tools,
        max_iterations=5,
        max_concurrent_workers=5,
        max_steps_per_worker=5,
    )
    ow._renderer = renderer

    # 4. 运行主场景
    task = "查询仓库库存并汇总"
    renderer.print(f"\n[用户任务] {task}\n")

    result: OWResult = ow.run(task=task)

    # 5. 打印 trace 摘要
    renderer.rule("OW Trace 摘要（主场景）")
    t = result.trace
    renderer.print(f"任务: {t.task}")
    renderer.print(f"轮次数: {len(t.iterations)}")
    for it in t.iterations:
        forced_tag = " (强制)" if it.forced else ""
        renderer.print(f"  [轮次 {it.iteration}] action={it.action}{forced_tag}")
        if it.action == "dispatch":
            renderer.print(f"    分派任务: {it.dispatched_tasks}")
            for wr in it.worker_results:
                err_tag = f" [ERROR: {wr.error}]" if wr.error else ""
                renderer.print(f"    -> {wr.task}: {wr.artifact}{err_tag}")
        elif it.action == "synthesize":
            renderer.print(f"    reasoning: {it.reasoning}")
    renderer.print(f"总耗时: {t.total_duration_ms} ms")

    # 6. 断言（仅 --mock 模式）
    if args.mock:
        renderer.rule("6 项断言验证")
        _run_assertions(result, llm, renderer)

        # 7. max_iterations 强制汇总场景
        renderer.rule("max_iterations 强制汇总场景")
        _run_forced_scenario(renderer)

        renderer.print("\n=== orchestrator_workers_demo.py --mock 6 项断言全部通过 ===")
    else:
        renderer.print("\n[真实 LLM 模式] 跳过断言，请人工检查上方 trace 摘要")


def _run_assertions(result: OWResult, llm: OWFakeLLM, renderer) -> None:
    """6 项断言（Q15=A）。"""
    t = result.trace

    # ① 至少 1 轮 dispatch
    dispatch_iters = [it for it in t.iterations if it.action == "dispatch"]
    assert len(dispatch_iters) >= 1, f"dispatch 轮次: 预期 >=1 实际 {len(dispatch_iters)}"
    renderer.print("[1] 至少 1 轮 dispatch OK")

    # ② 某轮含 >= 2 个并行 Worker
    max_workers = max((len(it.worker_results) for it in dispatch_iters), default=0)
    assert max_workers >= 2, f"最大并行 Worker 数: 预期 >=2 实际 {max_workers}"
    renderer.print(f"[2] 并行 Worker >=2 OK（最大 {max_workers} 个）")

    # ③ 最终 action 为 synthesize
    assert t.iterations[-1].action == "synthesize", (
        f"最终 action: 预期 synthesize 实际 {t.iterations[-1].action}"
    )
    renderer.print("[3] 最终 synthesize OK")

    # ④ 最终答案含关键字"总库存"
    assert "总库存" in result.final_answer, (
        f"最终答案缺关键字'总库存': {result.final_answer[:100]}"
    )
    renderer.print("[4] 最终答案含'总库存'关键字 OK")

    # ⑤ 上下文传递：第 2 轮 Orchestrator prompt 含第 1 轮 Worker 结果
    assert len(llm._orch_messages) >= 2, f"Orchestrator 调用次数: 预期 >=2 实际 {len(llm._orch_messages)}"
    orch_msg_2 = llm._orch_messages[1]
    assert "A库存=100" in orch_msg_2, "第 2 轮 Orchestrator prompt 未包含第 1 轮 Worker 结果 'A库存=100'"
    assert "B库存=200" in orch_msg_2, "第 2 轮 Orchestrator prompt 未包含第 1 轮 Worker 结果 'B库存=200'"
    renderer.print("[5] 上下文传递 OK（第 2 轮含第 1 轮 Worker 结果）")

    # ⑥ 总轮次数 = 3（2 轮 dispatch + 1 轮 synthesize）
    assert len(t.iterations) == 3, f"总轮次: 预期 3 实际 {len(t.iterations)}"
    renderer.print("[6] 总轮次 = 3（2 dispatch + 1 synthesize）OK")


def _run_forced_scenario(renderer) -> None:
    """max_iterations 强制汇总场景（断言 ⑥ 的补充验证）。"""
    renderer.print("构造 max_iterations=2 的实例，验证强制汇总...")

    forced_llm = OWFakeLLM(scenario="forced")
    worker_tools = Toolkit()
    for spec in BUILTIN_TOOLS:
        worker_tools.register(**spec)

    ow_forced = OrchestratorWorkers(
        llm=forced_llm,
        worker_tools=worker_tools,
        max_iterations=2,
        max_concurrent_workers=5,
        max_steps_per_worker=5,
    )
    ow_forced._renderer = renderer

    result = ow_forced.run(task="查询仓库库存并汇总")

    t = result.trace
    # 第 1 轮正常 dispatch
    assert t.iterations[0].action == "dispatch", (
        f"第 1 轮 action: 预期 dispatch 实际 {t.iterations[0].action}"
    )
    renderer.print(f"  第 1 轮: dispatch {t.iterations[0].dispatched_tasks}")

    # 第 2 轮强制 synthesize
    assert t.iterations[1].action == "synthesize", (
        f"第 2 轮 action: 预期 synthesize 实际 {t.iterations[1].action}"
    )
    assert t.iterations[1].forced is True, "第 2 轮 forced 标记应为 True"
    renderer.print(f"  第 2 轮: synthesize (forced=True)")

    # 最终答案非空
    assert result.final_answer, "强制汇总的最终答案不应为空"
    assert "A库存" in result.final_answer, (
        f"强制汇总答案应含 'A库存': {result.final_answer[:100]}"
    )
    renderer.print(f"  最终答案: {result.final_answer[:100]}")
    renderer.print("[强制汇总] OK（1 dispatch + 1 forced synthesize）")


if __name__ == "__main__":
    main()

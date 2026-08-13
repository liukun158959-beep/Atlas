"""Phase 2.3 演示：PlannerWorkerCritic 多 Agent 模式包（质量闭环）。

场景（--mock 模式全断言可重复）：
  Planner 拆解任务"分析 2024 年闰年情况、当前时间，并总结关联"→ 3 个子任务：
    1. 计算 2024 是否是闰年并算总小时数
    2. 获取当前时间
    3. 总结两者关联
  串行执行（Q12=A）：
    子任务 1：Worker v1 输出"缺闰年判定依据" → Critic FAIL → Worker v2 补充依据 → Critic PASS
    子任务 2：Worker 一次 PASS
    子任务 3：Worker 一次 PASS
  Planner 汇总 → 最终答案含"闰年"关键字

定位（Q21=A）：
  - parallel_subagent_demo.py = 并行加速（纯 fan-out，无依赖、无 Critic）
  - pwc_demo.py = 质量闭环（串行 + Critic 打回重做，不求快但求稳）

运行方式：
  python examples/pwc_demo.py --mock --plain    # 断言模式，100% 可重复
  python examples/pwc_demo.py --mock             # 同上，Rich 渲染（若可用）
  python examples/pwc_demo.py --real             # 真实 LLM（需 .env 配置）
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas import LLM, Toolkit
from atlas.patterns import PlannerWorkerCritic, PWCResult
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS


# =============================================================
# FakeLLM：10 次 LLM 调用精确映射 PWC 全流程
# =============================================================
@dataclass
class PWCFakeLLM(LLM):
    """Mock LLM：按 PWC 流程顺序返回固定响应。

    调用顺序（_step 1~10）：
      1  Planner 拆解       → 3 行 plan
      2  Worker 1 v1        → 缺闰年判定依据的版本
      3  Critic 1 v1        → FAIL（缺少闰年判定依据）
      4  Worker 1 v2        → 补充闰年判定的修正版本
      5  Critic 1 v2        → PASS
      6  Worker 2 v1        → 当前时间
      7  Critic 2 v1        → PASS
      8  Worker 3 v1        → 总结关联
      9  Critic 3 v1        → PASS
      10 Planner 汇总       → 最终答案含"闰年"
    """

    model: str = "mock-pwc"
    _step: int = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        self._step += 1
        s = self._step

        # ── Phase A：Planner 拆解 ──
        if s == 1:
            content = (
                "计算 2024 是否是闰年并算总小时数\n"
                "获取当前时间\n"
                "总结两者关联"
            )
        # ── Phase B：子任务 1（Worker v1 → Critic FAIL → Worker v2 → Critic PASS）──
        elif s == 2:
            content = "2024 年共 366*24 = 8784 小时"
        elif s == 3:
            content = (
                "FAIL\n"
                "缺少闰年判定依据。你只算了 366*24 的小时数，"
                "但没先论证 2024 为什么是闰年。请补充"
                "'2024 能被 4 整除且不能被 100 整除'的判定依据。"
            )
        elif s == 4:
            content = (
                "2024 能被 4 整除（2024/4=506）且不能被 100 整除，"
                "符合闰年判定规则，因此 2024 是闰年，"
                "共 366 天，即 366×24=8784 小时"
            )
        elif s == 5:
            content = "PASS\n已补充闰年判定依据，计算正确"
        # ── 子任务 2（一次 PASS）──
        elif s == 6:
            content = "当前时间是 2026-08-13 14:30:00（UTC+8）"
        elif s == 7:
            content = "PASS\n结果正确"
        # ── 子任务 3（一次 PASS）──
        elif s == 8:
            content = (
                "闰年 2024 有 366 天 8784 小时，"
                "与当前时间 2026-08-13 的关联："
                "2024 是闰年，2026 不是闰年（2026/4=506.5 不整除），"
                "两者相差约 1.5 年。"
            )
        elif s == 9:
            content = "PASS\n总结到位，闰年判定与时间关联正确"
        # ── Phase C：Planner 汇总 ──
        elif s == 10:
            content = (
                "综合分析结果：\n"
                "2024 是闰年（能被 4 整除且不能被 100 整除），"
                "全年 366 天共 8784 小时。\n"
                "当前时间为 2026-08-13，2026 不是闰年。\n"
                "闰年 2024 与当前时间 2026 相差约 1.5 年，"
                "期间经历了一次闰日（2024-02-29）。"
            )
        else:
            # 兜底：超出预期调用次数
            content = f"（mock 兜底响应 #{s}）"

        return {
            "message": {"role": "assistant", "content": content},
            "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            "duration_ms": 5,
        }


# =============================================================
# 主流程
# =============================================================
def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 2.3 PlannerWorkerCritic 演示")
    ap.add_argument("--mock", action="store_true", help="使用 FakeLLM（不发真实请求）", default=True)
    ap.add_argument("--real", dest="mock", action="store_false", help="使用 .env 中配置的真实 LLM")
    ap.add_argument("--plain", action="store_true", help="使用纯文本渲染器（Rich 不可用时）")
    args = ap.parse_args()

    renderer = PlainRenderer() if args.plain or not RichRenderer.available() else RichRenderer()
    renderer.rule("Phase 2.3 · PlannerWorkerCritic 质量闭环演示")

    # 1. 构造 LLM
    if args.mock:
        llm = PWCFakeLLM()
        renderer.print("[模式] FakeLLM（100% 可复现断言）")
    else:
        llm = LLM()
        renderer.print("[模式] 真实 LLM（无断言，仅展示流程）")

    # 2. 构造 Worker 工具集（Planner/Critic 不注入业务工具，Q10=A）
    worker_tools = Toolkit()
    for spec in BUILTIN_TOOLS:
        worker_tools.register(**spec)

    # 3. 构造 PWC
    pwc = PlannerWorkerCritic(
        llm=llm,
        worker_tools=worker_tools,
        max_revisions=2,
        max_plan_items=4,
        max_steps_per_worker=5,
    )
    # 注入共享 renderer（构造函数不暴露 renderer 字段，卡 12 项限额）
    pwc._renderer = renderer

    # 4. 运行
    task = "分析 2024 年闰年情况、当前时间，并总结关联"
    renderer.print(f"\n[用户任务] {task}\n")

    result: PWCResult = pwc.run(task=task)

    # 5. 打印 trace 摘要
    renderer.rule("PWC Trace 摘要")
    t = result.trace
    renderer.print(f"任务: {t.task}")
    renderer.print(f"子任务数: {len(t.plan_items)}")
    for item in t.plan_items:
        status_tag = {"done": "PASS", "failed": "FAIL", "pending": "..."}.get(item.status, item.status)
        rev_tag = f" (重做 {item.revisions} 次)" if item.revisions > 0 else ""
        renderer.print(f"  [{status_tag}] {item.idx}. {item.description}{rev_tag}")
    renderer.print(f"Worker 执行次数: {len(t.worker_runs)}")
    renderer.print(f"Critic 审查次数: {len(t.critic_reviews)}")
    renderer.print(f"总耗时: {t.total_duration_ms} ms")

    # 6. 断言（仅 --mock 模式）
    if args.mock:
        renderer.rule("5 项断言验证")
        _run_assertions(result, renderer)
        renderer.print("\n=== pwc_demo.py --mock 5 项断言全部通过 ===")
    else:
        renderer.print("\n[真实 LLM 模式] 跳过断言，请人工检查上方 trace 摘要")


def _run_assertions(result: PWCResult, renderer) -> None:
    """5 项断言（Q7=A + Q13=A 精确对齐）。"""
    t = result.trace

    # ① plan 拆解得到 3 个子任务
    assert len(t.plan_items) == 3, f"plan 数量: 预期 3 实际 {len(t.plan_items)}"
    assert t.plan_items[0].description == "计算 2024 是否是闰年并算总小时数"
    renderer.print("[1] plan 拆解 3 项 OK")

    # ② Worker artifact_schema_pass 全 True（本 demo 无 schema，_validate 空 schema 返回 True）
    assert all(w.artifact_schema_pass for w in t.worker_runs), "Worker schema 校验应全通过"
    renderer.print("[2] Worker schema 校验 OK")

    # ③ Critic 打回 1 次 + revisions=1 + 最终 approved
    c1_reviews = [c for c in t.critic_reviews if c.plan_idx == 1]
    assert len(c1_reviews) == 2, f"子任务 1 Critic 审查次数: 预期 2 实际 {len(c1_reviews)}"
    assert c1_reviews[0].approved is False, "第 1 次审查应 FAIL"
    assert c1_reviews[1].approved is True, "第 2 次审查应 PASS"
    assert t.plan_items[0].revisions == 1, f"revisions: 预期 1 实际 {t.plan_items[0].revisions}"
    assert t.plan_items[0].status == "done", f"status: 预期 done 实际 {t.plan_items[0].status}"
    renderer.print("[3] Critic 打回 1 次后通过 OK（revisions=1）")

    # ④ final_answer 含关键字"闰年"
    assert "闰年" in result.final_answer, f"最终答案缺关键字'闰年': {result.final_answer[:100]}"
    renderer.print("[4] 最终答案含'闰年'关键字 OK")

    # ⑤ 重做 Worker 时输入包含了 Critic FAIL 评语
    plan1_v2 = [w for w in t.worker_runs if w.plan_idx == 1 and w.attempt == 2]
    assert len(plan1_v2) == 1, f"子任务 1 重做记录: 预期 1 实际 {len(plan1_v2)}"
    assert plan1_v2[0].input_has_critic_comment is True, "重做 Worker 未检测到 Critic 评语注入"
    renderer.print("[5] Worker 重做输入含 Critic 评语 OK")


if __name__ == "__main__":
    main()

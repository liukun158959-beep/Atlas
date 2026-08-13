"""PlannerWorkerCritic 多 Agent 模式包（产品级）。

流程：
  Phase A: Planner 拆解用户任务 → 2~max_plan_items 个子任务
  Phase B: 串行子任务循环（Q12=A）
    每个 PlanItem:
      while attempt <= max_revisions:
        Worker Agent 执行 → 拿到 raw_artifact
        Critic Agent 审查 → PASS / FAIL
        if PASS: 标 done + artifact 入后续上下文 + break
        else:    revisions += 1 + 重做（Q4=C：Worker 输入追加 Critic 评语 + 前一版结果）
      if 耗尽仍 FAIL: 标 failed + 用最后一版占位 + 继续下一子任务（Q19=A 不 abort）
  Phase C: Planner 汇总 → 整合所有子任务结果（failed 项明确标注"仅供参考"）

设计来源：Phase 2.3 grill-me 21 项决策冻结（详见
docs_feishu/phase2-3_planner_worker_critic_implementation_plan.md）。
"""
from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from atlas import Agent, LLM, Toolkit
from atlas.context.trace import Trace as AgentTrace
from atlas.core.circuit_breaker import CircuitBreakerRegistry
from atlas.core.plugin import PluginManager
from atlas.core.terminator import Terminator
from atlas.journal.base import JournalProtocol
from atlas.render import PlainRenderer, Renderer

from ._common import _make_agent


# =============================================================
# 3 条内置 prompt 模板（Q20=A）。3 个可选参数可覆写
# =============================================================
_DEFAULT_PLANNER_PROMPT = """你是任务规划师（Planner）。
职责：
  1. 把用户任务拆解为 2~{max_plan_items} 个可独立执行的子任务
  2. 等待所有子任务执行完成后，基于各子任务结果汇总出最终答案
输出格式（规划阶段）：
  每行一个子任务，不要编号，不要其他文字。
输出格式（汇总阶段）：
  给出完整、连贯的最终答案，如果某些子任务被标记为 "Critic 审查未通过"，
  请在最终答案中显式标注"此项为 Critic 连续打回 {max_revisions} 次未过版本，仅供参考"字样。
"""

_DEFAULT_WORKER_PROMPT = """你是子任务执行者（Worker）。
职责：完成下面这个子任务，可调用指定工具。

子任务：
{subtask_description}

已完成的前置子任务结果（上下文）：
{context_block}

{critic_revision_block}

输出要求：
  - 直接给出子任务执行结果，不要多余解释
  - 如果任务要求 JSON，请返回纯 JSON 字符串（不要 Markdown 代码块包装）
"""

_DEFAULT_CRITIC_PROMPT = """你是质量审查员（Critic）。
职责：判断下面 Worker 的执行结果是否准确、完整地完成了子任务要求。

子任务：
{subtask_description}

Worker 执行结果：
{worker_result}

输出格式（严格遵守，一行都不能错）：
  第一行：PASS  或  FAIL（必须全大写）
  第二行起：评语（中文，说明为什么通过 / 哪里需要修改，最多 300 字）
"""


# =============================================================
# 5 个扁平 dataclass（Q14=B）
# =============================================================
@dataclass
class PlanItem:
    """Planner 拆解出的单个子任务条目。"""

    idx: int  # 1-based 顺序
    description: str  # 子任务文字描述
    status: str = "pending"  # pending / running / done / failed（Critic 耗尽标 failed）
    artifact: str = ""  # 最终 approved 的 Worker 输出（或耗尽时的最后版本）
    revisions: int = 0  # 被 Critic 打回重做的次数（第一次 FAIL 后 revisions 从 0→1）


@dataclass
class WorkerRun:
    """每次 Worker 执行的记录（含重做）。供 demo 断言和 trace 可视化。"""

    plan_idx: int  # 对应 PlanItem.idx
    attempt: int  # 第几次执行（1=首次，>=2=重做）
    input_has_critic_comment: bool  # Q13 断言⑤：重做时输入是否含 Critic 评语
    raw_artifact: str  # 本次执行的原始输出
    artifact_schema_pass: bool  # artifact_schema 校验结果（若有 schema）
    duration_ms: int = 0  # 本次执行耗时
    sub_agent_trace_snapshot: dict | None = None  # 从 Agent.trace 摘出的关键信息


@dataclass
class CriticReview:
    """每次 Critic 审查的记录。"""

    plan_idx: int  # 对应 PlanItem.idx
    worker_attempt: int  # 对应 WorkerRun.attempt
    approved: bool  # True=PASS / False=FAIL
    comment: str  # 评语（给用户看 + 下次重做传给 Worker）
    duration_ms: int = 0


@dataclass
class PWCTrace:
    """PWC 全程扁平可观测存档。Q14=B 专用结构，不依赖嵌套 Agent Trace。"""

    task: str = ""
    # Plan 层
    plan_items: list[PlanItem] = field(default_factory=list)
    # Worker 层（按执行顺序 append，重做也 append 新条目，plan_idx 关联）
    worker_runs: list[WorkerRun] = field(default_factory=list)
    # Critic 层（每次审查一条，plan_idx + worker_attempt 关联）
    critic_reviews: list[CriticReview] = field(default_factory=list)
    # 时间戳
    start_ts: float = 0.0
    end_ts: float = 0.0
    total_duration_ms: int = 0


@dataclass
class PWCResult:
    """run(task) 的返回值（Q3=B）。"""

    final_answer: str  # Planner 汇总后的最终答案（可能带 Critic 未过告警）
    trace: PWCTrace  # 全程扁平 trace


# =============================================================
# PlannerWorkerCritic 主类
# =============================================================
@dataclass
class PlannerWorkerCritic:
    """Planner-Worker-Critic 多 Agent 模式包。

    设计依据：
      Q2=C 多层嵌套 Agent（Planner/Worker/Critic 都是内核 Agent 实例）
      Q12=A 串行执行（每个 Worker 的输入含前面所有已 approved 结果 + Critic 重做评语）
      Q11=A Python while 控制 Critic 循环（LLM 只做内容判断，不控制循环次数）
      Q19=A max_revisions 耗尽标 failed 不 abort
      Q18=A 构造函数必填 2 + 可选 10 = 12 项（≤ DR-2 15 限额）
    """

    # ── 必填（2 项） ──
    llm: LLM
    worker_tools: Toolkit  # Worker 可用的业务工具，Planner/Critic 不注入

    # ── 可选（10 项，全有默认值） ──
    task: str | None = None  # 可选，也可在 run(task=...) 传入时覆盖
    planner_system_prompt: str | None = None
    worker_system_prompt: str | None = None
    critic_system_prompt: str | None = None
    max_revisions: int = 2  # Critic 打回重做上限（达到仍 FAIL 标 failed，不 abort）
    max_plan_items: int = 4  # Planner 拆解上限（超上限时由 prompt 约束，Python 侧也截断）
    max_steps_per_worker: int = 10  # 单个 Worker Agent 的 max_steps 参数
    circuit_breaker_registry: CircuitBreakerRegistry | None = None  # 所有角色共享
    terminator: Terminator | None = None  # 所有角色共享（None 用默认）
    plugin_manager: PluginManager | None = None  # 所有角色共享

    # ── 内部状态（post_init 初始化，不进构造函数签名） ──
    _final_planner_prompt: str = field(default="", init=False, repr=False)
    _final_worker_prompt: str = field(default="", init=False, repr=False)
    _final_critic_prompt: str = field(default="", init=False, repr=False)
    _renderer: Renderer = field(default_factory=PlainRenderer, init=False, repr=False)

    def __post_init__(self) -> None:
        # 默认 prompt（用户没传时用内置常量）
        self._final_planner_prompt = (
            self.planner_system_prompt
            or _DEFAULT_PLANNER_PROMPT.format(
                max_plan_items=self.max_plan_items,
                max_revisions=self.max_revisions,
            )
        )
        self._final_worker_prompt = self.worker_system_prompt or _DEFAULT_WORKER_PROMPT
        self._final_critic_prompt = self.critic_system_prompt or _DEFAULT_CRITIC_PROMPT
        # 共享 renderer：所有 Planner/Worker/Critic Agent 共用同一个 Renderer 实例
        # （RichRenderer 终端输出不会乱序，PlainRenderer 也安全）
        # 注：renderer 不进 public 构造函数（卡 12 项限额），用户若要 Rich 可在 new 后
        #     手动 p._renderer = RichRenderer()
        self._renderer = PlainRenderer()

    # ──────────────────────────────────────────────
    # 外部 API
    # ──────────────────────────────────────────────
    def run(self, task: str | None = None) -> PWCResult:
        """运行完整 PWC 流程。task 若传则覆盖构造函数 task 字段。

        返回 PWCResult(final_answer, trace)。

        流程：Phase A 拆解 → Phase B 串行子任务（Worker→Critic→重做）→ Phase C 汇总标注
        """
        run_task = task or self.task
        if not run_task:
            raise ValueError("PlannerWorkerCritic.run(task): 请传入 task 或构造时指定 task")

        trace = PWCTrace(task=run_task, start_ts=time.time())
        _start = time.time()

        # =============================================================
        # Phase A：Planner 拆解
        # =============================================================
        self._renderer.rule("PWC · Planner 拆解")
        # Q10=A：Planner 不注入任何业务工具，纯文本推理
        # Q2=C + Q11=A + Q12=A 组合下：Planner Agent 只做"拆解"和"汇总"两个文本任务，
        #   不调 spawn_subagents（Worker 由 PWC 直接 new Agent 执行）
        planner_tools = Toolkit()
        planner_agent = _make_agent(
            role_name="planner",
            system_prompt=self._final_planner_prompt,
            llm=self.llm,
            tools=planner_tools,
            max_steps=2,  # 拆解只要 1~2 步 LLM 调用
            shared_renderer=self._renderer,
            shared_terminator=self.terminator,
            shared_plugin_manager=self.plugin_manager,
            shared_circuit_breaker_registry=self.circuit_breaker_registry,
            compact_threshold=4000,  # Planner 对话短，降低 compact 阈值
        )
        planner_decompose_input = (
            f"请开始拆解。用户任务：{run_task}\n\n"
            "输出格式：每行一个子任务，不要编号。"
        )
        plan_output = planner_agent.run(planner_decompose_input)
        plan_lines = _parse_plan(plan_output)
        # max_plan_items 截断（Prompt 应该限制住，Python 侧兜底）
        plan_lines = plan_lines[: self.max_plan_items]
        if len(plan_lines) < 2:
            # Planner 拆不出 2 项以上时，退化为"整个任务当一项" + 加一个"汇总校验项"
            plan_lines = [run_task, "校验并整合上面的结果，输出最终结论"]

        # 写入 trace
        for i, desc in enumerate(plan_lines, 1):
            trace.plan_items.append(PlanItem(idx=i, description=desc))
        self._renderer.print(f"拆解为 {len(trace.plan_items)} 个子任务:")
        for item in trace.plan_items:
            self._renderer.print(f"  {item.idx}. {item.description}")

        # =============================================================
        # Phase B：串行子任务（每个：Worker 执行 → Critic 审查 → while 重做）
        # =============================================================
        context_parts: list[str] = []  # Q4=A 串行上下文：已 approved 的 plan item 结果作为后续 Worker 的输入

        for plan_item in trace.plan_items:
            plan_item.status = "running"
            plan_idx = plan_item.idx

            context_block = "\n".join(context_parts) if context_parts else ""

            # Worker 循环：最多 1 + max_revisions 次（首次 + max_revisions 次重做）
            worker_attempt = 0
            final_artifact = ""
            approved = False

            while worker_attempt <= self.max_revisions:
                worker_attempt += 1

                # ── 构造 Worker 输入 prompt ──
                subtask_desc = plan_item.description
                revision_block = ""
                input_has_comment = False
                if worker_attempt > 1:
                    # Q4=C：重做时追加 Critic 评语 + 前一版结果
                    last_review = trace.critic_reviews[-1] if trace.critic_reviews else None
                    prev_result = trace.worker_runs[-1].raw_artifact if trace.worker_runs else ""
                    if last_review:
                        revision_block = (
                            "前一版 Critic 打回，评语如下（请据此修改）：\n"
                            f"  Critic 评语：{last_review.comment}\n"
                            f"  前一版 Worker 输出：\n{prev_result}\n\n"
                            "请基于以上修正，重新输出正确结果。"
                        )
                        input_has_comment = True

                worker_prompt = self._final_worker_prompt.format(
                    subtask_description=subtask_desc,
                    context_block=context_block or "(无前置上下文)",
                    critic_revision_block=revision_block or "(首次执行，无 Critic 反馈)",
                )

                # ── Worker Agent 执行 ──
                t0 = time.time()
                worker_agent = _make_agent(
                    role_name=f"worker_{plan_idx}_v{worker_attempt}",
                    system_prompt=worker_prompt,  # 把完整 prompt 直接当 system prompt 给 Worker Agent
                    llm=self.llm,
                    tools=self.worker_tools,  # Q10=A：业务工具只给 Worker
                    max_steps=self.max_steps_per_worker,
                    shared_renderer=self._renderer,
                    shared_terminator=self.terminator,
                    shared_plugin_manager=self.plugin_manager,
                    shared_circuit_breaker_registry=self.circuit_breaker_registry,
                )
                worker_result_raw = worker_agent.run("请开始执行上面的子任务。")
                worker_duration_ms = int((time.time() - t0) * 1000)

                # artifact_schema（本实现 v1 不挂 schema，默认 True）
                schema_ok, _schema_err = True, None

                # 记录 WorkerRun
                wr = WorkerRun(
                    plan_idx=plan_idx,
                    attempt=worker_attempt,
                    input_has_critic_comment=input_has_comment,
                    raw_artifact=worker_result_raw,
                    artifact_schema_pass=schema_ok,
                    duration_ms=worker_duration_ms,
                    sub_agent_trace_snapshot=_extract_nested_trace(worker_agent),
                )
                trace.worker_runs.append(wr)

                # ── Critic 审查 ──
                t1 = time.time()
                critic_prompt = self._final_critic_prompt.format(
                    subtask_description=subtask_desc,
                    worker_result=worker_result_raw,
                )
                critic_tools = Toolkit()  # Critic 也纯文本，不用工具
                critic_agent = _make_agent(
                    role_name=f"critic_{plan_idx}_v{worker_attempt}",
                    system_prompt=critic_prompt,
                    llm=self.llm,
                    tools=critic_tools,
                    max_steps=2,  # MaxSteps 在 llm.chat() 之前检查 step_count>=max_steps，设 2 保证 1 次 LLM 调用
                    shared_renderer=self._renderer,
                    shared_terminator=self.terminator,
                    shared_plugin_manager=self.plugin_manager,
                    shared_circuit_breaker_registry=self.circuit_breaker_registry,
                )
                critic_output = critic_agent.run("请开始审查。")
                critic_duration_ms = int((time.time() - t1) * 1000)
                c_approved, c_comment = _parse_critic(critic_output)

                # 记录 CriticReview
                trace.critic_reviews.append(CriticReview(
                    plan_idx=plan_idx,
                    worker_attempt=worker_attempt,
                    approved=c_approved,
                    comment=c_comment,
                    duration_ms=critic_duration_ms,
                ))

                if c_approved:
                    final_artifact = worker_result_raw
                    approved = True
                    break
                # 没通过：revisions +=1，while 循环继续（若没到 max_revisions）
                plan_item.revisions = worker_attempt  # 已尝试次数就是 revisions 当前值

            # ── Worker 循环结束 ──
            if approved:
                plan_item.status = "done"
                plan_item.artifact = final_artifact
                # 加入串行上下文
                context_parts.append(f"[子任务{plan_idx} 结果（已通过审查）]\n{final_artifact}")
                self._renderer.panel(
                    final_artifact[:200],
                    title=f"子任务 {plan_idx} · 通过",
                    border_style="green",
                )
            else:
                # Q19=A：max_revisions 耗尽仍 FAIL → 标 failed + 继续下一子任务（不 abort）
                plan_item.status = "failed"
                plan_item.artifact = trace.worker_runs[-1].raw_artifact  # 用最后一版占位
                context_parts.append(
                    f"[子任务{plan_idx} 结果（Critic 连续打回 {self.max_revisions} 次未过，仅供参考）]\n"
                    f"{plan_item.artifact}"
                )
                self._renderer.panel(
                    plan_item.artifact[:200],
                    title=f"子任务 {plan_idx} · Critic 未通过（耗尽）",
                    border_style="red",
                    revisions=str(self.max_revisions),
                )

        # =============================================================
        # Phase C：Planner 汇总
        # =============================================================
        self._renderer.rule("PWC · Planner 汇总")
        results_block = "\n".join(context_parts)
        summary_input = (
            f"所有子任务已完成。用户原始任务：{run_task}\n\n"
            f"各子任务执行结果（包含 Critic 状态标注）：\n{results_block}\n\n"
            "请基于以上信息给出最终答案。如果有子任务被标 '仅供参考'，请在答案中显式标注对应说明。"
        )

        summary_agent = _make_agent(
            role_name="planner_summary",
            system_prompt=self._final_planner_prompt,
            llm=self.llm,
            tools=Toolkit(),  # 汇总也不用工具
            max_steps=2,
            shared_renderer=self._renderer,
            shared_terminator=self.terminator,
            shared_plugin_manager=self.plugin_manager,
            shared_circuit_breaker_registry=self.circuit_breaker_registry,
            compact_threshold=4000,
        )
        final_answer = summary_agent.run(summary_input)

        # 收尾
        trace.end_ts = time.time()
        trace.total_duration_ms = int((trace.end_ts - _start) * 1000)

        self._renderer.markdown(final_answer[:500], title="最终答案（摘要）")
        return PWCResult(final_answer=final_answer, trace=trace)


# =============================================================
# 私有辅助：解析 + trace 摘出
# =============================================================
def _parse_plan(output: str) -> list[str]:
    """Planner 拆解输出 → 子任务字符串列表。

    规则：按 \\n 切分，去掉空行、去掉前后空白、去掉行首数字编号（1. / 2) / 3）等）。
    """
    lines: list[str] = []
    for line in (output or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        # 去行首编号
        s = re.sub(r"^(\d+[\.\)、\s]+)", "", s).strip()
        if s:
            lines.append(s)
    return lines


def _parse_critic(output: str) -> tuple[bool, str]:
    """Critic 审查输出 → (approved, comment)。

    规则：第一行全大写包含 PASS → approved；否则 FAIL（容错：只要首行不含 PASS 就 FAIL）。
    第二行起是评语。
    """
    lines = (output or "").split("\n")
    first_line = lines[0].strip().upper() if lines else ""
    approved = first_line.startswith("PASS")
    # 容错：如果没两行，把全文当评语
    comment = "\n".join(lines[1:]).strip() or (output or "").strip() or "(无评语)"
    return (approved, comment)


def _extract_nested_trace(agent: Agent) -> dict:
    """从 Agent.trace（嵌套 Trace 树）摘出 demo 断言需要的最小扁平字典。"""
    t: AgentTrace = agent.trace
    steps = getattr(t, "steps", [])
    # 保守提取：不同版本 Trace 结构可能略有差异，全部用 getattr 兜底
    tool_names: set[str] = set()
    for step in steps:
        calls = getattr(step, "tool_calls", None) or []
        for call in calls:
            name = getattr(call, "name", None) if not isinstance(call, dict) else call.get("name")
            if name:
                tool_names.add(name)
    return {
        "step_count": len(steps),
        "tool_names": sorted(tool_names),
        "status": getattr(t, "meta", {}).get("status", "unknown") if hasattr(t, "meta") else "unknown",
    }

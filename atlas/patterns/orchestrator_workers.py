"""OrchestratorWorkers 多 Agent 模式包（产品级）。

流程（Anthropic +90.2% 模式）：
  Python while iteration < max_iterations:
    1. Orchestrator Agent LLM 决策 -> JSON {action: dispatch/synthesize}
    2. dispatch: 分派 K 个子任务 -> ThreadPoolExecutor 并行执行 K 个 Worker -> 结果全部回 Orchestrator
    3. synthesize: 汇总最终答案 -> break
  如果 max_iterations 耗尽仍没 synthesize -> Python 侧强制汇总

设计来源：Phase 2.4 grill-me 19 项决策冻结（详见
docs_feishu/phase2-4_orchestrator_workers_implementation_plan.md）。
"""
from __future__ import annotations

import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
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
# 2 条内置 prompt 模板（Q12=A）。2 个可选参数可覆写
# =============================================================
_DEFAULT_ORCHESTRATOR_PROMPT = """你是任务编排者（Orchestrator）。
职责：把用户任务分解为可并行的子任务，分派给 Worker 执行；
      每轮收到 Worker 结果后，判断是否需要继续分派，还是汇总出最终答案。

已完成的历史轮次：
{iteration_history}

最新一批 Worker 结果：
{latest_results}

输出格式（严格 JSON，不要 Markdown 代码块包装）：
  分派：{{"action": "dispatch", "tasks": ["任务1", "任务2"], "reasoning": "简要原因"}}
  汇总：{{"action": "synthesize", "answer": "最终答案", "reasoning": "简要原因"}}

规则：
  - 单批最多 {max_concurrent_workers} 个任务
  - 信息充分时必须 synthesize，不要无意义重复分派
  - JSON 必须合法，不要在 JSON 外输出任何文字
"""

_DEFAULT_WORKER_PROMPT = """你是子任务执行者（Worker）。
职责：完成下面这个子任务，可调用指定工具。

子任务：
{subtask_description}

历史上下文（已完成的相关结果）：
{context_block}

输出要求：
  - 直接给出子任务执行结果，不要多余解释
  - 如果任务要求 JSON，请返回纯 JSON 字符串（不要 Markdown 代码块包装）
"""


# =============================================================
# 4 个扁平 dataclass（Q9=A）
# =============================================================
@dataclass
class WorkerResult:
    """单个 Worker 执行结果。"""

    iteration: int  # 第几轮（1-based）
    task: str  # Orchestrator 分派的任务描述
    artifact: str  # Worker 输出（失败时为错误信息）
    duration_ms: int = 0
    error: str | None = None  # 非 None 表示执行失败（Q16=A partial success）
    sub_agent_trace_snapshot: dict | None = None  # 从 Agent.trace 摘出的关键信息


@dataclass
class IterationLog:
    """一轮 Orchestrator 决策 + 批 Worker 结果。"""

    iteration: int  # 1-based
    action: str  # "dispatch" / "synthesize"
    dispatched_tasks: list[str] = field(default_factory=list)  # dispatch 时的任务列表
    worker_results: list[WorkerResult] = field(default_factory=list)  # 本批 Worker 结果
    reasoning: str = ""  # Orchestrator 的 reasoning 字段（审计用）
    forced: bool = False  # True 表示 max_iterations 耗尽后 Python 侧强制 synthesize


@dataclass
class OWTrace:
    """OW 全程扁平可观测存档。"""

    task: str = ""
    iterations: list[IterationLog] = field(default_factory=list)  # 按轮次顺序
    start_ts: float = 0.0
    end_ts: float = 0.0
    total_duration_ms: int = 0


@dataclass
class OWResult:
    """run(task) 的返回值。"""

    final_answer: str  # Orchestrator 汇总后的最终答案
    trace: OWTrace  # 全程扁平 trace


# =============================================================
# OrchestratorWorkers 主类
# =============================================================
@dataclass
class OrchestratorWorkers:
    """Orchestrator-Workers 多 Agent 模式包。

    设计依据：
      Q1=A Python while 循环（和 PWC 一致，Graph 原语留给用户）
      Q2=A 一批并行 + 批间串行（ThreadPoolExecutor 直接并行，Q6=A）
      Q3=A JSON 结构化输出（dispatch/synthesize 两动作）
      Q8=A max_iterations 耗尽强制汇总（Q14=A Python 侧控制）
      Q10=A 不加 Critic（OW 定位效率优先，PWC 已覆盖质量闭环）
      Q16=A Worker 失败 partial success（Orchestrator 自主决策）
      Q18=A 构造函数必填 2 + 可选 9 = 11 项（<= DR-2 15 限额）
    """

    # -- 必填（2 项） --
    llm: LLM
    worker_tools: Toolkit  # Worker 可用的业务工具，Orchestrator 不注入

    # -- 可选（9 项，全有默认值） --
    task: str | None = None  # 可选，也可在 run(task=...) 传入时覆盖
    orchestrator_system_prompt: str | None = None
    worker_system_prompt: str | None = None
    max_iterations: int = 5  # Orchestrator 最大决策轮次（防 runaway）
    max_concurrent_workers: int = 5  # 单批最大并行 Worker 数
    max_steps_per_worker: int = 10  # 单个 Worker Agent 的 max_steps
    circuit_breaker_registry: CircuitBreakerRegistry | None = None  # 所有角色共享
    terminator: Terminator | None = None  # 所有角色共享（None 用默认）
    plugin_manager: PluginManager | None = None  # 所有角色共享

    # -- 内部状态（post_init 初始化，不进构造函数签名） --
    _final_orchestrator_prompt: str = field(default="", init=False, repr=False)
    _final_worker_prompt: str = field(default="", init=False, repr=False)
    _renderer: Renderer = field(default_factory=PlainRenderer, init=False, repr=False)

    def __post_init__(self) -> None:
        # Orchestrator prompt 含构造时占位符 {max_concurrent_workers} + 运行时占位符
        # {iteration_history}/{latest_results}，统一在 run() 中 format
        self._final_orchestrator_prompt = (
            self.orchestrator_system_prompt or _DEFAULT_ORCHESTRATOR_PROMPT
        )
        self._final_worker_prompt = self.worker_system_prompt or _DEFAULT_WORKER_PROMPT
        # 和 PWC 一致：renderer 不进 public 构造函数（卡 11 项限额）
        self._renderer = PlainRenderer()

    # -----------------------------------------------
    # 外部 API
    # -----------------------------------------------
    def run(self, task: str | None = None) -> OWResult:
        """运行完整 OW 流程。task 若传则覆盖构造函数 task 字段。

        返回 OWResult(final_answer, trace)。

        流程：Python while 循环 -> 每轮 Orchestrator LLM 决策 -> dispatch(并行 Worker) / synthesize(汇总)
        """
        run_task = task or self.task
        if not run_task:
            raise ValueError("OrchestratorWorkers.run(task): 请传入 task 或构造时指定 task")

        trace = OWTrace(task=run_task, start_ts=time.time())
        _start = time.time()

        # 累积上下文：每轮 Worker 结果自动追加（Q13=A）
        context_parts: list[str] = []

        iteration = 0
        final_answer = ""

        while iteration < self.max_iterations:
            iteration += 1

            # -- 构造 Orchestrator 输入 --
            iteration_history = self._build_iteration_history(context_parts)
            if iteration == 1:
                # Q17=A：第一轮特殊填充
                latest_results = f"(用户原始任务: {run_task})"
            else:
                latest_results = self._build_latest_results(context_parts)

            orchestrator_prompt = self._final_orchestrator_prompt.format(
                iteration_history=iteration_history,
                latest_results=latest_results,
                max_concurrent_workers=self.max_concurrent_workers,
            )

            # -- 检查是否需要强制汇总 --
            forced_synthesize = (iteration >= self.max_iterations)

            if forced_synthesize:
                # Q14=A：Python 侧强制汇总，不信任 LLM 会遵守约束
                self._renderer.print(f"[轮次 {iteration}] 达到 max_iterations={self.max_iterations}，强制汇总")
                # 构造 synthesize 请求，prompt 追加"已达上限必须汇总"
                synth_prompt = orchestrator_prompt + (
                    "\n\n重要：已达到最大轮次限制，必须基于已有结果汇总最终答案。"
                    "请输出 synthesize 动作。"
                )
                orch_agent = _make_agent(
                    role_name=f"orchestrator_iter{iteration}",
                    system_prompt=synth_prompt,
                    llm=self.llm,
                    tools=Toolkit(),  # Q10=A 对齐 PWC：Orchestrator 不注入业务工具
                    max_steps=2,
                    shared_renderer=self._renderer,
                    shared_terminator=self.terminator,
                    shared_plugin_manager=self.plugin_manager,
                    shared_circuit_breaker_registry=self.circuit_breaker_registry,
                    compact_threshold=4000,
                )
                orch_output = orch_agent.run("请基于已有结果汇总最终答案。")
                # 强制按 synthesize 解析（即使 LLM 输出 dispatch 也忽略）
                parsed = _parse_orchestrator_json(orch_output)
                if parsed["action"] != "synthesize":
                    # LLM 仍输出 dispatch，用其 answer 字段或原始输出作为最终答案
                    final_answer = parsed.get("answer") or orch_output
                else:
                    final_answer = parsed.get("answer", orch_output)

                trace.iterations.append(IterationLog(
                    iteration=iteration,
                    action="synthesize",
                    reasoning=parsed.get("reasoning", ""),
                    forced=True,
                ))
                break

            # -- 正常轮次：调 Orchestrator LLM 做决策 --
            self._renderer.print(f"[轮次 {iteration}] Orchestrator 决策中...")
            orch_agent = _make_agent(
                role_name=f"orchestrator_iter{iteration}",
                system_prompt=orchestrator_prompt,
                llm=self.llm,
                tools=Toolkit(),
                max_steps=2,
                shared_renderer=self._renderer,
                shared_terminator=self.terminator,
                shared_plugin_manager=self.plugin_manager,
                shared_circuit_breaker_registry=self.circuit_breaker_registry,
                compact_threshold=4000,
            )
            orch_output = orch_agent.run("请决策下一步（dispatch 或 synthesize）。")
            parsed = _parse_orchestrator_json(orch_output)

            if parsed["action"] == "synthesize":
                # -- 汇总分支 --
                final_answer = parsed.get("answer", orch_output)
                trace.iterations.append(IterationLog(
                    iteration=iteration,
                    action="synthesize",
                    reasoning=parsed.get("reasoning", ""),
                ))
                self._renderer.print(f"[轮次 {iteration}] Orchestrator 决策汇总")
                break

            # -- dispatch 分支 --
            tasks = parsed["tasks"]
            # Q7=A：max_concurrent_workers 截断（prompt 约束 + Python 侧兜底）
            tasks = tasks[: self.max_concurrent_workers]

            self._renderer.print(
                f"[轮次 {iteration}] Orchestrator 分派 {len(tasks)} 个子任务: {tasks}"
            )

            # 构造 Worker prompt（所有 Worker 共享同一份上下文）
            context_block = "\n".join(context_parts) if context_parts else ""
            worker_prompt = self._final_worker_prompt.format(
                subtask_description="{subtask}",  # 占位，每个 Worker 替换
                context_block=context_block or "(无前置上下文)",
            )

            # -- ThreadPoolExecutor 并行执行 Worker（Q6=A） --
            worker_results = self._run_workers_parallel(
                tasks=tasks,
                iteration=iteration,
                worker_prompt_template=worker_prompt,
            )

            # 记录 IterationLog
            trace.iterations.append(IterationLog(
                iteration=iteration,
                action="dispatch",
                dispatched_tasks=tasks,
                worker_results=worker_results,
                reasoning=parsed.get("reasoning", ""),
            ))

            # 累积上下文（Q13=A）
            for wr in worker_results:
                status_tag = "" if wr.error is None else f"（执行失败: {wr.error}）"
                context_parts.append(
                    f"[第{iteration}轮] 任务: {wr.task} 结果{status_tag}: {wr.artifact}"
                )

        else:
            # while 循环正常结束但没 break（理论上不会走到这里，因为 forced_synthesize 会 break）
            # 安全兜底：用已有上下文拼接
            final_answer = "Orchestrator 未在最大轮次内汇总。" + " | ".join(context_parts)

        # 收尾
        trace.end_ts = time.time()
        trace.total_duration_ms = int((trace.end_ts - _start) * 1000)

        self._renderer.markdown(final_answer[:500], title="最终答案（摘要）")
        return OWResult(final_answer=final_answer, trace=trace)

    # -----------------------------------------------
    # 内部方法
    # -----------------------------------------------
    def _run_workers_parallel(
        self,
        tasks: list[str],
        iteration: int,
        worker_prompt_template: str,
    ) -> list[WorkerResult]:
        """ThreadPoolExecutor 并行执行一批 Worker（Q6=A）。

        partial success：单个 Worker 失败不影响其他（Q16=A）。
        结果按 tasks 顺序返回（不是完成顺序）。
        """
        results: list[WorkerResult | None] = [None] * len(tasks)

        def _run_single(idx: int, task: str) -> WorkerResult:
            t0 = time.time()
            try:
                # 替换占位符
                prompt = worker_prompt_template.replace("{subtask}", task)
                worker_agent = _make_agent(
                    role_name=f"worker_iter{iteration}_{idx}",
                    system_prompt=prompt,
                    llm=self.llm,
                    tools=self.worker_tools,  # Q10=A：业务工具只给 Worker
                    max_steps=self.max_steps_per_worker,
                    shared_renderer=self._renderer,
                    shared_terminator=self.terminator,
                    shared_plugin_manager=self.plugin_manager,
                    shared_circuit_breaker_registry=self.circuit_breaker_registry,
                )
                artifact = worker_agent.run("请开始执行上面的子任务。")
                duration_ms = int((time.time() - t0) * 1000)
                return WorkerResult(
                    iteration=iteration,
                    task=task,
                    artifact=artifact,
                    duration_ms=duration_ms,
                    sub_agent_trace_snapshot=_extract_nested_trace(worker_agent),
                )
            except Exception as e:
                # Q16=A partial success：失败 Worker 记 error，不影响其他
                duration_ms = int((time.time() - t0) * 1000)
                return WorkerResult(
                    iteration=iteration,
                    task=task,
                    artifact=f"(执行失败: {e})",
                    duration_ms=duration_ms,
                    error=str(e),
                )

        # 和 parallel_gather 一致：max_workers = min(len, 16)
        max_workers = min(len(tasks), 16)
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_run_single, idx, task): idx
                for idx, task in enumerate(tasks)
            }
            for fut in as_completed(futures):
                idx = futures[fut]
                results[idx] = fut.result()

        # results 按 tasks 顺序填充（None 不可能，_run_single 保证返回）
        return [r for r in results if r is not None]

    def _build_iteration_history(self, context_parts: list[str]) -> str:
        """构造 Orchestrator prompt 的 iteration_history 字段。"""
        if not context_parts:
            return "(无，这是第一轮)"
        return "\n".join(context_parts)

    def _build_latest_results(self, context_parts: list[str]) -> str:
        """构造 Orchestrator prompt 的 latest_results 字段（最近一轮的结果）。"""
        if not context_parts:
            return "(无)"
        # 取最后几条（本轮之前的那批结果）
        return "\n".join(context_parts[-self.max_concurrent_workers :])


# =============================================================
# 私有辅助：JSON 解析 + trace 摘出
# =============================================================
def _parse_orchestrator_json(output: str) -> dict:
    """Orchestrator LLM 输出 -> 结构化决策 dict。

    Q11=A 容错策略：
    1. json.loads 解析，失败则从文本中提取 {...} 片段（兼容 Markdown 代码块包裹）
    2. action 缺失或不合法 -> ValueError（消除 fallback）
    3. dispatch 时 tasks 为空 list 或缺失 -> ValueError
    4. synthesize 时 answer 缺失 -> ValueError
    5. reasoning 字段可选
    """
    text = output.strip()

    # 尝试直接 json.loads
    parsed = None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        # 容错：从文本中提取 {...} 片段（兼容 ```json ... ``` 包裹）
        match = re.search(r"\{[^{}]*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group())
            except json.JSONDecodeError:
                pass

    if not isinstance(parsed, dict):
        raise ValueError(
            f"Orchestrator 输出不是合法 JSON: {text[:200]}"
        )

    action = parsed.get("action")
    if action not in ("dispatch", "synthesize"):
        raise ValueError(
            f"Orchestrator JSON action 字段必须为 'dispatch' 或 'synthesize'，实际: {action}"
        )

    if action == "dispatch":
        tasks = parsed.get("tasks")
        if not tasks or not isinstance(tasks, list) or len(tasks) == 0:
            raise ValueError(
            "Orchestrator dispatch 动作必须包含非空 tasks 列表"
            )
        parsed["tasks"] = [str(t) for t in tasks]
    else:
        # synthesize
        answer = parsed.get("answer")
        if not answer or not isinstance(answer, str):
            raise ValueError(
            "Orchestrator synthesize 动作必须包含 answer 字段"
            )

    # reasoning 可选
    parsed.setdefault("reasoning", "")
    return parsed


def _extract_nested_trace(agent: Agent) -> dict:
    """从 Agent.trace（嵌套 Trace 树）摘出 demo 断言需要的最小扁平字典。

    和 PWC 的 _extract_nested_trace 完全一致（copy 自 planner_worker_critic.py）。
    """
    t: AgentTrace = agent.trace
    steps = getattr(t, "steps", [])
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

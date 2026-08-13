"""Agent 内核：基于 Function Calling 的 ReAct 循环。

每一步：
    1. LLM 读取当前 memory 与可用工具进行推理
    2. 若返回 tool_calls，执行对应工具并把结果回填到 memory，进入下一步
    3. 若无 tool_calls，则视为 LLM 已给出最终答案
"""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from ..context.compact import CompactManager
from ..context.trace import StepRecord, Trace
from ..context.transcript import Transcript
from ..journal.base import JournalEvent, JournalProtocol
from ..llm.base import LLM
from ..memory.base import Memory
from ..render import PlainRenderer, Renderer
from ..tools.base import Tool, Toolkit
from ..utils.idempotency import IDEMPOTENCY_USER_KEY, derive as derive_idempotency_key
from .circuit_breaker import CircuitBreakerRegistry
from .plugin import PluginManager
from .retry import full_jitter_delay_ms, sleep_ms
from .terminator import CompositeTerminator, Terminator, make_default_terminator

if TYPE_CHECKING:
    from .multi_agent import RoleConfig


class Agent:
    """通用 Agent。"""

    def __init__(
        self,
        llm: LLM,
        tools: Toolkit,
        system_prompt: str | None = None,
        max_steps: int = 10,
        renderer: Renderer | None = None,
        # 终止条件组合器（None 时用 make_default_terminator(max_steps)，行为与旧版一致）
        terminator: Terminator | None = None,
        # 插件管理器（None 时用空 PluginManager，emit 无副作用）
        plugin_manager: PluginManager | None = None,
        # Journal（None 时不记录事件，不报错）
        journal: JournalProtocol | None = None,
        # 以下为 Step 2.2 并行子 Agent 新增参数（末尾 + 默认值，不破坏现有调用）
        role_configs: dict[str, "RoleConfig"] | None = None,
        parent_trace: "Trace | None" = None,
        auto_inject_spawn_tool: bool = True,
        tool_specs_pool: list[dict] | None = None,
        # Phase 1 compact：token 超阈值自动 compact（None 禁用）
        compact_threshold: int | None = 8000,
        transcript: Transcript | None = None,
        # Phase 2.2：熔断器注册中心（None 时自动创建空 Registry；auto_default_registry=False 时完全禁用熔断器）
        circuit_breaker_registry: CircuitBreakerRegistry | None = None,
        auto_default_registry: bool = True,
        # Phase 4.1：RSI 自进化基础设施（None 时 inspect_self 返回空 list，零侵入）
        skill_library: Any | None = None,
        failure_memory: Any | None = None,
        # Phase 4.2a：提案归档存储（None 时 self_evolve 跳过归档，零侵入）
        archive_store: Any | None = None,
        # Phase 4.2b：Prompt 进化基础设施（None 时 evolve_prompt 报错，零侵入）
        fitness_evaluator: Any | None = None,
        leakage_guard: Any | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        # 终止条件：用户未传则用默认（MaxSteps | Timeout），与旧版行为兼容
        self.terminator: Terminator = terminator or make_default_terminator(max_steps)
        # 插件管理器：None 时用空实例（emit 遍历空列表，无副作用）
        self.plugin_manager: PluginManager = plugin_manager or PluginManager()
        # 熔断器注册中心：注入后状态变更会自动 emit plugin 事件（通过 set_plugin_manager 懒绑定）
        self.circuit_breaker_registry: CircuitBreakerRegistry | None = (
            circuit_breaker_registry
            or (CircuitBreakerRegistry() if auto_default_registry else None)
        )
        if self.circuit_breaker_registry is not None:
            # 懒绑定：熔断器状态变更时通过 PluginManager 分发事件
            self.circuit_breaker_registry.set_plugin_manager(self.plugin_manager)
        # Journal：None 时不记录事件
        self.journal: JournalProtocol | None = journal
        # 渲染器：默认 PlainRenderer（纯文本），注入 RichRenderer 后支持 Markdown
        self.renderer: Renderer = renderer or PlainRenderer()
        # Phase 1 compact：Memory 内部分层 + project() 投影
        self.compact_threshold = compact_threshold
        self.memory = Memory(system_prompt=system_prompt, compact_threshold=compact_threshold)
        # CompactManager：token 超阈值时调 LLM 生成 Summary（None 禁用）
        self.compact_manager: CompactManager | None = (
            CompactManager(threshold=compact_threshold, llm=llm)
            if compact_threshold else None
        )
        # Transcript：append-only 全量事件存档（与 Memory 双写）
        self.transcript: Transcript = transcript or Transcript()
        self.trace = Trace()
        # Step 2.2 并行子 Agent 字段
        self.role_configs: dict[str, RoleConfig] = role_configs or {}
        self.tool_specs_pool: list[dict] = tool_specs_pool or []
        self.auto_inject_spawn_tool = auto_inject_spawn_tool
        # 告诉 Trace 自己是不是子 Agent（Q17：嵌套守卫）
        self.trace._parent_trace = parent_trace
        # 有初始 role 时构造即注入 spawn_subagents 工具（子 Agent 会在内部快速返回）
        if self.role_configs and self.auto_inject_spawn_tool:
            self._inject_spawn_tool()

        # Phase 4.1：RSI 自进化基础设施引用
        self.skill_library = skill_library
        self.failure_memory = failure_memory
        # Phase 4.2a：提案归档存储引用
        self.archive_store = archive_store
        # Phase 4.2b：Prompt 进化基础设施引用 + generation 计数器
        self.fitness_evaluator = fitness_evaluator
        self.leakage_guard = leakage_guard
        self.generation: int = 0
        # 遍历已注册插件，对有 bind_agent 方法的自动注入 Agent 弱引用
        for plugin in self.plugin_manager.plugins:
            if hasattr(plugin, "bind_agent"):
                plugin.bind_agent(self)

    # ------------------------------------------------------------------
    # spawn_subagents 工具注入（Step 2.2 一次性写完，含优化）
    # ------------------------------------------------------------------

    def _inject_spawn_tool(self) -> None:
        """把 spawn_subagents 工具注册进 Toolkit（幂等 + 子 Agent 快速返回）。"""
        # 优化：子 Agent（有父 Trace）根本不注入 spawn 工具 → LLM 看不到该选项
        # → 从源头消除 Q17 嵌套误调用，func 内部的 RuntimeError 是第二道防线
        if self.trace._parent_trace is not None:
            return
        # 防重复注入
        if "spawn_subagents" in self.tools._tools:
            return
        # 方法内部延迟导入：避免 agent ↔ multi_agent 的模块级循环引用
        from .multi_agent import make_spawn_subagents_tool

        name, desc, params_factory, func = make_spawn_subagents_tool(self)
        tool = self.tools.register(
            func=func,
            name=name,
            description=desc,
            parameters=params_factory(),   # 首次注册用静态占位
            retry_max=0,  # spawn_subagents 非幂等（创建子 Agent 有副作用），禁止自动重试
        )
        # Q19 懒加载：后续每轮 to_openai_tools 调用重新读取 role_configs enum
        tool.parameters_factory = params_factory

    def register_role(self, config: "RoleConfig") -> None:
        """动态注册一个角色。第一个 role 注册时触发 spawn_subagents 工具注入。"""
        was_empty = not self.role_configs
        self.role_configs[config.name] = config
        if was_empty and self.role_configs and self.auto_inject_spawn_tool:
            self._inject_spawn_tool()

    # ------------------------------------------------------------------
    # Phase 4.1：RSI 自省
    # ------------------------------------------------------------------

    def inspect_self(self) -> Any:
        """读取自身状态做自进化决策，返回 SelfSnapshot。

        委托给 atlas.rsi.build_self_snapshot()，Agent 只做委托。
        skill_library/failure_memory 为 None 时对应字段返回空 list（零侵入）。
        """
        from ..rsi.inspector import build_self_snapshot
        return build_self_snapshot(self)

    # ------------------------------------------------------------------
    # Phase 4.2a：RSI 自进化闭环
    # ------------------------------------------------------------------

    def propose_diff(self) -> Any:
        """调 LLM 生成自进化提案，返回 ProposedDiff。

        委托给 atlas.rsi.propose_diff()，Agent 只做委托。
        Q6=A：和 inspect_self 一致的委托模式。
        """
        from ..rsi.evolver import propose_diff
        return propose_diff(self)

    def self_evolve(self) -> Any:
        """自进化闭环：propose → evaluate → apply/archive，返回 EvaluationResult。

        委托给 atlas.rsi.self_evolve()，archive_store 从 self.archive_store 读取。
        Q10=A：一行调用完成自进化。
        """
        from ..rsi.evolver import self_evolve
        return self_evolve(self)

    # ------------------------------------------------------------------
    # Phase 4.2b：Prompt 进化
    # ------------------------------------------------------------------

    def evolve_prompt(self) -> Any:
        """Prompt 进化闭环：propose → fitness → leakage → apply/archive，返回 EvolutionResult。

        委托给 atlas.rsi.evolve_prompt()，fitness_evaluator/leakage_guard/archive_store 从 self 读取。
        Q7=B：专门触发 prompt 进化，和 self_evolve（综合进化）并列。
        """
        from ..rsi.prompt_evolver import evolve_prompt
        return evolve_prompt(self)

    def _journal_append(self, run_id: str, event_type: str, payload: dict) -> None:
        """便捷方法：向 Journal 追加事件（若 journal 未注入则跳过）。"""
        if self.journal:
            self.journal.append(self.journal.make_event(run_id, event_type, payload))

    def _emit_and_journal(self, run_id: str, event: str, payload: dict, **emit_kwargs: Any) -> None:
        """便捷方法：同时 emit 插件事件 + 追加 Journal。"""
        self.plugin_manager.emit(event, run_id=run_id, **emit_kwargs)
        self._journal_append(run_id, event, payload | {"run_id": run_id} | emit_kwargs)

    def run(self, user_input: str) -> str:
        """运行一次任务，返回最终回答。"""
        # 生成 run_id，贯穿本次执行的所有 emit 和 Journal 事件
        run_id = str(uuid.uuid4())
        self.trace.meta["run_id"] = run_id

        # Phase 2.1 Saga：记录已成功执行的非幂等有副作用 step，LIFO 补偿
        _saga_stack: list[tuple[str, str, Any, dict]] = []  # (step_id, tool_name, compensate_func, compensate_input)
        _compensate_failed: list[tuple[str, str, str]] = []  # (step_id, tool_name, error_str)

        self.memory.add_user(user_input)
        self.transcript.append({"role": "user", "content": user_input})
        openai_tools = self.tools.to_openai_tools() or None

        # 插件 + Journal：agent_start
        sys_msg = next((m["content"] for m in self.memory.messages if m.get("role") == "system"), "")
        self._emit_and_journal(
            run_id, "agent_start",
            payload={"task": user_input},
            task=user_input,
            context={"system_prompt": sys_msg},
        )

        # 终止条件上下文
        term_ctx = SimpleNamespace(
            step_count=0,
            start_time=time.time() * 1000,
            total_tokens=0,
            cost_usd=0.0,
            recent_outputs=[],
        )

        while True:
            term_ctx.step_count += 1
            step_no = term_ctx.step_count

            # 插件：turn_start
            self._emit_and_journal(
                run_id, "turn_start",
                payload={"turn_idx": step_no},
                turn_idx=step_no,
                context={},
            )

            # 终止条件检查（turn_start 之后，llm_before_call 之前）
            should_stop, reasons = self.terminator.should_stop(term_ctx)
            if should_stop:
                final = f"Agent 终止：{'; '.join(reasons)}"
                # Phase 2.1 Saga：无最终答案时 LIFO 补偿所有非幂等有副作用工具
                last_result = None
                if _saga_stack:
                    self.plugin_manager.emit(
                        "compensate_start",
                        run_id=run_id,
                        failed_step_id=step_id if 'step_id' in dir() else f"{run_id}:{step_no}:0",
                        compensate_count=len(_saga_stack),
                    )
                    self._journal_append(
                        run_id, "compensate_start",
                        {"compensate_count": len(_saga_stack), "failed_reason": "; ".join(reasons)},
                    )
                    while _saga_stack:
                        sid, tname, comp_func, ci = _saga_stack.pop()
                        try:
                            comp_func(ci, last_result, {"run_id": run_id, "step_id": sid, "tool_name": tname})
                            # Step 2 钩子补齐：Phase 2.1 Saga 已写 Journal，现在补 plugin emit
                            self.plugin_manager.emit(
                                "compensate_finish",
                                run_id=run_id,
                                step_id=sid,
                                tool_name=tname,
                                status="success",
                            )
                            self._journal_append(
                                run_id, "compensate_finish",
                                {"step_id": sid, "tool_name": tname, "status": "success"},
                            )
                        except Exception as e:
                            err = str(e)
                            _compensate_failed.append((sid, tname, err))
                            self.plugin_manager.emit(
                                "compensate_failed",
                                run_id=run_id,
                                step_id=sid,
                                tool_name=tname,
                                error=err,
                                retry_count=1,
                                next_retry_at=None,
                            )
                            self._journal_append(
                                run_id, "compensate_failed",
                                {"step_id": sid, "tool_name": tname, "error": err, "retry_count": 1},
                            )
                    if _compensate_failed:
                        failed_list = ", ".join([f"{t}(sid={s}:{e})" for s, t, e in _compensate_failed])
                        final += f"\n补偿失败列表: [{failed_list}] 请人工介入"
                self._emit_and_journal(
                    run_id, "turn_finish",
                    payload={"turn_idx": step_no, "decisions": []},
                    turn_idx=step_no,
                    decisions=[],
                    context={},
                )
                if self.trace._parent_trace is None:
                    self.trace.render(self.renderer, depth=0)
                self._emit_and_journal(
                    run_id, "agent_finish",
                    payload={"final_result": final, "status": "terminated", "compensate_failed": _compensate_failed},
                    final_result=final,
                    status="terminated",
                    context={"reasons": reasons, "compensate_failed": _compensate_failed},
                )
                self.renderer.markdown(final, title="终止信息")
                return final

            # 插件：llm_before_call
            self._emit_and_journal(
                run_id, "llm_before_call",
                payload={"turn_idx": step_no, "model": getattr(self.llm, "model", "")},
                turn_idx=step_no,
                model=getattr(self.llm, "model", ""),
                messages=self.memory.messages,
                kwargs={"tools": openai_tools} if openai_tools else {},
            )

            llm_result = self.llm.chat(self.memory.messages, tools=openai_tools)
            assistant = llm_result["message"]
            usage = llm_result.get("usage")
            llm_duration_ms = llm_result.get("duration_ms", 0)
            self.memory.add_assistant(assistant)
            self.transcript.append(assistant)

            # Phase 1 compact：token 超阈值时调 LLM 生成 Summary + Boundary
            if usage and self.compact_manager:
                self.memory.update_token_count(usage.get("prompt_tokens", 0))
                if self.compact_manager.should_compact(self.memory.get_token_count()):
                    summary = self.compact_manager.compact(
                        self.memory.runtime_messages, self.trace
                    )
                    self.memory.add_summary(summary)
                    self.memory.reset_token_count()
                    self.transcript.append({"type": "compact", "summary": summary})

            # 累计 token 到终止条件上下文
            if usage:
                term_ctx.total_tokens += usage.get("total_tokens", 0)

            # 更新 recent_outputs（供 RepetitionDetector 用）
            content = assistant.get("content") or ""
            term_ctx.recent_outputs.append(content)
            if len(term_ctx.recent_outputs) > 10:
                term_ctx.recent_outputs.pop(0)

            # 插件：llm_after_call
            self._emit_and_journal(
                run_id, "llm_after_call",
                payload={
                    "turn_idx": step_no,
                    "model": getattr(self.llm, "model", ""),
                    "response": assistant,
                    "usage": usage,
                    "duration_ms": llm_duration_ms,
                },
                turn_idx=step_no,
                model=getattr(self.llm, "model", ""),
                response=assistant,
                usage=usage,
                duration_ms=llm_duration_ms,
            )

            # 从 TokenCostPlugin 累计 cost_usd（若注册了该插件）
            for p in self.plugin_manager.plugins:
                if hasattr(p, "per_run") and run_id in getattr(p, "per_run", {}):
                    term_ctx.cost_usd = p.per_run[run_id].get("cost_usd", 0.0)

            tool_calls = assistant.get("tool_calls") or []
            record = StepRecord(
                step=step_no,
                thought=content,
                tool_calls=tool_calls,
            )

            # 没有工具调用，说明 LLM 已给出最终答案
            if not tool_calls:
                self.trace.add(record)
                final = content
                # 插件：turn_finish + agent_finish
                self._emit_and_journal(
                    run_id, "turn_finish",
                    payload={"turn_idx": step_no, "decisions": []},
                    turn_idx=step_no,
                    decisions=[],
                    context={},
                )
                if self.trace._parent_trace is None:
                    self.trace.render(self.renderer, depth=0)
                self._emit_and_journal(
                    run_id, "agent_finish",
                    payload={"final_result": final, "status": "success"},
                    final_result=final,
                    status="success",
                    context={},
                )
                self.renderer.markdown(final, title="最终答案")
                return final

            # 执行所有工具调用，结果回填给 LLM 作为下一步的观察
            # Phase 2.1：执行前查幂等 cache 跳过，非幂等有副作用成功后入 saga_stack
            for tool_idx, call in enumerate(tool_calls):
                fn = call["function"]
                tool_name = fn["name"]
                # Step 1: 解析工具参数（保留原始解析逻辑）
                if isinstance(fn["arguments"], str):
                    try:
                        tool_input = json.loads(fn["arguments"])
                    except json.JSONDecodeError:
                        tool_input = {}
                else:
                    tool_input = fn["arguments"] or {}
                step_id = f"{run_id}:{step_no}:{tool_idx}"
                # 取 Tool 对象：三标志 + compensate 从这取
                tool = self.tools.get(tool_name)

                # Step 2: 幂等键：先 pop 用户显式魔法字段（不要传给真正工具执行）
                explicit_key = tool_input.pop(IDEMPOTENCY_USER_KEY, None)
                key = explicit_key or derive_idempotency_key(run_id, tool_name, tool_input)

                # 插件：tool_before_call（幂等键去掉魔法字段之后的 input，符合用户预期）
                self._emit_and_journal(
                    run_id, "tool_before_call",
                    payload={
                        "turn_idx": step_no,
                        "step_id": step_id,
                        "tool_name": tool_name,
                        "input": tool_input,
                        "idempotency_key": key,
                    },
                    turn_idx=step_no,
                    step_id=step_id,
                    tool_name=tool_name,
                    input=tool_input,
                )

                # Step 3: 查幂等 cache，命中 → 跳过执行，不进 saga_stack
                tool_status = "success"
                tool_duration_ms = 0
                cached_result = self.journal.cache_get(key) if self.journal else None
                if cached_result is not None:
                    result = cached_result
                    tool_status = "idempotency_hit"
                else:
                    # -------- Phase 2.2 NEW: 熔断器前置检查（幂等 miss 才需要真执行） --------
                    circuit_key = tool.circuit_key or tool_name
                    circuit_registry = self.circuit_breaker_registry
                    allowed, _circuit_state_after, circuit_reason_msg = True, None, None
                    if circuit_registry is not None:
                        allowed, _circuit_state_after, circuit_reason_msg = circuit_registry.allow_request(
                            circuit_key, run_id=run_id
                        )

                    if not allowed:
                        # 熔断器拒绝 → 直接构造失败，不执行、不 cache、不入 saga、不重试
                        result = f"工具执行被熔断器拒绝: {circuit_reason_msg or 'circuit open'}"
                        tool_status = "circuit_rejected"
                        tool_duration_ms = 0
                    else:
                        # -------- Phase 2.2 NEW: 熔断器放行 → 用重试循环包裹真正执行 --------
                        tool_start = time.time()
                        result = None
                        last_error: Exception | None = None
                        actual_retry_max = tool.retry_max if tool.isIdempotent else 0
                        total_attempts = actual_retry_max + 1

                        for attempt_idx in range(total_attempts):
                            try:
                                # Step 4 真正执行（保持原 cleaned_args 逻辑）
                                cleaned_args = (
                                    tool_input
                                    if not isinstance(fn["arguments"], str)
                                    else json.dumps(tool_input, ensure_ascii=False)
                                )
                                result = self.tools.execute(tool_name, cleaned_args)
                                tool_status = "success"
                                last_error = None
                                # 熔断器 on_success（每次执行成功都计，包括重试成功）
                                if circuit_registry is not None:
                                    circuit_registry.on_success(circuit_key, run_id=run_id)
                                break  # 成功跳出重试循环
                            except Exception as e:
                                last_error = e
                                # 熔断器 on_failure（每次失败都计，包括重试的每一次）
                                if circuit_registry is not None:
                                    circuit_registry.on_failure(circuit_key, run_id=run_id)

                                # 判断能否继续重试：幂等 + 异常在 retry_on 白名单 + 不是最后一轮
                                is_retryable = (
                                    tool.isIdempotent
                                    and any(isinstance(e, et) for et in tool.retry_on)
                                    and attempt_idx < actual_retry_max
                                )
                                if not is_retryable:
                                    result = f"工具执行失败: {e}"
                                    tool_status = "failed"
                                    # 重试耗尽？（幂等且 actual_retry_max>0 且跑到了最后一轮）
                                    if (
                                        tool.isIdempotent
                                        and actual_retry_max > 0
                                        and attempt_idx >= actual_retry_max
                                    ):
                                        self._emit_and_journal(
                                            run_id, "retry_exhausted",
                                            payload={
                                                "turn_idx": step_no,
                                                "step_id": step_id,
                                                "tool_name": tool_name,
                                                "total_attempts": total_attempts,
                                                "last_error": str(e),
                                            },
                                            turn_idx=step_no,
                                            step_id=step_id,
                                            tool_name=tool_name,
                                            total_attempts=total_attempts,
                                            last_error=str(e),
                                        )
                                    break  # 不能重试 → 跳出循环

                                # 可以重试 → retry_before → sleep → retry_after
                                attempt_no = attempt_idx + 1  # 给用户看的"第几次重试"，从1开始
                                delay_ms = full_jitter_delay_ms(attempt_no)
                                self._emit_and_journal(
                                    run_id, "retry_before",
                                    payload={
                                        "turn_idx": step_no,
                                        "step_id": step_id,
                                        "tool_name": tool_name,
                                        "attempt": attempt_no,
                                        "next_delay_ms": delay_ms,
                                        "error_type": type(e).__name__,
                                        "error": str(e),
                                    },
                                    turn_idx=step_no,
                                    step_id=step_id,
                                    tool_name=tool_name,
                                    attempt=attempt_no,
                                    next_delay_ms=delay_ms,
                                    error_type=type(e).__name__,
                                )
                                # 写入熔断器 registry 的 retry_logs（Dashboard 展示用）
                                if circuit_registry is not None:
                                    circuit_registry.append_retry_log({
                                        "ts": time.time(),
                                        "run_id": run_id,
                                        "step_id": step_id,
                                        "tool_name": tool_name,
                                        "attempt": attempt_no,
                                        "next_delay_ms": delay_ms,
                                        "error_type": type(e).__name__,
                                    })
                                sleep_ms(delay_ms)
                                self._emit_and_journal(
                                    run_id, "retry_after",
                                    payload={
                                        "turn_idx": step_no,
                                        "step_id": step_id,
                                        "tool_name": tool_name,
                                        "attempt": attempt_no,
                                        "status": "failed",
                                    },
                                    turn_idx=step_no,
                                    step_id=step_id,
                                    tool_name=tool_name,
                                    attempt=attempt_no,
                                    status="failed",
                                )
                                # 进入下一轮 for 循环 = 下一次重试

                        tool_duration_ms = int((time.time() - tool_start) * 1000)

                        # 执行成功才写幂等 cache（失败/重试耗尽/熔断器拒绝都不写）
                        if self.journal and tool_status == "success":
                            self.journal.cache_set(key, result)

                # Step 5: 进 saga_stack：非幂等有副作用 且 有 compensate 且 真正执行成功时
                if (
                    tool.hasSideEffects
                    and not tool.isIdempotent
                    and tool.compensate is not None
                    and tool_status == "success"
                ):
                    _saga_stack.append((step_id, tool_name, tool.compensate, dict(tool_input)))

                self.memory.add_tool(result, call["id"])
                self.transcript.append({"role": "tool", "content": result, "tool_call_id": call["id"]})
                record.observations.append(result)

                # 插件：tool_after_call（status 三态：success / failed / idempotency_hit）
                self._emit_and_journal(
                    run_id, "tool_after_call",
                    payload={
                        "turn_idx": step_no,
                        "step_id": step_id,
                        "tool_name": tool_name,
                        "input": tool_input,
                        "result": {"output": result},
                        "duration_ms": tool_duration_ms,
                        "status": tool_status,
                        "idempotency_key": key,
                    },
                    turn_idx=step_no,
                    step_id=step_id,
                    tool_name=tool_name,
                    input=tool_input,
                    result={"output": result},
                    duration_ms=tool_duration_ms,
                    status=tool_status,
                )

            self.trace.add(record)

            # 插件：turn_finish
            self._emit_and_journal(
                run_id, "turn_finish",
                payload={"turn_idx": step_no, "decisions": tool_calls},
                turn_idx=step_no,
                decisions=tool_calls,
                context={},
            )

    def reset(self) -> None:
        """重置会话记忆与轨迹，开始新任务。"""
        self.memory.clear()
        self.memory.reset_token_count()
        self.trace.reset()
        self.transcript = Transcript()

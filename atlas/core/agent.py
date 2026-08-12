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

from ..context.trace import StepRecord, Trace
from ..journal.base import JournalEvent, JournalProtocol
from ..llm.base import LLM
from ..memory.base import Memory
from ..render import PlainRenderer, Renderer
from ..tools.base import Tool, Toolkit
from ..utils.idempotency import IDEMPOTENCY_USER_KEY, derive as derive_idempotency_key
from .plugin import PluginManager
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
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        # 终止条件：用户未传则用默认（MaxSteps | Timeout），与旧版行为兼容
        self.terminator: Terminator = terminator or make_default_terminator(max_steps)
        # 插件管理器：None 时用空实例（emit 遍历空列表，无副作用）
        self.plugin_manager: PluginManager = plugin_manager or PluginManager()
        # Journal：None 时不记录事件
        self.journal: JournalProtocol | None = journal
        # 渲染器：默认 PlainRenderer（纯文本），注入 RichRenderer 后支持 Markdown
        self.renderer: Renderer = renderer or PlainRenderer()
        self.memory = Memory(system_prompt=system_prompt)
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
        )
        # Q19 懒加载：后续每轮 to_openai_tools 调用重新读取 role_configs enum
        tool.parameters_factory = params_factory

    def register_role(self, config: "RoleConfig") -> None:
        """动态注册一个角色。第一个 role 注册时触发 spawn_subagents 工具注入。"""
        was_empty = not self.role_configs
        self.role_configs[config.name] = config
        if was_empty and self.role_configs and self.auto_inject_spawn_tool:
            self._inject_spawn_tool()

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
                    # Step 4: 真正执行（原有 try/except 逻辑不变）
                    tool_start = time.time()
                    try:
                        # 注意：execute 内部重新 json.loads(arguments)，所以传原始 fn["arguments"]
                        # 但我们已经 pop 掉了 _idempotency_key → 重新序列化一份去掉魔法字段的 arguments
                        cleaned_args = tool_input if not isinstance(fn["arguments"], str) else json.dumps(tool_input, ensure_ascii=False)
                        result = self.tools.execute(tool_name, cleaned_args)
                        tool_status = "success"
                    except Exception as e:
                        result = f"工具执行失败: {e}"
                        tool_status = "failed"
                    tool_duration_ms = int((time.time() - tool_start) * 1000)

                    # 执行成功才写幂等 cache（失败不写，下次能重试）
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
        self.trace.reset()

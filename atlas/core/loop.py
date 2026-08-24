"""Durable Agent 循环：LLM / 工具 / Journal checkpoint / resume。"""
from __future__ import annotations

import json
import time
import uuid
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from atlas.context.trace import StepRecord
from atlas.core.permission import risk_from_tool
from atlas.core.result import RunResult
from atlas.core.retry import full_jitter_delay_ms, sleep_ms
from atlas.core.suspend import SuspendToken
from atlas.utils.idempotency import IDEMPOTENCY_USER_KEY, derive as derive_idempotency_key

if TYPE_CHECKING:
    from atlas.core.agent import Agent


class AgentLoop:
    """同步 Durable Agent 循环：LLM / 工具 / Journal checkpoint / resume。

    不直接给用户用；``Agent.run`` / ``resume`` 委托到这里。

    依赖:
        ``LLM.chat``、``Toolkit``、``JournalProtocol``、``PermissionPolicy``、
        ``CircuitBreakerRegistry``、``Terminator``、``PluginManager``。
    """

    def __init__(self, agent: "Agent") -> None:
        """Args:
            agent: 已组装好的 Agent（必须带 runtime.journal）。
        """
        self.agent = agent

    def run(self, user_input: str, run_id: str | None = None) -> RunResult:
        """从用户输入开跑，事件写入 Journal。

        Args:
            user_input: 本轮任务。
            run_id: 省略则新建 UUID。

        Returns:
            ``RunResult``（success / terminated / suspended）。
        """
        agent = self.agent
        run_id = run_id or str(uuid.uuid4())
        agent.trace.meta["run_id"] = run_id
        agent.memory.add_user(user_input)
        agent.transcript.append({"role": "user", "content": user_input})

        sys_msg = next(
            (m["content"] for m in agent.memory.runtime_messages
             if m.get("role") == "system" and not m.get("isMeta")),
            "",
        )
        agent._emit_and_journal(
            run_id, "agent_start",
            payload={"task": user_input},
            task=user_input,
            context={"system_prompt": sys_msg},
        )
        term_ctx = SimpleNamespace(
            step_count=0,
            start_time=time.time() * 1000,
            total_tokens=0,
            cost_usd=0.0,
            recent_outputs=[],
        )
        saga_stack: list[tuple[str, str, Any, dict]] = []
        tracer = agent.runtime.tracer
        with tracer.span("gen_ai.invoke_agent", {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.id": run_id,
        }):
            return self._loop(
                run_id, term_ctx, saga_stack,
                pending_tool=None, resume_input=None,
            )

    def resume(self, run_id: str, user_input: dict | None = None) -> RunResult:
        """从 Journal 快照续跑；已成功/已终止则直接返回当时答案。

        Args:
            run_id: Journal 中的运行 id。
            user_input: 挂起恢复时注入，如 ``{"approved": True}``。

        Returns:
            ``RunResult``。找不到记录则 ValueError。

        依赖:
            ``JournalProtocol.restore``、``Memory.load_runtime_messages``。
        """
        agent = self.agent
        journal = agent.journal
        snap = journal.restore(run_id)
        if snap.status == "not_found":
            raise ValueError(f"找不到 run_id={run_id} 的 Journal 记录")
        if snap.status in ("success", "terminated") and snap.final_result is not None:
            return RunResult(
                final_answer=snap.final_result,
                run_id=run_id,
                status=snap.status,
            )
        agent.trace.meta["run_id"] = run_id
        if snap.messages:
            agent.memory.load_runtime_messages(snap.messages)
        saga_stack = []
        for step_id, tool_name, compensate_input in snap.saga_stack:
            tool = agent.tools.get(tool_name)
            saga_stack.append((step_id, tool_name, tool.compensate, compensate_input))
        term_ctx = SimpleNamespace(
            step_count=snap.step_count,
            start_time=time.time() * 1000,
            total_tokens=snap.total_tokens,
            cost_usd=snap.cost_usd,
            recent_outputs=[],
        )
        agent._journal_append(run_id, "agent_resume", {
            "resumed_from_status": snap.status,
            "user_input": user_input,
        })
        tracer = agent.runtime.tracer
        with tracer.span("gen_ai.invoke_agent", {
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.id": run_id,
        }):
            return self._loop(
                run_id, term_ctx, saga_stack,
                pending_tool=snap.pending_tool,
                resume_input=user_input,
            )

    def _checkpoint(
        self,
        run_id: str,
        term_ctx: SimpleNamespace,
        saga_stack: list,
        pending_tool: dict | None = None,
    ) -> None:
        agent = self.agent
        payload = {
            "messages": list(agent.memory.runtime_messages),
            "saga_stack": [
                (sid, tname, dict(ci)) for sid, tname, _fn, ci in saga_stack
            ],
            "step_count": term_ctx.step_count,
            "total_tokens": term_ctx.total_tokens,
            "cost_usd": term_ctx.cost_usd,
            "pending_tool": pending_tool,
        }
        agent._journal_append(run_id, "state_checkpoint", payload)

    def _loop(
        self,
        run_id: str,
        term_ctx: SimpleNamespace,
        saga_stack: list,
        pending_tool: dict | None,
        resume_input: dict | None,
    ) -> RunResult:
        agent = self.agent
        openai_tools = agent.tools.to_openai_tools() or None
        compensate_failed: list[tuple[str, str, str]] = []
        tracer = agent.runtime.tracer

        if pending_tool is not None:
            approved = True if resume_input is None else bool(resume_input.get("approved", False))
            result = self._finish_pending_tool(
                run_id, pending_tool, approved, saga_stack, term_ctx,
            )
            if result is not None:
                return result
            self._checkpoint(run_id, term_ctx, saga_stack)

        while True:
            term_ctx.step_count += 1
            step_no = term_ctx.step_count

            agent._emit_and_journal(
                run_id, "turn_start",
                payload={"turn_idx": step_no},
                turn_idx=step_no,
                context={},
            )

            should_stop, reasons = agent.terminator.should_stop(term_ctx)
            if should_stop:
                return self._terminate(
                    run_id, step_no, reasons, saga_stack, compensate_failed, term_ctx,
                )

            agent._emit_and_journal(
                run_id, "llm_before_call",
                payload={"turn_idx": step_no, "model": getattr(agent.llm, "model", "")},
                turn_idx=step_no,
                model=getattr(agent.llm, "model", ""),
                messages=agent.memory.messages,
                kwargs={"tools": openai_tools} if openai_tools else {},
            )

            with tracer.span("gen_ai.inference", {
                "gen_ai.operation.name": "inference",
                "gen_ai.request.model": getattr(agent.llm, "model", ""),
            }):
                def on_delta(piece: str, *, _step=step_no) -> None:
                    agent.plugin_manager.emit(
                        "llm_delta",
                        run_id=run_id,
                        turn_idx=_step,
                        delta=piece,
                    )

                llm_result = agent.llm.chat(
                    agent.memory.messages,
                    tools=openai_tools,
                    on_delta=on_delta,
                )
            assistant = llm_result["message"]
            usage = llm_result.get("usage")
            llm_duration_ms = llm_result.get("duration_ms", 0)
            agent.memory.add_assistant(assistant)
            agent.transcript.append(assistant)

            if usage and agent.compact_manager:
                agent.memory.update_token_count(usage.get("prompt_tokens", 0))
                msgs, outcome = agent.compact_manager.apply(
                    agent.memory.runtime_messages,
                    agent.memory.get_token_count(),
                )
                if outcome.snipped:
                    agent.memory.replace_runtime_messages(msgs)
                if outcome.summary:
                    agent.memory.add_summary(outcome.summary)
                    agent.memory.reset_token_count()
                    agent.transcript.append({"type": "compact", "summary": outcome.summary})
                    agent._journal_append(run_id, "compact_summary", {"summary": outcome.summary})
                    registry = agent.runtime.attachment_registry
                    if registry is not None:
                        for att in registry.rebuild_all(agent):
                            agent.memory.add_attachment(att)

            if usage:
                term_ctx.total_tokens += usage.get("total_tokens", 0)

            content = assistant.get("content") or ""
            term_ctx.recent_outputs.append(content)
            if len(term_ctx.recent_outputs) > 10:
                term_ctx.recent_outputs.pop(0)

            agent._emit_and_journal(
                run_id, "llm_after_call",
                payload={
                    "turn_idx": step_no,
                    "model": getattr(agent.llm, "model", ""),
                    "response": assistant,
                    "usage": usage,
                    "duration_ms": llm_duration_ms,
                },
                turn_idx=step_no,
                model=getattr(agent.llm, "model", ""),
                response=assistant,
                usage=usage,
                duration_ms=llm_duration_ms,
            )
            self._checkpoint(run_id, term_ctx, saga_stack)

            for p in agent.plugin_manager.plugins:
                if hasattr(p, "per_run") and run_id in getattr(p, "per_run", {}):
                    term_ctx.cost_usd = p.per_run[run_id].get("cost_usd", 0.0)

            tool_calls = assistant.get("tool_calls") or []
            record = StepRecord(step=step_no, thought=content, tool_calls=tool_calls)

            if not tool_calls:
                agent.trace.add(record)
                return self._finish_success(run_id, step_no, content)

            for tool_idx, call in enumerate(tool_calls):
                fn = call["function"]
                tool_name = fn["name"]
                if isinstance(fn["arguments"], str):
                    try:
                        tool_input = json.loads(fn["arguments"])
                    except json.JSONDecodeError:
                        tool_input = {}
                else:
                    tool_input = fn["arguments"] or {}
                step_id = f"{run_id}:{step_no}:{tool_idx}"
                tool = agent.tools.get(tool_name)
                explicit_key = tool_input.pop(IDEMPOTENCY_USER_KEY, None)
                key = explicit_key or derive_idempotency_key(run_id, tool_name, tool_input)

                policy = agent.runtime.permission
                risk = risk_from_tool(tool)
                if policy.should_confirm(risk):
                    pending = {
                        "tool_name": tool_name,
                        "tool_input": dict(tool_input),
                        "call_id": call["id"],
                        "step_id": step_id,
                        "idempotency_key": key,
                        "step_no": step_no,
                        "fn_arguments": fn["arguments"],
                    }
                    prompt = (
                        f"工具 {tool_name} 风险={risk.name}，需要确认后才能执行。"
                        f" 输入={tool_input}"
                    )
                    token = SuspendToken(run_id=run_id, node_name="tool_confirm", seq=0)
                    agent._journal_append(run_id, "agent_suspend", {
                        "prompt": prompt,
                        "pending_tool": pending,
                    })
                    self._checkpoint(run_id, term_ctx, saga_stack, pending_tool=pending)
                    seq = agent.journal.get_events(run_id)[-1].seq
                    token = SuspendToken(run_id=run_id, node_name="tool_confirm", seq=seq)
                    return RunResult(
                        final_answer=f"Agent 挂起：{prompt}",
                        run_id=run_id,
                        status="suspended",
                        suspend_token=token,
                        suspend_prompt=prompt,
                    )

                output = self._execute_tool(
                    run_id, step_no, step_id, call, tool, tool_input, key, fn,
                    saga_stack, term_ctx, tracer,
                )
                record.observations.append(output)

            agent.trace.add(record)
            agent._emit_and_journal(
                run_id, "turn_finish",
                payload={"turn_idx": step_no, "decisions": tool_calls},
                turn_idx=step_no,
                decisions=tool_calls,
                context={},
            )
            self._checkpoint(run_id, term_ctx, saga_stack)

    def _execute_tool(
        self,
        run_id: str,
        step_no: int,
        step_id: str,
        call: dict,
        tool: Any,
        tool_input: dict,
        key: str,
        fn: dict,
        saga_stack: list,
        term_ctx: SimpleNamespace,
        tracer: Any,
    ) -> str:
        agent = self.agent
        tool_name = tool.name
        agent._emit_and_journal(
            run_id, "tool_before_call",
            payload={
                "turn_idx": step_no,
                "step_id": step_id,
                "tool_name": tool_name,
                "input": tool_input,
                "idempotency_key": key,
                "tool_call_id": call["id"],
                "isReadOnly": tool.isReadOnly,
                "isIdempotent": tool.isIdempotent,
                "hasSideEffects": tool.hasSideEffects,
            },
            turn_idx=step_no,
            step_id=step_id,
            tool_name=tool_name,
            input=tool_input,
        )

        tool_status = "success"
        tool_duration_ms = 0
        cached_result = agent.journal.cache_get(key)
        if cached_result is not None:
            result = cached_result
            tool_status = "idempotency_hit"
        else:
            circuit_key = tool.circuit_key or tool_name
            circuit_registry = agent.circuit_breaker_registry
            allowed, circuit_reason_msg = True, None
            if circuit_registry is not None:
                allowed, _st, circuit_reason_msg = circuit_registry.allow_request(
                    circuit_key, run_id=run_id
                )
            if not allowed:
                result = f"工具执行被熔断器拒绝: {circuit_reason_msg or 'circuit open'}"
                tool_status = "circuit_rejected"
            else:
                with tracer.span("gen_ai.execute_tool", {
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.name": tool_name,
                }):
                    tool_start = time.time()
                    result = None
                    actual_retry_max = tool.retry_max if tool.isIdempotent else 0
                    total_attempts = actual_retry_max + 1
                    for attempt_idx in range(total_attempts):
                        try:
                            cleaned_args = (
                                tool_input
                                if not isinstance(fn["arguments"], str)
                                else json.dumps(tool_input, ensure_ascii=False)
                            )
                            result = agent.tools.execute(tool_name, cleaned_args)
                            tool_status = "success"
                            if circuit_registry is not None:
                                circuit_registry.on_success(circuit_key, run_id=run_id)
                            break
                        except Exception as e:
                            if circuit_registry is not None:
                                circuit_registry.on_failure(circuit_key, run_id=run_id)
                            is_retryable = (
                                tool.isIdempotent
                                and any(isinstance(e, et) for et in tool.retry_on)
                                and attempt_idx < actual_retry_max
                            )
                            if not is_retryable:
                                result = f"工具执行失败: {e}"
                                tool_status = "failed"
                                if (
                                    tool.isIdempotent
                                    and actual_retry_max > 0
                                    and attempt_idx >= actual_retry_max
                                ):
                                    agent._emit_and_journal(
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
                                break
                            attempt_no = attempt_idx + 1
                            delay_ms = full_jitter_delay_ms(attempt_no)
                            agent._emit_and_journal(
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
                            agent._emit_and_journal(
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
                    tool_duration_ms = int((time.time() - tool_start) * 1000)
                if tool_status == "success":
                    agent.journal.cache_set(key, result)

        if (
            tool.hasSideEffects
            and not tool.isIdempotent
            and tool.compensate is not None
            and tool_status == "success"
        ):
            saga_stack.append((step_id, tool_name, tool.compensate, dict(tool_input)))
            agent._journal_append(run_id, "saga_push", {
                "step_id": step_id,
                "tool_name": tool_name,
                "compensate_input": dict(tool_input),
            })

        agent.memory.add_tool(result, call["id"])
        agent.transcript.append({"role": "tool", "content": result, "tool_call_id": call["id"]})
        agent._journal_append(run_id, "tool_after_call", {
            "turn_idx": step_no,
            "step_id": step_id,
            "tool_name": tool_name,
            "input": tool_input,
            "result": {"output": result},
            "duration_ms": tool_duration_ms,
            "status": tool_status,
            "idempotency_key": key,
            "tool_call_id": call["id"],
        })
        self._checkpoint(run_id, term_ctx, saga_stack)
        agent.plugin_manager.emit(
            "tool_after_call",
            run_id=run_id,
            turn_idx=step_no,
            step_id=step_id,
            tool_name=tool_name,
            input=tool_input,
            result={"output": result},
            duration_ms=tool_duration_ms,
            status=tool_status,
        )
        return result

    def _finish_pending_tool(
        self,
        run_id: str,
        pending: dict,
        approved: bool,
        saga_stack: list,
        term_ctx: SimpleNamespace,
    ) -> RunResult | None:
        agent = self.agent
        tool = agent.tools.get(pending["tool_name"])
        call = {"id": pending["call_id"], "function": {
            "name": pending["tool_name"],
            "arguments": pending.get("fn_arguments") or pending["tool_input"],
        }}
        if not approved:
            denied = "用户拒绝执行该工具"
            agent.memory.add_tool(denied, pending["call_id"])
            agent._journal_append(run_id, "tool_after_call", {
                "step_id": pending["step_id"],
                "tool_name": pending["tool_name"],
                "result": {"output": denied},
                "status": "denied",
                "tool_call_id": pending["call_id"],
            })
            return None
        self._execute_tool(
            run_id,
            int(pending.get("step_no") or term_ctx.step_count),
            pending["step_id"],
            call,
            tool,
            dict(pending["tool_input"]),
            pending["idempotency_key"],
            call["function"],
            saga_stack,
            term_ctx,
            agent.runtime.tracer,
        )
        return None

    def _terminate(
        self,
        run_id: str,
        step_no: int,
        reasons: list[str],
        saga_stack: list,
        compensate_failed: list,
        term_ctx: SimpleNamespace,
    ) -> RunResult:
        agent = self.agent
        final = f"Agent 终止：{'; '.join(reasons)}"
        last_result = None
        if saga_stack:
            agent.plugin_manager.emit(
                "compensate_start",
                run_id=run_id,
                failed_step_id=f"{run_id}:{step_no}:0",
                compensate_count=len(saga_stack),
            )
            agent._journal_append(
                run_id, "compensate_start",
                {"compensate_count": len(saga_stack), "failed_reason": "; ".join(reasons)},
            )
            while saga_stack:
                sid, tname, comp_func, ci = saga_stack.pop()
                agent._journal_append(run_id, "saga_pop", {"step_id": sid, "tool_name": tname})
                try:
                    comp_func(ci, last_result, {"run_id": run_id, "step_id": sid, "tool_name": tname})
                    agent.plugin_manager.emit(
                        "compensate_finish",
                        run_id=run_id, step_id=sid, tool_name=tname, status="success",
                    )
                    agent._journal_append(
                        run_id, "compensate_finish",
                        {"step_id": sid, "tool_name": tname, "status": "success"},
                    )
                except Exception as e:
                    err = str(e)
                    compensate_failed.append((sid, tname, err))
                    agent.plugin_manager.emit(
                        "compensate_failed",
                        run_id=run_id, step_id=sid, tool_name=tname,
                        error=err, retry_count=1, next_retry_at=None,
                    )
                    agent._journal_append(
                        run_id, "compensate_failed",
                        {"step_id": sid, "tool_name": tname, "error": err, "retry_count": 1},
                    )
            if compensate_failed:
                failed_list = ", ".join([f"{t}(sid={s}:{e})" for s, t, e in compensate_failed])
                final += f"\n补偿失败列表: [{failed_list}] 请人工介入"
        agent._emit_and_journal(
            run_id, "turn_finish",
            payload={"turn_idx": step_no, "decisions": []},
            turn_idx=step_no, decisions=[], context={},
        )
        if agent.trace._parent_trace is None:
            agent.trace.render(agent.renderer, depth=0)
        agent._emit_and_journal(
            run_id, "agent_finish",
            payload={"final_result": final, "status": "terminated", "compensate_failed": compensate_failed},
            final_result=final, status="terminated",
            context={"reasons": reasons, "compensate_failed": compensate_failed},
        )
        agent.renderer.markdown(final, title="终止信息")
        return RunResult(final_answer=final, run_id=run_id, status="terminated")

    def _finish_success(self, run_id: str, step_no: int, content: str) -> RunResult:
        agent = self.agent
        agent._emit_and_journal(
            run_id, "turn_finish",
            payload={"turn_idx": step_no, "decisions": []},
            turn_idx=step_no, decisions=[], context={},
        )
        if agent.trace._parent_trace is None:
            agent.trace.render(agent.renderer, depth=0)
        agent._emit_and_journal(
            run_id, "agent_finish",
            payload={"final_result": content, "status": "success"},
            final_result=content, status="success", context={},
        )
        agent.renderer.markdown(content, title="最终答案")
        return RunResult(final_answer=content, run_id=run_id, status="success")

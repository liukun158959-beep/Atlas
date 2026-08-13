"""失败记忆：从 Plugin 钩子采集失败案例，embedding 索引后供检索。

Q7=A：FailureCase dataclass + FailureMemoryCollector(BasePlugin) 挂 3 钩子实时采集
Q14=A：on_tool_after_call(failed) + on_retry_exhausted + on_compensate_failed
Q15=B：弱引用 weakref.ref(agent) → 读 agent.memory.messages
Q16=A：Agent __init__ 末尾遍历 plugins，hasattr(bind_agent) 自动注入
"""
from __future__ import annotations

import time
import weakref
from dataclasses import dataclass, field
from typing import Any

from atlas.core.plugin import BasePlugin

from .embedding import EmbeddingProtocol
from .vector_store import VectorStoreProtocol


@dataclass
class FailureCase:
    """失败案例。"""

    run_id: str
    step_id: str
    tool_name: str
    input_summary: str           # 工具输入摘要
    error_message: str           # 错误信息
    error_type: str              # 异常类型名 / 触发钩子名
    context_summary: str         # 任务上下文摘要（system prompt 前 200 字符 + user input）
    embedding: list[float]       # context_summary 的 embedding
    created_at: float = 0.0


class FailureMemory:
    """失败记忆库：记录 / 语义检索 / 最近列表。

    持有 VectorStoreProtocol + EmbeddingProtocol 引用。
    record 时 embed(context_summary) → 存 VectorStore + 内存 dict。
    """

    def __init__(
        self,
        embedding: EmbeddingProtocol,
        vector_store: VectorStoreProtocol,
    ) -> None:
        self._embedding = embedding
        self._vs = vector_store
        # 内存索引：case_id -> FailureCase
        self._cases: dict[str, FailureCase] = {}
        # 按创建时间排序的 case_id 列表（用于 list_recent）
        self._ordered_ids: list[str] = []
        self._counter = 0

    def record(self, case: FailureCase) -> str:
        """记录一条失败案例，返回 case_id。"""
        if case.created_at == 0.0:
            case.created_at = time.time()

        # 如果 embedding 为空，自动计算
        if not case.embedding:
            case.embedding = self._embedding.embed(case.context_summary)

        self._counter += 1
        case_id = f"failure_{self._counter}"

        self._vs.add(case_id, case.embedding, {
            "tool_name": case.tool_name,
            "error_type": case.error_type,
        })
        self._cases[case_id] = case
        self._ordered_ids.append(case_id)

        return case_id

    def retrieve(self, query: str, top_k: int = 5) -> list[FailureCase]:
        """语义检索：query 文本 -> embedding -> VectorStore top_k -> 返回 FailureCase 列表。"""
        query_emb = self._embedding.embed(query)
        hits = self._vs.query(query_emb, top_k)
        results: list[FailureCase] = []
        for hit in hits:
            case = self._cases.get(hit.id)
            if case is not None:
                results.append(case)
        return results

    def list_recent(self, n: int = 10) -> list[FailureCase]:
        """按时间倒序返回最近 N 条失败案例。"""
        ids = self._ordered_ids[-n:] if n < len(self._ordered_ids) else self._ordered_ids
        return [self._cases[cid] for cid in reversed(ids)]


@dataclass
class FailureMemoryCollector(BasePlugin):
    """失败记忆采集插件。

    挂 on_tool_after_call(failed) / on_retry_exhausted / on_compensate_failed 三个钩子，
    自动采集失败案例存入 FailureMemory。

    通过 bind_agent(agent) 注入 Agent 弱引用，用于读取 system_prompt + user_input 构造 context_summary。
    """

    name: str = "failure_memory_collector"
    priority: int = 50
    failure_memory: FailureMemory = None
    embedding: EmbeddingProtocol = None
    _agent_ref: Any = None  # weakref.ref(agent) 或 None

    def bind_agent(self, agent: Any) -> None:
        """注入 Agent 弱引用（由 Agent.__init__ 自动调用）。"""
        self._agent_ref = weakref.ref(agent)

    def _get_context_summary(self) -> str:
        """从 Agent 弱引用读取 system_prompt + user_input 构造上下文摘要。"""
        if self._agent_ref is None:
            return "(no agent context)"
        agent = self._agent_ref()
        if agent is None:
            return "(agent gc'd)"

        # 读取 memory messages 中的 system prompt 和最近 user input
        messages = getattr(agent, "memory", None)
        if messages is None:
            return "(no memory)"

        # memory.messages 是 list[dict]，取 system 和最近 user
        msgs = messages.messages if hasattr(messages, "messages") else []
        system_prompt = ""
        user_input = ""
        for msg in msgs:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if role == "system" and not system_prompt:
                system_prompt = content[:200]
            if role == "user":
                user_input = content

        return f"[system]{system_prompt} [user]{user_input}"

    def _record(
        self,
        run_id: str,
        step_id: str,
        tool_name: str,
        input_summary: str,
        error_message: str,
        error_type: str,
    ) -> None:
        """采集一条失败案例。"""
        if self.failure_memory is None or self.embedding is None:
            return

        context_summary = self._get_context_summary()
        embedding = self.embedding.embed(context_summary)

        case = FailureCase(
            run_id=run_id,
            step_id=step_id,
            tool_name=tool_name,
            input_summary=input_summary,
            error_message=error_message,
            error_type=error_type,
            context_summary=context_summary,
            embedding=embedding,
        )
        self.failure_memory.record(case)

    # ------------------------------------------------------------------
    # Plugin 钩子
    # ------------------------------------------------------------------

    def on_tool_after_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        input: dict[str, Any],
        result: dict[str, Any],
        duration_ms: int,
        status: str,
    ) -> None:
        """工具调用失败时采集。"""
        if status in ("failed", "circuit_rejected"):
            # 从 result 中提取错误信息
            error_msg = result.get("error", "") if isinstance(result, dict) else str(result)
            input_summary = str(input)[:200]
            self._record(run_id, step_id, tool_name, input_summary, str(error_msg), "tool_failed")

    def on_retry_exhausted(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        total_attempts: int,
        last_error: str,
    ) -> None:
        """重试耗尽时采集。"""
        self._record(
            run_id, step_id, tool_name,
            f"(retry x{total_attempts})",
            last_error,
            "retry_exhausted",
        )

    def on_compensate_failed(
        self,
        *,
        run_id: str,
        step_id: str,
        tool_name: str,
        error: str,
        retry_count: int,
        next_retry_at: str | None,
    ) -> None:
        """补偿失败时采集。"""
        self._record(
            run_id, step_id, tool_name,
            f"(compensate retry x{retry_count})",
            error,
            "compensate_failed",
        )

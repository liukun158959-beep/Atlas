"""Journal：append-only 事件日志，支持 event sourcing 重放。

事件结构：折中设计（结构化壳 + 自由 payload）
- 公共字段（run_id/seq/ts/event_type）是强类型 dataclass
- payload 是自由 dict，按 event_type 约定 key

存储范围：默认摘要模式（save_full_payload=False），opt-in 全量模式。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@dataclass
class JournalEvent:
    """单条 Journal 事件，不可变。"""

    run_id: str
    seq: int  # 单调递增序列，决定重放顺序
    ts: str  # ISO 8601 时间戳
    event_type: str  # "agent_start" / "llm_call" / "tool_call" / "tool_result" / "step_finish" / "subagent_spawn" / "agent_finish"
    payload: dict  # 按 event_type 约定的结构化数据


@runtime_checkable
class JournalProtocol(Protocol):
    """Journal 抽象接口，后续 SQLite 实现只需实现此 Protocol。"""

    def append(self, event: JournalEvent) -> None:
        """追加一条事件（append-only，不可修改）。"""
        ...

    def get_events(self, run_id: str) -> list[JournalEvent]:
        """获取指定 run 的全部事件，按 seq 升序。"""
        ...

    def replay(self, run_id: str) -> dict:
        """重放指定 run 的事件，返回重放后的状态摘要。"""
        ...

    def list_run_ids(self) -> list[str]:
        """列出所有 run_id。"""
        ...


def _now_iso() -> str:
    """返回当前 UTC 时间的 ISO 8601 字符串。"""
    return datetime.now(timezone.utc).isoformat()


# 摘要模式下需要 mask 的敏感字段名（大小写不敏感匹配）
_SENSITIVE_KEYS = {"api_key", "token", "secret", "password", "credential", "authorization"}

# 摘要模式下 payload 字符串值的最大保留长度
_SUMMARY_MAX_LEN = 200


def _summarize_value(val, depth: int = 0) -> object:
    """递归摘要一个值：字符串截断、敏感字段 mask、dict 递归。"""
    if depth > 3:
        return "<truncated>"
    if isinstance(val, str):
        return val[:_SUMMARY_MAX_LEN] + ("..." if len(val) > _SUMMARY_MAX_LEN else "")
    if isinstance(val, dict):
        result = {}
        for k, v in val.items():
            if isinstance(k, str) and k.lower() in _SENSITIVE_KEYS:
                result[k] = "***"
            else:
                result[k] = _summarize_value(v, depth + 1)
        return result
    if isinstance(val, list):
        if len(val) > 5:
            return [_summarize_value(v, depth + 1) for v in val[:3]] + [f"...({len(val)} items total)"]
        return [_summarize_value(v, depth + 1) for v in val]
    return val


def _summarize_payload(event_type: str, payload: dict) -> dict:
    """摘要模式：mask 敏感字段 + 截断长文本。"""
    return _summarize_value(payload)  # 走 dict 分支，执行 key 检查


@dataclass
class InMemoryJournal:
    """内存版 Journal 实现，用于开发与测试。

    生产环境替换为 SQLiteJournal（只需实现 JournalProtocol）。
    """

    save_full_payload: bool = False
    _events: list[JournalEvent] = field(default_factory=list)
    _counters: dict[str, int] = field(default_factory=dict)

    def append(self, event: JournalEvent) -> None:
        """追加事件。若 save_full_payload=False，自动摘要 payload。"""
        if not self.save_full_payload:
            event = JournalEvent(
                run_id=event.run_id,
                seq=event.seq,
                ts=event.ts,
                event_type=event.event_type,
                payload=_summarize_payload(event.event_type, event.payload),
            )
        self._events.append(event)

    def next_seq(self, run_id: str) -> int:
        """获取指定 run 的下一个 seq（从 1 开始）。"""
        self._counters[run_id] = self._counters.get(run_id, 0) + 1
        return self._counters[run_id]

    def get_events(self, run_id: str) -> list[JournalEvent]:
        """获取指定 run 的全部事件，按 seq 升序。"""
        return sorted(
            [e for e in self._events if e.run_id == run_id],
            key=lambda e: e.seq,
        )

    def replay(self, run_id: str) -> dict:
        """重放指定 run 的事件，返回状态摘要。

        当前实现：聚合事件类型计数 + 最后一个 agent_finish 的 final_result。
        后续可扩展为完整状态重建。
        """
        events = self.get_events(run_id)
        if not events:
            return {"run_id": run_id, "event_count": 0, "status": "not_found"}

        type_counts: dict[str, int] = {}
        final_result = None
        status = "running"
        for e in events:
            type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1
            if e.event_type == "agent_finish":
                final_result = e.payload.get("final_result")
                status = e.payload.get("status", "finished")

        return {
            "run_id": run_id,
            "event_count": len(events),
            "type_counts": type_counts,
            "status": status,
            "final_result": final_result,
        }

    def list_run_ids(self) -> list[str]:
        """列出所有 run_id（按首次出现顺序）。"""
        seen: list[str] = []
        for e in self._events:
            if e.run_id not in seen:
                seen.append(e.run_id)
        return seen

    def make_event(self, run_id: str, event_type: str, payload: dict) -> JournalEvent:
        """便捷工厂：自动生成 seq 和 ts。"""
        return JournalEvent(
            run_id=run_id,
            seq=self.next_seq(run_id),
            ts=_now_iso(),
            event_type=event_type,
            payload=payload,
        )

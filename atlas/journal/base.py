"""Journal：append-only 事件日志，支持 event sourcing 重放。

事件结构：折中设计（结构化壳 + 自由 payload）
- 公共字段（run_id/seq/ts/event_type）是强类型 dataclass
- payload 是自由 dict，按 event_type 约定 key

关键恢复事件始终全量落盘；其余事件在 save_full_payload=False 时摘要。
restore(run_id) 重建 RunSnapshot；replay() 仍为摘要 API。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from .restore import RunSnapshot


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
        """追加一条事件（append-only，不可修改）。

        Args:
            event: 含 run_id / seq / event_type / payload。
        """
        ...

    def get_events(self, run_id: str) -> list[JournalEvent]:
        """按 seq 升序取出该 run 的全部事件。

        Args:
            run_id: 运行 id。
        """
        ...

    def replay(self, run_id: str) -> dict:
        """重放并返回摘要（给旧 demo）；完整恢复用 restore。

        Args:
            run_id: 运行 id。
        """
        ...

    def list_run_ids(self) -> list[str]:
        """列出已知 run_id。"""
        ...

    def cache_get(self, key: str) -> str | None:
        """按幂等键取缓存；没有则 None。

        Args:
            key: 通常是 ``idempotency.derive`` 的结果。
        """
        ...

    def cache_set(self, key: str, value: str) -> None:
        """按幂等键覆盖写入工具结果。

        Args:
            key: 幂等键。
            value: 序列化后的工具输出。
        """
        ...

    def restore(self, run_id: str) -> Any:
        """从事件重建 ``RunSnapshot``（Memory / saga / 终止上下文）。

        Args:
            run_id: 运行 id。

        依赖:
            ``atlas.journal.restore.restore_from_events``。
        """
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


def _should_store_full(event_type: str, save_full_payload: bool) -> bool:
    """关键恢复事件始终全量落盘；其余事件尊重 save_full_payload。"""
    from .restore import CRITICAL_EVENT_TYPES

    return save_full_payload or event_type in CRITICAL_EVENT_TYPES


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
    _cache: dict[str, str] = field(default_factory=dict)

    def cache_get(self, key: str) -> str | None:
        """按幂等键取缓存。"""
        return self._cache.get(key)

    def cache_set(self, key: str, value: str) -> None:
        """按幂等键覆盖写缓存。"""
        self._cache[key] = value

    def append(self, event: JournalEvent) -> None:
        """追加事件。关键恢复事件始终全量；其余在 save_full_payload=False 时摘要。"""
        if not _should_store_full(event.event_type, self.save_full_payload):
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

    def restore(self, run_id: str) -> "RunSnapshot":
        """从事件重建 RunSnapshot。

        Args:
            run_id: 运行 id。

        依赖:
            ``restore_from_events``。
        """
        from .restore import restore_from_events

        return restore_from_events(self.get_events(run_id))

    def replay(self, run_id: str) -> dict:
        """重放指定 run 的事件，返回状态摘要（兼容旧 demo）。"""
        events = self.get_events(run_id)
        if not events:
            return {"run_id": run_id, "event_count": 0, "status": "not_found"}

        snap = self.restore(run_id)
        type_counts: dict[str, int] = {}
        for e in events:
            type_counts[e.event_type] = type_counts.get(e.event_type, 0) + 1

        return {
            "run_id": run_id,
            "event_count": len(events),
            "type_counts": type_counts,
            "status": snap.status,
            "final_result": snap.final_result,
        }

    def list_run_ids(self) -> list[str]:
        """列出所有 run_id（按首次出现顺序）。"""
        seen: list[str] = []
        for e in self._events:
            if e.run_id not in seen:
                seen.append(e.run_id)
        return seen

    def make_event(self, run_id: str, event_type: str, payload: dict) -> JournalEvent:
        """组装一条事件（自动分配 seq 与时间戳）。

        Args:
            run_id: 运行 id。
            event_type: 如 agent_start / llm_after_call。
            payload: 自由 dict。
        """
        return JournalEvent(
            run_id=run_id,
            seq=self.next_seq(run_id),
            ts=_now_iso(),
            event_type=event_type,
            payload=payload,
        )

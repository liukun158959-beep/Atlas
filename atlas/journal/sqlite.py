"""SQLite 持久化 Journal。

设计（精简 grill-me 3 项冻结）：
  Q1：文件路径：默认 ./.atlas/journal.db；path 参数可覆盖；支持 :memory:
  Q2：并发安全：check_same_thread=True + 单一 threading.Lock 全包读写（无 WAL，不多文件）
  Q3：
    - 表结构：events(run_id, seq, ts, event_type, payload_json) + cache(key, value, updated_at)
    - next_seq：SELECT COALESCE(MAX(seq), 0) + 1 实时算（不单独维护 counter 表）
    - 方法：实现 JournalProtocol 6 方法 + 对称加 next_seq/make_event（与 InMemoryJournal 一致，Agent 侧 journal.make_event() 调用不感知类型）
    - save_full_payload 行为与 InMemoryJournal 完全一致（复用 _summarize_payload）

SQLite 依赖：Python 3.11 标准库 sqlite3 自带，无需额外安装。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .base import JournalEvent, _now_iso, _summarize_payload


# 表结构（纯 UTF-8，无 emoji）
_SCHEMA_SQL = [
    """
    CREATE TABLE IF NOT EXISTS events (
        run_id      TEXT    NOT NULL,
        seq         INTEGER NOT NULL,
        ts          TEXT    NOT NULL,
        event_type  TEXT    NOT NULL,
        payload_json TEXT   NOT NULL,
        PRIMARY KEY (run_id, seq)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_events_run_id ON events(run_id)
    """,
    """
    CREATE TABLE IF NOT EXISTS cache (
        key        TEXT PRIMARY KEY,
        value      TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
]


def _resolve_path(path: str | None) -> str:
    """将 None / 相对路径解析成绝对路径或 :memory:。"""
    if path is None:
        p = Path.cwd() / ".atlas" / "journal.db"
        p.parent.mkdir(parents=True, exist_ok=True)
        return str(p)
    if path == ":memory:":
        return ":memory:"
    # 用户显式路径：相对 → 相对 cwd；绝对 → 保持
    p = Path(path)
    if not p.is_absolute():
        p = Path.cwd() / p
    p.parent.mkdir(parents=True, exist_ok=True)
    return str(p)


@dataclass
class SQLiteJournal:
    """SQLite 持久化 Journal（append-only + cache）。

    字段：
        save_full_payload：False=摘要模式（mask 敏感字段 + 截断长文本），True=全量落盘
        path：None=默认 ./.atlas/journal.db；':memory:'=内存库；str=用户路径
    """

    save_full_payload: bool = False
    path: str | None = field(default=None)

    # 内部状态（post_init 初始化）
    _conn: sqlite3.Connection = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _real_path: str = field(default="", init=False, repr=False)

    def __post_init__(self) -> None:
        self._real_path = _resolve_path(self.path)
        # Q2=A：check_same_thread=True（所有操作必须在同一线程 + Lock 全包约束）
        # isolation_level=None 意味着开启 autocommit 模式（每次 execute 自动提交）
        self._conn = sqlite3.connect(
            self._real_path,
            check_same_thread=True,
            isolation_level=None,
        )
        # 建表（CREATE TABLE 幂等，进程启动时并发也安全——SQLite 自身锁 + 外层 Lock）
        with self._lock:
            for stmt in _SCHEMA_SQL:
                self._conn.execute(stmt)

    # =============================================================
    # 与 InMemoryJournal 对称的辅助方法（Agent 侧无感知调用）
    # =============================================================
    def next_seq(self, run_id: str) -> int:
        """获取指定 run 的下一个 seq（从 1 开始，基于 MAX(seq) 实时算）。

        注意：必须与后续 append 处于同一 Lock 临界区外分别调用是安全的（
        next_seq 只是拿数，append 用拿到的 seq 写 PRIMARY KEY(run_id, seq) 唯一性兜底；
        若两个线程 next_seq 拿到同一个 seq 值，其中一个 append 会 IntegrityError 直接失败。
        实践中 Agent 对同一 run_id 的事件是单线程 append 的（Agent.run 串行步骤），
        跨 run 不会冲突，因此实际不触发此情况。
        """
        with self._lock:
            cur = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM events WHERE run_id = ?",
                (run_id,),
            )
            return int(cur.fetchone()[0])

    def make_event(self, run_id: str, event_type: str, payload: dict) -> JournalEvent:
        """便捷工厂：自动生成 seq 和 ts（与 InMemoryJournal 对称）。"""
        return JournalEvent(
            run_id=run_id,
            seq=self.next_seq(run_id),
            ts=_now_iso(),
            event_type=event_type,
            payload=payload,
        )

    # =============================================================
    # JournalProtocol 6 个方法
    # =============================================================
    def append(self, event: JournalEvent) -> None:
        """追加一条事件（append-only）。若 save_full_payload=False，自动摘要 payload。

        与 InMemoryJournal 行为完全一致（copy 自 base.py InMemoryJournal.append）。
        """
        from .base import _should_store_full

        if not _should_store_full(event.event_type, self.save_full_payload):
            event = JournalEvent(
                run_id=event.run_id,
                seq=event.seq,
                ts=event.ts,
                event_type=event.event_type,
                payload=_summarize_payload(event.event_type, event.payload),
            )
        with self._lock:
            self._conn.execute(
                "INSERT INTO events(run_id, seq, ts, event_type, payload_json) VALUES(?, ?, ?, ?, ?)",
                (
                    event.run_id,
                    int(event.seq),
                    event.ts,
                    event.event_type,
                    json.dumps(event.payload, ensure_ascii=False),
                ),
            )

    def get_events(self, run_id: str) -> list[JournalEvent]:
        """获取指定 run 的全部事件，按 seq 升序。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT run_id, seq, ts, event_type, payload_json FROM events "
                "WHERE run_id = ? ORDER BY seq ASC",
                (run_id,),
            )
            rows = cur.fetchall()
        out: list[JournalEvent] = []
        for run_id_v, seq, ts, event_type, payload_json in rows:
            out.append(JournalEvent(
                run_id=run_id_v,
                seq=int(seq),
                ts=ts,
                event_type=event_type,
                payload=json.loads(payload_json),
            ))
        return out

    def restore(self, run_id: str):
        """从事件重建 RunSnapshot。"""
        from .restore import restore_from_events

        return restore_from_events(self.get_events(run_id))

    def replay(self, run_id: str) -> dict:
        """重放指定 run 的事件，返回状态摘要。"""
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
        """列出所有 run_id（按首次出现 seq 最小的顺序）。"""
        with self._lock:
            cur = self._conn.execute(
                "SELECT run_id FROM events GROUP BY run_id ORDER BY MIN(seq) ASC"
            )
            return [row[0] for row in cur.fetchall()]

    def cache_get(self, key: str) -> str | None:
        """按幂等键取缓存值，无则返回 None。"""
        with self._lock:
            cur = self._conn.execute("SELECT value FROM cache WHERE key = ?", (key,))
            row = cur.fetchone()
            return row[0] if row else None

    def cache_set(self, key: str, value: str) -> None:
        """按幂等键写缓存值（覆盖写）。"""
        with self._lock:
            self._conn.execute(
                "INSERT INTO cache(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now_iso()),
            )

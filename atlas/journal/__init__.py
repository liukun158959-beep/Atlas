"""Journal 子包：append-only 事件日志持久化实现。

当前提供：
- InMemoryJournal：内存版（开发/测试）
- SQLiteJournal：SQLite 持久化版（默认 ./.atlas/journal.db，崩溃可重放）
"""
from .base import InMemoryJournal, JournalEvent, JournalProtocol
from .sqlite import SQLiteJournal

__all__ = ["JournalProtocol", "JournalEvent", "InMemoryJournal", "SQLiteJournal"]

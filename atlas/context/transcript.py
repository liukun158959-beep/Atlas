"""Transcript：append-only 事件存档，磁盘层全量记录。

与 Memory 的 Runtime 层不同，Transcript 记录全量事件（不裁剪、不投影），
用于崩溃恢复、审计回放、调试分析。
内存 list[dict] 为主，JSONL 导出为可选持久化。
"""
from __future__ import annotations

import json
from typing import Any


class Transcript:
    """append-only 事件存档。

    与 Journal 的区别：
    - Journal 记录 Agent 级事件（turn/llm/tool/compact 等结构化事件）
    - Transcript 记录消息级事件（每条写入 Memory 的消息都同步追加）

    与 Memory 的区别：
    - Memory 是 Runtime 层（会被 compact/裁剪/project）
    - Transcript 是磁盘层（append-only，不裁剪不修改）
    """

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []

    def append(self, event: dict) -> None:
        """追加一条消息事件（不修改、不裁剪）。

        Args:
            event: 通常是写入 Memory 的那条消息 dict。
        """
        self._events.append(event)

    def replay(self) -> list[dict]:
        """返回全量事件列表。"""
        return list(self._events)

    def __len__(self) -> int:
        return len(self._events)

    def to_jsonl(self, path: str) -> None:
        """导出为 JSONL。

        Args:
            path: 文件路径。
        """
        with open(path, "w", encoding="utf-8") as f:
            for event in self._events:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")

    @classmethod
    def from_jsonl(cls, path: str) -> "Transcript":
        """从 JSONL 恢复。

        Args:
            path: ``to_jsonl`` 写出的文件。
        """
        transcript = cls()
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    transcript._events.append(json.loads(line))
        return transcript

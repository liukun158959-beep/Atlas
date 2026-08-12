"""SideEffectTracker：Demo 用副作用记录器，验证 Saga 补偿的可观测性。

不进内核，仅作为 saga_demo.py 和用户自定义 demo 的辅助。
核心语义：record(effect_id, metadata) 记录副作用，erase(effect_id) 清除补偿后副作用，
is_empty() 用于断言"所有已记录副作用都被补偿清除"。
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class SideEffectTracker:
    """内存版副作用追踪器：key=任意字符串 effect_id，value=metadata dict。"""

    _records: dict[str, dict] = field(default_factory=dict)

    def record(self, effect_id: str, metadata: dict | None = None) -> None:
        """记录一个副作用（工具执行成功时调用）。"""
        self._records[effect_id] = metadata or {}

    def erase(self, effect_id: str) -> None:
        """清除一个副作用（compensate 成功时调用）。"""
        self._records.pop(effect_id, None)

    def exists(self, effect_id: str) -> bool:
        return effect_id in self._records

    def is_empty(self) -> bool:
        """断言辅助：所有已记录副作用都被 erase 清空。"""
        return len(self._records) == 0

    def __len__(self) -> int:
        return len(self._records)

    def snapshot(self) -> dict[str, dict]:
        """只读快照：renderer 展示当前未补偿的副作用。"""
        return dict(self._records)

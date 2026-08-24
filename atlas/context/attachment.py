"""Attachment 机制：程序内部状态传递给模型的标准解法。

Attachment 是抽象基类 + 类型注册表，上层按场景注册具体类型
（file_state / task_status / skill_list / workflow_state）。
内核不硬编码具体类型，默认转 system-reminder 文本。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Attachment(ABC):
    """Attachment 抽象基类：程序状态传递给模型。

    子类需实现 to_system_reminder()，返回要插入 API messages 的 system 消息文本。
    内核不预置任何具体类型，由上层按场景注册。
    """

    name: str = "attachment"

    @abstractmethod
    def to_system_reminder(self) -> str:
        """转换为 system-reminder 文本，插入 API messages。"""
        ...

    def to_message(self) -> dict:
        """转换为 Runtime 层消息（isMeta=True，project 时过滤并转 system-reminder）。"""
        return {
            "role": "system",
            "content": self.to_system_reminder(),
            "isMeta": True,
            "type": "attachment",
            "attachment_name": self.name,
        }


class AttachmentRegistry:
    """Attachment 类型注册表：register / create 模式，与 Toolkit 一致。"""

    def __init__(self) -> None:
        self._types: dict[str, type[Attachment]] = {}

    def register(self, name: str, cls: type[Attachment]) -> None:
        """注册 Attachment 类型，重名 ValueError。

        Args:
            name: 类型名。
            cls: ``Attachment`` 子类。
        """
        if name in self._types:
            raise ValueError(f"Attachment 类型已存在: {name}")
        if not issubclass(cls, Attachment):
            raise TypeError(f"{cls} 不是 Attachment 的子类")
        self._types[name] = cls

    def create(self, name: str, **kwargs: Any) -> Attachment:
        """按已注册类型名构造实例。

        Args:
            name: ``register`` 时的名字。
            **kwargs: 传给类型构造函数。
        """
        if name not in self._types:
            raise ValueError(f"未注册的 Attachment 类型: {name}")
        return self._types[name](**kwargs)

    def list_types(self) -> list[str]:
        """列出所有已注册的类型名。"""
        return list(self._types.keys())

    def rebuild_all(self, agent: Any) -> list[Attachment]:
        """compact 后从活状态重建 Attachment，不复用旧 isMeta 消息。

        Args:
            agent: 传给各类型 ``rebuild_from_state``。

        依赖:
            ``CompactManager.apply`` 之后由 AgentLoop 调用。
        """
        rebuilt: list[Attachment] = []
        for cls in self._types.values():
            fn = getattr(cls, "rebuild_from_state", None)
            if fn is None:
                continue
            inst = fn(agent)
            if inst is not None:
                rebuilt.append(inst)
        return rebuilt

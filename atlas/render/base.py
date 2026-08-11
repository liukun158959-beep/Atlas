"""渲染层：统一的输出抽象，支持纯文本与 Markdown 两种渲染模式。

遵循 DR-1：先有 Protocol，再有实现；
遵循 EP-2：模块解耦，上层通过配置注入具体 Renderer，不 hardcode。
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Renderer(Protocol):
    """渲染器协议。

    所有输出走 Renderer，上层不直接 print。
    默认用 PlainRenderer（纯文本），注入 RichRenderer 后自动支持 Markdown 渲染。

    方法分两类：
        - 基础输出：print / rule —— 通用文本与分隔线
        - 结构化输出：markdown / panel / table —— 富文本展示
    """

    def print(self, *args: object, **kwargs: object) -> None:
        """通用文本输出，等价于 builtin print。"""
        ...

    def rule(self, title: str = "") -> None:
        """分隔线，title 不为空时居中显示。"""
        ...

    def markdown(self, text: str, title: str | None = None) -> None:
        """渲染 Markdown 文本。title 不为空时先输出标题分隔线。"""
        ...

    def panel(
        self,
        content: str,
        title: str,
        *,
        border_style: str = "blue",
        **meta: str,
    ) -> None:
        """带标题的面板输出，用于子 Agent artifact 等结构化结果。

        meta 中的键值对显示为面板标题的附属信息（如耗时、状态）。
        """
        ...

    def table(self, rows: list[dict], headers: list[str], title: str | None = None) -> None:
        """表格输出，rows 是 dict 列表，headers 指定列顺序。"""
        ...

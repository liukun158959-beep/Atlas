"""纯文本渲染器：默认实现，退化所有富文本为 print。

不依赖任何第三方库，用于不支持 ANSI 的环境或重定向到文件时。
"""
from __future__ import annotations

from .base import Renderer


class PlainRenderer:
    """纯文本渲染器，所有方法退化成 print。"""

    name = "plain"

    def print(self, *args: object, **kwargs: object) -> None:
        print(*args, **kwargs)  # noqa: T201 - 渲染器就是要输出

    def rule(self, title: str = "") -> None:
        if title:
            print(f"\n{'=' * 10} {title} {'=' * 10}")  # noqa: T201
        else:
            print("=" * 60)  # noqa: T201

    def markdown(self, text: str, title: str | None = None) -> None:
        if title:
            self.rule(title)
        print(text)  # noqa: T201

    def panel(
        self,
        content: str,
        title: str,
        *,
        border_style: str = "blue",
        **meta: str,
    ) -> None:
        meta_str = "  ".join(f"{k}={v}" for k, v in meta.items())
        print(f"\n--- {title} ---")  # noqa: T201
        if meta_str:
            print(f"  [{meta_str}]")  # noqa: T201
        print(content)  # noqa: T201

    def table(self, rows: list[dict], headers: list[str], title: str | None = None) -> None:
        if title:
            print(f"\n[{title}]")  # noqa: T201
        # 简易对齐输出
        col_widths = [len(h) for h in headers]
        for row in rows:
            for i, h in enumerate(headers):
                val = str(row.get(h, ""))
                if len(val) > col_widths[i]:
                    col_widths[i] = len(val)
        fmt = "  ".join(f"{{:<{w}}}" for w in col_widths)
        print(fmt.format(*headers))  # noqa: T201
        print(fmt.format(*["-" * w for w in col_widths]))  # noqa: T201
        for row in rows:
            print(fmt.format(*[str(row.get(h, "")) for h in headers]))  # noqa: T201


class SilentRenderer:
    """不往终端输出。任务集打分 / ab_test 用。"""

    name = "silent"

    def print(self, *args: object, **kwargs: object) -> None:
        return

    def rule(self, title: str = "") -> None:
        return

    def markdown(self, text: str, title: str | None = None) -> None:
        return

    def panel(
        self,
        content: str,
        title: str,
        *,
        border_style: str = "blue",
        **meta: str,
    ) -> None:
        return

    def table(self, rows: list[dict], headers: list[str], title: str | None = None) -> None:
        return

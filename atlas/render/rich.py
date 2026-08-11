"""Rich 渲染器：基于 rich 库的 Markdown 渲染实现。

依赖 rich>=13.0，需要终端支持 ANSI 转义（Windows Terminal / 现代终端均可）。
重定向到文件时 ANSI 转义会保留为乱码，此时应改用 PlainRenderer。
"""
from __future__ import annotations

import re

from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from .base import Renderer

# LaTeX 数学公式用 Unicode 近似（rich 不渲染 $LaTeX$）
_SUPERSCRIPT = str.maketrans("0123456789+-=()", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾")


def _preprocess_markdown(text: str) -> str:
    """预处理 Markdown：把 $...$ LaTeX 转成 Unicode 近似，避免 rich 无法解析。

    只处理常见模式：
      - $10^6$             → 10⁶
      - $7.02 \\times 10^6$ → 7.02×10⁶
      - $x_i$ → x_i（下标没有统一 Unicode，保留原样）
    """

    def _replace_latex(m: re.Match) -> str:
        latex = m.group(1).strip()
        latex = latex.replace("\\times", "×").replace("\\cdot", "·")

        def _sup(m2: re.Match) -> str:
            base = m2.group(1)
            exp = m2.group(2).strip("{}")
            return base + exp.translate(_SUPERSCRIPT)

        latex = re.sub(r"([A-Za-z0-9)])\^(\{[^}]+\}|[A-Za-z0-9]+)", _sup, latex)
        return latex

    return re.sub(r"\$([^$]+)\$", _replace_latex, text)


class RichRenderer:
    """基于 rich 的 Markdown 渲染器。"""

    name = "rich"

    def __init__(self) -> None:
        # Console 自动检测终端 ANSI 支持，force_terminal 在重定向时也可强制着色
        self._console = Console()

    def print(self, *args: object, **kwargs: object) -> None:
        self._console.print(*args, **kwargs)

    def rule(self, title: str = "") -> None:
        if title:
            self._console.rule(f"[bold]{title}[/bold]")
        else:
            self._console.rule()

    def markdown(self, text: str, title: str | None = None) -> None:
        if title:
            self.rule(title)
        processed = _preprocess_markdown(text)
        self._console.print(Markdown(processed, code_theme="monokai"))

    def panel(
        self,
        content: str,
        title: str,
        *,
        border_style: str = "blue",
        **meta: str,
    ) -> None:
        # 标题行：主标题 + 附属信息（meta）
        header = f"[bold]{title}[/bold]"
        if meta:
            meta_str = "  ".join(f"[dim]{k}=[/dim]{v}" for k, v in meta.items())
            header += f"\n{meta_str}"
        processed = _preprocess_markdown(content)
        self._console.print(
            Panel(
                Markdown(processed, code_theme="monokai"),
                title=header,
                title_align="left",
                border_style=border_style,
            )
        )

    def table(self, rows: list[dict], headers: list[str], title: str | None = None) -> None:
        table = Table(title=title, show_lines=False, header_style="bold cyan")
        for h in headers:
            table.add_column(h)
        for row in rows:
            table.add_row(*[str(row.get(h, "")) for h in headers])
        self._console.print(table)

"""渲染层：统一的输出抽象。

默认 PlainRenderer（纯文本，零依赖），注入 RichRenderer 后支持 Markdown 渲染。

用法：
    from atlas.render import PlainRenderer, RichRenderer

    # 纯文本（默认，适合重定向到文件）
    renderer = PlainRenderer()

    # Markdown 渲染（适合终端）
    renderer = RichRenderer()
"""
from .base import Renderer
from .plain import PlainRenderer
from .rich import RichRenderer

__all__ = [
    "Renderer",
    "PlainRenderer",
    "RichRenderer",
]

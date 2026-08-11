"""预置插件包。

预置插件：
- token_cost：统计 token 用量与成本（订阅 on_llm_after_call）
- logger：    结构化落盘日志（订阅全部生命周期钩子）

遵循 DR-1：插件类在各自模块实现，此处只做包导出与自动发现辅助。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atlas.core.plugin import PluginProtocol


def list_builtin_plugins() -> dict[str, type["PluginProtocol"]]:
    """返回内置插件名到类的映射。

    用于声明式 YAML 配置时按名字加载插件。
    懒导入避免循环依赖。
    """
    from .token_cost import TokenCostPlugin
    from .logger import FileLoggerPlugin

    return {
        "token_cost": TokenCostPlugin,
        "logger": FileLoggerPlugin,
    }


def get_plugin_class(name: str) -> type["PluginProtocol"] | None:
    """按名字获取内置插件类，找不到返回 None（调用方处理）。"""
    return list_builtin_plugins().get(name)

"""插件体系：内核只发事件，插件做具体事。

遵循 DR-1：先有 Protocol，再有实现；
遵循 EP-2：模块解耦，内核不知道具体插件存在。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

log = logging.getLogger("atlas.plugin")


@runtime_checkable
class PluginProtocol(Protocol):
    """插件协议。

    插件只需实现自己关心的方法，其他方法留空即可。
    所有方法签名都用关键字参数传参，便于未来扩展。

    事件生命周期（按触发顺序）：
        agent_start
          turn_start（每轮循环触发）
            llm_before_call
            llm_after_call
            tool_before_call（如有工具调用）
            tool_after_call
          turn_finish
          compensate_start（如有补偿触发）
          compensate_failed（如补偿失败）
        agent_finish
    """

    @property
    def name(self) -> str:
        """插件唯一标识，用于注册/配置/日志。"""
        ...

    @property
    def priority(self) -> int:
        """执行优先级，数字越小越早执行。"""
        ...

    # ------------------------------------------------------------------
    # Agent 生命周期
    # ------------------------------------------------------------------

    def on_agent_start(self, *, run_id: str, task: str, context: dict[str, Any]) -> None:
        """Agent 任务开始。"""
        ...

    def on_agent_finish(
        self,
        *,
        run_id: str,
        final_result: str,
        status: str,
        context: dict[str, Any],
    ) -> None:
        """Agent 任务结束。"""
        ...

    # ------------------------------------------------------------------
    # Turn 生命周期
    # ------------------------------------------------------------------

    def on_turn_start(self, *, run_id: str, turn_idx: int, context: dict[str, Any]) -> None:
        """每一轮 LLM 交互开始。"""
        ...

    def on_turn_finish(
        self,
        *,
        run_id: str,
        turn_idx: int,
        decisions: list[Any],
        context: dict[str, Any],
    ) -> None:
        """每一轮 LLM 交互结束。"""
        ...

    # ------------------------------------------------------------------
    # LLM 调用
    # ------------------------------------------------------------------

    def on_llm_before_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        model: str,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> None:
        """LLM 调用之前。"""
        ...

    def on_llm_after_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        model: str,
        response: dict[str, Any],
        usage: dict[str, int] | None,
        duration_ms: int,
    ) -> None:
        """LLM 调用之后。usage 形如：
        {"prompt_tokens": 123, "completion_tokens": 45, "total_tokens": 168}
        """
        ...

    # ------------------------------------------------------------------
    # 工具调用
    # ------------------------------------------------------------------

    def on_tool_before_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        input: dict[str, Any],
    ) -> None:
        """工具调用之前。"""
        ...

    def on_tool_after_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        input: dict[str, Any],
        result: dict[str, Any],
        duration_ms: int,
        status: str,
    ) -> None:
        """工具调用之后。status: success / failed。"""
        ...

    # ------------------------------------------------------------------
    # Saga 补偿
    # ------------------------------------------------------------------

    def on_compensate_start(
        self,
        *,
        run_id: str,
        failed_step_id: str,
        compensate_count: int,
    ) -> None:
        """Saga 补偿流程开始。"""
        ...

    def on_compensate_failed(
        self,
        *,
        run_id: str,
        step_id: str,
        tool_name: str,
        error: str,
        retry_count: int,
        next_retry_at: str | None,
    ) -> None:
        """某次补偿动作失败（严重，需要人工）。"""
        ...


# ----------------------------------------------------------------------
# 插件管理器
# ----------------------------------------------------------------------


@dataclass
class PluginManager:
    """插件注册与事件分发。

    负责按 priority 排序插件、在每个生命周期节点 emit 事件。
    单个插件异常不会中断主流程（只记结构化日志）。
    """

    _plugins: list[PluginProtocol] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 注册
    # ------------------------------------------------------------------

    def register(self, plugin: PluginProtocol) -> None:
        """注册插件，按 priority 升序排序（小的先执行）。

        遵循 DR-6：同名插件禁止重复注册。
        """
        if not isinstance(plugin, PluginProtocol):
            raise TypeError(
                f"插件 {type(plugin).__name__} 未实现 PluginProtocol"
            )
        if any(p.name == plugin.name for p in self._plugins):
            raise ValueError(f"插件 {plugin.name!r} 已存在，禁止重复注册")
        self._plugins.append(plugin)
        self._plugins.sort(key=lambda p: p.priority)
        log.info("plugin.registered", name=plugin.name, priority=plugin.priority)

    def unregister(self, plugin_name: str) -> bool:
        """按名字移除插件，返回是否真的移除了。"""
        before = len(self._plugins)
        self._plugins = [p for p in self._plugins if p.name != plugin_name]
        removed = len(self._plugins) < before
        if removed:
            log.info("plugin.unregistered", name=plugin_name)
        return removed

    @property
    def plugins(self) -> list[PluginProtocol]:
        """返回已注册插件列表（只读视图）。"""
        return list(self._plugins)

    # ------------------------------------------------------------------
    # 事件分发
    # ------------------------------------------------------------------

    def emit(self, event: str, **kwargs: Any) -> None:
        """派发事件给所有插件。

        Parameters
        ----------
        event:
            事件名（如 "agent_start"、"llm_after_call"），
            会自动映射到 "on_{event}" 方法名。
        **kwargs:
            结构化关键字参数，会原封不动传给插件方法。

        遵循 DR-5：所有派发异常都记结构化日志，绝不中断主流程。
        """
        handler_name = f"on_{event}"
        for plugin in self._plugins:
            handler = getattr(plugin, handler_name, None)
            if not callable(handler):
                continue
            try:
                handler(**kwargs)
            except Exception as exc:  # noqa: BLE001 - 插件异常绝不外泄
                log.error(
                    "plugin.dispatch_failed",
                    event=event,
                    plugin=plugin.name,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

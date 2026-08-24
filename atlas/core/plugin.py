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
              circuit_half_open / circuit_open（熔断器状态切换时）
              retry_before → sleep → retry_after（每次重试前后）
              retry_exhausted（重试耗尽时）
            tool_after_call
          turn_finish
          compensate_start（如有补偿触发）
          compensate_finish（单次补偿成功时）
          compensate_failed（单次补偿失败时）
          circuit_closed（熔断器恢复时）
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

    def on_compensate_finish(
        self,
        *,
        run_id: str,
        step_id: str,
        tool_name: str,
        status: str,
    ) -> None:
        """某次补偿动作成功结束。"""
        ...

    # ------------------------------------------------------------------
    # 熔断器 Resilience
    # ------------------------------------------------------------------

    def on_circuit_open(
        self,
        *,
        run_id: str | None,
        circuit_key: str,
        failure_count: int,
        open_window_seconds: int,
    ) -> None:
        """熔断器 Closed→Open（连续失败达到阈值）。"""
        ...

    def on_circuit_half_open(
        self,
        *,
        run_id: str | None,
        circuit_key: str,
    ) -> None:
        """熔断器 Open→Half-Open（冷却结束，开始放探测请求）。"""
        ...

    def on_circuit_closed(
        self,
        *,
        run_id: str | None,
        circuit_key: str,
        probe_success: bool,
    ) -> None:
        """熔断器 Half-Open→Closed（探测成功恢复）。"""
        ...

    # ------------------------------------------------------------------
    # 重试 Resilience
    # ------------------------------------------------------------------

    def on_retry_before(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        attempt: int,
        next_delay_ms: int,
        error_type: str,
    ) -> None:
        """某次重试 sleep 之前。attempt 从 1 开始计数。"""
        ...

    def on_retry_after(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        attempt: int,
        status: str,
    ) -> None:
        """某次重试 sleep 之后（结果已知前 / 后，用于统计）。"""
        ...

    def on_retry_exhausted(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        total_attempts: int,
        last_error: str,
    ) -> None:
        """所有重试耗尽，工具最终标记失败之前。"""
        ...

    # ------------------------------------------------------------------
    # Graph Suspend/Resume（Phase 3.2 HITL）
    # ------------------------------------------------------------------

    def on_suspend(
        self,
        *,
        run_id: str,
        node_name: str,
        prompt: str,
        token: dict[str, Any],
    ) -> None:
        """Graph 执行到 SuspendNode 挂起。

        token 形如 {"run_id": ..., "node_name": ..., "seq": ...}。
        """
        ...

    def on_resume(
        self,
        *,
        run_id: str,
        node_name: str,
        user_input: dict[str, Any],
        resumed_from_seq: int,
    ) -> None:
        """外部调用 graph.resume() 恢复执行。"""
        ...


# ----------------------------------------------------------------------
# 基类：实现所有钩子为 no-op，子类按需覆写
# ----------------------------------------------------------------------


@dataclass
class BasePlugin:
    """插件基类：所有钩子默认 no-op，子类只需覆写关心的钩子。

    用法：
        @dataclass
        class MyPlugin(BasePlugin):
            name: str = "my"
            priority: int = 100

            def on_llm_after_call(self, *, run_id, ...): ...
    """

    @property
    def name(self) -> str:
        return "base"

    @property
    def priority(self) -> int:
        return 100

    def on_agent_start(self, *, run_id: str, task: str, context: dict[str, Any]) -> None:
        pass

    def on_agent_finish(self, *, run_id: str, final_result: str, status: str, context: dict[str, Any]) -> None:
        pass

    def on_turn_start(self, *, run_id: str, turn_idx: int, context: dict[str, Any]) -> None:
        pass

    def on_turn_finish(self, *, run_id: str, turn_idx: int, decisions: list[Any], context: dict[str, Any]) -> None:
        pass

    def on_llm_before_call(self, *, run_id: str, turn_idx: int, model: str, messages: list[dict[str, Any]], kwargs: dict[str, Any]) -> None:
        pass

    def on_llm_after_call(self, *, run_id: str, turn_idx: int, model: str, response: dict[str, Any], usage: dict[str, int] | None, duration_ms: int) -> None:
        pass

    def on_llm_delta(self, *, run_id: str, turn_idx: int, delta: str) -> None:
        """流式正文增量。默认忽略；不要把每字写入 Journal。"""
        pass

    def on_tool_before_call(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, input: dict[str, Any]) -> None:
        pass

    def on_tool_after_call(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, input: dict[str, Any], result: dict[str, Any], duration_ms: int, status: str) -> None:
        pass

    def on_compensate_start(self, *, run_id: str, failed_step_id: str, compensate_count: int) -> None:
        pass

    def on_compensate_failed(self, *, run_id: str, step_id: str, tool_name: str, error: str, retry_count: int, next_retry_at: str | None) -> None:
        pass

    def on_compensate_finish(self, *, run_id: str, step_id: str, tool_name: str, status: str) -> None:
        pass

    def on_circuit_open(self, *, run_id: str | None, circuit_key: str, failure_count: int, open_window_seconds: int) -> None:
        pass

    def on_circuit_half_open(self, *, run_id: str | None, circuit_key: str) -> None:
        pass

    def on_circuit_closed(self, *, run_id: str | None, circuit_key: str, probe_success: bool) -> None:
        pass

    def on_retry_before(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, attempt: int, next_delay_ms: int, error_type: str) -> None:
        pass

    def on_retry_after(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, attempt: int, status: str) -> None:
        pass

    def on_retry_exhausted(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, total_attempts: int, last_error: str) -> None:
        pass

    def on_suspend(self, *, run_id: str, node_name: str, prompt: str, token: dict[str, Any]) -> None:
        pass

    def on_resume(self, *, run_id: str, node_name: str, user_input: dict[str, Any], resumed_from_seq: int) -> None:
        pass


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
        """注册插件，按 priority 升序；同名禁止。

        Args:
            plugin: 实现 ``PluginProtocol`` 的实例。
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
        """按名调用插件 ``on_{event}``；单个插件异常只记日志。

        Args:
            event: 如 agent_start、llm_after_call。
            **kwargs: 原样传给钩子。

        依赖:
            ``PluginProtocol``；由 ``Agent._emit_and_journal`` / 熔断器调用。
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

"""patterns 包内部共享辅助。

Q15=A：仅 2 个辅助函数：
1. _make_agent()：基于内核 Agent 构造函数 + 正确参数默认值快速造出角色 Agent
2. _validate_artifact_schema()：artifact JSON Schema 校验（从 multi_agent.py copy 简化版）

注意：参数表严格从 atlas/core/agent.py L37-L60 copy，避免默认值漂移（用户规则 6）。
"""
from __future__ import annotations

import json
from typing import Any

import jsonschema  # P-0 已依赖

from atlas import Agent, LLM, Toolkit
from atlas.core.circuit_breaker import CircuitBreakerRegistry
from atlas.core.plugin import PluginManager
from atlas.core.terminator import Terminator, make_default_terminator
from atlas.journal.base import JournalProtocol
from atlas.render import PlainRenderer, Renderer


def _make_agent(
    role_name: str,
    system_prompt: str,
    llm: LLM,
    tools: Toolkit,
    *,
    max_steps: int = 10,
    shared_renderer: Renderer | None = None,
    shared_terminator: Terminator | None = None,
    shared_plugin_manager: PluginManager | None = None,
    shared_circuit_breaker_registry: CircuitBreakerRegistry | None = None,
    shared_journal: JournalProtocol | None = None,
    compact_threshold: int | None = 8000,
    auto_inject_spawn_tool: bool = False,
    role_configs: dict | None = None,
) -> Agent:
    """基于 atlas Agent 内核构造一个角色 Agent。

    所有 shared_* 为 None 时走 Agent.__init__ 默认值（不破坏内核行为）。

    参数 copy 自 atlas/core/agent.py Agent.__init__ 签名（L37-L60），仅做参数适配：
      Agent(llm, tools, system_prompt, max_steps, renderer, terminator, plugin_manager,
            journal, role_configs, parent_trace, auto_inject_spawn_tool, tool_specs_pool,
            compact_threshold, transcript, circuit_breaker_registry, auto_default_registry)
    """
    renderer = shared_renderer or PlainRenderer()
    terminator = shared_terminator or make_default_terminator(max_steps)
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt=system_prompt,
        max_steps=max_steps,
        renderer=renderer,
        terminator=terminator,
        plugin_manager=shared_plugin_manager,  # None 时 Agent 内部自动造空 PluginManager
        journal=shared_journal,  # None 时不记录事件
        role_configs=role_configs,  # Worker/Critic = None，Planner 透传
        parent_trace=None,  # PWC 的 Planner/Worker/Critic 都是顶层 new Agent（无嵌套守卫）
        auto_inject_spawn_tool=auto_inject_spawn_tool,
        tool_specs_pool=None,
        compact_threshold=compact_threshold,
        transcript=None,  # Agent 默认自建 Transcript
        circuit_breaker_registry=shared_circuit_breaker_registry,  # None 时 Agent 自动造空 Registry
        auto_default_registry=(shared_circuit_breaker_registry is None),
    )


def _validate_artifact_schema(artifact: str, schema: dict) -> tuple[bool, str | None]:
    """artifact JSON Schema 校验。返回 (是否通过, 错误说明/None)。

    artifact 非合法 JSON 也视为不通过。不改写 artifact 原文。
    空 artifact 或空 schema 直接返回 (True, None)。
    """
    if not artifact or not schema:
        return (True, None)
    try:
        parsed = json.loads(artifact) if isinstance(artifact, str) else artifact
        jsonschema.validate(parsed, schema)
        return (True, None)
    except json.JSONDecodeError as e:
        return (False, f"artifact 非合法 JSON: {e}")
    except jsonschema.ValidationError as e:
        return (False, f"artifact schema 校验失败: {e.message}")

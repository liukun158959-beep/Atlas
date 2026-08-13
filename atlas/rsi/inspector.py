"""自省模块：Agent 读取自身状态做自进化决策。

Q5=A：SelfSnapshot dataclass（system_prompt / tool_definitions / skill_summaries / recent_failures / memory_summary）
Q10=A：Agent.inspect_self() 委托给 build_self_snapshot()
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SelfSnapshot:
    """Agent 自身状态快照。

    inspect_self() 的返回值 + propose_diff() 的输入（Phase 4.2 用）。
    """

    system_prompt: str                           # 当前 system prompt
    tool_definitions: list[dict] = field(default_factory=list)  # 当前工具定义（name + description + parameters）
    skill_summaries: list[str] = field(default_factory=list)    # Skill Library 中所有 skill 的描述摘要
    recent_failures: list[str] = field(default_factory=list)    # 最近 N 条失败案例摘要
    memory_summary: str = ""                     # 当前 memory 的 compact summary（如有）


def build_self_snapshot(agent: Any) -> SelfSnapshot:
    """从 Agent 读取各字段构造 SelfSnapshot。

    agent 需有以下属性：
    - memory.messages: list[dict]（含 role=system 的消息）
    - tools: Toolkit（有 to_openai_tools() 方法）
    - skill_library: SkillLibrary | None
    - failure_memory: FailureMemory | None
    - compact_manager: CompactManager | None
    """
    # system_prompt：从 memory.messages 中找 role=system
    system_prompt = ""
    for msg in agent.memory.messages:
        if msg.get("role") == "system":
            system_prompt = msg.get("content", "")
            break

    # tool_definitions：从 toolkit.to_openai_tools() 提取 name + description + parameters
    tool_definitions: list[dict] = []
    if hasattr(agent, "tools") and agent.tools is not None:
        for tool_def in agent.tools.to_openai_tools():
            fn = tool_def.get("function", tool_def)
            tool_definitions.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": fn.get("parameters", {}),
            })

    # skill_summaries：从 skill_library 读取所有 skill 描述
    skill_summaries: list[str] = []
    skill_lib = getattr(agent, "skill_library", None)
    if skill_lib is not None:
        for skill in skill_lib.list_all():
            skill_summaries.append(f"{skill.name} v{skill.version}: {skill.description}")

    # recent_failures：从 failure_memory 读取最近失败案例
    recent_failures: list[str] = []
    failure_mem = getattr(agent, "failure_memory", None)
    if failure_mem is not None:
        for case in failure_mem.list_recent(10):
            recent_failures.append(
                f"[{case.error_type}] {case.tool_name}: {case.error_message}"
            )

    # memory_summary：从 compact_manager 读取（如有 compact summary）
    memory_summary = ""
    compact_mgr = getattr(agent, "compact_manager", None)
    if compact_mgr is not None:
        # 从 memory.runtime_messages 中找 Summary 类型消息
        for msg in getattr(agent.memory, "runtime_messages", []):
            if msg.get("role") == "system" and "summary" in msg.get("content", "").lower():
                memory_summary = msg.get("content", "")
                break

    return SelfSnapshot(
        system_prompt=system_prompt,
        tool_definitions=tool_definitions,
        skill_summaries=skill_summaries,
        recent_failures=recent_failures,
        memory_summary=memory_summary,
    )

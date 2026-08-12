"""YAML 声明式配置加载工具。

将 examples/parallel_subagent_config.yaml 风格的配置（roles / parent / task）
解析为内核可用的 RoleConfig 对象，替代 demo 中硬编码的 RoleConfig 构造。

设计原则（用户规则 7）：必需字段缺失直接报错，不提供静默默认值，
从源头消除 fallback 触发场景。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from atlas.core.multi_agent import RoleConfig


def load_parallel_subagent_config(
    path: str,
) -> tuple[dict[str, RoleConfig], dict, str]:
    """读取并行子 Agent 声明式 YAML 配置。

    Args:
        path: YAML 配置文件路径。

    Returns:
        (roles_dict, parent_cfg, task_str) 三元组：
        - roles_dict: 角色名 -> RoleConfig
        - parent_cfg: 父 Agent 原始配置 dict
          （含 system_prompt_template / llm_config / max_steps，由调用方解读）
        - task_str: 演示任务文本

    Raises:
        RuntimeError: 必需字段缺失时抛出，错误信息含具体字段名。
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise RuntimeError(
            f"YAML 配置根节点必须是映射，实际类型: {type(raw).__name__}"
        )

    # ---- roles 节点：逐角色构造 RoleConfig ----
    roles_raw = _require(raw, "roles")
    roles: dict[str, RoleConfig] = {}
    for role_name, cfg in roles_raw.items():
        cfg = cfg or {}
        loc = f"roles.{role_name}"
        llm_cfg = _require(cfg, "llm_config", where=loc)
        roles[role_name] = RoleConfig(
            name=role_name,
            system_prompt=_require(cfg, "system_prompt", where=loc),
            tool_names=_require(cfg, "tools", where=loc),
            temperature=_require(
                llm_cfg, "temperature", where=f"{loc}.llm_config"
            ),
            max_steps=_require(llm_cfg, "max_steps", where=f"{loc}.llm_config"),
            timeout_ms=cfg.get("timeout_ms", 60_000),
        )

    # ---- parent 节点：原样返回 dict，由调用方解读 ----
    parent_cfg = _require(raw, "parent")

    # ---- task 节点：演示任务文本 ----
    task = _require(raw, "task")

    return roles, parent_cfg, task


def _require(container: Any, key: str, where: str = "") -> Any:
    """从 container 取必需字段，缺失时抛 RuntimeError 带具体字段名。"""
    if not isinstance(container, dict) or key not in container:
        loc = f"（位置: {where}）" if where else ""
        raise RuntimeError(f"YAML 配置缺少必需字段 '{key}'{loc}")
    return container[key]

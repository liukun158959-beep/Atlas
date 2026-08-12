"""基于内核原语的并行子 Agent demo。

对比旧版 examples 层自实现的 ParallelParentAgent，本 demo 全部使用内核 API：
  - atlas.core.Agent（自动注入 spawn_subagents 工具）
  - atlas.utils.yaml_loader（声明式 YAML 加载角色三元组）
  - tool_specs_pool=BUILTIN_TOOLS（全局工具池按 role.tool_names 过滤）
  - Trace 三段式递归渲染 + 并行效率 meta，均由内核负责

用法：
    python examples/parallel_subagent_demo.py
    python examples/parallel_subagent_demo.py --plain   # 纯文本渲染
    python examples/parallel_subagent_demo.py --config path/to/config.yaml

运行前确保已配置 .env（python -m atlas init）。
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from atlas import Agent, LLM, Toolkit
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS
from atlas.utils.yaml_loader import load_parallel_subagent_config


def main() -> None:
    load_dotenv()

    # --plain 沿用 sys.argv 判定；--config 走 argparse，二者互不干扰
    parser = argparse.ArgumentParser(description="并行子 Agent demo（声明式 YAML 配置）")
    parser.add_argument(
        "--config",
        default="examples/parallel_subagent_config.yaml",
        help="角色/父 Agent/任务 YAML 配置路径",
    )
    args, _ = parser.parse_known_args()
    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()

    # 1. 从 YAML 声明式加载角色、父 Agent 配置、任务文本（替代硬编码 RoleConfig）
    roles, parent_cfg, task = load_parallel_subagent_config(args.config)

    # 2. 父 Agent 构造：把 role_configs + tool_specs_pool 注入内核
    #    内核自动做三件事：
    #      a) spawn_subagents 工具注入 Toolkit
    #      b) schema 懒加载 role enum（Q19）
    #      c) 子 Agent 按 tool_names 从 BUILTIN_TOOLS 过滤工具池（EP-2 解耦）
    llm = LLM()
    toolkit = Toolkit()
    agent = Agent(
        llm=llm,
        tools=toolkit,
        system_prompt=parent_cfg["system_prompt_template"].format(
            known_roles=list(roles.keys())
        ),
        max_steps=parent_cfg["max_steps"],
        renderer=renderer,
        role_configs=roles,
        tool_specs_pool=BUILTIN_TOOLS,
    )

    renderer.rule("并行子 Agent Demo · Phase 2 内核原语版")
    renderer.print(f"可用角色: {list(agent.role_configs.keys())}")
    renderer.print(f"可用工具池: {[s['name'] for s in BUILTIN_TOOLS]}")
    renderer.print()
    renderer.markdown(task, title="任务")

    # 3. 父 Agent 自动走 ReAct → 调 spawn_subagents → parallel_gather → 汇总
    #    Trace.render + 最终答案渲染均由 Agent.run 内部自动完成
    answer = agent.run(task)
    _ = answer  # 仅供调试断点；最终答案已由 renderer.markdown(title='最终答案') 输出


if __name__ == "__main__":
    main()

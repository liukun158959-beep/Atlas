"""基于内核原语的并行子 Agent demo。

对比旧版 examples 层自实现的 ParallelParentAgent，本 demo 全部使用内核 API：
  - atlas.core.Agent（自动注入 spawn_subagents 工具）
  - atlas.core.RoleConfig（声明式角色三元组）
  - tool_specs_pool=BUILTIN_TOOLS（全局工具池按 role.tool_names 过滤）
  - Trace 三段式递归渲染 + 并行效率 meta，均由内核负责

用法：
    python examples/parallel_subagent_demo.py
    python examples/parallel_subagent_demo.py --plain   # 纯文本渲染

运行前确保已配置 .env（python -m atlas init）。
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from atlas import Agent, LLM, RoleConfig, Toolkit
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS


def main() -> None:
    load_dotenv()

    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()

    # 1. 声明式角色定义：Role 三元组（system_prompt + tool_names + llm 配置）
    roles = [
        RoleConfig(
            name="分析员",
            system_prompt=(
                "你是分析员，负责对给定主题进行结构化分析并直接给出结论。"
                "如果主题涉及数字计算，调用 calculator；涉及时间则调用 get_current_time。"
                "不要展开子问题，直接输出最终 artifact（简明段落 + 要点列表）。"
            ),
            tool_names=["calculator", "get_current_time"],
            temperature=0.2,
            max_steps=4,
            timeout_ms=60_000,
        ),
        RoleConfig(
            name="研究员",
            system_prompt=(
                "你是研究员，负责对给定主题列出 3~5 条关键事实或信息。"
                "如果需要获取当前时间，调用 get_current_time。"
                "输出严格是要点列表，不写解释性文字。"
            ),
            tool_names=["get_current_time"],
            temperature=0.3,
            max_steps=4,
            timeout_ms=60_000,
        ),
    ]

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
        system_prompt=(
            "你是任务编排 Agent。对于包含多个独立子主题的复合任务，"
            "主动调用 spawn_subagents 工具一次性并行启动多个子 Agent，"
            "不要逐个串行。收到子 Agent 的结构化 artifact 列表后，"
            "汇总生成最终答案。"
        ),
        max_steps=10,
        renderer=renderer,
        role_configs={r.name: r for r in roles},
        tool_specs_pool=BUILTIN_TOOLS,
    )

    # 3. 典型复合任务：3 个子主题，适合 fan-out 并行
    task = (
        "请并行完成以下 3 项工作：\n"
        "  1) 由分析员分析：计算 123 + 456 的结果，并给出 3 种常见应用场景\n"
        "  2) 由研究员列出：现在是什么时间（使用工具），以及时间管理的 3 条核心原则\n"
        "  3) 由分析员分析：计算 365 * 24，并简述该数值的含义（年度小时数）\n"
        "最后汇总三个结果，给出结构化总结。"
    )

    renderer.rule("并行子 Agent Demo · Phase 2 内核原语版")
    renderer.print(f"可用角色: {list(agent.role_configs.keys())}")
    renderer.print(f"可用工具池: {[s['name'] for s in BUILTIN_TOOLS]}")
    renderer.print()
    renderer.markdown(task, title="任务")

    # 4. 父 Agent 自动走 ReAct → 调 spawn_subagents → parallel_gather → 汇总
    #    Trace.render + 最终答案渲染均由 Agent.run 内部自动完成
    answer = agent.run(task)
    _ = answer  # 仅供调试断点；最终答案已由 renderer.markdown(title='最终答案') 输出


if __name__ == "__main__":
    main()

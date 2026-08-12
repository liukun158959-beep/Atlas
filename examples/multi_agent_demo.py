"""多 Agent 协作示例（Planner-Worker-Critic 架构）。

运行前确保已配置 .env（python -m atlas init）。

终端渲染：
    默认 RichRenderer（Markdown/Panel/Table，推荐 Windows Terminal 使用）
    --plain  切换 PlainRenderer（纯文本，任何环境/重定向文件都能跑）
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from atlas import LLM, Toolkit
from atlas.core.orchestrator import MultiAgentOrchestrator
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS


def main() -> None:
    load_dotenv()

    # 渲染器选择：和 quickstart / parallel_subagent_demo 保持一致
    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()

    llm = LLM()
    toolkit = Toolkit()
    for spec in BUILTIN_TOOLS:
        toolkit.register(**spec)

    # 把 renderer 注入 orchestrator，内部所有输出统一走 Renderer 抽象
    orchestrator = MultiAgentOrchestrator(
        llm=llm,
        tools=toolkit,
        max_revisions=2,
        renderer=renderer,
    )

    # 一个需要规划+执行+审查的复合任务
    task = (
        "帮我计算 2024 年是否是闰年（用 366 * 24 算总小时数），"
        "并告诉我现在几点，最后总结这两件事的关联"
    )

    result = orchestrator.run(task)

    # 最终答案用 Markdown 渲染
    renderer.markdown(result, title="最终答案")

    # 执行轨迹用 MultiAgentTrace.render（Plan 表格 + Worker/Critic 面板）
    orchestrator.trace.render(renderer)


if __name__ == "__main__":
    main()

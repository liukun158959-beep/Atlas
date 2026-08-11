"""多 Agent 协作示例。

运行前确保已配置 .env（python -m atlas init）。
"""
from __future__ import annotations

from dotenv import load_dotenv

from atlas import LLM, Toolkit
from atlas.core.multi_agent import MultiAgentOrchestrator
from atlas.tools.builtin import BUILTIN_TOOLS


def main() -> None:
    load_dotenv()

    llm = LLM()
    toolkit = Toolkit()
    for spec in BUILTIN_TOOLS:
        toolkit.register(**spec)

    orchestrator = MultiAgentOrchestrator(llm=llm, tools=toolkit, max_revisions=2)

    # 一个需要规划+执行+审查的复合任务
    task = "帮我计算 2024 年是否是闰年（用 366 * 24 算总小时数），并告诉我现在几点，最后总结这两件事的关联"
    print("=" * 60)
    print(f"任务: {task}")
    print("=" * 60)

    result = orchestrator.run(task)

    print("\n" + "=" * 60)
    print("最终答案:")
    print("=" * 60)
    print(result)

    print("\n" + "=" * 60)
    print("执行轨迹:")
    print("=" * 60)
    print(orchestrator.trace)


if __name__ == "__main__":
    main()

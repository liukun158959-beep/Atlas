"""Atlas 快速开始示例。

运行前请在项目根目录准备 .env 文件：
    ATLAS_API_KEY=sk-xxxx
    ATLAS_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
    ATLAS_MODEL=qwen-plus

启用终端 Markdown 渲染（粗体/列表/代码块上色）：
    把 Agent 构造处的 renderer=RichRenderer() 注释取消即可。
"""
from __future__ import annotations
import sys

from dotenv import load_dotenv

from atlas import Agent, LLM, Toolkit
from atlas.render import PlainRenderer,RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS


def main() -> None:
    # 从 .env 加载配置
    load_dotenv()

    llm = LLM()

    # 批量注册内置工具
    toolkit = Toolkit()
    for spec in BUILTIN_TOOLS:
        toolkit.register(**spec)

    system_prompt = (
        "你是通用助手 Atlas。遇到需要计算或获取时间的问题时，"
        "请主动调用对应工具，不要凭空猜测。"
    )
     # 用法一：默认纯文本输出（任何环境都能跑，适合重定向到文件）
    #agent = Agent(llm=llm, tools=toolkit, system_prompt=system_prompt)
     # 用法二：终端 Markdown 渲染（粗体/列表/代码块全部生效，推荐 Windows Terminal 使用）
    # 取消注释下面这行即可启用，无需改其他任何代码
    #agent = Agent(llm=llm, tools=toolkit, system_prompt=system_prompt, renderer=RichRenderer())

    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()
    agent = Agent(llm=llm, tools=toolkit, system_prompt=system_prompt, renderer=renderer)
    
    # 演示三类任务：单工具、纯计算、多步工具调用
    questions = [
        "现在是几点？",
        "帮我算一下 (123 + 456) * 2 等于多少",
        "先告诉我现在几点，然后计算 365 * 24 等于多少",
    ]

    for q in questions:
         # 问题和分隔线统一走 renderer，保持输出风格一致
        agent.renderer.print()
        agent.renderer.print(f"\n[User]   {q}")
        answer = agent.run(q)
        # Step 5.3：Agent.run 内部已自动渲染 Trace + 最终答案，外部不再手动遍历

        # 重置记忆与轨迹，开始下一轮独立任务
        agent.reset()


if __name__ == "__main__":
    main()

"""Agent 内核：基于 Function Calling 的 ReAct 循环。

每一步：
    1. LLM 读取当前 memory 与可用工具进行推理
    2. 若返回 tool_calls，执行对应工具并把结果回填到 memory，进入下一步
    3. 若无 tool_calls，则视为 LLM 已给出最终答案
"""
from __future__ import annotations

from ..context.trace import StepRecord, Trace
from ..llm.base import LLM
from ..memory.base import Memory
from ..render import PlainRenderer, Renderer
from ..tools.base import Toolkit


class Agent:
    """通用 Agent。"""

    def __init__(
        self,
        llm: LLM,
        tools: Toolkit,
        system_prompt: str | None = None,
        max_steps: int = 10,
        renderer: Renderer | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        # 渲染器：默认 PlainRenderer（纯文本），注入 RichRenderer 后支持 Markdown
        self.renderer: Renderer = renderer or PlainRenderer()
        self.memory = Memory(system_prompt=system_prompt)
        self.trace = Trace()

    def run(self, user_input: str) -> str:
        """运行一次任务，返回最终回答。"""
        self.memory.add_user(user_input)
        openai_tools = self.tools.to_openai_tools() or None

        for step_no in range(1, self.max_steps + 1):
            assistant = self.llm.chat(self.memory.messages, tools=openai_tools)
            self.memory.add_assistant(assistant)

            tool_calls = assistant.get("tool_calls") or []
            record = StepRecord(
                step=step_no,
                thought=assistant.get("content") or "",
                tool_calls=tool_calls,
            )

            # 没有工具调用，说明 LLM 已给出最终答案
            if not tool_calls:
                self.trace.add(record)
                final = assistant.get("content") or ""
                # 用 renderer 输出最终答案（PlainRenderer 退化为 print，RichRenderer 渲染 Markdown）
                self.renderer.markdown(final, title="最终答案")
                return final

            # 执行所有工具调用，结果回填给 LLM 作为下一步的观察
            for call in tool_calls:
                fn = call["function"]
                try:
                    result = self.tools.execute(fn["name"], fn["arguments"])
                except Exception as e:
                    # 工具执行失败是有效信息，回填给 LLM 让其自行调整
                    result = f"工具执行失败: {e}"
                self.memory.add_tool(result, call["id"])
                record.observations.append(result)

            self.trace.add(record)

        return f"Agent 在 {self.max_steps} 步内未完成任务。"

    def reset(self) -> None:
        """重置会话记忆与轨迹，开始新任务。"""
        self.memory.clear()
        self.trace.reset()

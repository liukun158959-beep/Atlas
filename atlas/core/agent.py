"""Agent 内核：基于 Function Calling 的 ReAct 循环。

每一步：
    1. LLM 读取当前 memory 与可用工具进行推理
    2. 若返回 tool_calls，执行对应工具并把结果回填到 memory，进入下一步
    3. 若无 tool_calls，则视为 LLM 已给出最终答案
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from ..context.trace import StepRecord, Trace
from ..llm.base import LLM
from ..memory.base import Memory
from ..render import PlainRenderer, Renderer
from ..tools.base import Toolkit

if TYPE_CHECKING:
    from .multi_agent import RoleConfig


class Agent:
    """通用 Agent。"""

    def __init__(
        self,
        llm: LLM,
        tools: Toolkit,
        system_prompt: str | None = None,
        max_steps: int = 10,
        renderer: Renderer | None = None,
        # 以下为 Step 2.2 并行子 Agent 新增参数（末尾 + 默认值，不破坏现有调用）
        role_configs: dict[str, "RoleConfig"] | None = None,
        parent_trace: "Trace | None" = None,
        auto_inject_spawn_tool: bool = True,
        tool_specs_pool: list[dict] | None = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        # 渲染器：默认 PlainRenderer（纯文本），注入 RichRenderer 后支持 Markdown
        self.renderer: Renderer = renderer or PlainRenderer()
        self.memory = Memory(system_prompt=system_prompt)
        self.trace = Trace()
        # Step 2.2 并行子 Agent 字段
        self.role_configs: dict[str, RoleConfig] = role_configs or {}
        self.tool_specs_pool: list[dict] = tool_specs_pool or []
        self.auto_inject_spawn_tool = auto_inject_spawn_tool
        # 告诉 Trace 自己是不是子 Agent（Q17：嵌套守卫）
        self.trace._parent_trace = parent_trace
        # 有初始 role 时构造即注入 spawn_subagents 工具（子 Agent 会在内部快速返回）
        if self.role_configs and self.auto_inject_spawn_tool:
            self._inject_spawn_tool()

    # ------------------------------------------------------------------
    # spawn_subagents 工具注入（Step 2.2 一次性写完，含优化）
    # ------------------------------------------------------------------

    def _inject_spawn_tool(self) -> None:
        """把 spawn_subagents 工具注册进 Toolkit（幂等 + 子 Agent 快速返回）。"""
        # 优化：子 Agent（有父 Trace）根本不注入 spawn 工具 → LLM 看不到该选项
        # → 从源头消除 Q17 嵌套误调用，func 内部的 RuntimeError 是第二道防线
        if self.trace._parent_trace is not None:
            return
        # 防重复注入
        if "spawn_subagents" in self.tools._tools:
            return
        # 方法内部延迟导入：避免 agent ↔ multi_agent 的模块级循环引用
        from .multi_agent import make_spawn_subagents_tool

        name, desc, params_factory, func = make_spawn_subagents_tool(self)
        tool = self.tools.register(
            func=func,
            name=name,
            description=desc,
            parameters=params_factory(),   # 首次注册用静态占位
        )
        # Q19 懒加载：后续每轮 to_openai_tools 调用重新读取 role_configs enum
        tool.parameters_factory = params_factory

    def register_role(self, config: "RoleConfig") -> None:
        """动态注册一个角色。第一个 role 注册时触发 spawn_subagents 工具注入。"""
        was_empty = not self.role_configs
        self.role_configs[config.name] = config
        if was_empty and self.role_configs and self.auto_inject_spawn_tool:
            self._inject_spawn_tool()

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
                # Step 5.2：仅顶层 Agent 自动渲染 Trace（子 Agent 的 trace 由父 Trace 递归渲染，避免重复）
                if self.trace._parent_trace is None:
                    self.trace.render(self.renderer, depth=0)
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

        # Step 5.2：超时（max_steps 耗尽）前同样仅顶层渲染
        if self.trace._parent_trace is None:
            self.trace.render(self.renderer, depth=0)
        return f"Agent 在 {self.max_steps} 步内未完成任务。"

    def reset(self) -> None:
        """重置会话记忆与轨迹，开始新任务。"""
        self.memory.clear()
        self.trace.reset()

"""上下文层：执行轨迹记录，便于调试与回放。"""
from __future__ import annotations

from dataclasses import dataclass, field

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..render.base import Renderer


_MAX_RENDER_DEPTH = 16   # 递归深度上限，理论上不会超过，但保险避免栈溢出


@dataclass
class StepRecord:
    """单个推理步骤的记录。"""

    step: int
    thought: str  # 本步 LLM 输出的文本内容
    tool_calls: list[dict] = field(default_factory=list)  # 本步发起的工具调用
    observations: list[str] = field(default_factory=list)  # 对应的工具返回结果

    def render(self, renderer: "Renderer") -> None:
        """渲染单步推理-行动-观察三元组。"""
        title = f"Step {self.step}"
        if self.thought and len(self.thought) <= 40:
            title = f"Step {self.step}  ·  {self.thought[:40]}"
        renderer.rule(title)
        if self.thought:
            renderer.markdown(self.thought)
        for i, call in enumerate(self.tool_calls):
            fn = call.get("function", {}) or {}
            action_line = f"> 调用工具  {fn.get('name')}"
            arg = fn.get("arguments")
            obs = self.observations[i] if i < len(self.observations) else None
            # meta 显示 arguments
            renderer.panel(
                obs if obs else "(无观察结果)",
                action_line,
                border_style="green" if obs else "yellow",
                arguments=str(arg) if arg else "",
                result_length=str(len(obs)) if obs else "0",
            )


class Trace:
    """执行轨迹容器，按步聚合推理-行动-观察三元组。"""

    def __init__(self) -> None:
        self.steps: list[StepRecord] = []
        # Step 2.1 新增：父子 Trace 嵌套（Q5 + Q17）
        self.subagent_traces: list["Trace"] = []
        self._parent_trace: "Trace | None" = None
        # Step 5.1 正式追加 meta：存 role/task/duration_ms/parallel_efficiency/spawn_counts 等
        self.meta: dict = {}

    def add(self, record: StepRecord) -> None:
        self.steps.append(record)

    def reset(self) -> None:
        self.steps.clear()
        # 重置时同步清嵌套引用，避免持有旧对象
        self.subagent_traces.clear()
        self._parent_trace = None
        self.meta.clear()

    def render(self, renderer: "Renderer", depth: int = 0) -> None:
        """三段式递归渲染：头部+meta → steps → subagent_traces（带深度限制）。"""
        # ---- 深度兜底：理论上 Q17 守卫保证 ≤2，但防循环/滥用 ----
        if depth > _MAX_RENDER_DEPTH:
            renderer.print(f"(渲染深度超过 {_MAX_RENDER_DEPTH}，已截断)")
            return

        indent = "  " * depth
        # ------------------------------------------------------------------
        # 段 1：头部 + meta 面板（子/根 Trace 不同标题策略）
        # ------------------------------------------------------------------
        is_sub = self._parent_trace is not None
        meta = self.meta or {}
        # 标题：子 Agent 用 meta 里的 subagent_id+role，根 Trace 用固定文案
        if is_sub:
            head_title = (
                f"{indent}子 Agent  {meta.get('subagent_id', '?')}  "
                f"[{meta.get('role', 'unknown_role')}]"
            )
        else:
            head_title = f"{indent}执行轨迹（根 Agent）"
        # meta 里的关键字段转为 panel 的 **meta kv
        panel_meta: dict[str, str] = {}
        for k in ["task", "duration_ms", "wall_ms", "total_sub_ms",
                  "parallel_efficiency_ratio", "schema_passed", "schema_error"]:
            if k in meta and meta[k] is not None:
                panel_meta[k] = str(meta[k])
        if meta.get("spawn_counts"):
            panel_meta["spawn_counts"] = str(meta["spawn_counts"])
        if meta.get("error"):
            panel_meta["error"] = str(meta["error"])

        head_body = meta.get("task", "") if is_sub else ""
        # 根 Trace 段 1 不用 panel 包裹（避免和 StepRecord.panel 嵌套过深）
        renderer.rule(head_title)
        if head_body:
            renderer.print(head_body)
        if panel_meta:
            # 根 meta 简要打印 key=value；子 Trace 用 panel 突出
            if is_sub:
                renderer.panel(
                    "meta: " + ", ".join(f"{k}={v}" for k, v in panel_meta.items()),
                    title="状态",
                    border_style="blue",
                )
            else:
                renderer.print("｜ " + " ｜ ".join(f"{k}={v}" for k, v in panel_meta.items()) + " ｜")

        # ------------------------------------------------------------------
        # 段 2：steps
        # ------------------------------------------------------------------
        for s in self.steps:
            s.render(renderer)

        # ------------------------------------------------------------------
        # 段 3：递归渲染子 Agent traces
        # ------------------------------------------------------------------
        for sub in self.subagent_traces:
            sub.render(renderer, depth=depth + 1)

    def __str__(self) -> str:
        if not self.steps:
            return "(无执行轨迹)"
        lines: list[str] = []
        for r in self.steps:
            lines.append(f"=== Step {r.step} ===")
            if r.thought:
                lines.append(f"[Thought]  {r.thought}")
            for i, call in enumerate(r.tool_calls):
                fn = call.get("function", {})
                lines.append(f"[Action]   {fn.get('name')}: {fn.get('arguments')}")
                if i < len(r.observations):
                    lines.append(f"[Observe]  {r.observations[i]}")
        return "\n".join(lines)

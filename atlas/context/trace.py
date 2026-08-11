"""上下文层：执行轨迹记录，便于调试与回放。"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class StepRecord:
    """单个推理步骤的记录。"""

    step: int
    thought: str  # 本步 LLM 输出的文本内容
    tool_calls: list[dict] = field(default_factory=list)  # 本步发起的工具调用
    observations: list[str] = field(default_factory=list)  # 对应的工具返回结果


class Trace:
    """执行轨迹容器，按步聚合推理-行动-观察三元组。"""

    def __init__(self) -> None:
        self.steps: list[StepRecord] = []

    def add(self, record: StepRecord) -> None:
        self.steps.append(record)

    def reset(self) -> None:
        self.steps.clear()

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

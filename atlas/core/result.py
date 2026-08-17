"""Agent 循环的结构化运行结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .suspend import SuspendToken


@dataclass
class RunResult:
    """一次 Agent 运行的结构化结果。run() 仍返回 final_answer 字符串以兼容 demo。"""

    final_answer: str
    run_id: str
    status: str  # success / terminated / suspended / running
    suspend_token: SuspendToken | None = None
    suspend_prompt: str | None = None
    events: list[Any] = field(default_factory=list)

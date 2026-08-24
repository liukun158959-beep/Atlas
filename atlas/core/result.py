"""Agent 循环的结构化运行结果。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .suspend import SuspendToken


@dataclass
class RunResult:
    """一次 Agent 运行的结构化结果。

    ``Agent.run()`` 只返回 ``final_answer`` 字符串；挂起/续跑请看本对象。

    Args:
        final_answer: 最终文本（终止或挂起时也可能是提示语）。
        run_id: Journal 主键。
        status: success / terminated / suspended / running。
        suspend_token: 挂起时的恢复凭证。
        suspend_prompt: 展示给调用方的确认文案。

    依赖:
        ``SuspendToken``；由 ``AgentLoop`` 构造。
    """

    final_answer: str
    run_id: str
    status: str  # success / terminated / suspended / running
    suspend_token: SuspendToken | None = None
    suspend_prompt: str | None = None
    events: list[Any] = field(default_factory=list)

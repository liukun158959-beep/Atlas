"""Phase 3.2：Suspend/Resume Durable HITL 原语（Graph 层 Q1=A）。

设计（grill-me 20 项冻结）：
  Q1=A 抽象层级：Graph 层做 HITL，Agent 通过 react_graph 桥接获得
  Q2=A 交互模式：同步阻塞（graph.run() 挂起后返回挂起态 GraphResult，由调用方显式调 resume）
  Q5=B 触发方式：SuspendNode(prompt_fn) 专用节点类（和 ParallelNode 同风格 Q13=B）
  Q6=A SuspendToken 内容：SuspendToken(run_id, node_name, seq) 对齐 Journal event sourcing
  Q10=A 挂起超时：可选 suspend_timeout=None（当前不阻塞线程下暂无作用，仅保留签名）
  Q13=(1)A prompt_fn(state) -> str 动态生成提示文本
  Q13=(2)B resume 时 state.update(user_input)，和 Phase 3.1 节点输出 merge 语义一致 Q4=A
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


__all__ = ["SuspendToken", "SuspendTimeout", "SuspendNode"]


@dataclass
class SuspendToken:
    """恢复 token（Q6=A：run_id + node_name + seq）。

    run_id 标识 Graph 运行、node_name 标识挂起节点、seq 标识 Journal 事件位置。
    graph.resume(token, user_input) 用它从 Journal 恢复 state 到挂起点。
    """

    run_id: str
    node_name: str
    seq: int


class SuspendTimeout(Exception):
    """graph.run() 的 suspend_timeout 超时异常（Q10=A 预留）。

    超时不丢失状态：graph_suspend 事件已写入 Journal，
    调用方可事后用 token 调 resume() 继续。
    """

    def __init__(self, token: SuspendToken, prompt: str, timeout_sec: float) -> None:
        self.token = token
        self.prompt = prompt
        self.timeout_sec = timeout_sec
        super().__init__(
            f"Suspend 超时 {timeout_sec}s 未 resume。"
            f" token={token}, prompt='{prompt[:50]}...'。"
            f" 可事后调 graph.resume(token, user_input) 恢复。"
        )


class SuspendNode:
    """挂起节点（Q5=B：SuspendNode 专用类，和 ParallelNode 同风格）。

    用法：
        g.add_node("wait_approval", SuspendNode(lambda s: f"请审批订单 {s['order_id']}"))
        # 执行到 wait_approval 节点时，Graph.run() 返回 status="suspended" 的 GraphResult
        # 调用方拿到 suspend_token 后显式调 graph.resume(token, user_input) 继续
        # resume 时 user_input merge 进 state，从 wait_approval 下一条边继续（不重跑 SuspendNode）

    语义（Q13）：
      - prompt_fn(state) -> str 动态生成提示文本（和 Phase 3.1 节点签名对齐）
      - __call__(state) -> {"__suspend__": prompt} 返回特殊标记（Q4=A 节点返回 dict）
      - 挂起节点只执行一次，resume 时不重跑（Q14=A 从下一条边继续）
    """

    def __init__(self, prompt_fn: Callable[[dict], str]) -> None:
        self.prompt_fn = prompt_fn

    def __call__(self, state: dict) -> dict:
        """节点签名 Callable[[dict], dict]，和普通函数节点 / ParallelNode 对齐。

        返回值是 `{"__suspend__": prompt_text}` 标记。
        Graph.run() 解释器检测到 `__suspend__` key 就挂起（见 graph.py Step 3）。
        """
        prompt = self.prompt_fn(state)
        return {"__suspend__": prompt}

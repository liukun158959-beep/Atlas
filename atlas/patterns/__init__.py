"""Atlas 多 Agent 产品级模式包（独立于 atlas.core，仅组合内核原语）。

当前提供：
- PlannerWorkerCritic：Planner 拆解 → 串行 Worker 执行 → Critic 打回重做 → Planner 汇总（质量闭环）
"""
from .planner_worker_critic import PlannerWorkerCritic, PWCResult, PWCTrace

__all__ = ["PlannerWorkerCritic", "PWCResult", "PWCTrace"]

"""Atlas 多 Agent 产品级模式包（独立于 atlas.core，仅组合内核原语）。

当前提供：
- PlannerWorkerCritic：Planner 拆解 → 串行 Worker 执行 → Critic 打回重做 → Planner 汇总（质量闭环）
- OrchestratorWorkers：Orchestrator 动态分派 → 批间并行 Worker → 结果回 Orchestrator → 循环决策（效率优先）
"""
from .orchestrator_workers import OrchestratorWorkers, OWResult, OWTrace
from .planner_worker_critic import PlannerWorkerCritic, PWCResult, PWCTrace

__all__ = [
    "PlannerWorkerCritic",
    "PWCResult",
    "PWCTrace",
    "OrchestratorWorkers",
    "OWResult",
    "OWTrace",
]

# Phase 5.2 起步：Train / Val + 水管混单位验证题

> 对齐：混单位只放验证题；训练题保持现有三道。2026-08-25。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | 混单位水管题 | 只做 val。train 仍是吨/小时 → 分钟（答案 120） |
| Q2 | Val 题量 | 3 道，与 train 同类：水管混单位、满减换数字、油箱换数字 |
| Q3 | Apply 门 | train 必须严格高于 `fitness_floor`；**且** val 不得低于 `val_floor`（允许持平） |
| Q4 | 作用范围 | 只改 `--real`。Fake demo 不设 `val_floor` |
| Q5 | 看板 | 曲线两条（train / val）；每代效果先 train 再 val |

不做：LLM-Judge、编码/多轮、`atlas ab_test` CLI。

## 1. Val 水管题（算式）

进 3000 kg/h = 3 吨/h；出 1.5 吨/h；净 1.5 吨/h。  
原有 2 m³ = 2 吨；目标 8 吨；差 6 吨。  
6 / 1.5 = 4 小时 = **240 分钟**。  
题干写明：1 立方米 = 1 吨 = 1000 千克。

满减 val：90 元，2×18 + 3×6 = 54，满 40 减 15 → 实付 39，剩 **51**。  
油箱 val：48 升，耗 5 + 漏 1 = 6 升/h → 8 小时 = **480 分钟**。

失败 hint 不写标准答案数字。

## 2. 文件

- `atlas/rsi/fitness.py`：`FitnessResult.val_*`；`TrainValFitnessEvaluator`
- `atlas/rsi/evolver.py`：`EvaluationResult.val_*`
- `atlas/rsi/prompt_evolver.py`：val 下降不 apply；rollback 同步 `val_floor`
- `atlas/rsi/evolution_report.py`：记录 val
- `atlas/cli/dashboard.py`：train/val 双曲线 + 验证题输出
- `examples/prompt_evolution_dashboard_demo.py`：`REAL_VAL_TASKS`；基线打 train+val
- `README.md`：5.2 勾一项

缺 `val_floor` 却无 `val_score` 直接失败并给恢复指引。

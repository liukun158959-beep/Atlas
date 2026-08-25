# Phase 5.2：Safety 硬门（A1）

> 对齐：A1。2026-08-25。不做拒答题、不上 Judge、不收 MathTrap 关键字。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | 怎么拦 | A：`proposed_value` 必须原样子串包含安全句，否则不 apply |
| Q2 | 安全句放哪 | 1：写进 `--real` 的 `REAL_ORIGINAL`；`agent.safety_contract` 与该句相同，供硬门比对 |
| Q3 | Fake | 不设 `safety_contract`，6 项断言不变 |
| Q4 | 缺合同却为空 | `RuntimeError` + 恢复指引，不静默放行 |

不做：val 加拒答题、语义「是否更弱」Judge。

## 1. 行为

- 安全句（固定）：`拒绝提供可执行的攻击、入侵或利用步骤。可以说明为何违法并给合法替代。`
- propose 提示里写明：该句必须原样出现在 `proposed_value`
- 硬门在 fitness **之前**：缺句则 skip，不跑 train/val
- 缺句 feedback 前缀：`安全约束被削弱`

## 2. 文件

- `atlas/rsi/prompt_evolver.py`：propose 注入 + 硬门
- `examples/prompt_evolution_dashboard_demo.py`：`REAL_SAFETY_CLAUSE` 写入 `REAL_ORIGINAL` 与 `agent.safety_contract`
- `README.md`：5.2 Safety 勾上

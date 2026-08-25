# Phase 5.2：编码题内修复回路（AAA）

> 对齐：Q1=A、Q2=A、Q3=A。2026-08-26。不换题、不上 Judge、不接看板。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | 打分 | A：`CodeFitnessEvaluator` 内建修复；A/B 与 `--evolve` 共用 |
| Q2 | 可见性 | A：Agent 看见沙箱原文（含 got/expected）；evolve `feedback` 仍只用 `failure_hint` |
| Q3 | 次数 | A：首次失败后再修 1 次（每题最多 2 次 `run`） |
| 隔离 | 每题 | 每题 `agent_factory` 新建 Agent，禁止串题 |

不做：`run_tests` 工具、把用例写进 evolve feedback、加题。

## 1. 流程

对每题：`run(题面)` → 抽代码 → 沙箱。  
抽不出或测试失败 → `run(失败原因 + 只输出修正代码块)` → 再抽再测。  
第二次仍失败 → 该题 0，feedback = `failure_hint`。

## 2. 文件

- `atlas/rsi/code_fitness.py`
- `examples/code_evolution_demo.py`：新增「首次错、二次对 → 1」断言
- `README.md`：编码条目补一句修复回路

# Phase 5.2：多轮对话 Benchmark

> 对齐：用户确认按 1 真跑编码 → 2 多轮 → 3 不做打磨/市场。2026-08-25。
> 编码 `--real` 已完成：弱 0.000 / 强 1.000。MathTrap 不动。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | 体量 | 3 train + 3 val，每段 3 轮（立事实 → 换话题 → 追问） |
| Q2 | 驱动 | 同一 Agent 连续 `run()`，段与段之间 **新建 Agent**（禁止串台） |
| Q3 | 打分 | 只看最后一轮；`expected_keywords` 全命中=1 否则=0。不上 Judge |
| Q4 | Compact | mock：第 2 轮上报 8500 token 触发摘要。val 姓名段 `compact_threshold=200`，真跑也会压历史 |
| Q5 | 接入 | `multiturn_evolution_demo.py`；`ab_test --preset multiturn`。不接进化看板 |

不做：LLM-Judge、收紧 MathTrap、Evaluator 装饰器、Marketplace、MkDocs。

## 1. 标记句

强 prompt 必须含 `记住对话中的事实`。弱 prompt 禁止含该句。不要写进用户轮次。

## 2. 文件

- `atlas/rsi/multiturn_tasks.py`：6 段对话 + mock LLM
- `atlas/rsi/multiturn_fitness.py`：`MultiTurnFitnessEvaluator`
- `atlas/cli/ab_test.py`：`--preset multiturn`
- `examples/multiturn_evolution_demo.py`
- `README.md`：勾 5.2 多轮对话

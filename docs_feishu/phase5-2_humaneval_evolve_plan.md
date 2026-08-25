# Phase 5.2：HumanEval 接到 Prompt 进化（BAA）

> 对齐：Q1=B、Q2=A、Q3=A。2026-08-25。MathTrap 看板 `--real` 不动。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | 入口 | B：`--plain` / `--real`（A/B）不动；加 `--evolve` 与 `--evolve --real` |
| Q2 | 初始 prompt | A：编码弱 prompt + 与 MathTrap **同一句** `safety_contract` |
| Q3 | mock | A：第 1 代提案含「只输出一个 python 代码块」→ train/val 0→1 → apply |

不做：改看板、Judge、`atlas evolve` CLI、换题集、改 Apply 门。

## 1. 接线

- `TrainValFitnessEvaluator(HUMANEVAL_TRAIN, HUMANEVAL_VAL, factory, inner_cls=CodeFitnessEvaluator)`
- 基线评估弱 prompt（无标记句）→ train=0 / val=0；`fitness_floor` / `val_floor` 取基线
- `agent.safety_contract` = 与 dashboard 相同的安全句
- mock FakeLLM：propose 走 JSON；答题复用 `HumanEvalMockLLM`

## 2. 文件

- `examples/code_evolution_demo.py`：`--evolve` / `--evolve --real`
- `docs_feishu/phase5-2_humaneval_evolve_plan.md`（本文件）
- `README.md`：补运行命令

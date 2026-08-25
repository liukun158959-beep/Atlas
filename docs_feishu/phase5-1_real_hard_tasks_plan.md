# Phase 5.1 补丁：真模型难题 + 必须提升才 apply

> grill-me 对齐：换题 / 用户题允许写步骤 / 分数必须高于当前 prompt 才 apply。2026-08-24。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | `--real` 题集 | 丢掉 192 / Ada JSON / 180。换成 DUP 式陷阱题。用户题不写「只输出数字」，也不写「请逐步计算」（策略放 system prompt） |
| Q2 | 打分 | 仍用 `TaskSetFitnessEvaluator` 关键字。失败 feedback 用 `failure_hint`（讲方法，不把标准答案数字写进反馈，避免 60→120 奖励黑客） |
| Q3 | apply | `FITNESS_THRESHOLD=0.5` 不变。`--real` 先评估当前 prompt，写入 `baseline_fitness` + `fitness_floor`。`evolve_prompt` 仅在该字段已设置时要求 `score > fitness_floor`。Fake 不设字段，行为不变 |
| Q4 | 回滚 | `rollback_to` 若启用了 `fitness_floor`：0 代回到 `baseline_fitness`，N 代回到该代评估分 |

不做：改 Fake demo 序列；LLM-as-Judge；动 complex_benchmark 里写错的 answer。

## 1. 真跑过的题（qwen-plus）

弱 prompt「尽量短、不要解释」vs 强 prompt「标陷阱、逐步算」：

| 题 | 弱 | 强 | 是否进 `--real` |
|---|---|---|---|
| 水池分钟（应 120） | 60 | 120 | 是 |
| 满减余额（应 31） | 1 元 | 31 | 是 |
| 油箱耗+漏换分钟（应 360） | 同结构第三题 | 是 |
| 调和平均 48 / 银盒 / IFEval [[ANS]] | 弱也过 | 否 |

## 2. 文件

- `atlas/rsi/prompt_evolver.py`：apply 增加 floor 门；rollback 同步 floor
- `atlas/rsi/fitness.py`：任务可选 `failure_hint`
- `examples/prompt_evolution_dashboard_demo.py`：换 `REAL_TASKS`；`--real` 先打基线再进化；`max_steps=6`
- `README.md`：`--real` 用法补一行

## 3. apply 伪代码

```
floor = getattr(agent, "fitness_floor", None)
if score < 0.5: 不 apply
elif leakage: 不 apply
elif floor is not None and score <= floor: 不 apply，passed=False，feedback 写明未超过当前
else: apply；若 floor is not None：fitness_floor = score
```

## 4. 看板补丁（看效果，不只看 diff）

进化页中间栏原先只渲染 unified diff，看不到「改完之后模型答了什么」。补：

- `TaskSetFitnessEvaluator` 把截断后的实际输出写入 `task_results`
- `EvaluationResult.task_results` 随 `evolve_prompt` 带上
- `publish_evolution` 附带 `baseline`（分数 + 每题输出）
- 曲线含基线点；中间栏先渲染每题 PASS/FAIL + 输出，diff 收进折叠

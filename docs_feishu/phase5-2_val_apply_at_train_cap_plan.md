# Phase 5.2：train 满分时 val 升高可 apply

> 对齐：B。2026-08-25。真跑基线 train=1.0 / val=0.333，提案 val→1.0 却因 `score > floor` 全 skip。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q0 | 方向 | B：不改题集 |
| Q1 | 例外触发 | `fitness_floor == 1.0` **且** 提案 train `== 1.0` **且** 已启用 `val_floor` |
| Q2 | val | 必须 **严格高于** `val_floor`（持平仍 skip） |
| Q3 | 非满分持平 | train=0.67 持平、即使 val 升高，仍 skip |
| Q4 | 其它门 | `score >= 0.5`、越狱仍拦 |
| Q5 | 范围 | 只影响启用了 `val_floor` 的 `--real`。Fake 不设 val_floor，行为不变 |

不做：加难题、LLM-Judge、任意 train 持平也可 apply。

## 1. Apply 门（改后）

1. train < 0.5 或越狱 → skip（不变）
2. train 未过 floor，**除非** 走 Q1+Q2 例外 → skip
3. val < val_floor → skip（不变）
4. 否则 apply；`fitness_floor` / `val_floor` 写成这一代的 train / val

缺 `val_score` 而 `val_floor` 已启用：`RuntimeError` + 恢复指引。

满分持平但 val 没升高：feedback 前缀 `训练集已满分且验证集未升高`。

## 2. 文件

- `atlas/rsi/prompt_evolver.py`：例外判断 + 新 skip 前缀
- `README.md`：5.2 Train/Val 那条补一句
- Fake demo 不改；`--real` 预期第 1 代 apply（val 0.333→1.0）

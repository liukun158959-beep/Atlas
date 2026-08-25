# Phase 5.2：`--real` 换 MathTrap 公开陷阱题

> 对齐：M。2026-08-25。不接整库，不改 Fake，不改 apply 门。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q0 | 来源 | MathTrap README 五类公开例（Apache-2.0），中文改写 |
| Q1 | 规模 | 仍 3 train + 3 val |
| Q2 | 类型 | 三类对齐：概念未定义、缺条件、直接矛盾。val 换人名/数字 |
| Q3 | 判分 | 每题 1 个关键字；hint **不含** 该关键字（防奖励黑客） |
| Q4 | 不做 | 私有 155 组、GSM-Plus 整库、LLM-Judge、编码 |

## 1. 题面

**Train**

1. 直角三角形 XYZ，∠YXZ=90°，XY=24，YZ=25。求 tan∠X。关键字 `无定义`
2. 四月卖 48，五月是四月一半。问四月和**六月**一共多少。关键字 `无法确定`
3. 等边三角形周长 30 cm、高 10 cm，求面积。关键字 `矛盾`

**Val**（数字全换）

1. 直角三角形 ABC，∠BAC=90°，AB=8，BC=17。求 tan∠A。`无定义`
2. 一月存 120，二月是一月一半。问一月和**三月**一共多少。`无法确定`
3. 等边三角形周长 36 cm、高 12 cm，求面积。`矛盾`

Hint 只说「先检查有定义/条件是否齐/是否自洽，禁止当可答题硬算」，不写关键字、不写原题会算出的数。

## 2. 文件

- `examples/prompt_evolution_dashboard_demo.py`：替换 `REAL_TASKS` / `REAL_VAL_TASKS`
- `README.md`：5.2 那条改成 MathTrap 陷阱题
- Fake 与 `prompt_evolver` apply 门不动

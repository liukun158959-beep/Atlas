# Phase 5.2：`atlas ab_test`（PFS）

> 对齐：P + F + S。2026-08-25。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | 题 | `--preset mathtrap`：6 道（train+val 合成一套） |
| Q2 | prompt | `--prompt-a` / `--prompt-b` 两个 utf-8 文件，空文件失败 |
| Q3 | 统计 | 每套 prompt 跑 n 轮，输出 **均值 + 样本标准差**。不做 p 值 |
| Q4 | n | 默认 3，必须 ≥ 1；n=1 时标准差打印为 `n/a`（不算 0） |
| Q5 | Fake | `--mock`：题面关键字答；强 prompt 须含标记 `识别逻辑陷阱` |

不做：从进化报告抠 prompt、scipy、显著性 p 值、`--tasks json`。

## 1. 命令

```
python -m atlas ab_test --preset mathtrap --prompt-a a.txt --prompt-b b.txt [--n 3] [--mock] [--plain]
```

每轮对每个 prompt 用 `TaskSetFitnessEvaluator` 打 6 题，score = 通过数/6。

## 2. 文件

- `atlas/rsi/mathtrap_tasks.py`：6 道题（dashboard `--real` 改 import）
- `atlas/rsi/ab_test.py`：`ab_test_prompts` + mock LLM
- `atlas/cli/ab_test.py` + `__main__.py`
- `atlas/render/plain.py`：`SilentRenderer`（CLI 与 dashboard demo 共用）
- `examples/ab_test_demo.py`：Fake 断言 + 调 CLI `--mock`
- `README.md`：5.2 勾上

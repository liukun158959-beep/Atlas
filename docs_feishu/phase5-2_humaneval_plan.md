# Phase 5.2：编码 Benchmark（HPD）

> 对齐：H + P + D。2026-08-25。MathTrap `--real` 不动。

## 0. 冻结决策

| # | 决策 | 选择 |
|---|---|---|
| Q1 | 题 | HumanEval 手抄 3 train + 3 val，写进仓库（MIT 改编） |
| Q2 | 打分 | 抽出第一段 Python → 现有 subprocess 沙箱跑隐式用例。过=1 否则=0 |
| Q3 | 接入 | `code_evolution_demo.py`；`ab_test --preset humaneval` |
| Q4 | 不做 | 整库、pip human-eval、进程内 exec、LLM-Judge、格式分、推理分、换掉 MathTrap |

## 1. 题

Train：`has_close_elements` / `truncate_number` / `below_zero`  
Val：`intersperse` / `string_xor` / `count_distinct_characters`

题面不写测试期望值。`failure_hint` 讲方法，不写标准实现、不写用例数字。  
禁止 `from typing import List`（worker 无 import）；题面用 `list[float]` 等 3.11 内置泛型。  
浮点期望用二进制友好值（`0.25` / `0.5` / `0.75`），避免 `==` 误杀。

标记句只写在强 **system prompt**，不写进题面（避免弱 prompt 仍被用户消息带飞）。

## 2. 沙箱

扩展现有 worker，不新开执行通道：

`run_function_cases_in_sandbox(code, func_name, cases)`  
`cases = [{args: [...], expected: ...}, ...]` 全部 `func(*args)` 相等才 ok。  
tuple 返回 vs JSON list：按 list 比较。  
超时仍走现有 subprocess；失败 `ValueError`，禁止回退进程内 exec。

白名单可安全扩 `all`/`any`/`ord`/`chr` 等；不能放 `open` / `__import__`。

## 3. 抽代码

第一段 ` ```python ` 或 ` ``` `；没有代码块则找 `def {func_name}`。  
抽不出或函数不存在或任一用例失败 → 该题 0 分。

## 4. `--mock`

强 prompt 必须含 `只输出一个 python 代码块`。弱 prompt 禁止含该句。  
弱答散文；强答按函数名吐正确实现（围栏代码块）。

## 5. 文件

- `atlas/rsi/humaneval_tasks.py`：6 题 + mock LLM
- `atlas/rsi/code_fitness.py`：抽代码 + `CodeFitnessEvaluator`
- `atlas/rsi/fitness.py`：`TrainValFitnessEvaluator(inner_cls=...)`
- `atlas/rsi/sandbox.py` / `_sandbox_worker.py`：cases 模式
- `atlas/rsi/ab_test.py`：`evaluator_cls` 注入
- `atlas/cli/ab_test.py`：`--preset humaneval`
- `examples/code_evolution_demo.py`
- `README.md`：勾 5.2 编码 Benchmark

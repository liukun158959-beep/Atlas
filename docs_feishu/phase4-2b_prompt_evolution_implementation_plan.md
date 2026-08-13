# Phase 4.2b 实施计划：Prompt 进化 · 反思式变异 + 适应度 + Leakage Guardrails

> grill-me 3 轮共享理解冻结（16 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 4.1 / 4.2a 成功范式 + 用户规则）：
> 1. 每步独立验证：完成后立即有验证命令
> 2. 代码迁移先 copy 再改写（用户规则 6）
> 3. Windows 纯 Python 验证命令
> 4. 不考虑 fallback / 旧兼容（用户规则 7）
> 5. 不折中方案（用户规则 8）
> 6. 不重复造轮子：复用 4.2a 的 propose_diff/apply_diff/ArchiveStore（用户规则 1）

---

## 0. 决策速查表（16 项 grill-me 冻结）

### 0.1 范围与架构

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | 4.2b 范围 | A：最小版（单 lineage + 反思式变异 + 适应度评估，不做种群） |
| Q5 | FREE 层角色 | A：4.2b 不启用 FREE（prompt 属 BUFFERED，FREE 留给未来工具自生成） |
| Q6 | 文件结构 | B：2 新建（fitness.py + prompt_evolver.py）+ 2 修改（__init__.py + agent.py）+ 1 demo |
| Q17 | 提交策略 | A：1 次提交（代码 + demo + README 修正） |

### 0.2 适应度评估

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q2 | 评估方式 | B：FakeLLM mock 打分（demo 可复现）+ FitnessEvaluator Protocol 真实接口预留 |
| Q9 | Protocol 签名 | B：`evaluate(prompt) -> FitnessResult(score, feedback, task_results)` |
| Q13 | generation 状态 | A：Agent.generation: int = 0，apply 成功后 +1 |

### 0.3 反思式变异

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q3 | 变异策略 | A：复用 ArchiveStore（list_failed + list_passed 注入 propose prompt） |
| Q15 | fitness 历史注入 | A：从 list_passed 提取 score/feedback，格式化为"第 N 代 score=X feedback=Y" |

### 0.4 Leakage Guardrails

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q4 | 防御范围 | C：最小版正则黑名单（越狱模式匹配，命中即拒绝） |
| Q12 | 黑名单配置 | B：内置默认 ~10 个中英文越狱模式 + `extra_patterns` 可追加 |
| Q16 | 命中后 archive | A：archive 为 failed（EvaluationResult(passed=False, feedback="leakage detected")） |

### 0.5 Agent API

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q7 | 方法名 | B：新增 `agent.evolve_prompt()`（和 self_evolve 并列，内部复用 apply_diff） |
| Q11 | 返回值 | B：`EvolutionResult(generation, proposal, evaluation, leakage_check, applied)` |

### 0.6 Demo

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q8 | 断言数 | B：8 项 |
| Q14 | FakeLLM 序列 | A：6 次调用（#1-2 第 1 代低分 / #3-4 第 2 代高分 / #5-6 leakage 拦截） |

---

## 1. 文件变更清单

### 1.1 新建文件（3 个）

| 文件 | 说明 |
|------|------|
| `atlas/rsi/fitness.py` | FitnessEvaluator Protocol + FitnessResult dataclass + FakeFitnessEvaluator（确定性 mock 打分）+ TaskSetFitnessEvaluator（真实接口预留，跑任务集 + Evaluator） |
| `atlas/rsi/prompt_evolver.py` | LeakageGuard Protocol + LeakageCheckResult dataclass + RegexLeakageGuard（默认 ~10 个中英文越狱模式 + extra_patterns）+ EvolutionResult dataclass + evolve_prompt(agent) 闭环函数 + _build_evolution_prompt（注入 fitness 历史 + recent_failures） |
| `examples/prompt_evolution_demo.py` | Phase 4.2b demo（8 项断言，FakeLLM 6 次调用） |

### 1.2 修改文件（3 个）

| 文件 | 修改内容 |
|------|---------|
| `atlas/core/agent.py` | 新增 `fitness_evaluator` / `leakage_guard` / `generation` 构造参数 + `evolve_prompt()` 方法委托 rsi |
| `atlas/rsi/__init__.py` | 追加导出 FitnessEvaluator / FitnessResult / FakeFitnessEvaluator / LeakageGuard / LeakageCheckResult / RegexLeakageGuard / EvolutionResult / evolve_prompt |
| `README.md` | 核心特性追加 / 项目结构追加 / 快速开始追加 / 路线图 Phase 4.2b 打勾 + FREE 层描述修正 |

### 1.3 不改动文件

| 文件 | 理由 |
|------|------|
| `atlas/rsi/archive.py` | 4.2a 已稳定，leakage 命中复用 archive.add(proposal, evaluation) 接口 |
| `atlas/rsi/evolver.py` | 4.2a 已稳定，evolve_prompt 内部复用 propose_diff + apply_diff |
| `atlas/rsi/inspector.py` | SelfSnapshot 在 4.1 已完成 |
| `atlas/rsi/skill.py` | 4.2b 不动 skill |
| `atlas/core/plugin.py` | 不新增钩子 |

---

## 2. 实施步骤

### Step 1：atlas/rsi/fitness.py

最底层：适应度评估协议 + Fake 实现，无外部依赖（除 dataclass）。

内容：
- `FitnessResult` dataclass（score: float / feedback: str / task_results: list = []）
- `FitnessEvaluator` Protocol（`evaluate(prompt: str) -> FitnessResult`）
- `FakeFitnessEvaluator`：按内部 `_call_count` 返回预设序列（第 1 次低分 0.4 / 第 2 次高分 0.8 / 第 3 次低分 0.3），demo 100% 可复现
- `TaskSetFitnessEvaluator`：真实接口预留（跑任务集 + ArtifactSchemaEvaluator 打分），4.2b 不实现细节，只留 Protocol 实现骨架

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi.fitness import FitnessEvaluator, FitnessResult, FakeFitnessEvaluator; f=FakeFitnessEvaluator(); print(f.evaluate('p1').score, f.evaluate('p2').score)"
```

### Step 2：atlas/rsi/prompt_evolver.py

依赖 Step 1（fitness）+ 4.2a（archive/evolver/inspector）。

内容：
- `LeakageCheckResult` dataclass（passed: bool / matched_patterns: list[str]）
- `LeakageGuard` Protocol（`check(prompt: str) -> LeakageCheckResult`）
- `RegexLeakageGuard`：内置默认 ~10 个中英文越狱模式（ignore previous / 忽略以上 / you are now / 你现在是 / system override / jailbreak / DAN / 忽略所有规则 / disregard above / new instructions）+ `extra_patterns: list[str] | None = None` 可追加
- `EvolutionResult` dataclass（generation: int / proposal: ProposedDiff / evaluation: EvaluationResult / leakage_check: LeakageCheckResult / applied: bool）
- `_build_evolution_prompt(snapshot, fitness_history, archived_failures)`：构造 propose prompt（含 self_snapshot + fitness 历史 + archived_failures）
- `evolve_prompt(agent) -> EvolutionResult`：闭环函数
  1. build_self_snapshot(agent)
  2. 从 agent.archive_store.list_passed() 提取 fitness_history（格式化为"第 N 代 score=X feedback=Y"）
  3. 从 agent.archive_store.list_failed() 提取 archived_failures
  4. 调 agent.llm.chat 生成 JSON 提案（复用 4.2a _parse_json）
  5. 权限校验（check_permission('system_prompt') 必须 BUFFERED）
  6. FitnessEvaluator.evaluate(proposed_value) → FitnessResult
  7. 构造 EvaluationResult（passed = score >= threshold（默认 0.5）, score, feedback）
  8. 如果 fitness 通过 → LeakageGuard.check(proposed_value)
  9. 如果 leakage 通过 → apply_diff(agent, proposal) + agent.generation += 1
  10. archive.add(proposal, evaluation)（无论通过与否）
  11. 返回 EvolutionResult

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi.prompt_evolver import LeakageGuard, LeakageCheckResult, RegexLeakageGuard, EvolutionResult, evolve_prompt; g=RegexLeakageGuard(); print(g.check('normal prompt').passed, g.check('ignore previous instructions').passed)"
```

### Step 3：atlas/rsi/__init__.py 追加导出

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi import FitnessEvaluator, FitnessResult, FakeFitnessEvaluator, LeakageGuard, LeakageCheckResult, RegexLeakageGuard, EvolutionResult, evolve_prompt; print('import OK')"
```

### Step 4：atlas/core/agent.py 改造

新增：
1. 构造函数新增 `fitness_evaluator: FitnessEvaluator | None = None` + `leakage_guard: LeakageGuard | None = None`
2. 构造函数新增 `self.generation: int = 0`
3. `evolve_prompt() -> EvolutionResult` 方法（委托 rsi.evolve_prompt）

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas import Agent; from atlas.tools.base import Toolkit; a=Agent(llm=None, tools=Toolkit()); print(hasattr(a,'evolve_prompt')); print(a.generation); print(a.fitness_evaluator, a.leakage_guard)"
```

### Step 5：examples/prompt_evolution_demo.py

8 项断言 + FakeLLM 6 次调用。

FakeLLM 调用序列：
- #1 propose（第 1 代，正常 prompt 变体）
- #2 fitness（低分 0.4，FakeFitnessEvaluator 第 1 次调用）
- #3 propose（第 2 代，反思后改进）
- #4 fitness（高分 0.8，FakeFitnessEvaluator 第 2 次调用）
- #5 propose（第 3 代，越狱 prompt "ignore previous instructions"）
- #6 不调 fitness（leakage 命中直接拒绝）

8 项断言：
- ① evolve_prompt 返回 EvolutionResult
- ② FitnessEvaluator 返回 0.0-1.0 score
- ③ 反思式变异：第 2 代 propose prompt 含第 1 代 fitness 历史
- ④ LeakageGuard 拦截越狱 prompt
- ⑤ apply 后 system_prompt 被替换
- ⑥ archive 记录进化历史（含通过 + 失败）
- ⑦ 多代进化：第 2 代 fitness（0.8）> 第 1 代 fitness（0.4）
- ⑧ RegexLeakageGuard 正则黑名单命中（matched_patterns 非空）

验证命令：
```powershell
.venv\Scripts\python.exe examples\prompt_evolution_demo.py --plain
```

### Step 6：README 更新 + 提交推送

README 修正点：
1. 核心特性追加 4.2b（FitnessEvaluator / LeakageGuard / evolve_prompt / RegexLeakageGuard）
2. 项目结构追加 fitness.py / prompt_evolver.py / prompt_evolution_demo.py
3. 快速开始追加 prompt_evolution_demo
4. 路线图 Phase 4.2b 打勾
5. FREE 层描述修正（4.2b 实际在 BUFFERED 层，FREE 留给未来工具自生成）

---

## 3. 验证矩阵

| 层级 | 内容 | 通过标准 |
|------|------|---------|
| V1 | import + 基本构造 | 无报错 |
| V2 | demo 8 项断言 | 8/8 通过 |
| V3 | 18 个老 demo 回归 | 全通过（4.2a rsi_evolution + 17 个老 demo） |

---

## 4. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Agent __init__ 改动引入回归 | 低 | 老 demo 失败 | 新参数 None 默认，零侵入 |
| FakeFitnessEvaluator 调用序列错位 | 中 | demo 断言失败 | _call_count 精确映射（第 1 次 0.4 / 第 2 次 0.8） |
| RegexLeakageGuard 误伤正常 prompt | 低 | demo 断言失败 | 默认模式只匹配明确越狱短语，正常 prompt 不含 |
| fitness 历史注入 prompt 过长 | 低 | LLM token 超限 | 只取最近 5 代 + 截断 feedback 到 100 字符 |
| evolve_prompt 和 self_evolve 语义混淆 | 低 | 用户调用错 | README 明确区分（evolve_prompt 专 prompt / self_evolve 综合） |

---

## 5. 交付依赖图

```
Step 1 (fitness.py) ──┐
                       ├──> Step 2 (prompt_evolver.py) ──> Step 3 (__init__.py) ──> Step 4 (agent.py) ──> Step 5 (demo) ──> V1+V2+V3 ──> Step 6 (README + 提交)
4.2a (archive/evolver) ┘
```

Step 1 和 4.2a 既有代码可并行读取，Step 2 依赖 Step 1 + 4.2a。后续步骤严格串行。

# Phase 2.4 实施计划：OrchestratorWorkers 动态分派模式包

> grill-me 4 轮共享理解冻结（19 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 Phase 2.3 PWC 成功范式 + 用户规则）：
> 1. 每步独立验证：完成后立即有验证命令，不做"半成品等下一步"
> 2. 代码迁移先 copy 再改写：从 planner_worker_critic.py copy 结构再改写（用户规则 6）
> 3. Windows 纯 Python 验证命令
> 4. 不考虑 fallback / 旧兼容，没有老用户（用户规则 7）
> 5. 不折中方案（用户规则 8）
> 6. 不重复造轮子：复用 _common._make_agent + _extract_nested_trace（用户规则 1）

---

## 0. 决策速查表（19 项 grill-me 冻结）

### 0.1 抽象层级与执行方式

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | 抽象层级 | A：Python while 循环（和 PWC 一致，Graph 原语留给用户自行组装） |
| Q2 | Worker 执行方式 | A：一批并行 + 批间串行（Orchestrator 每轮分派 K 个 Worker 并行执行，结果全部回来后回 Orchestrator 决策下一轮） |
| Q6 | 并行实现方式 | A：ThreadPoolExecutor 直接并行（不走 parallel_gather，Worker 任务动态生成不适合预注册 RoleConfig） |

### 0.2 决策格式与容错

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q3 | Orchestrator 决策输出格式 | A：JSON 结构化输出 `{"action": "dispatch"/"synthesize", ...}` |
| Q11 | JSON 解析容错策略 | A：json.loads 失败则提取 `{...}` 片段；action/tasks/answer 缺失直接 ValueError（消除 fallback）；reasoning 可选 |

### 0.3 代码复用与构造函数

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q4 | 是否复用 _common.py | A：复用 _make_agent + _extract_nested_trace |
| Q5 | 构造函数必填参数 | A：2 项（llm + worker_tools），Orchestrator 不需要业务工具 |
| Q7 | 构造函数可选参数 | A：必填 2 + 可选 9 = 11 项（task / 2 prompt / max_iterations / max_concurrent_workers / max_steps_per_worker / circuit_breaker_registry / terminator / plugin_manager） |

### 0.4 终止与错误处理

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q8 | max_iterations 耗尽行为 | A：强制汇总（Python 侧强制走 synthesize 路径，不信任 LLM 会遵守约束） |
| Q14 | 强制汇总实现方式 | A：Python 侧检测到 iteration >= max_iterations 时直接构造 synthesize 请求，prompt 追加"已达上限必须汇总" |
| Q16 | 单个 Worker 执行失败 | A：partial success（失败 Worker 结果记 error 字符串，成功的正常收集，全部回 Orchestrator 决策） |

### 0.5 数据结构与 Prompt

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q9 | OWResult/OWTrace 数据结构 | A：4 个扁平 dataclass（WorkerResult / IterationLog / OWTrace / OWResult） |
| Q10 | 是否加 Critic | A：不加（OW 定位效率优先，PWC 已覆盖质量闭环） |
| Q12 | 内置 prompt 模板 | A：2 条（Orchestrator 含 iteration_history + latest_results；Worker 含 subtask_description + context_block），可覆写 |
| Q13 | 上下文传递方式 | A：每轮 Worker 结果自动累积到 context_parts，下轮 Orchestrator 和 Worker prompt 都从 context_parts 拼接 |
| Q17 | 第一轮初始输入 | A：iteration_history 填"(无，这是第一轮)"，latest_results 填"(用户原始任务: {task})" |

### 0.6 Demo 与文件结构

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q15 | demo 断言设计 | A：6 项（dispatch≥1轮 / 并行Worker≥2 / 最终synthesize / 答案关键字 / 上下文传递 / max_iterations强制汇总） |
| Q18 | FakeLLM 调用序列 | A：7 次调用（2轮dispatch含3个Worker + 1轮synthesize），另含max_iterations断言单独实例 |
| Q19 | 文件结构 + 导出 | A：orchestrator_workers.py + __init__.py追加导出 + 不进顶层 + demo（对齐 PWC） |

---

## 1. 文件变更清单

### 1.1 新建文件（3 个）

| 文件 | 说明 |
|------|------|
| `atlas/patterns/orchestrator_workers.py` | 主实现（OrchestratorWorkers 类 + 4 dataclass + 2 条 prompt + _parse_orchestrator_json） |
| `examples/orchestrator_workers_demo.py` | Demo（6 项断言，FakeLLM 7 次调用精确映射 + max_iterations 强制汇总单独实例） |
| `docs_feishu/phase2-4_orchestrator_workers_implementation_plan.md` | 本文档 |

### 1.2 修改文件（2 个）

| 文件 | 修改内容 |
|------|---------|
| `atlas/patterns/__init__.py` | 追加 `from .orchestrator_workers import OrchestratorWorkers, OWResult, OWTrace` + `__all__` 追加 |
| `README.md` | 核心特性追加 / 项目结构追加 / 快速开始追加 / 路线图 Phase 2.4 打勾 |

### 1.3 不改动文件

| 文件 | 理由 |
|------|------|
| `atlas/patterns/_common.py` | 复用现有 _make_agent + _extract_nested_trace，无需改动 |
| `atlas/__init__.py` | 模式包不进顶层导出（和 PWC 一致） |
| `atlas/core/*` | OW 只组合内核原语，不改内核 |

---

## 2. 实施步骤

### Step 1：新建 atlas/patterns/orchestrator_workers.py

**从 planner_worker_critic.py copy 结构再改写**（用户规则 6）。

内容：
1. 2 条内置 prompt（`_DEFAULT_ORCHESTRATOR_PROMPT` + `_DEFAULT_WORKER_PROMPT`）
2. 4 个 dataclass（`WorkerResult` / `IterationLog` / `OWTrace` / `OWResult`）
3. `OrchestratorWorkers` 主类（11 项构造参数 + `run()` 方法）
4. `_parse_orchestrator_json()` 解析函数（JSON 容错提取 + 缺字段 ValueError）

关键实现：
- `run()` 主循环：Python while `iteration < max_iterations`
- 每轮：Orchestrator Agent LLM → `_parse_orchestrator_json` → dispatch/synthesize 分支
- dispatch 分支：`tasks[:max_concurrent_workers]` 截断 → ThreadPoolExecutor 并行执行 Worker → 收集结果 → 累积上下文 → 下一轮
- synthesize 分支：break 循环 → 返回 OWResult
- 强制汇总：`iteration >= max_iterations` 时 Python 侧直接构造 synthesize 请求

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.patterns import OrchestratorWorkers, OWResult, OWTrace; print('import OK')"
```

### Step 2：atlas/patterns/__init__.py 追加导出

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.patterns import OrchestratorWorkers; print('export OK')"
```

### Step 3：examples/orchestrator_workers_demo.py

6 项断言 + FakeLLM 7 次调用 + max_iterations 强制汇总实例。

验证命令：
```powershell
.venv\Scripts\python.exe examples/orchestrator_workers_demo.py --plain
```

### Step 4：README.md 更新 + 提交推送

---

## 3. 验证矩阵

| 层级 | 内容 | 通过标准 |
|------|------|---------|
| V1 | import + 基本构造 | 无报错 |
| V2 | demo 6 项断言 | 6/6 通过 |
| V3 | 11 个老 demo 回归 | 全通过（不影响既有逻辑） |

---

## 4. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| FakeLLM 调用序列错位 | 中 | demo 断言失败 | 逐次调用编号注释 + 调用计数器断言 |
| ThreadPoolExecutor 并行时序不确定 | 低 | 并行断言偶发失败 | 断言只验证结果集不验证顺序 |
| JSON 解析 LLM 输出带 Markdown 包裹 | 中 | 解析失败 | _parse_orchestrator_json 提取 `{...}` 片段容错 |

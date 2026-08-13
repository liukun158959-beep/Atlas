# Phase 4.2a 实施计划：RSI 自修改闭环 + 分层权限

> grill-me 4 轮共享理解冻结（20 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 Phase 4.1 成功范式 + 用户规则）：
> 1. 每步独立验证：完成后立即有验证命令
> 2. 代码迁移先 copy 再改写（用户规则 6）
> 3. Windows 纯 Python 验证命令
> 4. 不考虑 fallback / 旧兼容（用户规则 7）
> 5. 不折中方案（用户规则 8）
> 6. 不重复造轮子：复用 4.1 的 Embedding/SkillLibrary/FailureMemory（用户规则 1）

---

## 0. 决策速查表（20 项 grill-me 冻结）

### 0.1 范围与架构

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | Phase 4.2 范围切分 | A：分 4.2a（propose_diff+evaluate+archive+权限）/ 4.2b（Prompt 进化） |
| Q14 | 文件结构 | A：atlas/rsi/evolver.py + atlas/rsi/archive.py 两个新文件 |
| Q20 | 交付物 | A：2 新建 + 2 修改 + 1 demo + README（和 4.1 对齐） |

### 0.2 propose_diff

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q2 | 作用域 | A：改 Skill code + system prompt，不改架构 |
| Q3 | 返回值结构 | A：ProposedDiff dataclass（target_type/target_id/current_value/proposed_value/reasoning/diff_summary）+ EvaluationResult dataclass（passed/score/feedback/test_result） |
| Q6 | 位置 | C：Agent 方法委托 rsi（和 inspect_self 一致） |
| Q7 | LLM prompt | A：JSON 结构化输出（含 self_snapshot + recent_failures + archived_failures） |

### 0.3 evaluate

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q4 | 评估方式 | A：测试执行（exec+函数存在）+ LLM Critic 双层（测试失败不调 LLM） |
| Q11 | Critic prompt | A：JSON 结构化输出（passed/score/feedback） |
| Q13 | 测试执行逻辑 | A：exec(code) + 验证同名函数存在（最小可用性验证，不调用 func） |

### 0.4 apply + archive

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q9 | apply_diff | A：自动生效（skill 新版本入库 version 递增 / system_prompt 替换 _runtime_messages[0]） |
| Q5 | archive 策略 | A：ArchiveStore 归档失败提案 + 下次 propose_diff 传给 LLM 避免重复 |
| Q10 | 闭环函数 | A：self_evolve() 封装 propose→evaluate→apply/archive 三步 |
| Q18 | prompt apply 安全 | A：验证 role==system + 替换 content，不清空历史 |

### 0.5 权限模型

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q8 | 三层定义 | A：PermissionLevel 枚举（CORE 只读 / BUFFERED 受控 / FREE 预留）+ check_permission() |

### 0.6 Agent 改造

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q12 | 新增构造参数 | A：只加 archive_store: ArchiveStore \| None = None（零侵入） |
| Q16 | 新增方法 | A：propose_diff() + self_evolve()（evaluate/apply 内部不暴露） |

### 0.7 Demo

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q15 | demo 断言 | A：7 项断言（propose / 权限校验 / 测试通过 / 测试拒绝 / Critic 通过 / apply 版本递增 / archive 失败反馈） |
| Q17 | FakeLLM 序列 | A：5 次调用（成功闭环 #1-2 + 失败闭环 #3-4 不调 LLM + archive 反馈 #5） |

### 0.8 后续

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q19 | 4.2a 后 | B：暂停问用户（4.2a 是独立里程碑） |

---

## 1. 文件变更清单

### 1.1 新建文件（3 个）

| 文件 | 说明 |
|------|------|
| `atlas/rsi/archive.py` | ArchivedProposal dataclass + ArchiveStore + PermissionLevel 枚举 + check_permission() |
| `atlas/rsi/evolver.py` | ProposedDiff + EvaluationResult dataclass + propose_diff() + evaluate() + apply_diff() + self_evolve() + _test_skill_code() + _parse_json() |
| `examples/rsi_evolution_demo.py` | Phase 4.2a demo（7 项断言，FakeLLM 5 次调用） |

### 1.2 修改文件（3 个）

| 文件 | 修改内容 |
|------|---------|
| `atlas/core/agent.py` | 新增 archive_store 构造参数 + propose_diff() + self_evolve() 方法 |
| `atlas/rsi/__init__.py` | 追加导出 ProposedDiff / EvaluationResult / ArchivedProposal / ArchiveStore / PermissionLevel / self_evolve |
| `README.md` | 核心特性追加 / 项目结构追加 / 快速开始追加 / 路线图 Phase 4.2a 打勾 |

### 1.3 不改动文件

| 文件 | 理由 |
|------|------|
| `atlas/rsi/skill.py` | SkillLibrary.register 已支持版本递增，无需改 |
| `atlas/rsi/failure.py` | 失败记忆在 4.1 已完成 |
| `atlas/rsi/inspector.py` | SelfSnapshot 在 4.1 已完成 |
| `atlas/core/plugin.py` | 不新增钩子 |

---

## 2. 实施步骤

### Step 1：atlas/rsi/archive.py

最底层：权限枚举 + 归档存储，无外部依赖。

内容：
- `PermissionLevel` 枚举（CORE / BUFFERED / FREE）
- `check_permission(target_type: str) -> PermissionLevel`
- `ArchivedProposal` dataclass（proposal + evaluation + archived_at）
- `ArchiveStore` 类（add / list_recent / list_failed / list_passed）

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi.archive import ArchiveStore, PermissionLevel, check_permission; print(check_permission('skill')); print(check_permission('graph'))"
```

### Step 2：atlas/rsi/evolver.py

依赖 Step 1（archive）+ 4.1（inspector/skill/failure）。

内容：
- `ProposedDiff` dataclass（target_type / target_id / current_value / proposed_value / reasoning / diff_summary）
- `EvaluationResult` dataclass（passed / score / feedback / test_result）
- `_parse_json(text) -> dict`：JSON 解析容错（json.loads 失败则提取 {...}，缺字段 ValueError）
- `_test_skill_code(code, skill_name) -> str | None`：exec + 函数存在检查
- `propose_diff(agent) -> ProposedDiff`：调 LLM 生成提案（权限校验 + prompt 含 snapshot/failures/archive）
- `evaluate(agent, proposal) -> EvaluationResult`：测试执行 + LLM Critic 双层
- `apply_diff(agent, proposal) -> None`：skill 新版本入库 / system_prompt 替换
- `self_evolve(agent, archive_store) -> EvaluationResult`：闭环函数

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi.evolver import ProposedDiff, EvaluationResult, propose_diff, evaluate, apply_diff, self_evolve; print('import OK')"
```

### Step 3：atlas/rsi/__init__.py 追加导出

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi import ProposedDiff, EvaluationResult, ArchiveStore, PermissionLevel, self_evolve; print('import OK')"
```

### Step 4：atlas/core/agent.py 改造

新增：
1. 构造函数新增 `archive_store: ArchiveStore | None = None`
2. `propose_diff() -> ProposedDiff` 方法（委托 rsi.propose_diff）
3. `self_evolve() -> EvaluationResult` 方法（委托 rsi.self_evolve）

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas import Agent; from atlas.tools.base import Toolkit; a=Agent(llm=None, tools=Toolkit()); print(hasattr(a,'propose_diff')); print(hasattr(a,'self_evolve')); print(a.archive_store)"
```

### Step 5：examples/rsi_evolution_demo.py

7 项断言 + FakeLLM 5 次调用。

验证命令：
```powershell
.venv\Scripts\python.exe examples\rsi_evolution_demo.py --plain
```

### Step 6：README 更新 + 提交推送

---

## 3. 验证矩阵

| 层级 | 内容 | 通过标准 |
|------|------|---------|
| V1 | import + 基本构造 | 无报错 |
| V2 | demo 7 项断言 | 7/7 通过 |
| V3 | 14 个老 demo 回归 | 全通过（4.1 rsi_demo + 13 个老 demo） |

---

## 4. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Agent __init__ 改动引入回归 | 低 | 老 demo 失败 | 新参数 None 默认，零侵入 |
| exec(proposed_value) 安全风险 | 低 | 代码注入 | 4.2a 只在 evaluate 内部 exec（用户主动调 self_evolve 才触发），4.2b 加沙箱 |
| LLM JSON 解析失败 | 中 | propose/evaluate 报错 | _parse_json 容错（json.loads 失败提取 {...}），缺字段 ValueError |
| apply_diff 替换 system_prompt | 低 | 对话历史丢失 | 只替换 _runtime_messages[0] content，不清空其他消息 |
| FakeLLM 调用序列错位 | 中 | demo 断言失败 | _step 计数器精确映射 5 次调用 |

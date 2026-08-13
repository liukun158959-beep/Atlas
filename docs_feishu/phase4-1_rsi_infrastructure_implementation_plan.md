# Phase 4.1 实施计划：RSI 自进化基础设施（Skill Library + 失败记忆 + inspect_self）

> grill-me 4 轮共享理解冻结（20 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 Phase 2.3/2.4/3.2 成功范式 + 用户规则）：
> 1. 每步独立验证：完成后立即有验证命令
> 2. 代码迁移先 copy 再改写（用户规则 6）
> 3. Windows 纯 Python 验证命令
> 4. 不考虑 fallback / 旧兼容（用户规则 7）
> 5. 不折中方案（用户规则 8）
> 6. 不重复造轮子：复用 Plugin/Embedding/Journal 基础设施（用户规则 1）

---

## 0. 决策速查表（20 项 grill-me 冻结）

### 0.1 范围与架构

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | Phase 4 范围切分 | A：分 4.1（感知+记忆层）/ 4.2（修改+验证层） |
| Q19 | 文件结构 | A：atlas/rsi/ 独立模块（和 patterns 同级，不进 core） |

### 0.2 Embedding 基础设施

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q2 | 向量存储选型 | D：可插拔 VectorStoreProtocol + SQLiteVectorStore 默认实现（不引入 Chroma 重依赖） |
| Q3 | Embedding 来源 | C：FakeEmbedding（确定性 hash，demo 用）+ OpenAIEmbedding（复用 LLM client，真实用） |
| Q9 | EmbeddingProtocol API | A：embed / embed_batch / dimension 三方法 |
| Q17 | FakeEmbedding hash 算法 | A：MD5 hash → 64 维归一化向量（跨进程确定性） |

### 0.3 Skill Library

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q4 | Skill 数据结构 | B：新建 Skill dataclass（独立于 Tool，含 code/description/embedding/version/fitness_score） |
| Q6 | SkillLibrary API | A：register / retrieve / get / list_all / latest_version |
| Q12 | Skill code 字段格式 | A：完整 def 语句字符串（可 exec() 执行） |
| Q13 | 入库闸门 | A：4.1 不做 Critic 验证（register 直接入库，Critic 留给 4.2） |
| Q18 | to_tool() 转换 | A：exec(code) → 提取同名函数 → 包装成 Tool（打通 SkillLibrary 和 Toolkit） |

### 0.4 失败记忆

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q7 | 失败记忆数据结构+采集 | A：FailureCase dataclass + FailureMemoryCollector(BasePlugin) 挂 3 钩子实时采集 |
| Q8 | VectorStoreProtocol API | A：add / query / get / delete / count + VectorHit(id, score, metadata) |
| Q14 | Plugin 采集逻辑 | A：on_tool_after_call(failed/circuit_rejected) + on_retry_exhausted + on_compensate_failed，入库即 embed |
| Q15 | Plugin 获取 Agent 上下文 | B：弱引用 weakref.ref(agent) → 读 agent.memory.messages |
| Q16 | 弱引用注入时机 | A：Agent __init__ 末尾遍历 plugins，hasattr(bind_agent) 自动注入 |

### 0.5 inspect_self + Agent 改造

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q5 | inspect_self 返回值 | A：SelfSnapshot dataclass（system_prompt / tool_definitions / skill_summaries / recent_failures / memory_summary） |
| Q10 | inspect_self 位置 | A：Agent 方法（委托给 atlas.rsi.build_self_snapshot()） |
| Q11 | Agent 构造函数新增参数 | A：skill_library: SkillLibrary \| None = None + failure_memory: FailureMemory \| None = None（零侵入） |

### 0.6 Demo

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q20 | demo 设计 | A：1 个 demo 8 项断言（SkillLibrary register/retrieve/版本化/to_tool + FailureMemory record/retrieve + Collector Plugin + inspect_self 空值 + FakeEmbedding 确定性） |

---

## 1. 文件变更清单

### 1.1 新建文件（7 个）

| 文件 | 说明 |
|------|------|
| `atlas/rsi/__init__.py` | 导出 SkillLibrary / Skill / FailureMemory / FailureCase / SelfSnapshot / FakeEmbedding / OpenAIEmbedding / VectorStoreProtocol / SQLiteVectorStore / EmbeddingProtocol / FailureMemoryCollector |
| `atlas/rsi/skill.py` | Skill dataclass + SkillLibrary 类 + to_tool() |
| `atlas/rsi/failure.py` | FailureCase dataclass + FailureMemory 类 + FailureMemoryCollector(BasePlugin) |
| `atlas/rsi/embedding.py` | EmbeddingProtocol + FakeEmbedding + OpenAIEmbedding |
| `atlas/rsi/vector_store.py` | VectorStoreProtocol + VectorHit + SQLiteVectorStore |
| `atlas/rsi/inspector.py` | SelfSnapshot dataclass + build_self_snapshot() |
| `examples/rsi_demo.py` | Phase 4.1 demo（8 项断言，FakeLLM + FakeEmbedding 双确定性） |

### 1.2 修改文件（2 个）

| 文件 | 修改内容 |
|------|---------|
| `atlas/core/agent.py` | 新增 skill_library/failure_memory 构造参数 + __init__ 末尾 bind_agent 遍历 + inspect_self() 方法 |
| `README.md` | 核心特性追加 / 项目结构追加 / 快速开始追加 / 路线图 Phase 4.1 打勾 |

### 1.3 不改动文件

| 文件 | 理由 |
|------|------|
| `atlas/core/plugin.py` | FailureMemoryCollector 是外部 Plugin 子类，不改 Protocol |
| `atlas/patterns/*` | RSI 独立于 patterns |
| `atlas/tools/*` | to_tool() 返回 Tool 对象，不改 Tool/Toolkit |

---

## 2. 实施步骤

### Step 1：atlas/rsi/embedding.py + vector_store.py

最底层基础设施，无外部依赖。

内容：
- `EmbeddingProtocol`（embed / embed_batch / dimension）
- `FakeEmbedding`（MD5 hash → 64 维，跨进程确定性）
- `OpenAIEmbedding`（复用 LLM.client.embeddings.create()，默认 text-embedding-3-small）
- `VectorStoreProtocol`（add / query / get / delete / count）
- `VectorHit` dataclass（id / score / metadata）
- `SQLiteVectorStore`（embedding 序列化 bytes 存 SQLite，检索时 numpy 余弦相似度 top-k）

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi import FakeEmbedding, SQLiteVectorStore; e=FakeEmbedding(); print(e.embed('hello')[:4]); vs=SQLiteVectorStore(); vs.add('x', e.embed('hello'), {'k':'v'}); print(vs.query(e.embed('hello'), 1)[0].id)"
```

### Step 2：atlas/rsi/skill.py

依赖 Step 1（embedding + vector_store）。

内容：
- `Skill` dataclass（name / description / code / embedding / version / fitness_score / created_at / metadata）
- `SkillLibrary` 类（register / retrieve / get / list_all / latest_version）
  - 持有 `VectorStoreProtocol` + `EmbeddingProtocol` 引用
  - register：embed(description) → 存 VectorStore + 内存 dict
  - retrieve：embed(query) → VectorStore.query → 映射回 Skill
- `Skill.to_tool()`：exec(code) → 提取同名函数 → 包装成 Tool

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi import SkillLibrary, Skill, FakeEmbedding, SQLiteVectorStore; sl=SkillLibrary(FakeEmbedding(), SQLiteVectorStore()); sl.register(Skill('sum','求和','def sum(a,b):\n    return a+b',[])); print(sl.list_all()[0].name)"
```

### Step 3：atlas/rsi/failure.py

依赖 Step 1（embedding + vector_store）+ atlas.core.plugin（BasePlugin）。

内容：
- `FailureCase` dataclass（run_id / step_id / tool_name / input_summary / error_message / error_type / context_summary / embedding / created_at）
- `FailureMemory` 类（record / retrieve / list_recent）
  - 持有 `VectorStoreProtocol` + `EmbeddingProtocol` 引用
  - record：embed(context_summary) → 存 VectorStore + 内存 dict
  - retrieve：embed(query) → VectorStore.query → 映射回 FailureCase
- `FailureMemoryCollector(BasePlugin)`：
  - name="failure_memory_collector" / priority=50
  - 持有 failure_memory + embedding 引用 + agent 弱引用
  - bind_agent(agent)：weakref.ref(agent)
  - on_tool_after_call / on_retry_exhausted / on_compensate_failed：构造 FailureCase → record
  - _record()：从 agent 弱引用读 system_prompt + user_input → context_summary → embed → record

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi import FailureMemory, FakeEmbedding, SQLiteVectorStore; fm=FailureMemory(FakeEmbedding(), SQLiteVectorStore()); print('OK')"
```

### Step 4：atlas/rsi/inspector.py + __init__.py

依赖 Step 2 + Step 3。

内容：
- `SelfSnapshot` dataclass（system_prompt / tool_definitions / skill_summaries / recent_failures / memory_summary）
- `build_self_snapshot(agent) -> SelfSnapshot`：从 agent 读取各字段
- `atlas/rsi/__init__.py`：导出全部公共类

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas.rsi import SkillLibrary, Skill, FailureMemory, FailureCase, SelfSnapshot, FakeEmbedding, OpenAIEmbedding, SQLiteVectorStore, FailureMemoryCollector; print('import OK')"
```

### Step 5：atlas/core/agent.py 改造

依赖 Step 4（rsi 模块）。

修改：
1. 构造函数新增 `skill_library: SkillLibrary | None = None` + `failure_memory: FailureMemory | None = None`
2. `__init__` 末尾遍历 plugins，`hasattr(plugin, "bind_agent")` 则调 `plugin.bind_agent(self)`
3. 新增 `inspect_self() -> SelfSnapshot` 方法，委托 `atlas.rsi.build_self_snapshot(self)`

验证命令：
```powershell
.venv\Scripts\python.exe -c "from atlas import Agent; a=Agent(llm=None, tools=None); print(hasattr(a, 'inspect_self'))"
```

### Step 6：examples/rsi_demo.py

8 项断言 + FakeLLM + FakeEmbedding 双确定性。

验证命令：
```powershell
.venv\Scripts\python.exe examples/rsi_demo.py --plain
```

### Step 7：README 更新 + 提交推送

---

## 3. 验证矩阵

| 层级 | 内容 | 通过标准 |
|------|------|---------|
| V1 | import + 基本构造 | 无报错 |
| V2 | demo 8 项断言 | 8/8 通过 |
| V3 | 12 个老 demo 回归 | 全通过（不影响既有逻辑） |

---

## 4. 风险矩阵

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Agent __init__ 改动引入回归 | 中 | 老 demo 失败 | 新参数全 None 默认，零侵入 |
| SQLiteVectorStore 并发安全 | 低 | 数据损坏 | threading.Lock 包裹读写（和 SQLiteJournal 一致） |
| FakeEmbedding 向量退化 | 低 | 检索无区分度 | MD5 hash 保证不同文本不同向量 |
| exec(code) 安全风险 | 低 | 4.1 不安全执行 | 4.1 只在用户主动调用 to_tool() 时 exec，4.2 加沙箱 |
| weakref Agent 被 GC | 低 | Collector 读 None | Agent 持有 PluginManager 持有 Plugin，不会先于 Agent 被 GC |

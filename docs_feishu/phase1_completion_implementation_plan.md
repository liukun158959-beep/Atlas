# Phase 1 补完实施计划

> 基于 grill-me 4 轮 21 决策设计树生成。设计树已闭合，无未声明假设。

## 决策总表

| # | 决策 | 选择 | 理由 |
|---|------|------|------|
| Q1 | 优先级排序 | A: Attachment→Transcript→Compact→project→Scope→Dashboard | 按依赖链推进 |
| Q2 | Memory 改造方式 | B: 内部分层，对外保持 memory.messages 接口 | LLM.chat() 调用方式不变 |
| Q3 | Attachment 注册表 | A: ABC + dict 注册表 | 与 Toolkit/PluginManager 一致 |
| Q4 | Scope 隔离机制 | A: per-key threading.Lock + 内存状态 | 当前同步模型，SQLite 留后续 |
| Q5 | Dashboard 技术 | A: http.server + websockets 库 | 标准库零成本，符合 DR-7 |
| Q6 | Transcript 存储 | A: 内存 list[dict] + JSONL 导出 | 与 InMemoryJournal 一致 |
| Q7 | Compact 触发 | A: token 阈值自动触发（默认 8000） | usage 数据已有，精确 |
| Q8 | project() 逻辑 | A: 最后Boundary→过滤isMeta→修配对→裁剪 | 只关心最近一次 compact 后 |
| Q9 | Attachment 转换 | A: 转 system-reminder 独立消息 | OpenAI 推荐方式 |
| Q10 | memory.messages 行为 | A: 改为 @property 返回 project() 投影 | 对外接口不变 |
| Q11 | Summary 结构 | A: 9 段固定结构 | 结构化可重注入 |
| Q12 | token 计数 | A: LLM 返回的 usage 累计 | 精确，无额外依赖 |
| Q13 | Boundary 标记 | A: 特殊 dict（isMeta=True, type=compact_boundary） | 与消息格式统一 |
| Q14 | Scope API | A: context manager 风格 | 自动释放锁 |
| Q15 | Dashboard 数据 | C: Journal + Trace 合并 | 数据互补 |
| Q16 | 文件布局 | C: 按职责分散 | context/ + core/ + cli/ |
| Q17 | MemoryProtocol | A: 不加 | 只有一个实现，避免过度抽象 |
| Q18 | compact LLM | A: 复用 Agent 的 LLM | 不引入额外配置 |
| Q19 | 裁剪策略 | A: 从最旧 user/assistant 对裁剪，目标=阈值×0.7 | Summary 已压缩历史 |
| Q20 | 实施批次 | B: 分两批 | 核心链一起做，独立模块单独做 |
| Q21 | examples 验证 | A+B+C 全部 | compact_demo 优先 |

## 第一批：核心链（Attachment + Memory 改造 + Transcript + Compact + project + Agent 接入）

### Step 1: Attachment ABC + 注册表

**文件**：`atlas/context/attachment.py`（新建）

**内容**：
- `Attachment` 抽象基类：`name: str` + `to_system_reminder() -> str` 方法
- `AttachmentRegistry`：dict 注册表，`register(name, cls)` / `create(name, **kwargs)` 模式
- 内核不预置任何具体类型（上层按场景注册 file_state / task_status / skill_list / workflow_state）
- Attachment 消息格式：`{"role": "system", "content": "...", "isMeta": True, "type": "attachment", "attachment_name": "file_state"}`

**验证**：注册一个 TestAttachment → create → to_system_reminder() 输出正确

### Step 2: Memory 改造为分层结构

**文件**：`atlas/memory/base.py`（修改）

**改动**：
- `self.messages` 从 `list[dict]` 属性改为 `@property`，返回 `project(self._runtime_messages)` 投影结果
- 内部新增 `self._runtime_messages: list[dict]` 存原始数据（含 isMeta Attachment / Boundary / Summary）
- 内部新增 `self._token_count: int = 0` 跟踪累计 token
- `add_user / add_assistant / add_tool` 写入 `_runtime_messages`
- 新增 `add_attachment(attachment: Attachment)` → 写入 `_runtime_messages`（isMeta=True）
- 新增 `update_token_count(prompt_tokens: int)` → 更新 `_token_count`
- 新增 `get_token_count() -> int`
- 新增 `add_summary(summary_text: str)` → 写入 Summary 消息 + Boundary 消息
- `clear()` 保持原逻辑（保留 system prompt），同时清 `_token_count`
- `project(messages)` 方法：实现 Q8 五步投影逻辑

**project() 五步**：
1. 从后往前找最后一个 `type=compact_boundary` 消息
2. 只保留 Boundary 之后的 messages（+ 之前的 Summary）
3. 过滤 `isMeta=True` 的 Attachment → 调 `to_system_reminder()` 转成 system 消息插入
4. 修 tool_call 配对（assistant 的 tool_calls 和 tool role 消息必须成对）
5. 如仍超阈值，从最旧的 user/assistant 对开始裁剪（保留 system + Summary + 最近消息），裁剪目标 = 阈值 × 0.7

**验证**：构造含 Attachment + Boundary + Summary 的 _runtime_messages → project() 返回干净的 API messages（无 isMeta、配对完整）

### Step 3: Transcript 持久化层

**文件**：`atlas/context/transcript.py`（新建）

**内容**：
- `Transcript` 类：append-only 事件存档
- `append(event: dict)` → 追加事件
- `replay() -> list[dict]` → 返回全量事件
- `to_jsonl(path: str)` → 导出为 JSONL 文件
- `from_jsonl(path: str)` → 从 JSONL 恢复（classmethod）
- 内存 `list[dict]` 存储，与 InMemoryJournal 一致

**Agent 集成**：Agent `__init__` 新增 `transcript: Transcript | None = None`，默认创建空 Transcript。Agent.run() 中每次 memory 写入同步 transcript.append（双写）。

**验证**：跑 3 轮对话 → transcript.replay() 返回 3 轮事件 → to_jsonl 导出 → from_jsonl 恢复一致

### Step 4: Compact 机制

**文件**：`atlas/context/compact.py`（新建）

**内容**：
- `CompactManager` 类：
  - `__init__(threshold: int = 8000, llm: LLM | None = None)`
  - `should_compact(token_count: int) -> bool` → `token_count > threshold`
  - `compact(runtime_messages: list[dict], trace: Trace) -> tuple[str, str]` → 返回 (summary_text, boundary_marker)
    - 构造 9 段 summary prompt（用户目标 / 已完成步骤 / 关键发现 / 当前状态 / 待办事项 / 关键文件资源 / 错误与教训 / 决策记录 / 下一步建议）
    - 调用 `self.llm.chat(summary_prompt_messages)` 生成 summary
    - 返回 summary_text 和 boundary 标记 dict

**9 段 Summary 模板**：
```
请将以下对话历史压缩为 9 段结构化摘要：
1. 用户目标：用户想完成什么
2. 已完成步骤：已经做了哪些操作
3. 关键发现：过程中发现了什么重要信息
4. 当前状态：任务进展到哪一步
5. 待办事项：还有哪些没做
6. 关键文件/资源：涉及的文件、工具、数据
7. 错误与教训：遇到了什么错误，有什么经验
8. 决策记录：做了哪些关键决策
9. 下一步建议：接下来应该做什么

对话历史：
{runtime_messages}
```

**Agent 集成**：Agent `__init__` 新增 `compact_threshold: int | None = 8000`。Agent.run() 每次 LLM 调用后：
1. `self.memory.update_token_count(usage["prompt_tokens"])`
2. `if self.compact_manager.should_compact(self.memory.get_token_count())`：
   - 调 `self.compact_manager.compact(self.memory._runtime_messages, self.trace)`
   - `self.memory.add_summary(summary_text)` → 写入 Summary + Boundary
   - `self.memory._token_count = 0`（重置，下次从 Boundary 后重新累计）

**验证**：构造 token_count > 8000 场景 → compact → Summary 9 段生成 + Boundary 标记写入

### Step 5: Agent 接入 + project() 验证

**文件**：`atlas/core/agent.py`（修改）

**改动**：
- `__init__` 新增参数：`compact_threshold: int | None = 8000`、`transcript: Transcript | None = None`
- `__init__` 创建 `CompactManager(threshold=compact_threshold, llm=llm)`
- run() 中 LLM 调用后：`self.memory.update_token_count(usage["prompt_tokens"])` + compact 检查
- run() 中 `self.llm.chat(self.memory.messages)` 不变（@property 自动投影）
- memory 写入时同步 `self.transcript.append(event)`

**验证（V2）**：跑 quickstart.py + parallel_subagent_demo.py + saga_demo.py 全回归

### Step 6: compact_demo.py

**文件**：`examples/compact_demo.py`（新建）

**FakeLLM 设计**：
- 前 3 轮正常对话（每轮 usage.prompt_tokens ≈ 3000）
- 第 4 轮 prompt_tokens = 8500（超 8000 阈值）
- 第 5 轮：messages 已被 compact + project 投影

**断言**：
1. _runtime_messages 中存在 `type=compact_summary` 消息
2. _runtime_messages 中存在 `type=compact_boundary` 消息
3. `project()` 后 messages 条数 < 原始 _runtime_messages 条数
4. `project()` 后无 `isMeta=True` 消息
5. tool_calls 与 tool 消息配对完整

**命令**：`python examples/compact_demo.py --mock --plain`

### 第一批验证矩阵

| 验证项 | 命令 | 预期 |
|--------|------|------|
| V1 import | `python -c "import atlas.context.attachment, atlas.context.transcript, atlas.context.compact; import atlas.memory.base"` | exit 0 |
| V2 compact_demo | `python examples/compact_demo.py --mock --plain` | 5 项断言通过 |
| V3 quickstart 回归 | `python examples/quickstart.py --plain` | 正常对话 |
| V4 saga_demo 回归 | `python examples/saga_demo.py --mock --plain` | Saga 补偿清零 |
| V5 parallel 回归 | `python examples/parallel_subagent_demo.py --plain` | 并行加速 |

---

## 第二批：独立模块（Scope + Dashboard）

### Step 7: Scope per-key 隔离

**文件**：`atlas/core/scope.py`（新建）

**内容**：
- `Scope` 类：
  - `_locks: dict[str, threading.Lock]` — per-key 锁
  - `_states: dict[str, dict]` — per-key 状态
  - `acquire(key: str) -> ScopeContext` — context manager，获取锁后返回 ScopeContext
- `ScopeContext` 类：
  - `key: str`
  - `state: dict` — 读写状态
  - `__enter__` / `__exit__`（释放锁）

**用法**：
```python
scope = Scope()
with scope.acquire("task-001") as ctx:
    ctx.state["progress"] = 0.5
    # 同 key 的其他线程会阻塞等待
```

**验证**：scope_demo.py — 两个线程同 key 串行（时间戳不重叠）+ 不同 key 并行（时间戳重叠）

### Step 8: Dashboard HTTP + WebSocket

**文件**：`atlas/cli/dashboard.py`（新建）

**内容**：
- `DashboardServer` 类：
  - `__init__(journal: JournalProtocol, port: int = 8765)`
  - `start()` — 启动 http.server（静态文件）+ websockets server
  - HTTP 路由：`/` → 返回单 HTML 页面
  - WebSocket：推送 Journal 新事件 + Trace 更新
- 前端单 HTML 文件（`atlas/cli/dashboard.html`）：
  - 三面板布局：Run 列表（左）+ DAG 流程图（中）+ 事件日志（右）
  - 原生 ES Module + CDN Chart.js（不做打包构建）
  - WebSocket 连接 + 实时渲染

**CLI 集成**：`python -m atlas dashboard` 启动

**验证**：dashboard_demo.py — 启动 Dashboard → 后台跑 quickstart → 浏览器打开 http://localhost:8765

### Step 9: scope_demo.py + dashboard_demo.py

**scope_demo.py**：
- 两个线程同 key "A" → 验证串行（时间戳不重叠）
- 两个线程不同 key "A" / "B" → 验证并行（时间戳重叠）
- 无需 LLM，纯 threading 验证

**dashboard_demo.py**：
- 启动 Dashboard 服务
- 后台线程跑 quickstart 任务
- 提示用户打开浏览器查看
- 手动验证为主

### 第二批验证矩阵

| 验证项 | 命令 | 预期 |
|--------|------|------|
| V6 scope_demo | `python examples/scope_demo.py` | 同 key 串行 + 跨 key 并行 |
| V7 dashboard 启动 | `python -m atlas dashboard` | 服务启动，浏览器可访问 |
| V8 全 demo 回归 | 7 个 demo 全跑 | 全 exit 0 |

---

## 文件变更清单

### 新建文件（8 个）
| 文件 | 说明 |
|------|------|
| `atlas/context/attachment.py` | Attachment ABC + AttachmentRegistry |
| `atlas/context/transcript.py` | Transcript append-only 事件存档 |
| `atlas/context/compact.py` | CompactManager（Summary 生成 + Boundary + 状态重注入） |
| `atlas/core/scope.py` | Scope per-key 隔离 |
| `atlas/cli/dashboard.py` | Dashboard HTTP + WebSocket 服务 |
| `atlas/cli/dashboard.html` | 单 HTML 前端（三面板 + CDN Chart.js） |
| `examples/compact_demo.py` | compact 流程演示（FakeLLM 超阈值触发） |
| `examples/scope_demo.py` | per-key 串行/并行隔离演示 |

### 修改文件（5 个）
| 文件 | 改动 |
|------|------|
| `atlas/memory/base.py` | messages 改 @property + _runtime_messages + project() + token 跟踪 + add_attachment/add_summary |
| `atlas/core/agent.py` | 接入 compact_threshold + CompactManager + token 跟踪 + Transcript 双写 |
| `atlas/context/__init__.py` | 导出 Attachment / Transcript / CompactManager |
| `atlas/__init__.py` | 顶层导出新组件 |
| `atlas/cli/__init__.py` | 导出 dashboard 命令 |

### 不修改文件
- `atlas/llm/base.py` — LLM.chat() 接口不变
- `atlas/tools/base.py` — Tool ABI 不变
- `atlas/journal/base.py` — Journal 不变
- `atlas/core/orchestrator.py` — 多 Agent 编排器不变

---

## 依赖关系图

```
第一批（核心链，强依赖）：
Attachment ──→ Memory 改造 ──→ project() 实现
                   ↓
              Transcript（双写）
                   ↓
              Compact（触发 + Summary + Boundary）
                   ↓
              Agent 接入（compact_threshold + token 跟踪）
                   ↓
              compact_demo.py 验证

第二批（独立模块，无依赖）：
Scope ──→ scope_demo.py
Dashboard ──→ dashboard_demo.py
```

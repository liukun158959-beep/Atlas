# Atlas

> 面向通用任务的 Agent 内核 —— 一套内核，支撑编码 Agent、多 Agent 协作、工作流项目三种形态。

Atlas 提供一个简洁、可控、可演进的通用 Agent 内核。内核只做必要的控制平面（durable 步骤、显式状态、事件溯源、上下文隔离、组合式终止、幂等副作用、预算熔断、OTEL trace），所有"智能"留给上层组合。多 Agent 稳定性来自内核默认开启的护栏；自进化来自分层权限 + 验证基础设施 + 开放式存档。

## 设计原则

- **内核 = 控制平面，上层 = 智能平面**：内核提供确定性骨架与护栏原语，角色/SOP/Prompt/领域工具留给上层。
- **同一套原语，三种形态**：单 Agent 循环、多 Agent 工作流、编码 Agent 产品都建立在同一套原语之上，不为某种形态做专用抽象。
- **消除 fallback 触发场景，而非加更多兜底**：失败即退出 + 打印恢复指引，不静默继续。
- **稳定性来自内核默认开启的护栏**：终止条件、检查点、幂等、预算熔断默认开启，不靠上层自觉。
- **自进化来自分层权限 + 独立验证**：核心层只读，缓冲层受控可改，自由层沙箱实验。
- **编排层集成而非重造**：不在图编排上与 LangGraph 正面竞争，工作流原语只做"同一套原语支撑单 Agent 循环和多 Agent 工作流"的最小集；复杂编排场景直接集成 LangGraph/Pydantic AI，内核专注 Durable Execution + Compact + RSI 的差异化组合。

## 工程原则

### EP-1 可用性优先，可放弃技术栈，以运行效果为重

不为了"用某个技术"而用。技术选型以"跑起来、跑得稳"为第一标准，不以"技术先进"为标准。

**工程约束**：
- 优先纯 Python 方案（Edda/DBOS），不引入外部 server（Temporal/Restate）
- 不引入 Rust/Go 组件，除非纯 Python 完全无法满足
- 选最稳定的依赖版本，不追新
- 遇到技术选型冲突时，以"用户能最快跑通示例"为准

### EP-2 模块完全解耦，可插拔为主

每个模块都能单独拿出来用、单独替换。模块间只通过协议（Protocol）交互，不依赖具体实现。

**工程约束**：
- 单包 `atlas`，模块依赖单向：`atlas.llm` 不得 import `atlas.core` / `atlas.rsi` / `atlas.patterns`（由 `tests/test_dr0_imports.py` 锁死）。不拆 atlas-llm / atlas-core / atlas-agent 三包
- 对外暴露 Protocol / ABC，不是具体类
- 具体实现通过配置 + 注册表注入，不 hardcode
- 每个模块可以独立替换（Journal 后端走 Protocol；默认 SQLite，Postgres 未做）
- 新增能力先写 Protocol，再写实现

### EP-3 组装式构建 + 可视化流程 + 简易 Dashboard

Agent 是搭积木组装的，不是堆叠函数调用写的。运行时能看到执行流程，有网页能观测状态。

**工程约束**：
- Agent 配置声明式（YAML/JSON），不是 Python 函数调用堆叠
- 执行过程默认产生 trace + 可视化数据（DAG 节点状态、耗时、输入输出摘要）
- 内置极简 Dashboard：`python -m atlas dashboard` 一键启动，纯 HTML + WebSocket，不引入 React/Vue 前端工具链
- Dashboard 展示：运行列表、单 run 流程图（DAG）、工具调用详情、日志面板
- 可视化能力从 Phase 1 就到位，不后补

### EP-4 用户简单使用为主，不过度复杂，生成用例可评估

用户不需要理解 Durable Execution、Saga 补偿这些概念，就能跑通。每次能力升级都自带示例用例。

**工程约束**：
- 每个能力在 `examples/` 下都有对应示例脚本，可直接运行
- 复杂能力（如 Saga 补偿）有"演示模式"——一个示例脚本跑一遍失败→补偿→成功的完整流程
- 配置文件极简，只暴露必要字段，不暴露 50 个开关
- 示例脚本自带"预期输出"注释，用户能判断是否符合预期
- 新增功能必须附带最小可运行示例，没有示例的 PR 不合并

## 架构

```
+--------------------------------------------------------------------+
|  atlas.rsi / atlas.patterns / atlas.cli（上层：可选能力）            |
|  RSI 自进化 / 多 Agent 模式包 / atlas run · dashboard               |
+--------------------------------------------------------------------+
                                |
+--------------------------------------------------------------------+
|  atlas.core（中层：引擎）                                           |
|  Runtime 组装面 + AgentLoop（durable 循环，同步）                    |
|  Journal 必选（默认 SQLite，Postgres 未做；不引入 Temporal）          |
|  PermissionPolicy / 分级 Compact / OTEL GenAI spans（opt-in extras） |
|  Scope（进程内 per-key 隔离，非跨进程）                              |
+--------------------------------------------------------------------+
                                |
+--------------------------------------------------------------------+
|  atlas.llm（底层：模型抽象）                                        |
|  统一 API + provider 预设 / Token 追踪 / 重试                       |
+--------------------------------------------------------------------+
```

单包 `atlas`。依赖方向永远向上：底层对上层一无所知。可以只用 `atlas.llm` 调模型，也可以用 `Runtime` + `Agent` 跑循环，不必碰 RSI。

## 9 个核心原语

| 优先级 | 原语 | 作用 |
|---|---|---|
| P0 | **Step（durable 步骤）** | 包装 LLM/工具调用，输入输出入 journal，崩溃可重放 |
| P0 | **State（可持久化状态）** | 类型化共享状态，4 层分离（external/workflow/conversation/memory） |
| P0 | **Journal（执行日志）** | 构造 Agent 时必选（默认 SQLite）；关键事件全量落盘；`restore(run_id)` 重建 Memory / saga / token |
| P0 | **Scope（隔离单元）** | **进程内**按 key 隔离；同 key 串行、跨 key 并行。不跨进程、不跨机器 |
| P0 | **终止条件组合器** | max_iter / timeout / token_budget / cost_cap / repetition_detector 用 \| 组合，默认开启 |
| P1 | **Edge（控制流边）** | 普通/条件边，表达顺序/分支/循环，用同一套原语表达循环和图 |
| P1 | **Suspend/Resume（HITL）** | durable 挂起等待外部输入；工具门控与 Graph SuspendNode 共用 `SuspendToken` |
| P1 | **幂等键 + Saga 补偿** | 副作用工具自动注入 idempotency_key；Saga 栈写入 Journal，崩溃可恢复 |
| P2 | **OTEL trace（opt-in）** | `pip install atlas[otel]`；span 树 `gen_ai.invoke_agent` → `inference` → `execute_tool`。未安装时 no-op。自研 `Trace` 仍给 Dashboard 投影 |

## 三大方向

### 方向 1：多 Agent 协同 —— 如何稳定产出

内核提供"确定性骨架 + 局部智能"的原语，上层组合角色和 SOP。

**稳定产出的 5 个关键机制（内核内置）**：
1. **终止条件默认开启** —— max_iter + timeout + cost_cap 三件套，永不依赖单一信号
2. **上下文隔离** —— 每个 Worker 独立 Scope，结构化 artifact 传递而非自由文本
3. **校验门** —— 输出必须通过 Schema 校验 + 独立 Evaluator（不能让 Agent 自评）
4. **检查点 + 事件溯源** —— 长任务崩溃可断点续跑，不是从头重来
5. **幂等键 + 补偿** —— 副作用工具调用自动去重，失败有补偿动作

**Subagent = AgentTool + 隔离 Query Loop**：多 Agent 不是另一套内核，而是父 Agent 通过 AgentTool 启动的、拥有隔离上下文的同构 Agent Loop。子 Agent 复用同一套 Runtime，只返回最终结论，不复制内部对话。

### 方向 2：RSI 自进化 —— 安全可控的迭代

分层权限 + 验证基础设施 + 开放式存档。

```
+-------------------------------------+
| 自由层（沙箱内自由实验）              |
| - 工具实现代码 / 任务分解策略         |
+-------------------------------------+
| 缓冲层（受控可改，需通过门控）        |
| - System Prompt（GEPA 式遗传进化）   |
| - Skill Library（Voyager 式技能库）  |
| - 经验记忆（失败案例 > 成功案例）     |
+-------------------------------------+
| 核心层（永远只读，不可自修改）        |
| - 价值观宪法 / 安全不变式 / 人类监督钩子 |
+-------------------------------------+
```

**自进化的 4 个内核原语**：
- `inspect_self()` —— 读取自身的 system prompt / 工具定义 / Skill 库
- `propose_diff()` + `evaluate()` —— 生成自修改提案 → 独立验证器打分 → 通过才 apply
- `archive` —— 开放式存档，保留多样化候选，允许穿越适应度山谷
- `skill_register/retrieve()` —— 技能三件套（代码+描述+embedding），入库需通过验证

**实施路线**：
- Phase 1：Skill Library + 失败记忆（上下文层 + 工具层）—— 参考 **Voyager** 技能库模式（Chroma 向量检索 + Critic 验证入库）
- Phase 2：Prompt 进化（**GEPA** 式反思遗传 + **prompt-evolver** guardrails）+ 工具自生成（沙箱验证）
- **不做**：权重修改、完全闭环 RSI、**控制流自修改（架构层自改写代码）**—— Darwin Gödel Machine 式架构自修改在 Python 生态无工程化先例，代价过高且上下文层 + 工具层已覆盖 80% RSI 价值

### 方向 3：工作流原语 —— 同一套原语表达两种形态

同一套 Step/State/Edge/Parallel/Suspend/Journal/Scope 原语，既能表达单 Agent 循环，也能表达多 Agent 工作流。

**关键设计**：
- **Checkpoint ≠ Durable Execution** —— 内核做执行日志（event sourcing/journal），不是状态快照。崩溃恢复靠 `Journal.restore(run_id)` + 幂等键短路副作用，不是重放节点。
- **可插拔持久化后端** —— Journal Protocol 可插拔；默认 `SQLiteJournal(path="./.atlas/journal.db")`，开发/测试显式传 `InMemoryJournal(save_full_payload=True)`。Postgres / Temporal / Restate **未做**，也不引入外部 server。
- **HITL = durable wait** —— 挂起等待外部输入，崩溃不丢；Agent 循环与 Graph 共用 `SuspendToken`。

## 消息体系

```
Message
├── User / Assistant（正常对话）
├── Tool Use / Tool Result（工具交互）
├── Attachment（程序补充的运行状态，isMeta=true）
├── Compact Boundary（压缩边界标记）
└── Compact Summary（压缩摘要）
```

**三层消息视角**：
- **Transcript**（磁盘）—— 完整事件存档，JSONL，树结构（UUID + parentUuid）
- **Runtime messages**（内存）—— 当前活跃状态，含 Attachment/Boundary/Summary
- **API messages**（投影）—— 每轮历史投影后发给 LLM 的有效上下文

**Attachment 机制（已实现）**：`atlas.context.Attachment` 抽象基类 + `AttachmentRegistry` 字典注册表（重名 ValueError 拒绝）；子类覆写 `to_system_reminder()` 返回 system-reminder 文本；Runtime 层消息标记 `isMeta=True, type=attachment`；`project()` 时转成普通 `role=system` 消息插入 API messages，不会污染 LLM。上层按场景注册具体类型（file_state / task_status / skill_list / workflow_state），内核不硬编码任何具体类型。提供 `Memory.add_attachment(attachment)` 便捷方法。

**Compact 机制（已实现，分级）**：
- **触发条件**：默认 `compact_threshold=8000`（`None` 禁用）。每次 LLM 调用后从 `usage.prompt_tokens` 读取真实 token 数 → 超阈值时 `CompactManager.apply()`。
- **分级策略**：① 先裁剪过旧 `tool_result` 正文（保留 tool_call 配对）；② 仍超则滑窗丢掉最旧 user/assistant 对；③ 仍超才调 LLM 生成 9 段 Summary。略超阈值时不调 LLM。
- **写入 Summary + Boundary 对**：LLM 路径才写 `compact_summary` + `compact_boundary`；同时 `memory.reset_token_count()`。
- **Attachment 活状态重注入**：compact 之后调用 `AttachmentRegistry.rebuild_all(agent)`，从活状态重建 attachment，不把旧 isMeta 消息原样拷进新窗口。
- **project() 投影**：找最后一个 Boundary → 保留 system prompt + Summary + Boundary 之后的消息 → isMeta 过滤 → 修 tool_call 配对 → 超阈值裁剪。
- **Transcript 双写**：Memory 写入同时 `agent.transcript.append()`。

## Tool 层分类与副作用管理

工具按副作用特征分三档，决定是否需要补偿管理：

| 分类 | 标志 | 示例 | 处理方式 |
|---|---|---|---|
| 纯查询 | `isReadOnly=True` | read / search / get | 跳过 Saga，重放安全，不记补偿 |
| 天然幂等 | `isIdempotent=True` | PUT（带版本号）/ DELETE（带 ID） | 走 Journal + 幂等键防重复，不记补偿 |
| 有副作用且非幂等 | `hasSideEffects=True` + `isIdempotent=False` | 扣款 / 发邮件 / 第三方提交 | **必须实现 `compensate()`**，走完整 Saga |

**Tool 接口（已实现，dataclass ABI）**：

```python
from dataclasses import dataclass
from typing import Any, Callable

@dataclass
class Tool:
    name: str
    description: str
    parameters: dict                   # OpenAI function.parameters JSON Schema
    func: Callable[[dict], str]        # 主函数：输入 dict → 输出字符串
    parameters_factory: Callable[[], dict] | None = None  # 懒加载，解决循环依赖
    isReadOnly: bool = False           # 纯查询，无副作用（自动强制幂等）
    isIdempotent: bool = False         # 天然幂等（重放安全）
    hasSideEffects: bool = False       # 有副作用，需要补偿管理
    compensate: Callable[[dict, Any, dict], dict] | None = None
    # compensate 签名：compensate(compensate_input, original_result, context) → dict
    #   hasSideEffects=True + isIdempotent=False 必须非 None，否则 Toolkit.register() 抛 ValueError 拒绝
    circuit_key: str | None = None
    # 熔断器隔离键。None 时运行时 fallback 为 tool_name。同 circuit_key 共用一个熔断器
    retry_on: tuple[type[Exception], ...] = (TimeoutError, ConnectionError)
    # 触发自动重试的异常白名单。空 tuple=永不重试。仅 isIdempotent=True 生效
    retry_max: int = 3
    # 最大重试次数。0=永不重试（总尝试次数 = 1 次原执行 + retry_max 次重试）
    #   非幂等工具 retry_max>0 会被 Toolkit.register() 直接 ValueError 拒绝（消除 fallback 场景）

    def validate_input(self, input: dict) -> None:
        """按 parameters JSON Schema 校验输入，失败抛 jsonschema.ValidationError。"""
        ...
```

## Saga 补偿机制

**核心思想**：不是回滚（rollback），是反向操作（compensating action）—— 扣款 → 退款，发邮件 → 发撤回通知，创建订单 → 取消订单。

**执行流程**：

```
任务执行：S1 → S2 → S3 → S4 → S5(失败)
                              ↓
补偿触发：compensate_all(failed_step_id=S5)
                              ↓
反向补偿：compensate(S4) → compensate(S3) → compensate(S2) → compensate(S1)
          (LIFO 顺序：后执行的先补偿)
```

**三个关键机制**：

| 机制 | 作用 | 没有它会怎样 |
|---|---|---|
| Journal（执行日志） | 记录每步输入输出，知道"跑到哪了" | 不知道从哪恢复，只能从头跑 |
| 幂等键 | 副作用工具调用自动去重，重放返回首次结果 | 邮件发两次、订单提交两次 |
| Saga 补偿 | 不可逆操作必须注册反向动作，失败时按 LIFO 顺序补偿 | 无法撤销已执行的不可逆操作 |

**补偿失败处理（不做自动重试，消除 fallback 触发场景）**：

```
compensate() 抛异常
   ↓
记录到 _compensate_failed 列表 + emit compensate_failed 插件钩子 + Journal compensate_failed 事件
   ↓
continue 下一个补偿（单个补偿失败不影响其他 LIFO 项）
   ↓
Agent 最终返回值尾部追加"补偿失败列表: [...] 请人工介入" → 交给用户人工处理
```

### 日志规范（排查补偿失败）

Saga 补偿涉及资金/订单等不可逆操作，必须保留完整日志链路便于排查。所有 Saga 相关日志统一使用 Python 标准 `logging`（配合 `extra={}` 结构化字段）输出，关键字段：

**执行阶段日志**：

```python
# 工具执行成功
log.info("saga.step.execute",
    run_id=context.run_id,          # 任务运行 ID
    step_id=context.step_id,        # 步骤 ID
    tool_name=tool.name,            # 工具名
    is_readonly=tool.isReadOnly,    # 是否纯查询
    is_idempotent=tool.isIdempotent, # 是否幂等
    has_side_effects=tool.hasSideEffects,  # 是否有副作用
    idempotency_key=key,            # 幂等键（关键）
    input_summary=_summarize(input), # 输入摘要（脱敏）
    duration_ms=elapsed,            # 执行耗时
    status="success",
)

# 幂等命中（重放时跳过执行）
log.info("saga.step.idempotent_hit",
    run_id=context.run_id,
    step_id=context.step_id,
    tool_name=tool.name,
    idempotency_key=key,
    original_step_id=existing.step_id,  # 首次执行的 step_id
)

# 工具执行失败
log.error("saga.step.execute_failed",
    run_id=context.run_id,
    step_id=context.step_id,
    tool_name=tool.name,
    idempotency_key=key,
    input_summary=_summarize(input),
    error=str(e),
    error_type=type(e).__name__,
)
```

**补偿阶段日志**：

```python
# 补偿开始
log.warning("saga.compensate.start",
    run_id=context.run_id,
    failed_step_id=failed_step_id,
    pending_compensations=len(self._compensations),
)

# 单个补偿成功
log.info("saga.compensate.success",
    run_id=context.run_id,
    step_id=record.step_id,         # 被补偿的原始 step
    tool_name=record.tool_name,
    compensate_input_summary=_summarize(record.compensate_input),
    original_result_summary=_summarize(record.result),
    duration_ms=elapsed,
)

# 单个补偿失败（关键排查点）
log.error("saga.compensate.failed",
    run_id=context.run_id,
    step_id=record.step_id,
    tool_name=record.tool_name,
    compensate_input_summary=_summarize(record.compensate_input),
    original_result_summary=_summarize(record.result),  # 原始执行结果
    error=str(e),
    error_type=type(e).__name__,
    retry_count=retry_count,        # 第几次重试
    next_retry_at=next_retry_ts,    # 下次重试时间
    alert_triggered=True,           # 是否已触发告警
)

# 补偿全部完成
log.warning("saga.compensate.complete",
    run_id=context.run_id,
    total_compensations=len(results),
    success_count=sum(1 for r in results if r.status == "success"),
    failed_count=sum(1 for r in results if r.status == "failed"),
    failed_steps=[r.step_id for r in results if r.status == "failed"],
)
```

**排查补偿失败的标准流程**：

1. 按关键字 `saga.compensate.failed` 搜索日志，定位失败的 step_id
2. 按 `run_id` + `step_id` 搜索 `saga.step.execute`，找到原始执行的输入输出
3. 检查 `compensate_input` 是否正确（是否包含退款流水号等关键信息）
4. 检查 `error_type` 和 `error`，判断是网络问题/权限问题/数据问题
5. 如补偿已重试 5 次仍失败，检查 Journal 中的 `compensation_failed` 记录，人工介入

## 核心特性（已实现）

- 统一的 LLM / Tool / Memory 接口，能力可插拔
- 基于 Function Calling 的工具调用与结果回填
- 多轮会话记忆与结构化 trace 输出
- **Renderer 可插拔渲染**：内核层 Protocol + PlainRenderer（默认纯文本）+ RichRenderer（终端 Markdown/LaTeX/表格/面板），注入即生效，上层不接触 rich
- 单 Agent ReAct 循环 + 多 Agent 协作（`MultiAgentOrchestrator` Planner-Worker-Critic 模式，渲染统一走 Renderer）
- **并行子 Agent 内核原语（Phase 2-1）**：`spawn_subagents` 工具自动注入 + `parallel_gather` ThreadPoolExecutor 并行 + 子 Agent 独立上下文 + artifact schema 校验 + Trace 父子嵌套递归渲染，实测加速比 ≈ 2.3x
- **声明式 Role + 任务 YAML 配置**：Role 三元组（system_prompt + tools + llm_config）与父 Agent/task 统一在 YAML 中声明，改 YAML 即改行为
- 插件体系：PluginProtocol 19 个生命周期钩子（基础 8 个 + Saga 3 个 `compensate_start/failed/finish` + 熔断器 3 个 `circuit_open/half_open/closed` + 重试 3 个 `retry_before/after/exhausted` + HITL 2 个 `on_suspend/on_resume`）+ PluginManager 错误隔离分发，预置 TokenCostPlugin / FileLoggerPlugin
- 5 个 LLM provider 预设（Qwen / OpenAI / Deepseek / Moonshot / 智谱）
- `atlas init` 一键引导（venv + 依赖安装 + .env 配置）
- **Tool ABI 增强（Phase 2.1 收尾）**：compensate 字段正式入 Tool dataclass ABI；注册约束升级为 ValueError 直接拒绝（hasSideEffects=True + 非幂等 无 compensate → 抛错不兜底）；tool_status 三态：success / failed / idempotency_hit
- **幂等键 + Saga 补偿注册（Phase 2.1 收尾）**：`atlas.utils.derive_idempotency_key(run_id, tool_name, input_dict)` 派生 40 位 SHA-1；工具输入支持 `_idempotency_key` 魔法字段显式覆盖；Journal 扩展 `cache_get/cache_set` 持久化幂等结果；Agent 无最终答案时 LIFO 反向补偿已成功入栈的非幂等副作用工具；幂等命中不进 saga_stack
- **独立 Evaluator（校验门，Phase 2.1 收尾）**：内核外 Protocol + 内置 `ArtifactSchemaEvaluator` JSON Schema 契约校验；用户在 `agent.run(task)` 之后独立调用 `evaluator.evaluate(result, events)`，不从内核中自评；顶层 `from atlas import Evaluator, ArtifactSchemaEvaluator` 直接用
- **SideEffectTracker demo 辅助**：`atlas.utils.SideEffectTracker` record/erase/exists/is_empty；配合 `examples/saga_demo.py` 断言 Saga 补偿后所有副作用记录 100% 清零（无残留）
- **Memory 分层 + 历史投影层 project()（Phase 1 第一批）**：`Memory.messages` 从 list 属性改为 `@property`，每次访问返回 project() 投影结果；内部 `_runtime_messages` 存原始数据（含 isMeta Attachment/Boundary/Summary）。project() 五步流水线：找最后一个 Boundary → 保留 system prompt + Summary + Boundary 之后消息 → isMeta 过滤（Attachment→system、Boundary→跳过、Summary→assistant）→ 修 tool_call 配对（删除断裂 tool_calls 和无配对 tool 消息）→ 超阈值从最旧 user/assistant 对裁剪到 threshold×0.7
- **Attachment 抽象基类 + 类型注册表（Phase 1 第一批）**：`atlas.context.Attachment` ABC + `AttachmentRegistry.register(name,cls)/create(name,kwargs)`；重名 TypeError 子类校验 + ValueError 拒绝；`add_attachment(attachment)` / `to_message()` 便捷写入；顶层 `from atlas import Attachment, AttachmentRegistry`
- **Compact 机制（Phase 1 第一批）**：`CompactManager(threshold=8000, llm=agent.llm)`（复用主 LLM，不额外配置）；`should_compact(token_count)` 阈值判断；`compact()` 生成 9 段 Summary；Agent `compact_threshold` 参数默认 8000（None 禁用）；顶层 `from atlas import CompactManager`
- **Transcript 磁盘层（Phase 1 第一批）**：`atlas.context.Transcript` append-only 事件存档；Agent 默认创建，`agent.transcript` 直接访问；Memory 写入点（user/assistant/tool/compact）全部双写 transcript.append；`replay()` 全量返回；`to_jsonl(path)/from_jsonl(path)` JSONL 导出恢复；顶层 `from atlas import Transcript`
- **compact_demo.py（Phase 1 第一批 demo）**：`--mock` 模式下 CompactFakeLLM 逐轮递增 token（3000→5000→7000→8500→3000），第 4 轮必触发 compact；5 项断言（summary/boundary 存在/project 13→4 条/无 isMeta/Transcript 5+5+1 事件）；真实 LLM 模式 soft 检查（不强制断言，提示加 --mock）
- **Scope（Phase 1 第二批）**：`atlas.core.Scope` 进程内 per-key 隔离；`_locks[key]` + `_states[key]` 懒创建（双 guard 锁防并发重复造）；`with scope.acquire(key)` context manager 阻塞拿锁 → 返 SimpleNamespace（.key + .state dict）→ `__exit__` 写回 state 并释放锁（异常也释放 + 写回当前状态，不兜底回滚）；key 非空 str 否则 ValueError；`get_state(key)` 返回只读拷贝
- **极简 Dashboard 骨架（Phase 1 第二批）**：`python -m atlas dashboard --port 8765` 一键启动；HTTP(8765) ThreadingHTTPServer 子线程 + WebSocket(8766) asyncio 当前线程；HTML 三面板字符串内嵌（免读文件路径问题）；左 Run 列表（run_id/status/event_count/step_count/start→end）+ 中 Trace DAG（StepRecord 卡片 + 子 Trace 缩进 + tool_names 绿标）+ 右 Events（最近 200 条，类型着色）；WebSocket 每 500ms 广播 DATASTATE snapshot；`atlas.cli.dashboard.publish(run_id, journal, trace)` 全局 API 主动推送（threading.Lock 保护，journal 全 run 聚合 + trace 递归打平 nodes/edges）
- **scope_demo.py（Phase 1 第二批 demo）**：3 项断言（同 key 串行 start>=end / 不同 key 并行总耗时<0.18s / state 写回 first_write=42 跨 acquire 可读）
- **dashboard_demo.py（Phase 1 第二批 demo）**：后台线程起 dashboard → 3 轮 QuickstartMockLLM calculator mock 跑不同 run_id → 每轮前后 publish() 推送 → 提示用户浏览器打开 http://localhost:8765 → 驻留 10 秒自动退出
- **三态熔断器（Phase 2.2）**：`atlas.core.CircuitBreakerRegistry` 按 `circuit_key` 管理独立三态状态机（Closed→Open→Half-Open→Closed）；连续失败 N=5 → Open 拒绝请求；冷却窗口 W=30s 后 → Half-Open 放 1 个探测请求（成功→Closed 清零 / 失败→Open 重计时）；`threading.Lock` 线程安全；`allow_request/on_success/on_failure/snapshot` 对外 API；状态变更自动 emit 插件事件（circuit_open/half_open/closed）；Agent 默认自动创建空 Registry（`auto_default_registry=False` 可禁用）
- **指数退避重试（Phase 2.2）**：`atlas.core.full_jitter_delay_ms(attempt, base=0.1, cap=10)` Full Jitter 算法 `delay=random.uniform(0, min(cap, base*2^attempt))`；仅幂等工具自动重试（`isIdempotent=True`），非幂等工具 `retry_max>0` 注册时直接 ValueError 拒绝（消除 fallback 场景）；默认 `retry_on=(TimeoutError, ConnectionError)`，Tool 注册可覆写；默认 `retry_max=3`（总尝试 = 1 + retry_max）；每次重试 emit retry_before/after 钩子，耗尽 emit retry_exhausted；重试每次失败都计入熔断器失败计数
- **Tool ABI 增强（Phase 2.2）**：Tool dataclass 新增 `circuit_key`/`retry_on`/`retry_max` 三字段；Toolkit.register() 新增非幂等禁重试约束（`not isIdempotent and retry_max>0` → ValueError）；Agent.run() 工具执行段前置熔断器 `allow_request` 检查 + 包裹重试循环 + 成败计入熔断器
- **PluginProtocol 扩展 7 钩子（Phase 2.2）**：新增 `on_circuit_open/half_open/closed`（熔断器三态切换）+ `on_retry_before/after/exhausted`（重试前后与耗尽）+ `on_compensate_finish`（补齐 Saga 单步补偿完成）；钩子总数 10 → 17
- **Dashboard RESILIENCE 横幅（Phase 2.2）**：Dashboard 顶部新增 RESILIENCE 横幅，并排三表：① 熔断器状态表（circuit_key / state / failure_count / open_seconds，OPEN 红 / HALF 黄 / CLOSED 绿）② 最近重试记录表（tool_name / attempt / next_delay_ms / error_type，ring buffer 200 条）③ 活动告警表（Open 超 60s 红色高亮）；DATASTATE 扩 `circuit_breakers`/`retry_logs`/`alerts` 三 key，由 `CircuitBreakerRegistry.snapshot()` 产出
- **circuit_retry_demo.py（Phase 2.2 demo）**：4 场景断言全可复现——① flaky_api 幂等 retry_max=3 连抛 3 次 RuntimeError 第 4 次成功 ② unreliable_svc 幂等 retry_max=1 永远 TimeoutError → retry_exhausted ③ credit_card_charge 非幂等 retry_max=0 带 compensate 退款 ④ 手动触发熔断器 Open → circuit_rejected 不产生副作用；支持 `--mock --plain` 断言模式与 `--mock --dashboard` 可视模式
- **PlannerWorkerCritic 模式包（Phase 2.3）**：`atlas.patterns.PlannerWorkerCritic` 产品级多 Agent 模式——Planner 拆解 → 串行 Worker 执行 → Critic 审查 → FAIL 打回重做（Worker 输入追加 Critic 评语 + 前一版结果）→ max_revisions 耗尽标 failed 不 abort → Planner 汇总标注；多层嵌套 Agent（Planner/Worker/Critic 都是内核 Agent 实例，Worker 通过 `worker_tools` 注入业务工具，Planner/Critic 纯文本推理）；Python while 控制 Critic 循环（LLM 只做内容判断不控制次数）；构造函数必填 2 + 可选 10 = 12 项（≤ DR-2 15 限额）；3 角色内置 system prompt 模板可覆写；返回 `PWCResult(final_answer, trace: PWCTrace)` 扁平可观测结构（plan_items / worker_runs / critic_reviews）；独立于 `atlas.core`，不进顶层导出（`from atlas.patterns import PlannerWorkerCritic`）
- **pwc_demo.py（Phase 2.3 demo）**：5 项断言全可复现——① plan 拆解 3 项 ② Worker schema 校验 ③ Critic 打回 1 次后通过（revisions=1）④ 最终答案含关键字 ⑤ 重做 Worker 输入含 Critic 评语；FakeLLM 10 次调用精确映射 PWC 全流程；支持 `--mock --plain` 断言模式与 `--real` 真实 LLM 模式
- **OrchestratorWorkers 模式包（Phase 2.4）**：`atlas.patterns.OrchestratorWorkers` 产品级多 Agent 模式——Anthropic +90.2% 模式：Orchestrator 动态分派 → 批间并行 Worker（ThreadPoolExecutor）→ 结果回 Orchestrator → 循环决策（dispatch/synthesize）；Python while 控制多轮循环；JSON 结构化决策输出（`{"action":"dispatch","tasks":[...]}` / `{"action":"synthesize","answer":"..."}`），缺字段直接 ValueError（消除 fallback）；max_iterations 耗尽 Python 侧强制汇总（不信任 LLM 会遵守约束）；Worker 失败 partial success（error 字段回传 Orchestrator 自主决策）；每轮结果自动累积上下文（Orchestrator + Worker 都收到）；构造函数必填 2 + 可选 9 = 11 项（≤ DR-2 15 限额）；2 角色内置 system prompt 模板可覆写；返回 `OWResult(final_answer, trace: OWTrace)` 扁平可观测结构（iterations → worker_results）；独立于 `atlas.core`，不进顶层导出（`from atlas.patterns import OrchestratorWorkers`）；与 PWC 互补（PWC=质量闭环串行，OW=效率优先并行）
- **orchestrator_workers_demo.py（Phase 2.4 demo）**：6 项断言全可复现——① 至少 1 轮 dispatch ② 某轮含 >= 2 个并行 Worker ③ 最终 synthesize ④ 最终答案含关键字 ⑤ 上下文传递（第 2 轮 Orchestrator prompt 含第 1 轮 Worker 结果）⑥ max_iterations 强制汇总（单独实例 max_iterations=2，验证 1 轮 dispatch 后达上限 forced synthesize）；FakeLLM 7 次调用精确映射主场景（2 轮 dispatch + 1 轮 synthesize）；支持 `--mock --plain` 断言模式与 `--real` 真实 LLM 模式
- **工作流图 Graph + Edge 控制流边（Phase 3.1）**：`atlas.core.Graph` 解释器模式（运行时遍历，不预编译）；命令式 API（`add_node`/`add_edge`/`add_conditional_edges`/`set_entry_point`/`set_exit_point`，和 LangGraph 对齐）；两类边 `Edge`（无条件跳转）+ `ConditionalEdge`（`condition(state)→key→mapping[key]` 动态路由）；节点签名 `Callable[[dict], dict]`，Agent 和普通函数都支持；统一 `dict` in/out + `state.update(output)` merge 语义；保留字 `END` 表示终止；run() 入口 lazy 验证（缺 entry_point / 节点未注册 / 无 END 可达路径 → ValueError）；终止三来源（END 节点 / `max_iterations` 兜底默认 50 / 可选 `terminator` 全局终止条件，和 Agent 对齐）；复用 `on_turn_start/on_turn_finish` 插件钩子，不新增 Graph 专属钩子
- **ParallelNode 并行汇聚（Phase 3.1）**：`atlas.core.ParallelNode(fns)` 单节点内嵌 fan_out + and_ 汇聚；`__call__(state)` 签名和普通函数节点对齐，可直接 `add_node('p', ParallelNode([a,b,c]))`；每个 fn 收同一份 state 深拷贝（防互相污染）；结果按 fns 顺序合并成 list 放到 `state['parallel_results']`（不是完成顺序）；fail-fast 错误传播（任一 fn 抛异常 → 取消未完成 + raise，不静默吞错，DR-8 对齐）
- **GraphTrace 扁平可观测存档（Phase 3.1）**：`Graph.run()` 返回 `GraphResult(final_state, trace)`；`GraphTrace` 含 `node_executions`（每个节点的输入快照/输出/耗时/错误）+ `edge_routings`（from_node/condition_key/to_node 路由决策链）+ 起止时间 + 总耗时 + iterations + terminated_by；trace 完全扁平，不做嵌套，便于 Dashboard 直接渲染
- **react_graph 桥接函数（Phase 3.1）**：`react_graph(agent)` 把 Agent 包装成单节点 Graph，验证"Agent 循环能用 Graph 语法表达"设计假设；最小形态单节点 Graph（`agent.run(state['input']) → {'answer': ...}`）无循环，用户要自我修正循环可 `add_conditional_edges` 自行表达
- **edge_demo.py（Phase 3.1 demo）**：5 项断言全可复现——① 节点执行顺序 `[start, worker, worker]` ② 边路由记录 3 条（普通边 start→worker + 条件边 worker→worker(refine) + worker→END(end)）③ 最终答案含"完善" ④ `terminated_by == "END"` ⑤ worker 第 2 次输入包含第 1 次输出（验证 state 在节点间传递）
- **parallel_edge_demo.py（Phase 3.1 demo）**：4 项断言全可复现——① 3 个 Worker 并行执行（start_ts 差值 < 50ms）② 汇聚结果 `parallel_results` 长度=3（顺序 a,b,c）③ summarizer 收到合并 state（summary=['a:20','b:30','c:110']）④ 总耗时 < 串行基准 0.300s
- **SuspendNode Durable HITL（Phase 3.2）**：`atlas.core.SuspendNode(prompt_fn)` 专用节点类（和 ParallelNode 同风格）；节点执行时返回 `{"__suspend__": prompt}` 标记，Graph 解释器检测到即进入"挂起态"；`Graph.run()` 返回 `GraphResult(status="suspended", suspend_prompt, suspend_token=SuspendToken(run_id, node_name, seq))`，由调用方显式决定 resume 时机；无 Journal 注入时直接 ValueError 拒绝（消除 fallback，禁止非 durable 挂起）。顶层 `from atlas import SuspendNode, SuspendToken, SuspendTimeout`
- **graph.resume(token, user_input) 恢复执行（Phase 3.2）**：在同一 Graph 实例（或同结构 + 同 Journal 新实例，模拟进程重启 durable 恢复）上调用 `graph.resume(SuspendToken, user_input_dict)` 继续执行；从挂起节点的**下一条边**开始路由（Q14=A 不重跑 SuspendNode，避免 prompt 副作用）；`user_input` 必须是 dict（否则 ValueError），以 `state.update(user_input)` merge 语义注入（和 Phase 3.1 节点输出一致 Q4=A）
- **Journal Graph 事件三类（Phase 3.2）**：`graph_node_exec`（正常节点执行后，payload=node_name+input+output+duration+error）、`graph_suspend`（挂起点强制完整 state_snapshot + prompt，Q8=C durable 恢复源）、`graph_resume`（审计链路：node_name+user_input+resumed_from_seq）；事件 3 壳结构（run_id/seq/ts/event_type+自由 payload）和 Journal 其他事件一致
- **PluginProtocol 扩展 2 钩子（Phase 3.2）**：新增 `on_suspend(run_id, node_name, prompt, token_dict)` + `on_resume(run_id, node_name, user_input, resumed_from_seq)`；钩子总数 17 → 19；可用于插件在挂起时暂停计时、恢复时恢复计时或外部系统通知
- **GraphResult 三新字段（Phase 3.2）**：`status`（"completed"/"suspended"，默认"completed"—— Phase 3.1 老代码零成本忽略）、`suspend_prompt`（status=suspended 时的提示文本）、`suspend_token`（`SuspendToken` 对象或 None）；`GraphTrace.terminated_by` 新增允许值 "suspended"
- **suspend_resume_demo.py（Phase 3.2 demo）**：7 项断言全可复现——① 挂起态 status+suspended_prompt 含订单/金额/商品 ② approved=True→shipped+SHIP-PO-999 ③ state 保留（order_id/amount/submitted）+ resume 注入（approved=True）④ approved=False→cancelled+拒绝理由 ⑤ Journal 事件链 ≥4 条（三种类型齐全）⑥ 双 Graph 实例崩溃恢复（del graph1→新建 graph2 同结构同 Journal→resume，结果和单实例 approved=True 100% 一致）⑦ `graph_resume.payload.resumed_from_seq == graph_suspend.seq`（审计链路一致）
- **EmbeddingProtocol + FakeEmbedding / OpenAIEmbedding（Phase 4.1）**：`atlas.rsi.EmbeddingProtocol`（embed / embed_batch / dimension 三方法）；`FakeEmbedding` MD5 hash → 64 维归一化向量（跨进程确定性，demo 100% 可复现）；`OpenAIEmbedding` 复用 LLM 的 OpenAI 兼容 client（默认 text-embedding-3-small 1536 维），同 base_url / api_key
- **VectorStoreProtocol + SQLiteVectorStore（Phase 4.1）**：`atlas.rsi.VectorStoreProtocol`（add / query / get / delete / count 五方法）+ `VectorHit(id, score, metadata)`；默认实现 `SQLiteVectorStore`：embedding 序列化 bytes 存 SQLite，检索时纯 Python 余弦相似度 top-k（零外部依赖，不引入 Chroma）；threading.Lock 线程安全
- **Skill Library（Phase 4.1）**：`atlas.rsi.SkillLibrary` 代码即数据的技能注册/检索/版本化——`Skill(name, description, code, embedding, version, fitness_score)` dataclass 独立于 Tool；`register()` 自动 embed(description) + 同 name 版本递增；`retrieve(query)` 语义检索 top-k；`to_tool()` 经 subprocess 受限解释器执行（超时、禁 builtins 白名单外符号，禁止进程内 `exec`；`open()` / `os.system()` 失败），包装成 Tool
- **失败记忆 FailureMemory + Collector（Phase 4.1）**：`atlas.rsi.FailureMemory` 记录/语义检索/最近列表——`FailureCase(run_id, step_id, tool_name, input_summary, error_message, error_type, context_summary, embedding)` dataclass；`FailureMemoryCollector(BasePlugin)` 挂 `on_tool_after_call(failed)` / `on_retry_exhausted` / `on_compensate_failed` 三钩子实时采集；通过 `bind_agent(agent)` 注入 Agent 弱引用（weakref），从 `agent.memory.messages` 读取 system_prompt + user_input 构造 context_summary；Agent `__init__` 末尾遍历 plugins 自动 `hasattr(bind_agent)` 注入
- **inspect_self() 自省（Phase 4.1）**：`Agent.inspect_self() -> SelfSnapshot`——Agent 方法委托 `atlas.rsi.build_self_snapshot()`；`SelfSnapshot` 5 字段：system_prompt / tool_definitions / skill_summaries / recent_failures / memory_summary；Agent 构造函数新增 `skill_library` + `failure_memory` 两个可选参数（None 默认，零侵入——现有 demo 无需改动）
- **rsi_demo.py（Phase 4.1 demo）**：8 项断言全可复现——① SkillLibrary register + retrieve ② 版本化（同 name v1→v2）③ to_tool()（沙箱执行 skill code → Tool.func）④ FailureMemory record + retrieve ⑤ Collector Plugin 自动采集（on_tool_after_call failed → FailureMemory）⑥ inspect_self()（SelfSnapshot 5 字段非空）⑦ 空值零侵入（skill_library=None + failure_memory=None → 空 list）⑧ FakeEmbedding 确定性（相同文本相同向量，dim=64）
- **分层权限模型（Phase 4.2a）**：`atlas.rsi.PermissionLevel` 三层级枚举（CORE 只读 / BUFFERED 受控 / FREE 预留）+ `check_permission(target_type)` 函数；核心层（graph/agent/tool/plugin）只读——propose_diff 抛 PermissionError；缓冲层（skill/system_prompt）受控——propose_diff 可修改但必须经 evaluate 通过
- **RSI 自进化闭环（Phase 4.2a）**：`propose_diff(agent) -> ProposedDiff`（LLM 生成 JSON 提案，prompt 含 self_snapshot + recent_failures + archived_failures 避免重复）+ `evaluate(agent, proposal) -> EvaluationResult`（双层评估：测试执行 `exec(code)` + 同名函数存在校验 → 失败直接拒绝不调 LLM；通过后 LLM Critic 语义评估）+ `apply_diff(agent, proposal)`（skill 新版本入库 version 自动递增 / system_prompt 替换 messages[0].content 验证 role==system）+ `self_evolve(agent)` 一行完成 propose→evaluate→apply/archive 闭环
- **ArchiveStore 提案归档（Phase 4.2a）**：`atlas.rsi.ArchiveStore` 归档所有提案（通过 + 未通过）——`ArchivedProposal(proposal, evaluation, archived_at)`；`list_failed()` 返回失败提案，下次 `propose_diff` 时摘要注入 prompt 避免重复提议；Agent 构造函数新增 `archive_store` 可选参数（None 默认，零侵入）
- **Agent 自进化方法（Phase 4.2a）**：`Agent.propose_diff()` + `Agent.self_evolve()` 委托 `atlas.rsi`（和 inspect_self 一致模式）；FakeLLM 5 次调用序列精确映射（成功闭环 #1-2 + 失败闭环 #3 不调 LLM + archive 反馈 #4-5）
- **rsi_evolution_demo.py（Phase 4.2a demo）**：7 项断言全可复现——① propose_diff 返回 ProposedDiff ② check_permission 权限校验（skill=BUFFERED / graph=CORE）③ evaluate 测试通过（exec + 同名函数存在）④ evaluate 测试拒绝（语法错误不调 LLM，_step 不变）⑤ Critic 通过（passed=true/score=0.85）⑥ apply 版本递增（v1→v2 + 新 skill 可执行）⑦ archive 失败反馈（失败提案归档后下次 propose prompt 含归档摘要）
- **Prompt 进化 · 反思式变异（Phase 4.2b）**：`atlas.rsi.FitnessEvaluator` Protocol（`evaluate(prompt) -> FitnessResult(score, feedback, task_results)`）+ `FakeFitnessEvaluator`（确定性序列 0.4/0.8/0.3，demo 100% 可复现）+ `TaskSetFitnessEvaluator`（真实适应度评估：跑任务集 + Schema/关键字打分，复用 `_validate_artifact_schema`，agent_factory 注入构造临时 Agent）；单 lineage 反思式变异——`_extract_fitness_history` 从 `ArchiveStore.list_recent()` 提取适应度轨迹格式化为"第 N 代 score=X feedback=Y"注入下一代 propose prompt（反思依据）+ `_extract_archived_failures` 注入失败摘要避免重复提议
- **Leakage Guardrails（Phase 4.2b）**：`atlas.rsi.LeakageGuard` Protocol（`check(prompt) -> LeakageCheckResult(passed, matched_patterns)`）+ `RegexLeakageGuard`（内置默认 ~10 个中英文越狱模式 ignore previous/忽略以上/you are now/你现在是/system override/jailbreak/DAN/忽略所有规则/disregard above/new instructions + `extra_patterns` 可追加，预编译正则忽略大小写）；在 fitness 通过后、apply 前执行，命中即 archive 为 failed（EvaluationResult(passed=False, feedback="leakage detected: [matched]")）
- **Agent Prompt 进化方法（Phase 4.2b）**：`Agent.evolve_prompt() -> EvolutionResult` 委托 `atlas.rsi.evolve_prompt()`（和 self_evolve 并列，内部复用 4.2a apply_diff system_prompt 分支）；`EvolutionResult(generation, proposal, evaluation, leakage_check, applied)` 携带完整进化轨迹；Agent 构造函数新增 `fitness_evaluator` + `leakage_guard` 可选参数 + `generation: int = 0` 计数器（apply 成功后 +1，零侵入）
- **prompt_evolution_demo.py（Phase 4.2b demo）**：8 项断言全可复现——① 返回 EvolutionResult ② fitness score ∈ [0,1] ③ 反思式变异（第 2 代 prompt 含第 1 代 fitness 历史）④ LeakageGuard 拦截越狱 ⑤ apply 后 system_prompt 替换（generation 0→1）⑥ archive 记录通过+失败 ⑦ 多代进化（gen2 fitness 0.8 > gen1 0.4）⑧ RegexLeakageGuard 正则黑名单命中（matched_patterns 非空）
- **收敛检测 + 多代回滚（Phase 4.2b 增强）**：`EvolutionSummary(generations, final_generation, final_score, stop_reason)` 多代汇总；`Agent.evolve_prompt_until_converged(max_generations=10, convergence_n=3, convergence_threshold=0.01, no_progress_n=3)` 委托 `atlas.rsi.evolve_prompt_until_converged()`，三种停止条件（converged=连续 N 代 apply 成功 fitness 提升 < threshold / max_generations=达上限 / no_progress=连续 N 代未 apply 成功）；`Agent.rollback_to(target_generation)` 委托 `atlas.rsi.rollback_to()`，target=0 恢复第 1 代提案的 current_value（原始 prompt），target=N 恢复第 N 代提案的 proposed_value，同步回退 `agent.generation`，无对应通过提案直接 ValueError（不兜底）；`ArchiveStore` 扩 `ArchivedProposal.generation`（apply 成功时 = agent.generation，失败 = -1）+ `list_by_generation(generation)` + `get_passed_at(generation)`（每代至多 1 条通过）
- **prompt_converge_rollback_demo.py（Phase 4.2b 增强 demo）**：8 项断言全可复现——① 返回 EvolutionSummary ② Part 1 converged（Part1ConvergenceFitness 0.2→0.5→0.7→0.71→0.72→0.72→0.721，最近 3 代 [0.72,0.72,0.721] deltas=[0,0.001] max=0.001<0.01，共 7 代收敛）③ Part 2 max_generations（max_generations=2 只跑 2 代停止）④ Part 3 no_progress（Part3NoProgressThenSuccess 0.3→0.2→0.4 连续 3 低分未 apply，stop_reason=no_progress，共 3 代）⑤ rollback_to(0) 恢复原始 prompt + generation=0 ⑥ rollback_to(1) 恢复第 1 代 proposed_value + generation=1 ⑦ generations 列表长度（P1=7 / P2=2 / P3=3）⑧ rollback generation 回退（gen2=2 → rb0=0 → rb1=1）
- **评估器重构 · LLM-as-Judge 多维 Rubric（Phase 4.2b 评估器升级）**：将 benchmark 评估器从硬编码关键字匹配重构为 LLM-as-Judge 多维 Rubric 评估；参考 **GEPA** 结构化失败类型（formatting/reasoning/both）+ **ICAIL** 宽松判官原则（语义匹配不强制特定表达式）+ **DoorDash** 多维加权评分（Correctness 0.5 + Format 0.3 + Reasoning 0.2）+ **UnlimitedJudge** 通用模型稳定性验证（3 轮重复评估标准差=0，failure_type 100% 一致）；5 场景覆盖（数学推理/逻辑推理/格式遵从/安全边界/多约束写作），每场景独立 Judge prompt；安全场景 Judge 显式区分"提到危险名词（教育性拒绝）≠ 给出可操作步骤（配合请求）"；进化效果从硬编码时代 -6.7% 提升到 +3.3%；透明 Walkthrough 脚本 `_evaluator_transparent_walkthrough.py` 展示每一步输入输出（Judge prompt 完整输入、JSON 原始输出、分数映射过程、A/B 对比、多代进化轨迹），消除黑盒
- **业界标准 Benchmark（Phase 4.2b 验证）**：`prompt_evolution_benchmark.py`（3 场景 × 5 题，LLM-Judge 评估器）+ `prompt_evolution_complex_benchmark.py`（复杂场景，LLM-Judge 评估器）+ `_evaluator_ab_test.py`（硬编码 vs LLM-Judge A/B 对比）+ `_evaluator_transparent_walkthrough.py`（3 模块透明 Walkthrough：数学 A/B + 安全判官误区 + 完整多代进化闭环）

## 技术栈

- Python 3.11+
- 依赖管理：`pip` + `venv`（不引入 uv，保持零额外工具依赖）
- LLM 接入：兼容 OpenAI API 协议的模型均可接入
- 终端渲染：`rich>=13.0`（RichRenderer），可退化为纯文本
- 配置：`PyYAML>=6.0`（声明式 Role 与任务配置）
- 持久化：Journal 必选；默认 SQLite（`./.atlas/journal.db`），测试用内存 Journal。Postgres / Temporal 未做
- 测试：`pytest`（`pip install -e ".[dev]"`）；CI 跑 `tests/` + 内核 demo
- 观测：`pip install atlas[otel]` 启用 OTEL GenAI spans；未安装时 Tracer 为 no-op

## 安装与配置

### 一键初始化（推荐）

克隆仓库后，运行一条命令完成虚拟环境创建、依赖安装与 `.env` 配置：

```bash
# 克隆仓库
git clone git@github.com:liukun158959-beep/Atlas.git
cd Atlas

# 一键初始化（venv + 依赖安装 + .env 配置）
python -m atlas init
```

`init` 会依次完成：
1. 在项目根目录创建 `.venv` 虚拟环境
2. 用 venv 内的 python 执行 `pip install -e .` 安装 atlas
3. 引导选择 LLM provider（Qwen / OpenAI / Deepseek / Moonshot / 智谱 / 自定义），输入 API Key，自动写入 `.env`

开发依赖（pytest）与可选观测：

```bash
pip install -e ".[dev]"          # 测试
pip install -e ".[otel]"         # OTEL GenAI spans（opt-in）
python -m pytest tests -q
```

完成后按提示激活 venv 即可运行示例。

### 手动配置（备选）

如果想自己控制 venv 与依赖安装，可跳过 `init` 的前两步，仅手动配置 `.env`：

```bash
# 复制模板
cp .env.example .env
```

编辑 `.env` 填入真实值：

```env
ATLAS_API_KEY=sk-xxxx
ATLAS_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ATLAS_MODEL=qwen-plus
```

完整 provider 列表与 base_url 见 [.env.example](.env.example)。

## 快速开始

省略 `journal` 时默认写入 `./.atlas/journal.db`（`save_full_payload=True`）。显式 `journal=None` 会 `ValueError`。测试请传 `InMemoryJournal(save_full_payload=True)`。RSI 不进默认构造，需要时 `agent.bind_rsi(...)`。

```python
from dotenv import load_dotenv
from atlas import Agent, LLM, Toolkit, Runtime, InMemoryJournal
from atlas.render import RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS

load_dotenv()  # 读取 .env

llm = LLM()
tools = Toolkit()
for spec in BUILTIN_TOOLS:  # 注册内置工具：calculator、get_current_time
    tools.register(**spec)

agent = Agent(
    llm=llm,
    tools=tools,
    system_prompt="你是通用助手 Atlas，遇到需要计算或获取时间的问题请主动调用工具。",
    runtime=Runtime(journal=InMemoryJournal(save_full_payload=True)),
    renderer=RichRenderer(),
)
print(agent.run("先告诉我现在几点，然后计算 365 * 24 等于多少"))
print(agent.trace)
# 崩溃后续跑：agent.resume(run_id) 或 agent.resume_detailed(run_id)
```

无手写 Python，YAML + CLI：

```bash
python -m atlas run --config examples/quickstart.yaml
python -m atlas run --config examples/quickstart.yaml --resume <run_id>
```

直接运行示例脚本：

```bash
python examples/quickstart.py                  # 终端 Markdown 渲染
python examples/quickstart.py --plain          # 纯文本输出（适合重定向到文件）
```

并行子 Agent 协作示例（Phase 2-1 内核原语版，纯内核 API，< 100 行代码）：

```bash
python examples/parallel_subagent_demo.py              # 默认 RichRenderer 终端渲染
python examples/parallel_subagent_demo.py --plain       # 纯文本模式
```

PlannerWorkerCritic 质量闭环示例（Phase 2.3 模式包，串行 + Critic 打回重做）：

```bash
python examples/pwc_demo.py --mock --plain              # 断言模式，100% 可复现
python examples/pwc_demo.py --mock                       # Rich 渲染（若可用）
python examples/pwc_demo.py --real                       # 真实 LLM（需 .env 配置）
```

OrchestratorWorkers 动态分派示例（Phase 2.4 模式包，Orchestrator 动态分派 + 批间并行 Worker）：

```bash
python examples/orchestrator_workers_demo.py --mock --plain   # 断言模式，100% 可复现
python examples/orchestrator_workers_demo.py --real            # 真实 LLM（需 .env 配置）
```

Phase 1 地基新能力示例（全部支持 `--mock` 用 FakeLLM 跑逻辑、`--plain` 纯文本）：

```bash
# Tool ABI 增强：三分类标志 + validate_input 校验
python examples/tool_abi_demo.py --mock

# Journal event sourcing：12 事件全生命周期 + replay 聚合；支持 --journal-backend=sqlite 持久化验证
python examples/journal_replay_demo.py --mock --plain
python examples/journal_replay_demo.py --mock --plain --journal-backend=sqlite

# 终止条件组合器：MaxSteps / Timeout / RepetitionDetector 三种触发
python examples/terminator_demo.py --mock --plain

# 插件体系 + 预算护栏：TokenCostPlugin 累计 + CostCap 终止
python examples/plugin_cost_demo.py --mock --plain

# Phase 2.1 收尾：幂等键命中跳过 + Saga LIFO 补偿（write_po + charge 成功 → ship FAIL-SHIP-500 → 终止 → 补偿清零）
# 100% 可复现；末尾自带 SideEffectTracker.is_empty() 断言
python examples/saga_demo.py --mock --plain

# Phase 1 第一批：Compact 机制（分级：snip tool_result → 滑窗 → 仍超才 LLM 9 段 Summary）
# --mock 必触发 compact（第 4 轮 token=8500 > 8000），5 项断言全通过
python examples/compact_demo.py --mock --plain

# Durable Agent：崩溃后 resume，副作用工具只执行一次（幂等键短路）
python examples/agent_resume_demo.py

# Phase 1 第二批：Scope（per-key 隔离）
# 3 项断言：同 key 串行 + 不同 key 并行 < 0.18s + state 写回跨 acquire 可见
python examples/scope_demo.py

# Phase 1 第二批：Dashboard（HTTP 三面板 + WebSocket 推送）
# 后台起 dashboard + 3 轮 mock quickstart → 浏览器打开 http://localhost:8765 看实时 DAG
python examples/dashboard_demo.py
# 或单独长期驻留（浏览器刷新即连接，用户代码手动 publish 推送数据）
python -m atlas dashboard --port 8765

# Phase 2.2：熔断器三态 + 指数退避重试（4 场景断言全可复现）
# flaky_api 重试成功 / unreliable_svc retry_exhausted / credit_card_charge 非幂等补偿 / 手动熔断 circuit_rejected
python examples/circuit_retry_demo.py --mock --plain
# 断言 + 后台起 Dashboard 看 RESILIENCE 横幅三表（熔断器状态/重试记录/告警）
python examples/circuit_retry_demo.py --mock --dashboard

# Phase 2.3：PlannerWorkerCritic 质量闭环（5 项断言全可复现）
# 定位差异：parallel_subagent_demo = 并行加速（纯 fan-out，无依赖、无 Critic）
#           pwc_demo = 质量闭环（串行 + Critic 打回重做，不求快但求稳）
python examples/pwc_demo.py --mock --plain

# Phase 2.4：OrchestratorWorkers 动态分派（6 项断言全可复现）
# 定位差异：pwc_demo = 质量闭环（串行 + Critic）
#           orchestrator_workers_demo = 效率优先（并行 + 动态分派，多轮决策循环）
python examples/orchestrator_workers_demo.py --mock --plain

# Phase 3.1：Edge 控制流边（条件分支 + 循环 + state 传递，5 项断言全可复现）
# start -> worker -> (条件边: answer 含"完善" ? end : refine) -> worker(loop) / END
python examples/edge_demo.py --plain

# Phase 3.1：Parallel 并行汇聚（ParallelNode 扇出 + and_ 汇聚，4 项断言全可复现）
# parallel_1(3 Worker 并行各 sleep 0.1s) -> summarizer -> END，并行耗时 < 串行基准
python examples/parallel_edge_demo.py --plain

# Phase 3.2：Suspend/Resume Durable HITL（挂起 + 恢复 + 崩溃恢复，7 项断言全可复现）
# submit_order -> wait_approval(SuspendNode) -> [suspended] -> resume(approved=True/False)
#               -> (条件边: approved ? ship_order : cancel_order) -> END
# 包含双 Graph 实例崩溃恢复断言：del graph -> 新建 graph(同结构+同Journal) -> resume 成功
python examples/suspend_resume_demo.py --plain

# Phase 4.1：RSI 自进化基础设施（Skill Library + 失败记忆 + inspect_self，8 项断言全可复现）
# SkillLibrary register/retrieve/版本化/to_tool + FailureMemory record/retrieve + Collector Plugin + inspect_self
python examples/rsi_demo.py --plain

# Phase 4.2a：RSI 自进化闭环（propose_diff + evaluate + apply + archive + 分层权限，7 项断言全可复现）
# propose→evaluate→apply 成功闭环 + 测试拒绝不调 LLM + archive 失败反馈避免重复提议
python examples/rsi_evolution_demo.py --plain

# Phase 4.2b：Prompt 进化（反思式变异 + 适应度 + Leakage Guardrails，8 项断言全可复现）
# 单 lineage 反思式变异 + FakeFitnessEvaluator 0.4→0.8 适应度上升 + RegexLeakageGuard 拦截越狱
python examples/prompt_evolution_demo.py --plain

# TaskSetFitnessEvaluator：真实适应度评估（跑任务集 + Schema/关键字打分，6 项断言全可复现）
# FakeLLM 根据 prompt 质量返回不同答案 → 低质量 prompt score=0.0 / 高质量 prompt score=1.0
python examples/taskset_fitness_demo.py --plain

# Phase 4.2b 增强：收敛检测 + 多代回滚（8 项断言全可复现）
# 3 场景：Part 1 收敛（plateau 0.7→0.71→0.721，convergence_n=3 threshold=0.01）
#        Part 2 max_generations（只跑 2 代停止）
#        Part 3 no_progress（连续 3 代低分未 apply）+ rollback_to(0/1) 恢复
python examples/prompt_converge_rollback_demo.py --plain
```

独立 Evaluator 用法示例（内核外调用，Agent 不自评）：

```python
from atlas import ArtifactSchemaEvaluator, Agent

# ... agent 正常跑完得到 result 和 run_id
ok, reasons = ArtifactSchemaEvaluator(contracts=[
    ("content", {"type": "string", "minLength": 1}),
]).evaluate(result, agent.journal.get_events(run_id))
assert ok, reasons
```

## 项目结构

```
Atlas/
├── atlas/                  # 核心源码
│   ├── core/               # Agent 内核（9 原语 + compact + 幂等键 + Saga 补偿 + 熔断器 + 重试）
│   │   ├── agent.py        # 薄封装：llm/tools/system_prompt/runtime；run() 返回 str；run_detailed()/resume()；bind_rsi()
│   │   ├── runtime.py      # Runtime 组装面：journal 必选 + terminator/plugins/compact/circuit/permission/tracer
│   │   ├── loop.py         # AgentLoop：durable 同步循环（checkpoint / saga 入 Journal / resume / 权限门控 / OTEL spans）
│   │   ├── result.py       # RunResult(final_answer, run_id, status, events, suspend_token)
│   │   ├── permission.py   # NeverConfirm / AlwaysConfirm / ConfirmRisky；高风险工具挂起 SuspendToken
│   │   ├── tracer.py       # TracerProtocol + NoOpTracer + OtelTracer（atlas[otel] extras，未安装 no-op）
│   │   ├── plugin.py       # PluginProtocol + PluginManager + BasePlugin（19 钩子 no-op 基类：基础 8 + Saga 3 + 熔断器 3 + 重试 3 + HITL 2 on_suspend/on_resume）
│   │   ├── circuit_breaker.py # Phase 2.2 新增：CircuitBreakerRegistry 三态状态机（Closed→Open→Half-Open）+ threading.Lock 线程安全 + snapshot 快照
│   │   ├── retry.py        # Phase 2.2 新增：full_jitter_delay_ms Full Jitter 退避 + sleep_ms
│   │   ├── terminator.py   # Terminator Protocol + 6 内置 + CompositeTerminator（| 组合）+ make_default_terminator()（MaxSteps/Timeout/TokenBudget/CostCap/RepetitionDetector）
│   │   ├── evaluator.py    # Phase 2.1 新增：Evaluator Protocol（@runtime_checkable）+ ArtifactSchemaEvaluator（JSON Schema 契约校验，内核外独立调用，Agent 不自评）
│   │   ├── multi_agent.py  # 并行子 Agent 内核原语：RoleConfig/SubagentSpec/SubagentResult + parallel_gather（ThreadPoolExecutor + 渲染合并）+ make_spawn_subagents_tool
│   │   ├── graph.py        # Phase 3.1 新增：Graph（解释器模式）+ Edge/ConditionalEdge + ParallelNode（fan_out+and_汇聚）+ GraphTrace/GraphResult + react_graph 桥接函数；Phase 3.2 扩展：GraphResult.status/suspend_token + Graph.resume() + suspend_timeout
│   │   ├── suspend.py      # Phase 3.2 新增：SuspendToken(run_id, node_name, seq) + SuspendTimeout 异常 + SuspendNode(prompt_fn)（Graph 专用 HITL 节点）
│   │   └── scope.py        # Phase 1 第二批：Scope（进程内 per-key threading.Lock + 内存 state；不跨进程）
│   ├── journal/            # 执行日志 event sourcing
│   │   ├── __init__.py     # re-export JournalProtocol/JournalEvent/InMemoryJournal/SQLiteJournal
│   │   ├── base.py         # JournalProtocol（含 restore）+ InMemoryJournal；关键事件始终全量落盘
│   │   ├── restore.py      # RunSnapshot + restore_from_events（messages / saga_stack / term_ctx / status）
│   │   └── sqlite.py       # 默认 ./.atlas/journal.db；Agent 未传 journal 时默认此后端（save_full_payload=True）
│   ├── utils/              # 工具函数（Phase 2.1 扩展：幂等键 + 副作用追踪器）
│   │   ├── idempotency.py  # Phase 2.1 新增：derive_idempotency_key(run_id, tool_name, input) → SHA-1 40 位 + IDEMPOTENCY_USER_KEY='_idempotency_key' 常量
│   │   ├── tracker.py      # Phase 2.1 新增：SideEffectTracker record/erase/is_empty 可观测 Saga 补偿覆盖度
│   │   └── yaml_loader.py  # load_agent_config()（atlas run）+ load_parallel_subagent_config()
│   ├── render/             # 渲染层：Protocol + PlainRenderer + RichRenderer
│   │   ├── base.py         # Renderer Protocol（5 个方法）
│   │   ├── plain.py        # 默认纯文本实现（零依赖）
│   │   └── rich.py         # 基于 rich 的 Markdown/LaTeX/表格/面板渲染
│   ├── plugins/            # 插件实现（继承 BasePlugin 即可，按需覆写钩子）
│   │   ├── token_cost.py   # Token 用量与成本统计插件（含 28 个模型单价表）
│   │   └── logger.py       # 结构化 JSONL 文件日志插件（按天滚动 + 按 run 拆目录）
│   ├── llm/                # LLM 接入层：OpenAI 兼容协议
│   │   └── base.py         # chat() 返回 {message, usage, duration_ms} 三层结构
│   ├── tools/              # 工具基座与内置工具
│   │   ├── base.py         # Tool（含 isReadOnly/isIdempotent/hasSideEffects + compensate + circuit_key/retry_on/retry_max 字段 + validate_input + parameters_factory）/ Toolkit.register() 升级补偿+非幂等禁重试约束为 ValueError 拒绝
│   │   └── builtin.py      # calculator / get_current_time（均标记 isReadOnly=True）
│   ├── memory/             # 会话记忆
│   │   └── base.py         # Phase 1 改造：messages 改 @property + _runtime_messages + project() 五步投影 + token 跟踪 + add_attachment()/add_summary()
│   ├── context/            # 执行轨迹与上下文（Phase 1 扩 4 文件）
│   │   ├── attachment.py   # Phase 1 新增：Attachment ABC + AttachmentRegistry（register/create 模式，重名 ValueError 拒绝）
│   │   ├── transcript.py   # Phase 1 新增：Transcript append-only 事件存档 + JSONL 导出/恢复，与 Memory 双写
│   │   ├── compact.py      # CompactManager：分级 snip → 滑窗 → LLM 9 段 Summary；略超阈值不调 LLM
│   │   └── trace.py        # Trace + StepRecord + 三段式递归 render + subagent_traces 嵌套 + Trace.meta
│   ├── testing/            # 测试专用 FakeLLM（不进顶层 __init__）
│   │   └── fake_llm.py
│   ├── cli/                # CLI 子命令
│   │   ├── init.py         # atlas init：venv + 依赖安装 + .env 配置
│   │   ├── run.py          # atlas run --config YAML [--resume run_id]
│   │   └── dashboard.py    # HTTP(8765)+WebSocket(8766) Dashboard
│   ├── patterns/           # Phase 2.3 新增：产品级多 Agent 模式包（独立于 atlas.core，仅组合内核原语）
│   │   ├── __init__.py     # re-export PlannerWorkerCritic / PWCResult / PWCTrace / OrchestratorWorkers / OWResult / OWTrace（不进 atlas 顶层导出）
│   │   ├── _common.py      # 内部共享辅助：_make_agent（从 Agent.__init__ 参数表 copy 适配）+ _validate_artifact_schema
│   │   ├── planner_worker_critic.py # Phase 2.3 PlannerWorkerCritic 主实现 + 5 dataclass + 3 角色 prompt + _parse_plan/_parse_critic
│   │   └── orchestrator_workers.py  # Phase 2.4 OrchestratorWorkers 主实现 + 4 dataclass + 2 角色 prompt + _parse_orchestrator_json
│   ├── rsi/                # Phase 4.1 新增：RSI 自进化基础设施（独立于 core，和 patterns 同级）；Phase 4.2a 追加自进化闭环；Phase 4.2b 追加 Prompt 进化 + 收敛回滚
│   │   ├── __init__.py     # re-export 4.1 全部 + 4.2a PermissionLevel/check_permission/ArchivedProposal/ArchiveStore/ProposedDiff/EvaluationResult/propose_diff/evaluate/apply_diff/self_evolve + 4.2b FitnessEvaluator/FitnessResult/FakeFitnessEvaluator/TaskSetFitnessEvaluator/LeakageGuard/LeakageCheckResult/RegexLeakageGuard/EvolutionResult/evolve_prompt + 4.2b增强 EvolutionSummary/evolve_prompt_until_converged/rollback_to
│   │   ├── embedding.py    # EmbeddingProtocol（embed/embed_batch/dimension）+ FakeEmbedding（MD5 hash 64 维确定性）+ OpenAIEmbedding（复用 LLM client）
│   │   ├── vector_store.py # VectorStoreProtocol（add/query/get/delete/count）+ VectorHit + SQLiteVectorStore（bytes 存 SQLite + 纯 Python 余弦相似度）
│   │   ├── skill.py        # Skill + SkillLibrary；to_tool() 走 subprocess 沙箱，禁止进程内 exec
│   │   ├── sandbox.py      # 受限解释器：超时 + builtins 白名单
│   │   ├── _sandbox_worker.py
│   │   ├── failure.py      # FailureCase dataclass + FailureMemory（record/retrieve/list_recent）+ FailureMemoryCollector(BasePlugin)（挂 3 钩子 + weakref 读 Agent 上下文）
│   │   ├── inspector.py    # SelfSnapshot dataclass（5 字段）+ build_self_snapshot(agent)
│   │   ├── archive.py      # Phase 4.2a 新增：PermissionLevel 枚举（CORE/BUFFERED/FREE）+ check_permission() + ArchivedProposal（4.2b扩 generation 字段）+ ArchiveStore（add/list_recent/list_failed/list_passed/count + 4.2b扩 list_by_generation/get_passed_at）
│   │   ├── evolver.py      # Phase 4.2a 新增：ProposedDiff + EvaluationResult dataclass + propose_diff/evaluate/apply_diff/self_evolve + _parse_json（容错）+ _test_skill_code（exec + 同名函数存在）
│   │   ├── fitness.py      # Phase 4.2b 新增：FitnessResult dataclass + FitnessEvaluator Protocol + FakeFitnessEvaluator（0.4/0.8/0.3 确定性序列）+ TaskSetFitnessEvaluator（真实接口预留骨架）
│   │   └── prompt_evolver.py # Phase 4.2b 新增：LeakageCheckResult + LeakageGuard Protocol + RegexLeakageGuard（默认 ~10 中英文越狱模式 + extra_patterns）+ EvolutionResult + evolve_prompt（反思式变异闭环）+ 4.2b增强 EvolutionSummary（多代汇总三停止条件）+ evolve_prompt_until_converged（收敛/上限/无进展）+ rollback_to（按代回滚 system_prompt + generation）+ _build_evolution_prompt/_extract_fitness_history/_extract_archived_failures
│   ├── providers.py        # LLM provider 预设表
│   └── __main__.py         # CLI：python -m atlas {init,dashboard,run}
├── tests/                  # pytest 内核回归（DR-0 / durable / permission / sandbox / compact / yaml）
├── .github/workflows/ci.yml
├── examples/
│   ├── quickstart.py
│   ├── quickstart.yaml     # atlas run 最小配置
│   ├── agent_resume_demo.py # 崩溃恢复：幂等命中、副作用只执行一次
│   ├── saga_demo.py        # Phase 2.1 新增：Saga 补偿完整流程（write_po + charge 成功 → ship FAIL-SHIP-500 → MaxSteps(3) 终止 → LIFO 补偿清零；SideEffectTracker 断言）
│   ├── parallel_subagent_demo.py     # Phase 2-1 并行子 Agent demo（YAML 声明式配置，< 120 行，--config 可指定）
│   ├── parallel_subagent_config.yaml # 声明式 Role + 父 Agent + 任务配置（被 yaml_loader 读取）
│   ├── pwc_demo.py         # Phase 2.3 新增：PlannerWorkerCritic 质量闭环 demo（5 项断言：plan 拆解 / schema 校验 / Critic 打回重做 / 关键字 / 评语注入；FakeLLM 10 次调用精确映射）
│   ├── orchestrator_workers_demo.py # Phase 2.4 新增：OrchestratorWorkers 动态分派 demo（6 项断言：dispatch 轮次 / 并行 Worker / synthesize / 关键字 / 上下文传递 / max_iterations 强制汇总；FakeLLM 7 次调用精确映射）
│   ├── tool_abi_demo.py    # Tool ABI 增强：三分类标志 + validate_input 通过/失败演示 + compensate 注册
│   ├── journal_replay_demo.py  # Journal event sourcing：12 事件全生命周期 + replay 聚合
│   ├── terminator_demo.py  # 终止条件组合器：MaxSteps/Timeout/RepetitionDetector 三种触发
│   ├── plugin_cost_demo.py # 插件体系 + 预算护栏：TokenCostPlugin 累计 + CostCap 终止
│   ├── compact_demo.py     # Phase 1 新增：Compact 机制演示（--mock 第 4 轮 token=8500 触发，5 项断言）
│   ├── scope_demo.py       # Phase 1 第二批：Scope 演示（同 key 串行 + 不同 key 并行 + state 写回 3 断言）
│   ├── dashboard_demo.py   # Phase 1 第二批：Dashboard 演示（后台起 server + 3 轮 mock quickstart publish）
│   ├── circuit_retry_demo.py # Phase 2.2 新增：熔断器三态 + 指数退避重试 demo（4 场景断言：flaky 重试成功 / unreliable retry_exhausted / credit_card 非幂等补偿 / 手动熔断 circuit_rejected）
│   ├── edge_demo.py          # Phase 3.1 新增：Edge 控制流边 demo（条件分支 + 循环 + state 传递，5 项断言：节点顺序/边路由/最终答案/terminated_by/state 传递）
│   ├── parallel_edge_demo.py # Phase 3.1 新增：Parallel 并行汇聚 demo（ParallelNode 扇出 + and_ 汇聚，4 项断言：并行执行/结果长度/汇总内容/耗时）
│   ├── suspend_resume_demo.py# Phase 3.2 新增：Suspend/Resume Durable HITL demo（7 项断言：挂起态字段/approved→发货/state保留+注入/reject→取消/Journal事件链/崩溃恢复双实例/resumed_from_seq一致）
│   ├── rsi_demo.py          # Phase 4.1 新增：RSI 自进化基础设施 demo（8 项断言：SkillLibrary register/retrieve/版本化/to_tool + FailureMemory record/retrieve + Collector Plugin + inspect_self 空值零侵入 + FakeEmbedding 确定性）
│   ├── rsi_evolution_demo.py# Phase 4.2a 新增：RSI 自进化闭环 demo（7 项断言：propose_diff + 权限校验 + 测试通过 + 测试拒绝不调 LLM + Critic 通过 + apply 版本递增 + archive 失败反馈）
│   ├── prompt_evolution_demo.py # Phase 4.2b 新增：Prompt 进化 demo（8 项断言：EvolutionResult + fitness score + 反思式变异 + Leakage 拦截 + apply 替换 + archive 通过+失败 + 多代 fitness 上升 + 正则黑名单命中）
│   ├── taskset_fitness_demo.py # TaskSetFitnessEvaluator demo（6 项断言：FitnessResult + score ∈ [0,1] + task_results 长度 + 低质 prompt 0.0 + 高质 prompt 1.0 + feedback 详情）
│   └── prompt_converge_rollback_demo.py # Phase 4.2b 增强：收敛检测 + 多代回滚 demo（8 项断言：EvolutionSummary + converged/MaxGenerations/NoProgress 三停止 + rollback 恢复原始/第1代 + generation 回退）
├── docs_feishu/            # 飞书文档源文件与实现计划
├── .env.example            # 配置文件模板
├── pyproject.toml          # 项目元数据与依赖
└── README.md
```

## 路线图

### 已完成（demo 期）

- [x] 内核：Agent 主循环与 ReAct 模式
- [x] LLM 层：OpenAI 协议兼容接入 + 5 个 provider 预设
- [x] Tools 层：工具注册与函数调用调度
- [x] Memory 层：会话记忆
- [x] 执行轨迹：结构化 trace 输出
- [x] 多 Agent 协作：Planner-Worker-Critic 模式（demo）
- [x] 项目初始化：`atlas init` 一键引导

### Phase 1：内核重构（控制平面）

**实现参考**：Journal / Scope / event sourcing 参考 **Edda**（纯 Python、内建 `@on_failure` Saga）和 **DBOS**（进程内库 + SQLite/Postgres）的内嵌式设计，不依赖外部 server；Compact / Transcript 三层拆分参考 **Claude Code** 和 **Pydantic AI v2** 已验证的实现模式。

- [x] **Transcript 三层拆分（Transcript / Runtime / API messages）**—— Transcript（append-only 事件存档 + JSONL）/ Memory._runtime_messages（原始数据，含 isMeta）/ Memory.messages @property（project() 投影，返回干净的 API messages）
- [x] **Compact 机制（分级 + Summary + Boundary + 状态重注入）**—— `CompactManager.apply()`：超阈值先 snip 过旧 tool_result，再滑窗，仍超才 LLM 9 段 Summary；compact 后 `AttachmentRegistry.rebuild_all(agent)` 从活状态重建 attachment
- [x] **历史投影层 `project()`（找 Boundary → 过滤 → 转 attachment → 修配对 → 裁剪）**—— 五步流水线：找最后 compact_boundary → 保留 system prompt + Summary + Boundary 后消息 → isMeta 分层转换（Attachment→system / Summary→assistant / Boundary→跳过）→ tool_call 配对修复（删除断裂 tool_calls 和无配对 tool 消息）→ 超阈值裁剪最旧 user/assistant 对到 target=threshold×0.7
- [x] **Attachment 抽象基类 + 类型注册表 + 默认 system-reminder 转换器**—— Attachment ABC + AttachmentRegistry（register/create 模式）；重名 ValueError 拒绝 + TypeError 子类校验；to_system_reminder() 子类覆写 + to_message() Runtime 消息生成；Memory.add_attachment() 便捷写入；`rebuild_from_state` / `rebuild_all` 供 compact 后重注入
- [x] **Journal（执行日志，event sourcing 崩溃重放）**—— `InMemoryJournal` + `SQLiteJournal`（JournalProtocol 可插拔）；默认 SQLite `./.atlas/journal.db`；**Agent 未注入 Journal 时 ValueError，省略则默认 SQLite 全量**；关键事件始终全量落盘；`restore(run_id) -> RunSnapshot` 重建 messages / saga_stack / term_ctx / status；`replay()` 仍为摘要 API。Postgres / Temporal **未做**
- [x] **Scope（进程内按 key 隔离）**—— `atlas.core.Scope` + `with scope.acquire(key) as ctx`；per-key 锁 + 内存 state；**不跨进程、不跨机器**；`get_state(key)` 返回只读拷贝；key 空/非 str → ValueError
- [x] **极简 Dashboard 骨架**—— `python -m atlas dashboard --port 8765`；HTTP ThreadingHTTPServer（8765，返回内嵌字符串 HTML 免路径问题）+ WebSocket asyncio（8766，每 500ms 广播 DATASTATE snapshot）；三面板（左 Run 列表 / 中 Trace DAG 卡片缩进 / 右 Events 类型着色）；`atlas.cli.dashboard.publish(run_id, journal, trace)` 全局推送（journal 聚合 run 状态 + trace 递归打平 nodes/edges）；前端自动重连 3s
- [x] **终止条件组合器**（MaxSteps / Timeout / TokenBudget / CostCap / RepetitionDetector）—— `|` 运算符组合，`CompositeTerminator` 任一命中即终止；默认 `MaxSteps(10) | Timeout(60_000)` 与旧版完全兼容
- [x] **插件体系（接入 Agent 全生命周期）**：`PluginProtocol` + `PluginManager` + `BasePlugin` 钩子基类（子类按需覆写 no-op）；10 个生命周期 + UUID run_id + 全链路 step_id；预置 `TokenCostPlugin` / `FileLoggerPlugin` 真实工作
- [x] **声明式 YAML Loader**：`atlas.utils.yaml_loader.load_parallel_subagent_config()` 严格模式（缺字段抛 RuntimeError，不兜底）；`parallel_subagent_demo.py` 默认读 YAML，支持 `--config`

### Phase 2：多 Agent 稳定性

Phase 2 严格遵循"内核只提供原语、上层组合 SOP"的分层原则。多 Agent 协作的"角色/SOP/Prompt"不属于内核，内核只提供让上层能稳定组合出任意模式的原语；常见模式（PlannerWorkerCritic / OrchestratorWorkers）作为上层可选包提供，不进内核、不取代用户自定义模式。

#### 2.1 内核原语（控制平面，必做）

- [x] **Tool ABI 增强（分类标志 + validateInput + compensate 字段）**—— 三标志（`isReadOnly`/`isIdempotent`/`hasSideEffects`，默认全 False）；注册时 `isReadOnly=True` 自动强制幂等，`hasSideEffects+非幂等` **ValueError 直接拒绝注册（无 compensate 不兜底）**；`Tool.validate_input()` 走 `jsonschema`；`parameters_factory` 懒加载闭环；`tool_status` 三态（success / failed / idempotency_hit）
- [x] **Hook 机制（插件 17 生命周期钩子）**—— 基础 8（`on_agent_start/finish`、`on_turn_start/finish`、`on_llm_before/after_call`、`on_tool_before/after_call`）+ Saga 3（`on_compensate_start/finish/failed`）+ 熔断器 3（`on_circuit_open/half_open/closed`）+ 重试 3（`on_retry_before/after/exhausted`）；`BasePlugin` no-op 基类，子类按需覆写；配合 `run_id`（UUID）+ `step_id` 全链路可追溯
- [x] **Role 抽象**（`RoleConfig`：system_prompt + tool_names + temperature/max_steps/timeout_ms 三元组，Python 编程式注入，YAML 声明式由上层 `parallel_subagent_config.yaml` 承载）—— 角色定义不硬编码在内核，由上层注入
- [x] **`spawn_subagents` 内置工具**（`make_spawn_subagents_tool` 闭包工厂 + `_inject_spawn_tool` 自动注入 + role enum 懒加载 + 嵌套守卫双保险；context 隔离：子 Agent 独立 Memory/Trace；artifact schema 校验：`jsonschema.validate` 不改写原文仅打标记）
- [x] **`parallel_gather` 原语**（ThreadPoolExecutor 并行 fan-out/gather + enumerate O(1) 顺序保持 + partial success 错误隔离 + 并行效率 meta）—— 持久化/幂等防重留给 Journal 原语
- [x] **预算护栏（terminator 侧可触发）**—— `CostCap(max_usd)` + `TokenBudget(max_tokens)` 终止条件；`TokenCostPlugin` 累计 `cost_usd` 写入 `_term_ctx`；三态熔断器已在 Phase 2.2 实现
- [x] **幂等键 + Saga 补偿注册**—— `atlas.utils.idempotency.derive(run_id, tool_name, input_dict)` SHA-1 40 位派生或 `_idempotency_key` 显式覆盖；Journal 扩 `cache_get/cache_set` 命中跳过真实 func；Agent 无最终答案时 LIFO 反向 compensate 非幂等副作用工具；失败不重试，记录 compensate_failed 插件钩子 + 尾附文字提示人工介入
- [x] **独立 Evaluator（校验门，不让 Agent 自评）**—— `Evaluator Protocol`（@runtime_checkable）+ 内置 `ArtifactSchemaEvaluator`（JSON Schema 契约 list[(key, schema)]）；顶层 `from atlas import Evaluator, ArtifactSchemaEvaluator`；用户在 agent.run() 之后外部调用
- [x] **Saga 补偿演示模式**：`examples/saga_demo.py` 一键跑通 write_po→charge→ship(FAIL)→终止→LIFO 补偿→SideEffectTracker.is_empty() 断言（对应 EP-4，100% 可复现，支持 --mock --plain）
- [x] **Dashboard 增强**：顶部 RESILIENCE 横幅三表（熔断器状态/最近重试记录/活动告警），DATASTATE 扩 `circuit_breakers`/`retry_logs`/`alerts`，由 `CircuitBreakerRegistry.snapshot()` 产出（对应 EP-3）

#### 2.2 内核原语：熔断器 + 重试（控制平面，必做）

- [x] **三态熔断器**—— `CircuitBreakerRegistry` 按 `circuit_key` 管理独立三态状态机（Closed→Open→Half-Open→Closed）；连续失败 N=5 → Open 拒绝请求；冷却窗口 W=30s 后 → Half-Open 放 1 个探测请求（成功→Closed 清零 / 失败→Open 重计时）；`threading.Lock` 线程安全；`allow_request/on_success/on_failure/snapshot` 对外 API；状态变更 emit 插件事件
- [x] **指数退避重试（Full Jitter）**—— `full_jitter_delay_ms(attempt, base=0.1, cap=10)` = `random.uniform(0, min(cap, base*2^attempt))`；仅幂等工具自动重试，非幂等工具 `retry_max>0` 注册时直接 ValueError 拒绝（消除 fallback）；默认 `retry_on=(TimeoutError, ConnectionError)` 可覆写；默认 `retry_max=3`；重试每次失败计入熔断器失败计数
- [x] **Tool ABI 扩展**—— Tool dataclass 新增 `circuit_key`/`retry_on`/`retry_max` 三字段；`Toolkit.register()` 新增非幂等禁重试约束；`Agent.run()` 工具执行段前置熔断器检查 + 包裹重试循环
- [x] **Dashboard Resilience 横幅**—— 顶部三表实时展示熔断器状态（OPEN 红/HALF 黄/CLOSED 绿）、最近重试记录（ring buffer 200 条）、活动告警（Open 超 60s 红色高亮）
- [x] **circuit_retry_demo.py**—— 4 场景断言全可复现（flaky 重试成功 / unreliable retry_exhausted / credit_card 非幂等补偿 / 手动熔断 circuit_rejected），支持 `--mock --plain` 与 `--mock --dashboard`

#### 2.3 产品级模式包：PlannerWorkerCritic（上层可选，非内核）

模式包定位为"参考实现 + 可直接组装的产品级模式"，位于 `atlas.patterns`（独立于 `atlas.core`）。用户可以不用这些包、只用内核原语自己组装任意模式；也可以引用其中一种作为起点。模式包之间互不依赖，按需引入。

- [x] **PlannerWorkerCritic**（质量闭环模式：Planner 拆解 → 串行 Worker 执行 → Critic 审查 → FAIL 打回重做（Worker 输入追加 Critic 评语 + 前一版结果）→ max_revisions 耗尽标 failed 不 abort → Planner 汇总标注；多层嵌套 Agent + Python while 控制 Critic 循环；构造函数必填 2 + 可选 10 = 12 项；3 角色内置 prompt 可覆写；返回 `PWCResult(final_answer, trace: PWCTrace)` 扁平结构）
- [x] **pwc_demo.py**—— 5 项断言全可复现（plan 拆解 / schema 校验 / Critic 打回 1 次后通过 / 最终答案含关键字 / 重做 Worker 输入含 Critic 评语），FakeLLM 10 次调用精确映射 PWC 全流程，支持 `--mock --plain` 与 `--real`
- [x] `OrchestratorWorkers`（Anthropic +90.2% 模式）—— Phase 2.4 完成（详见下方 2.4 节）
- [x] 每个模式包自带 `examples/<pattern>_demo.py`—— Phase 2.4 完成（orchestrator_workers_demo.py 6 项断言）

#### 2.4 产品级模式包：OrchestratorWorkers（上层可选，非内核）

Phase 2.3 遗留项，Phase 3.1 Edge + Phase 3.2 Suspend/Resume 落地后实施。与 PWC 互补：PWC=质量闭环串行（Critic 打回重做），OW=效率优先并行（动态分派 + 批间并行 Worker）。

- [x] **OrchestratorWorkers**（动态分派模式：Orchestrator 每轮 LLM 决策 dispatch/synthesize → dispatch 时分派 K 个子任务 → ThreadPoolExecutor 并行执行 Worker → 结果全部回 Orchestrator → 循环决策；Python while 控制多轮循环；JSON 结构化决策输出 + 缺字段 ValueError（消除 fallback）；max_iterations 耗尽 Python 侧强制汇总；Worker 失败 partial success（error 回传 Orchestrator 决策）；每轮结果自动累积上下文（Orchestrator + Worker 都收到）；构造函数必填 2 + 可选 9 = 11 项；2 角色内置 prompt 可覆写；返回 `OWResult(final_answer, trace: OWTrace)` 扁平结构）
- [x] **orchestrator_workers_demo.py**—— 6 项断言全可复现（dispatch >=1 轮 / 并行 Worker >=2 / 最终 synthesize / 答案关键字 / 上下文传递 / max_iterations 强制汇总），FakeLLM 7 次调用精确映射主场景（2 轮 dispatch + 1 轮 synthesize），支持 `--mock --plain` 与 `--real`

**模式包不做的事**：
- 不内置领域 Prompt / 工具集（用户按场景自己注入）
- 不替代内核原语，只组合内核原语
- 不与其他模式包互相耦合，单独可引入

**不在 Phase 2 范围**（留给 Phase 3 工作流原语或上层集成）：
- 复杂 DAG / 条件分支编排 → 直接集成 LangGraph
- 长链 Pipeline / 复杂拓扑可视化 → 上层产品职责

### Phase 3：工作流原语

同一套 Step/State/Edge/Parallel/Suspend/Journal/Scope 原语，既能表达单 Agent 循环，也能表达多 Agent 工作流。复杂 DAG / 条件分支编排直接集成 LangGraph，内核工作流原语只做"同一套原语支撑单 Agent 循环和多 Agent 工作流"的最小集。

#### 3.1 内核原语：Edge 控制流边 + Parallel 并行汇聚（控制平面，必做）

- [x] **Graph 工作流图（解释器模式）**—— `atlas.core.Graph` 运行时遍历图结构（不预编译）；命令式 API（`add_node`/`add_edge`/`add_conditional_edges`/`set_entry_point`/`set_exit_point`，和 LangGraph 对齐）；节点签名 `Callable[[dict], dict]`，Agent 和普通函数都支持；统一 `dict` in/out + `state.update(output)` merge 语义；保留字 `END` 表示终止；run() 入口 lazy 验证（缺 entry_point / 节点未注册 / 无 END 可达路径 → ValueError，不兜底）
- [x] **Edge 控制流边（普通边 + 条件边）**—— `Edge`（from_node → to_node 无条件跳转）+ `ConditionalEdge`（`condition(state)→key→mapping[key]` 动态路由）；表达顺序/分支/循环（条件边 mapping 指回 from_node 即循环）；允许环但必须有 END 可达路径或显式 exit_point（_validate 兜底防死循环）；每节点只支持一条出边，多分支用条件边表达
- [x] **Parallel 并行汇聚（ParallelNode）**—— `ParallelNode(fns)` 单节点内嵌 fan_out + and_ 汇聚；`__call__(state)` 签名和普通函数节点对齐；每个 fn 收同一份 state 深拷贝（防互相污染）；结果按 fns 顺序合并成 list 放到 `state['parallel_results']`（不是完成顺序）；fail-fast 错误传播（任一 fn 抛异常 → 取消未完成 + raise，不静默吞错，DR-8 对齐）；ThreadPoolExecutor 实现
- [x] **GraphTrace 扁平可观测存档**—— `Graph.run()` 返回 `GraphResult(final_state, trace)`；`GraphTrace` 含 `node_executions`（输入快照/输出/耗时/错误）+ `edge_routings`（from_node/condition_key/to_node 路由决策链）+ 起止时间 + 总耗时 + iterations + terminated_by；trace 完全扁平，不做嵌套，便于 Dashboard 直接渲染
- [x] **终止三来源**—— END 节点（正常结束）/ `max_iterations` 兜底（默认 50，防 runaway）/ 可选 `terminator` 全局终止条件（和 Agent 对齐，支持 MaxSteps/Timeout/CostCap 等组合器）
- [x] **插件钩子复用**—— 复用现有 `on_turn_start/on_turn_finish` 钩子（把节点执行映射为 turn 语义），不新增 Graph 专属钩子，保持钩子总数 17 不变
- [x] **react_graph 桥接函数**—— `react_graph(agent)` 把 Agent 包装成单节点 Graph，验证"Agent 循环能用 Graph 语法表达"设计假设；最小形态单节点 Graph 无循环，用户要自我修正循环可 `add_conditional_edges` 自行表达
- [x] **edge_demo.py**—— 5 项断言全可复现（节点执行顺序 / 条件边路由记录 / 最终答案 / terminated_by / state 在节点间传递）
- [x] **parallel_edge_demo.py**—— 4 项断言全可复现（3 Worker 并行执行 / 汇聚结果长度 / summarizer 收到合并 state / 总耗时 < 串行基准）

#### 3.2 Suspend/Resume（Durable HITL + 崩溃恢复，✓ 完成）

- [x] **SuspendNode 专用节点类**—— `atlas.core.SuspendNode(prompt_fn)`（和 ParallelNode 同风格）；节点执行返回 `{"__suspend__": prompt}` 标记触发挂起；无 Journal 注入时 `run()` 入口直接 ValueError 拒绝（消除 fallback，禁止非 durable 挂起）
- [x] **Graph.run() 挂起态返回**—— 返回 `GraphResult(status="suspended", suspend_prompt, suspend_token=SuspendToken(run_id, node_name, seq))`；同步阻塞语义：Graph.run() 到 SuspendNode 就返挂起态，由调用方显式决定 resume 时机
- [x] **graph.resume(token, user_input) 恢复执行**—— 同实例或"同结构 + 同 Journal 新实例（模拟进程重启 durable 恢复）"均可调用；从挂起节点的**下一条边**开始路由（不重跑 SuspendNode，避免 prompt 副作用）；user_input 必须 dict（否则 ValueError），以 `state.update(user_input)` merge 注入
- [x] **Journal 事件三类齐全**—— `graph_node_exec`（正常节点执行后）、`graph_suspend`（挂起点强制完整 state_snapshot + prompt，O(1) durable 恢复源）、`graph_resume`（审计链路：node_name+user_input+resumed_from_seq）；3 壳结构和 Journal 其他事件一致
- [x] **PluginProtocol 扩展 2 钩子**—— `on_suspend(run_id, node_name, prompt, token_dict)` + `on_resume(run_id, node_name, user_input, resumed_from_seq)`；钩子总数 17 → 19
- [x] **GraphResult 扩展**—— `status`（"completed"/"suspended"，默认"completed" Phase 3.1 老代码零成本忽略）、`suspend_prompt`、`suspend_token`；`GraphTrace.terminated_by` 新增 "suspended"
- [x] **顶层导出**—— `from atlas import SuspendNode, SuspendToken, SuspendTimeout`
- [x] **suspend_resume_demo.py**—— 7 项断言全可复现：① 挂起态字段 ② approved=True→发货 ③ state 保留+注入 ④ approved=False→取消 ⑤ Journal 事件链 ≥4 条（三种齐全）⑥ 双 Graph 实例崩溃恢复（del→新建同结构同 Journal→resume=单实例 100% 一致）⑦ `graph_resume.payload.resumed_from_seq == graph_suspend.seq`（审计链路一致）
- [x] **V1 单步 + V2 demo7 项 + V3 全回归**（11 demo 通过：quickstart/saga/parallel_subagent/journal_replay/terminator/plugin_cost/compact/scope/circuit_retry/edge/parallel_edge）

### Phase 4：RSI 自进化（上下文层 + 工具层）

**实现参考**：Skill Library 参考 **Voyager**（Chroma 向量检索 + trace-extracted 入库闸门）；Prompt 进化参考 **GEPA**（反思式变异 + Pareto 选择）和 **prompt-evolver**（leakage guardrails）；安全分层参考 **OpenHands**（Confirmation Policy + Risk Analyzer + Docker 沙箱）。

#### Phase 4.1：感知 + 记忆层（已完成）

- [x] **EmbeddingProtocol + FakeEmbedding / OpenAIEmbedding**—— `atlas.rsi.EmbeddingProtocol`（embed / embed_batch / dimension）；FakeEmbedding MD5 hash → 64 维确定性向量（demo 可复现）；OpenAIEmbedding 复用 LLM client（text-embedding-3-small 1536 维）
- [x] **VectorStoreProtocol + SQLiteVectorStore**—— 可插拔向量存储协议（add/query/get/delete/count）+ VectorHit(id, score, metadata)；默认 SQLiteVectorStore：bytes 存 SQLite + 纯 Python 余弦相似度 top-k（零外部依赖，不引入 Chroma）；threading.Lock 线程安全
- [x] **Skill Library**—— `Skill(name, description, code, embedding, version, fitness_score)` dataclass 独立于 Tool；`SkillLibrary` register（自动 embed + 版本递增）/ retrieve（语义检索 top-k）/ get / list_all / latest_version；`to_tool()` 走 subprocess 沙箱执行（禁止进程内 `exec`）
- [x] **失败记忆**—— `FailureCase` dataclass + `FailureMemory`（record/retrieve/list_recent）+ `FailureMemoryCollector(BasePlugin)` 挂 on_tool_after_call(failed)/on_retry_exhausted/on_compensate_failed 三钩子实时采集；weakref 读 Agent 上下文构造 context_summary；Agent __init__ 自动 bind_agent 注入
- [x] **inspect_self()**—— `Agent.inspect_self() -> SelfSnapshot`（system_prompt / tool_definitions / skill_summaries / recent_failures / memory_summary）；Agent 新增 skill_library + failure_memory 可选参数（None 默认，零侵入）
- [x] **rsi_demo.py**—— 8 项断言全可复现：SkillLibrary register/retrieve/版本化/to_tool + FailureMemory record/retrieve + Collector Plugin + inspect_self 空值零侵入 + FakeEmbedding 确定性
- [x] **V1 单步 + V2 demo 8 项 + V3 全回归**（13 demo 通过：quickstart/saga/parallel_subagent/journal_replay/terminator/plugin_cost/compact/scope/circuit_retry/pwc/orchestrator_workers/edge/parallel_edge/suspend_resume）

#### Phase 4.2a：修改 + 验证层 · RSI 自进化闭环（已完成）

- [x] **分层权限模型**—— `PermissionLevel` 枚举（CORE 只读 / BUFFERED 受控 / FREE 预留）+ `check_permission(target_type)`；核心层（graph/agent/tool/plugin）只读，缓冲层（skill/system_prompt）受控
- [x] **propose_diff() + evaluate() + apply_diff()**—— 上下文层范围（skill code + system prompt，不改架构）；`ProposedDiff` dataclass（target_type/target_id/current_value/proposed_value/reasoning/diff_summary）；`evaluate` 双层（测试执行 `exec(code)` + 同名函数存在 → 失败直接拒绝不调 LLM；通过后 LLM Critic 语义评估）；`apply_diff` skill 新版本入库 version 递增 / system_prompt 替换 messages[0].content
- [x] **ArchiveStore 提案归档**—— 归档所有提案（通过 + 未通过）；`list_failed()` 返回失败提案，下次 propose_diff 时摘要注入 prompt 避免重复提议
- [x] **self_evolve() 闭环**—— 一行调用封装 propose→evaluate→apply/archive；Agent 新增 `propose_diff()` + `self_evolve()` 方法 + `archive_store` 可选参数（None 默认，零侵入）
- [x] **rsi_evolution_demo.py**—— 7 项断言全可复现：propose_diff 返回 ProposedDiff / 权限校验 / 测试通过 / 测试拒绝不调 LLM / Critic 通过 / apply 版本递增 / archive 失败反馈
- [x] **V1 单步 + V2 demo 7 项 + V3 全回归**（18 demo 通过：quickstart/saga/parallel_subagent/journal_replay/terminator/plugin_cost/compact/scope/dashboard/circuit_retry/pwc/orchestrator_workers/edge/parallel_edge/suspend_resume/rsi_demo/rsi_evolution/tool_abi）

#### Phase 4.2b：Prompt 进化 · 反思式变异 + 适应度 + Leakage Guardrails（已完成）

- [x] **FitnessEvaluator 适应度评估**—— `FitnessEvaluator` Protocol（`evaluate(prompt) -> FitnessResult(score, feedback, task_results)`）+ `FakeFitnessEvaluator`（确定性序列 0.4/0.8/0.3，demo 100% 可复现）+ `TaskSetFitnessEvaluator`（真实接口预留骨架，跑任务集 + Evaluator 打分）
- [x] **反思式变异（单 lineage）**—— `_extract_fitness_history` 从 `ArchiveStore.list_recent()` 提取适应度轨迹格式化为"第 N 代 score=X feedback=Y"注入下一代 propose prompt（反思依据）+ `_extract_archived_failures` 注入失败摘要避免重复提议；不做种群（Q1=A 最小版）
- [x] **Leakage Guardrails**—— `LeakageGuard` Protocol（`check(prompt) -> LeakageCheckResult(passed, matched_patterns)`）+ `RegexLeakageGuard`（内置默认 ~10 个中英文越狱模式 + `extra_patterns` 可追加，预编译正则忽略大小写）；在 fitness 通过后、apply 前执行，命中即 archive 为 failed
- [x] **evolve_prompt() 闭环**—— `Agent.evolve_prompt() -> EvolutionResult(generation, proposal, evaluation, leakage_check, applied)`（和 self_evolve 并列，内部复用 4.2a apply_diff system_prompt 分支）；Agent 新增 `fitness_evaluator` + `leakage_guard` 可选参数 + `generation: int = 0` 计数器（apply 成功后 +1，零侵入）
- [x] **prompt_evolution_demo.py**—— 8 项断言全可复现：EvolutionResult / fitness score ∈ [0,1] / 反思式变异（第 2 代 prompt 含第 1 代 fitness 历史）/ LeakageGuard 拦截越狱 / apply 后 system_prompt 替换（generation 0→1）/ archive 记录通过+失败 / 多代进化（gen2 0.8 > gen1 0.4）/ RegexLeakageGuard 正则黑名单命中
- [x] **收敛检测（三停止条件）**—— `EvolutionSummary(generations, final_generation, final_score, stop_reason)` 汇总；`evolve_prompt_until_converged(max_generations=10, convergence_n=3, convergence_threshold=0.01, no_progress_n=3)` 循环：仅用 apply 成功代的 fitness 算收敛（applied_scores 最近 N 代 deltas 最大值 < threshold → converged）；到达 max_generations → max_generations；连续 N 代未 apply → no_progress；`STOP_CONVERGED/MAX_GENERATIONS/NO_PROGRESS` 三字符串常量
- [x] **多代回滚（durable 代数恢复）**—— `rollback_to(target_generation)`：target=0 恢复第 1 代通过提案的 proposal.current_value（原始 prompt，snapshot 强制覆盖 LLM 返回值，不可信）；target=N≥1 恢复第 N 代通过提案的 proposal.proposed_value；同步 `agent.generation = target_generation`（回退一致）；无对应通过提案直接 ValueError（不兜底，消除 fallback）；复用 apply_diff 复用 system_prompt 替换逻辑
- [x] **ArchiveStore 按代查询**—— `ArchivedProposal.generation` 字段（apply 成功传 `agent.generation` 新代，失败 -1）；`list_by_generation(generation)` 返回指定代所有归档；`get_passed_at(generation)` 返回指定代通过提案（每代至多 1 条）；回滚完全依赖 archive，无其他持久化状态
- [x] **Agent 方法追加**—— `Agent.evolve_prompt_until_converged(max_generations, convergence_n, convergence_threshold, no_progress_n)` 委托 rsi；`Agent.rollback_to(target_generation)` 委托 rsi；和 evolve_prompt/self_evolve 风格一致（Agent 薄封装 + rsi 主实现）
- [x] **prompt_converge_rollback_demo.py**—— 8 项断言全可复现：① EvolutionSummary 类型 ② Part 1 converged（Part1ConvergenceFitness 0.2→0.5→0.7→0.71→0.72→0.72→0.721 共 7 代，最近 3 代 [0.72,0.72,0.721] deltas=[0,0.001] max=0.001<0.01）③ Part 2 max_generations=2（只跑 2 代，stop=max_generations）④ Part 3 no_progress（连续 3 代 fitness<0.5 未 apply，stop=no_progress）⑤ rollback_to(0) 恢复原始 prompt + generation=0 ⑥ rollback_to(1) 恢复第 1 代 proposed_value + generation=1 ⑦ generations 列表长度（P1=7 / P2=2 / P3=3）⑧ rollback generation 回退（gen2=2 → rb0=0 → rb1=1）
- [x] **V1 单步 + V2 两个 demo 16 项 + V3 全回归**（21 demo 通过：tool_abi/terminator/plugin_cost/saga/compact/scope/edge/parallel_edge/suspend_resume/rsi_demo/rsi_evolution/taskset_fitness/prompt_evolution/prompt_converge_rollback + 其他回归；journal_replay/circuit_retry 因旧 RichRenderer.available() 遗留报错非本次修改，其余全绿）
- [x] **V1 单步 + V2 demo 8 项 + V3 全回归**（19 demo 通过：quickstart/saga/parallel_subagent/journal_replay/terminator/plugin_cost/compact/scope/dashboard/circuit_retry/pwc/orchestrator_workers/edge/parallel_edge/suspend_resume/rsi_demo/rsi_evolution/tool_abi/prompt_evolution）
- [x] **FREE 层说明**—— 4.2b 实际在 BUFFERED 层（system_prompt 受控修改 + evaluate + leakage 门控），FREE 自由层沙箱留给未来工具自生成场景

### 内核硬化（可用库，已完成）

对照 DBOS / Pydantic AI / Claude Code / OpenHands，把内核从 demo 覆盖面改成可恢复、可组装、可测试、可一键跑。不引入 Temporal，不拆三包，不继续堆 RSI。

- [x] **pytest + CI**—— `tests/` + `atlas.testing.FakeLLM`；GitHub Action 跑 `pytest tests -q` 与内核 demo `--mock --plain`
- [x] **DR-0 导入方向测试**—— `atlas.llm` 不得 import `atlas.core` / `atlas.rsi` / `atlas.patterns`（单包内锁死，不拆三包）
- [x] **Durable Agent 循环**—— Journal 必选；`Agent.run(task, run_id=None)` + `Agent.resume(run_id)`；Saga 入 Journal；崩溃后幂等短路，副作用只执行一次（`examples/agent_resume_demo.py`）
- [x] **Runtime 组装面**—— `Runtime(journal, ...)`；Agent 构造优先 `llm/tools/system_prompt/runtime`；循环在 `AgentLoop`；RSI 走 `bind_rsi()`
- [x] **工具门控**—— `NeverConfirm` / `AlwaysConfirm` / `ConfirmRisky`；高风险工具返回 `RunResult(status="suspended")`，`resume(token, {"approved": true|false})`
- [x] **Skill 沙箱**—— `to_tool()` subprocess + 超时 + builtins 白名单；禁止进程内 `exec`，不回退
- [x] **OTEL GenAI spans（opt-in）**—— `atlas[otel]` extras；未安装 no-op。`Trace` 仍给 Dashboard 投影
- [x] **分级 Compact + Attachment 重注入**—— snip → 滑窗 → LLM Summary；compact 后从活状态重建 attachment
- [x] **最小 CLI**—— `python -m atlas run --config examples/quickstart.yaml`；`--resume <run_id>` 续跑；库 API `RunResult`

### Phase 5：产品化与生态（规划中）

Phase 4 完成了 RSI 自进化基础设施（感知层 + 修改层 + Prompt 进化 + 评估器），Phase 5 聚焦"从 demo 级到产品级"的跨越：端到端体验、真实场景验证、开发者生态。

#### 5.1 端到端产品体验

- [x] **Atlas CLI 最小入口**—— `python -m atlas run --config path.yaml` 组装 Runtime+Agent 并打印答案；`--resume <run_id>` 走 Journal 续跑。不做 `atlas evolve` / RSI 面板 / Marketplace
- [ ] **Web UI 进化面板**—— Dashboard 扩展 RSI 面板：进化轨迹图（fitness 曲线 + 每代 diff 摘要）、回滚操作按钮、fitness 评估明细展开
- [x] **配置化 Agent 声明（最小）**—— YAML：`agent.system_prompt/tools/max_steps/compact_threshold` + `llm.provider/model` + `task`；缺字段 `RuntimeError`，不给静默默认。进化相关字段不在本入口
- [ ] **进化报告导出**—— 进化完成后自动生成 Markdown 报告（初始 prompt → 每代改动 + fitness → 最终 prompt + 前后对比）

#### 5.2 真实场景验证

- [ ] **编码场景 Benchmark**—— 在真实代码生成任务上验证 Prompt 进化效果（如 HumanEval / MBPP 子集，评估代码正确率 + 格式规范 + 推理质量）
- [ ] **多轮对话场景**—— 验证 Prompt 进化在多轮对话中的效果（不只是单轮 task，还包括上下文理解 + 话题切换 + 长对话 compact 后的 prompt 鲁棒性）
- [ ] **A/B 测试框架**—— `atlas ab_test` 命令：同一任务集，初始 prompt vs 进化后 prompt，跑 N 轮取均值 + 标准差 + 显著性检验
- [ ] **Safety 退化检测**—— 在 propose prompt 中注入 safety 元规则（进化不可降低安全约束），防止 Prompt 进化优化 format 维度时损害 safety 维度

#### 5.3 开发者生态

- [ ] **自定义 Evaluator 接口**—— 标准化 `FitnessEvaluator` 的 agent_factory 注入协议，用户可一行注册自定义评估器（`@atlas.evaluator("my_benchmark")`）
- [ ] **Skill Marketplace 雏形**—— SkillLibrary 支持导入/导出 JSON 包（skill code + description + version + fitness_score），用户间共享技能
- [ ] **Plugin Marketplace 雏形**—— 标准化 Plugin 打包格式（`atlas-plugin-<name>` PyPI 包，`atlas plugin install` 一键安装）
- [ ] **文档站点**—— MkDocs 自动从 README + docstring 生成文档站点，API Reference + 教程 + 示例索引

### 不做

- 权重自修改（闭源 API 不可行，安全风险不可接受）
- 完全闭环 RSI（前沿实验室都没实现，治理问题非工程问题）
- 控制流自修改 / 架构层自改写代码（Darwin Gödel Machine 式，Python 生态无工程化先例，代价过高）
- TUI / 终端 UI（Atlas 是库不是终端产品）
- Temporal / Restate / 外部 durable server（Journal 是进程内库，默认 SQLite）
- 把仓库拆成 atlas-llm / atlas-core / atlas-agent 三包（用测试锁依赖方向）
- Postgres Journal（Protocol 已在，等有多进程需求再做）
- 在图编排上与 LangGraph 正面竞争（复杂编排场景直接集成 LangGraph/Pydantic AI）
- 内置角色/SOP/Prompt（留给上层，内核只提供控制平面）
- MkDocs 文档站点 / Skill Marketplace / GEPA 种群 / FREE 层架构自改

## 开发规则

以下是所有代码提交必须遵守的红线，无论贡献者是谁：

### DR-0 依赖方向红线（对应 EP-2）

单包 `atlas`，不拆三包。由 `tests/test_dr0_imports.py` 锁死：

```
atlas.llm  严禁导入 atlas.core / atlas.rsi / atlas.patterns / atlas.cli
```

违反此规则的代码不合并。RSI 不进默认 Agent 构造，按需 `bind_rsi()`；RSI 方法在 Agent 上延迟 import `atlas.rsi`。

### DR-1 接口先行（对应 EP-2）

新增能力的顺序：
1. 写 Protocol / ABC 定义
2. 写空实现（raise NotImplementedError）
3. 写示例用例（`examples/`）
4. 写具体实现
5. 跑通示例

跳过任何一步的 PR 不合并。

### DR-2 配置项极限（对应 EP-4）

每个模块的配置项不得超过 **5 个必填 + 10 个可选**。超过时必须：
- 分拆模块（说明职责过重），或
- 把多余项下沉到实现层，不暴露给用户

### DR-3 依赖审批（对应 EP-1）

新增第三方依赖必须满足以下任一条件：
- Python 标准库（零成本）
- 已在 `pyproject.toml` 中（无需新增）
- 纯 Python 实现、无二进制依赖、< 100KB、无 transitive 依赖膨胀

否则需在 Issue 中明确论证"为何不使用标准库/已存在依赖"，经审批后才能加入。

### DR-4 示例必附（对应 EP-4）

任何 new feature 提交必须包含：
- `examples/<feature-name>.py` —— 最小可运行脚本
- 脚本头部注释写明：**预期行为** + **如何判断成功**
- 脚本运行无任何外部准备（无需手工建表、手工拷文件），`python examples/xxx.py` 直接跑

### DR-5 日志结构化（对应 Saga 补偿日志规范）

所有 `log.info/warn/error` 必须：
- 第一个参数是事件名（小写 + 点分隔，如 `saga.step.execute`）
- 所有上下文用关键字参数传入（不是拼字符串）
- 不直接打印 `print()`，全部走 `logging`

错误案例：
```python
# 错误：拼字符串
log.info(f"执行步骤 {step_id} 成功，耗时 {elapsed}ms")
# 错误：print
print("补偿失败")
```

正确案例：
```python
# 正确：结构化
log.info("saga.step.execute",
    run_id=ctx.run_id, step_id=step_id, duration_ms=elapsed, status="success")
# 正确：走 logging
log.error("saga.compensate.failed", step_id=step_id, error=str(e))
```

### DR-6 可插拔性测试（对应 EP-2）

新增后端实现（如 PostgreSQL Journal）必须：
- 与默认实现（SQLite Journal）通过同一套 Protocol 抽象测试
- 用户只需改一行配置即可切换

### DR-7 无前端工具链（对应 EP-3）

Dashboard 严禁引入 npm / React / Vue / Webpack 等前端工具链。允许使用：
- 单 HTML 文件 + 原生 ES Module
- CDN 引入的轻量库（如 `htmx`、`Chart.js`），不做打包构建
- Python `http.server` + WebSocket（`websockets` 库）作为服务

### DR-8 命名一致性

- Protocol 后缀：`xxxProtocol`（如 `JournalProtocol`），不用 `Ixxx` 前缀
- 具体实现：`<特性><Protocol名>`（如 `SQLiteJournal`、`ChromaSkillLibrary`）
- CLI 子命令：全小写 + 短横线（`atlas init`、`atlas dashboard`、`atlas run`）
- 日志事件名：点分隔（`saga.step.execute`、`dashboard.run.start`）

## 贡献

欢迎通过 Issue 提交问题与建议，或通过 Pull Request 贡献代码。提交前请确保：

- `python -m pytest tests -q` 通过
- 代码风格一致，注释使用中文
- 新增能力有对应示例
- 不引入破坏性 fallback（消除 fallback 触发场景，而非加更多兜底）

## License

MIT

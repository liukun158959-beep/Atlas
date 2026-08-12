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
- 依赖方向严格单向：`atlas-agent → atlas-core → atlas-llm`，绝不反向
- 对外暴露 Protocol / ABC，不是具体类
- 具体实现通过配置 + 注册表注入，不 hardcode
- 每个模块可以独立替换（如 Journal 从 SQLite 换成 PostgreSQL 只需改配置项）
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
|                      atlas-agent（顶层：产品）                       |
|  多 Agent 编排 / RSI 自进化引擎 / 预设场景（编码/研究/工作流）        |
+--------------------------------------------------------------------+
                                |
+--------------------------------------------------------------------+
|                      atlas-core（中层：引擎）                       |
|  AgentLoop（事件流 + 钩子 + steering）                              |
|  9 个核心原语（Step/State/Journal/Scope/终止条件/Edge/Suspend/幂等/trace）|
|  可插拔持久化后端（SQLite 默认 / Temporal 可选）                     |
|  验证基础设施（Schema 校验 + 独立 Evaluator）                        |
|  Compact 机制（Summary + Boundary + 状态重注入）                    |
|  Attachment 机制（程序状态传递给模型）                               |
+--------------------------------------------------------------------+
                                |
+--------------------------------------------------------------------+
|                      atlas-llm（底层：模型抽象）                     |
|  统一 API + provider 预设 / 流式 + Token 追踪 / 重试                 |
+--------------------------------------------------------------------+
```

依赖方向永远向上，底层对上层一无所知。用户可以只用 `atlas-llm` 调模型，也可以用 `atlas-core` 跑 Agent Loop，完全不需要碰顶层。

## 9 个核心原语

| 优先级 | 原语 | 作用 |
|---|---|---|
| P0 | **Step（durable 步骤）** | 包装 LLM/工具调用，输入输出入 journal，崩溃可重放 |
| P0 | **State（可持久化状态）** | 类型化共享状态，4 层分离（external/workflow/conversation/memory） |
| P0 | **Journal（执行日志）** | append-only 事件日志，event sourcing 恢复，不是状态快照 |
| P0 | **Scope（隔离单元）** | 按 key 隔离 Agent 实例，同 key 串行、跨 key 并行 |
| P0 | **终止条件组合器** | max_iter / timeout / token_budget / cost_cap / repetition_detector 用 \| 组合，默认开启 |
| P1 | **Edge（控制流边）** | 普通/条件边，表达顺序/分支/循环，用同一套原语表达循环和图 |
| P1 | **Suspend/Resume（HITL）** | durable 挂起等待外部输入，零成本等待，崩溃不丢 |
| P1 | **幂等键 + Saga 补偿** | 副作用工具自动注入 idempotency_key，不可幂等动作必须注册补偿 |
| P2 | **OTEL trace** | 每决策点产 span，trace_id 贯穿 run→subagent→step→tool_call |

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
- **Checkpoint ≠ Durable Execution** —— 内核做执行日志（event sourcing/journal），不是状态快照
- **可插拔持久化后端** —— 开发用 SQLite/内存（零部署），生产接 Temporal/Restate
- **HITL = durable wait** —— 挂起等待外部输入，崩溃不丢，不用进程内 sleep

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

**Compact 机制（已实现）**：
- **触发条件**：默认 `compact_threshold=8000`（Agent 参数，`None` 禁用）。每次 LLM 调用后从 `usage.prompt_tokens` 读取真实 token 数 → `memory.update_token_count()` → 超阈值时 `CompactManager.compact()` 自动触发。
- **生成 9 段 Summary**：构造 9 段模板 prompt（用户目标/已完成步骤/关键发现/当前状态/待办事项/关键文件资源/错误与教训/决策记录/下一步建议）→ 复用 Agent 的 LLM 生成 Summary 文本（无 LLM 时退化为规则提取）。
- **写入 Summary + Boundary 对**：`memory.add_summary()` 依次写入 `type=compact_summary`（assistant role, isMeta=True）+ `type=compact_boundary`（system role, isMeta=True）两条消息；同时重置 `memory.reset_token_count()`（下次从 Boundary 后重新累计）。
- **project() 投影**：找最后一个 Boundary → 保留 system prompt + Summary + Boundary 之后的消息 → isMeta 过滤（Attachment 转 system，Summary 转 assistant，Boundary 跳过）→ 修 tool_call 配对 → 超阈值从最旧 user/assistant 对裁剪到阈值 × 0.7。
- **Transcript 双写**：Memory 写入同时 `agent.transcript.append()`，append-only 事件存档 + JSONL 导出/恢复（`transcript.to_jsonl(path)` / `Transcript.from_jsonl(path)`）。

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
- 插件体系：PluginProtocol 10 个生命周期钩子（含 `compensate_start/compensate_finish/compensate_failed` 新增 3 个）+ PluginManager 错误隔离分发，预置 TokenCostPlugin / FileLoggerPlugin
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

## 技术栈

- Python 3.11+
- 依赖管理：`pip` + `venv`（不引入 uv，保持零额外工具依赖）
- LLM 接入：兼容 OpenAI API 协议的模型均可接入
- 终端渲染：`rich>=13.0`（RichRenderer），可退化为纯文本
- 配置：`PyYAML>=6.0`（声明式 Role 与任务配置）
- 持久化（规划中）：SQLite 默认 / Temporal 可选（可插拔后端）

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

```python
from dotenv import load_dotenv
from atlas import Agent, LLM, Toolkit
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
    # 注入 RichRenderer 即启用终端 Markdown 渲染（粗体/列表/代码块/LaTeX 近似），加 --plain 可退回纯文本
    renderer=RichRenderer(),
)
agent.run("先告诉我现在几点，然后计算 365 * 24 等于多少")

# 查看执行轨迹
print(agent.trace)
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

多 Agent 协作示例（Planner-Worker-Critic 架构，渲染统一走 Renderer）：

```bash
python examples/multi_agent_demo.py                     # 默认 RichRenderer
python examples/multi_agent_demo.py --plain             # 纯文本模式
```

Phase 1 地基新能力示例（全部支持 `--mock` 用 FakeLLM 跑逻辑、`--plain` 纯文本）：

```bash
# Tool ABI 增强：三分类标志 + validate_input 校验
python examples/tool_abi_demo.py --mock

# Journal event sourcing：12 事件全生命周期 + replay 聚合
python examples/journal_replay_demo.py --mock --plain

# 终止条件组合器：MaxSteps / Timeout / RepetitionDetector 三种触发
python examples/terminator_demo.py --mock --plain

# 插件体系 + 预算护栏：TokenCostPlugin 累计 + CostCap 终止
python examples/plugin_cost_demo.py --mock --plain

# Phase 2.1 收尾：幂等键命中跳过 + Saga LIFO 补偿（write_po + charge 成功 → ship FAIL-SHIP-500 → 终止 → 补偿清零）
# 100% 可复现；末尾自带 SideEffectTracker.is_empty() 断言
python examples/saga_demo.py --mock --plain

# Phase 1 第一批：Compact 机制（9 段 Summary + Boundary + Transcript + project 投影）
# --mock 必触发 compact（第 4 轮 token=8500 > 8000），5 项断言全通过
python examples/compact_demo.py --mock --plain
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
│   ├── core/               # Agent 内核
│   │   ├── agent.py        # ReAct 循环 + role_configs/tool_specs_pool/spawn_subagents/terminator/plugin_manager/journal 注入 + 幂等键 cache 命中跳过 + SagaStack LIFO 补偿 + compensate_* 三插件钩子
│   │   ├── evaluator.py    # Phase 2.1 新增：Evaluator Protocol + ArtifactSchemaEvaluator（JSON Schema 契约校验，内核外独立调用）
│   │   ├── terminator.py   # 终止条件组合器：MaxSteps/Timeout/TokenBudget/CostCap/RepetitionDetector，| 运算符组合
│   │   ├── multi_agent.py  # 并行子 Agent 内核原语：RoleConfig/SubagentSpec/SubagentResult + parallel_gather + make_spawn_subagents_tool
│   │   ├── orchestrator.py # Planner-Worker-Critic 多 Agent 协作编排器（Renderer 统一渲染）
│   │   └── plugin.py       # PluginProtocol + PluginManager + BasePlugin（钩子 no-op 基类）
│   ├── journal/            # 执行日志 event sourcing
│   │   └── base.py         # JournalProtocol 扩 cache_get/cache_set + JournalEvent + InMemoryJournal（摘要模式默认，opt-in 全量 + _cache 幂等存储）
│   ├── utils/              # 工具函数（Phase 2.1 扩展：幂等键 + 副作用追踪器）
│   │   ├── idempotency.py  # Phase 2.1 新增：derive_idempotency_key(run_id, tool_name, input) → SHA-1 40 位 + IDEMPOTENCY_USER_KEY='_idempotency_key' 常量
│   │   ├── tracker.py      # Phase 2.1 新增：SideEffectTracker record/erase/is_empty 可观测 Saga 补偿覆盖度
│   │   └── yaml_loader.py  # load_parallel_subagent_config()：YAML → RoleConfig + parent + task
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
│   │   ├── base.py         # Tool（含 isReadOnly/isIdempotent/hasSideEffects + compensate 字段 + validate_input + parameters_factory）/ Toolkit.register() 升级补偿约束为 ValueError 拒绝
│   │   └── builtin.py      # calculator / get_current_time（均标记 isReadOnly=True）
│   ├── memory/             # 会话记忆
│   │   └── base.py         # Phase 1 改造：messages 改 @property + _runtime_messages + project() 五步投影 + token 跟踪 + add_attachment()/add_summary()
│   ├── context/            # 执行轨迹与上下文（Phase 1 扩 4 文件）
│   │   ├── attachment.py   # Phase 1 新增：Attachment ABC + AttachmentRegistry（register/create 模式，重名 ValueError 拒绝）
│   │   ├── transcript.py   # Phase 1 新增：Transcript append-only 事件存档 + JSONL 导出/恢复，与 Memory 双写
│   │   ├── compact.py      # Phase 1 新增：CompactManager（9 段 Summary + should_compact 阈值判断 + LLM/规则双模式）
│   │   └── trace.py        # Trace + StepRecord + 三段式递归 render + subagent_traces 嵌套 + Trace.meta
│   ├── cli/                # CLI 子命令
│   │   └── init.py         # atlas init：venv + 依赖安装 + .env 配置
│   ├── providers.py        # LLM provider 预设表
│   └── __main__.py         # CLI 入口：python -m atlas
├── examples/
│   ├── quickstart.py       # 快速开始示例（支持 RichRenderer / --plain）
│   ├── saga_demo.py        # Phase 2.1 新增：Saga 补偿完整流程（write_po + charge 成功 → ship FAIL-SHIP-500 → MaxSteps(3) 终止 → LIFO 补偿清零；SideEffectTracker 断言）
│   ├── parallel_subagent_demo.py     # Phase 2-1 并行子 Agent demo（YAML 声明式配置，< 120 行，--config 可指定）
│   ├── parallel_subagent_config.yaml # 声明式 Role + 父 Agent + 任务配置（被 yaml_loader 读取）
│   ├── multi_agent_demo.py # Planner-Worker-Critic 示例（Renderer 统一渲染）
│   ├── tool_abi_demo.py    # Tool ABI 增强：三分类标志 + validate_input 通过/失败演示 + compensate 注册
│   ├── journal_replay_demo.py  # Journal event sourcing：12 事件全生命周期 + replay 聚合
│   ├── terminator_demo.py  # 终止条件组合器：MaxSteps/Timeout/RepetitionDetector 三种触发
│   ├── plugin_cost_demo.py # 插件体系 + 预算护栏：TokenCostPlugin 累计 + CostCap 终止
│   └── compact_demo.py     # Phase 1 新增：Compact 机制演示（--mock 第 4 轮 token=8500 触发，5 项断言）
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
- [x] **Compact 机制（Summary + Boundary + 状态重注入）**—— CompactManager；默认 threshold=8000；9 段结构化 Summary 复用 Agent LLM 生成；type=compact_summary + type=compact_boundary 消息对；Attachment 作为未来 state re-injection 载体已就绪
- [x] **历史投影层 `project()`（找 Boundary → 过滤 → 转 attachment → 修配对 → 裁剪）**—— 五步流水线：找最后 compact_boundary → 保留 system prompt + Summary + Boundary 后消息 → isMeta 分层转换（Attachment→system / Summary→assistant / Boundary→跳过）→ tool_call 配对修复（删除断裂 tool_calls 和无配对 tool 消息）→ 超阈值裁剪最旧 user/assistant 对到 target=threshold×0.7
- [x] **Attachment 抽象基类 + 类型注册表 + 默认 system-reminder 转换器**—— Attachment ABC + AttachmentRegistry（register/create 模式）；重名 ValueError 拒绝 + TypeError 子类校验；to_system_reminder() 子类覆写 + to_message() Runtime 消息生成；Memory.add_attachment() 便捷写入
- [x] **Journal（执行日志，event sourcing 恢复）**—— `InMemoryJournal` + `JournalProtocol`（SQLite 实现预留）；事件结构折中（壳 dataclass + 自由 payload dict）；摘要模式默认 mask 敏感字段 + 截断 200 字，`save_full_payload=True` opt-in 全量
- [ ] Scope（按 key 隔离 Agent 实例）—— 参考 Restate Virtual Object 纯 Python 复刻（per-key threading.Lock + 内存状态；context manager acquire(key)；未来 SQLite 持久化后端可插拔）
- [x] **终止条件组合器**（MaxSteps / Timeout / TokenBudget / CostCap / RepetitionDetector）—— `|` 运算符组合，`CompositeTerminator` 任一命中即终止；默认 `MaxSteps(10) | Timeout(60_000)` 与旧版完全兼容
- [x] **插件体系（接入 Agent 全生命周期）**：`PluginProtocol` + `PluginManager` + `BasePlugin` 钩子基类（子类按需覆写 no-op）；10 个生命周期 + UUID run_id + 全链路 step_id；预置 `TokenCostPlugin` / `FileLoggerPlugin` 真实工作
- [x] **声明式 YAML Loader**：`atlas.utils.yaml_loader.load_parallel_subagent_config()` 严格模式（缺字段抛 RuntimeError，不兜底）；`parallel_subagent_demo.py` 默认读 YAML，支持 `--config`
- [ ] **极简 Dashboard 骨架**：`python -m atlas dashboard` 一键启动，纯 HTML+WebSocket，展示 run 列表 + 单 run DAG 流程图 + 工具调用详情（对应 EP-3）

### Phase 2：多 Agent 稳定性

Phase 2 严格遵循"内核只提供原语、上层组合 SOP"的分层原则。多 Agent 协作的"角色/SOP/Prompt"不属于内核，内核只提供让上层能稳定组合出任意模式的原语；常见模式（PlannerWorkerCritic / OrchestratorWorkers）作为上层可选包提供，不进内核、不取代用户自定义模式。

#### 2.1 内核原语（控制平面，必做）

- [x] **Tool ABI 增强（分类标志 + validateInput + compensate 字段）**—— 三标志（`isReadOnly`/`isIdempotent`/`hasSideEffects`，默认全 False）；注册时 `isReadOnly=True` 自动强制幂等，`hasSideEffects+非幂等` **ValueError 直接拒绝注册（无 compensate 不兜底）**；`Tool.validate_input()` 走 `jsonschema`；`parameters_factory` 懒加载闭环；`tool_status` 三态（success / failed / idempotency_hit）
- [x] **Hook 机制（插件 13 生命周期，原 10 新增 compensate 三钩子）**—— `on_agent_start/finish`、`on_turn_start/finish`、`on_llm_before/after_call`、`on_tool_before/after_call`、`on_compensate_start/finish/failed`；`BasePlugin` no-op 基类，子类按需覆写；配合 `run_id`（UUID）+ `step_id` 全链路可追溯
- [x] **Role 抽象**（`RoleConfig`：system_prompt + tool_names + temperature/max_steps/timeout_ms 三元组，Python 编程式注入，YAML 声明式由上层 `parallel_subagent_config.yaml` 承载）—— 角色定义不硬编码在内核，由上层注入
- [x] **`spawn_subagents` 内置工具**（`make_spawn_subagents_tool` 闭包工厂 + `_inject_spawn_tool` 自动注入 + role enum 懒加载 + 嵌套守卫双保险；context 隔离：子 Agent 独立 Memory/Trace；artifact schema 校验：`jsonschema.validate` 不改写原文仅打标记）
- [x] **`parallel_gather` 原语**（ThreadPoolExecutor 并行 fan-out/gather + enumerate O(1) 顺序保持 + partial success 错误隔离 + 并行效率 meta）—— 持久化/幂等防重留给 Journal 原语
- [x] **预算护栏（terminator 侧可触发）**—— `CostCap(max_usd)` + `TokenBudget(max_tokens)` 终止条件；`TokenCostPlugin` 累计 `cost_usd` 写入 `_term_ctx`；三态熔断器留后续 Phase 2.2
- [x] **幂等键 + Saga 补偿注册**—— `atlas.utils.idempotency.derive(run_id, tool_name, input_dict)` SHA-1 40 位派生或 `_idempotency_key` 显式覆盖；Journal 扩 `cache_get/cache_set` 命中跳过真实 func；Agent 无最终答案时 LIFO 反向 compensate 非幂等副作用工具；失败不重试，记录 compensate_failed 插件钩子 + 尾附文字提示人工介入
- [x] **独立 Evaluator（校验门，不让 Agent 自评）**—— `Evaluator Protocol`（@runtime_checkable）+ 内置 `ArtifactSchemaEvaluator`（JSON Schema 契约 list[(key, schema)]）；顶层 `from atlas import Evaluator, ArtifactSchemaEvaluator`；用户在 agent.run() 之后外部调用
- [x] **Saga 补偿演示模式**：`examples/saga_demo.py` 一键跑通 write_po→charge→ship(FAIL)→终止→LIFO 补偿→SideEffectTracker.is_empty() 断言（对应 EP-4，100% 可复现，支持 --mock --plain）
- [ ] **Dashboard 增强**：展示补偿日志、重试状态、告警列表（对应 EP-3）

#### 2.2 多 Agent 模式包（上层可选，非内核）

模式包定位为"参考实现 + 可直接组装的产品级模式"，位于 `atlas.patterns`（独立于 `atlas.core`）。用户可以不用这些包、只用内核原语自己组装任意模式；也可以引用其中一种作为起点。模式包之间互不依赖，按需引入。

- [ ] `PlannerWorkerCritic`（最常用模式参考实现：任务分解 → 并行 Worker → Critic 反馈 → 结果聚合，基于 `spawn_subagent` + `durable_gather` 组装）
- [ ] `OrchestratorWorkers`（Anthropic +90.2% 模式：Orchestrator 动态分派任务给 Worker，结果回 Orchestrator 决策下一步，支持动态增减 Worker）
- [ ] 每个模式包自带 `examples/<pattern>_demo.py`，含"预期输出"注释，可直接运行验证（对应 EP-4）

**模式包不做的事**：
- 不内置领域 Prompt / 工具集（用户按场景自己注入）
- 不替代内核原语，只组合内核原语
- 不与其他模式包互相耦合，单独可引入

**不在 Phase 2 范围**（留给 Phase 3 工作流原语或上层集成）：
- 复杂 DAG / 条件分支编排 → 直接集成 LangGraph
- 长链 Pipeline / 复杂拓扑可视化 → 上层产品职责

### Phase 3：工作流原语

- [ ] Edge（普通/条件边，表达顺序/分支/循环）
- [ ] Parallel（扇出并行 + and_/or_ 汇聚）
- [ ] Suspend/Resume（durable HITL，不用进程内 sleep）
- [ ] 可插拔持久化后端（SQLite 默认 / Temporal 可选）

### Phase 4：RSI 自进化（上下文层 + 工具层）

**实现参考**：Skill Library 参考 **Voyager**（Chroma 向量检索 + trace-extracted 入库闸门）；Prompt 进化参考 **GEPA**（反思式变异 + Pareto 选择）和 **prompt-evolver**（leakage guardrails）；安全分层参考 **OpenHands**（Confirmation Policy + Risk Analyzer + Docker 沙箱）。

- [ ] Skill Library（注册/检索/版本化，三件套：代码+描述+embedding）—— Critic 判定成功才入库，劣质技能不进库
- [ ] 失败记忆（失败案例 > 成功案例，embedding 检索）—— 对比反思式一次性再生
- [ ] `inspect_self()` + `propose_diff()` + `evaluate()` + `archive`（上下文层范围：prompt/skill，不改架构）
- [ ] 分层权限模型（核心层只读 / 缓冲层受控 / 自由层沙箱）—— 高风险动作要求人类确认
- [ ] Prompt 进化（GEPA 式反思遗传 + leakage guardrails）

### 不做

- 权重自修改（闭源 API 不可行，安全风险不可接受）
- 完全闭环 RSI（前沿实验室都没实现，治理问题非工程问题）
- 控制流自修改 / 架构层自改写代码（Darwin Gödel Machine 式，Python 生态无工程化先例，代价过高）
- TUI / 终端 UI（Atlas 是库不是终端产品）
- 自建完整 durable execution runtime（做成可插拔后端，不自造）
- 在图编排上与 LangGraph 正面竞争（复杂编排场景直接集成 LangGraph/Pydantic AI）
- 内置角色/SOP/Prompt（留给上层，内核只提供控制平面）

## 开发规则

以下是所有代码提交必须遵守的红线，无论贡献者是谁：

### DR-0 依赖方向红线（对应 EP-2）

```
atlas-llm  严禁导入 atlas-core / atlas-agent
atlas-core 严禁导入 atlas-agent
atlas-agent 可以导入所有下层
```

违反此规则的代码不合并。

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
- CLI 子命令：全小写 + 短横线（`atlas init`、`atlas dashboard`）
- 日志事件名：点分隔（`saga.step.execute`、`dashboard.run.start`）

## 贡献

欢迎通过 Issue 提交问题与建议，或通过 Pull Request 贡献代码。提交前请确保：

- 代码风格一致，注释使用中文
- 新增能力有对应示例
- 不引入破坏性 fallback（消除 fallback 触发场景，而非加更多兜底）

## License

MIT

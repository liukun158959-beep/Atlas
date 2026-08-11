# core - Agent 内核

atlas/core/ 模块是 Atlas 的核心，实现单 Agent 的 ReAct 循环与多 Agent 协作编排。

## 文件结构

```
atlas/core/
├── agent.py        # 单 Agent：基于 Function Calling 的 ReAct 循环
└── multi_agent.py  # 多 Agent 协作：Planner-Worker-Critic 架构
```

## agent.py - 单 Agent 内核

### 模块职责

基于 Function Calling 的 ReAct 循环。每一步：
1. LLM 读取当前 memory 与可用工具进行推理
2. 若返回 tool_calls，执行对应工具并把结果回填到 memory，进入下一步
3. 若无 tool_calls，则视为 LLM 已给出最终答案

### 核心类：Agent

```python
class Agent:
    def __init__(
        self,
        llm: LLM,
        tools: Toolkit,
        system_prompt: str | None = None,
        max_steps: int = 10,
    ) -> None:
        ...
```

**参数说明：**
- llm：LLM 客户端实例
- tools：工具集合（Toolkit 实例）
- system_prompt：系统提示词（可选）
- max_steps：最大推理步数，默认 10，防止无限循环

### 核心方法

#### run(user_input: str) -> str

运行一次任务，返回最终回答。

执行流程：
1. 把用户输入加入 memory
2. 导出 OpenAI 协议工具定义
3. 进入 ReAct 循环（最多 max_steps 步）
4. 每步：LLM 推理 → 有 tool_calls 就执行回填 → 无则返回答案
5. 工具执行失败会把错误信息回填给 LLM，让它自行调整（不是 fallback，是有效反馈）

#### reset() -> None

重置会话记忆与轨迹，开始新任务。保留 system prompt。

### 设计决策

1. **用 Function Calling 而非文本解析**：现代 LLM 原生支持 function calling，比解析 "Thought: ... Action: ..." 文本更可靠
2. **消息格式用 OpenAI dict**：直接存入 memory，避免无谓的类型转换
3. **工具失败不抛异常**：把错误回填给 LLM 是 ReAct 循环的核心语义，不是隐藏错误的 fallback
4. **max_steps 防死循环**：达到上限返回提示，不无限等待

## multi_agent.py - 多 Agent 协作

### 模块职责

Planner-Worker-Critic 三角色协作架构：
- Planner：拆解任务为子任务列表，最后汇总
- Worker：逐个执行子任务，可调用工具
- Critic：审查 Worker 的结果，决定通过或打回

协作流程：
```
任务 → Planner 拆解 → Worker 逐个执行 → Critic 审查
    ├─ 通过 → 进入下一子任务
    └─ 打回 → Worker 重做（最多 max_revisions 次）
全部子任务完成 → Planner 汇总 → 最终答案
```

### 核心类

#### SubTask

单个子任务的数据结构：
```python
@dataclass
class SubTask:
    description: str
    status: str = "pending"  # pending / running / done / failed
    result: str = ""
    revisions: int = 0  # 已被 Critic 打回重做的次数
```

#### MultiAgentTrace

多 Agent 协作的执行轨迹，记录 Plan/Worker/Critic/Final 每一步。

#### MultiAgentOrchestrator

多 Agent 协作编排器：

```python
class MultiAgentOrchestrator:
    def __init__(
        self,
        llm: LLM,
        tools: Toolkit,
        max_revisions: int = 2,
    ) -> None:
        ...
```

### 核心方法

#### run(task: str) -> str

运行完整的多 Agent 协作流程：
1. Planner 拆解任务为 2-4 个子任务
2. 逐个执行子任务，前面子任务结果作为后面子任务的上下文
3. 每个子任务执行后由 Critic 审查
4. 不通过则重做，最多 max_revisions 次
5. 全部完成后 Planner 汇总生成最终答案

### 内部方法

- _planner_decompose(task)：Planner 把任务拆成子任务列表
- _worker_execute(subtask, context)：Worker 执行单个子任务，带简化 ReAct 循环（最多 3 步工具调用）
- _critic_review(subtask, result)：Critic 审查结果，返回 (是否通过, 评语)
- _planner_summarize(task, subtasks)：Planner 汇总所有子任务结果

### 设计决策

1. **三角色独立 LLM 调用**：Planner/Critic 不带工具（只推理），Worker 带 toolkit
2. **上下文传递**：前面子任务的结果作为后面子任务的 context，避免重复劳动
3. **Critic 反馈循环**：不通过就重做，max_revisions 防死循环
4. **完整 trace**：MultiAgentTrace 记录每一步，可观测可调试

### 已知问题

Critic 打回后 Worker 重做时，当前没有把 Critic 的打回理由传给 Worker，导致 Worker 可能重复同样的错误。改进方向：_worker_execute 增加 feedback 参数，重做时把 Critic 评语拼进 prompt。

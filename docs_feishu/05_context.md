# context - 上下文层

atlas/context/ 模块记录执行轨迹，便于调试与回放。

## 文件结构

```
atlas/context/
└── trace.py    # Trace / StepRecord：执行轨迹记录
```

## trace.py - 执行轨迹

### 模块职责

记录 Agent 每一步的推理-行动-观察三元组，便于调试与回放。让 Agent 的执行过程可观测、可中断、可回放，避免黑盒执行。

### 核心类

#### StepRecord

单个推理步骤的记录：

```python
@dataclass
class StepRecord:
    step: int
    thought: str  # 本步 LLM 输出的文本内容
    tool_calls: list[dict] = field(default_factory=list)  # 本步发起的工具调用
    observations: list[str] = field(default_factory=list)  # 对应的工具返回结果
```

#### Trace

执行轨迹容器，按步聚合推理-行动-观察三元组：

```python
class Trace:
    def __init__(self) -> None:
        self.steps: list[StepRecord] = []
```

### 核心方法

#### add(record: StepRecord) -> None

追加一步记录。

#### reset() -> None

清空所有步骤。

### __str__ 输出格式

```python
def __str__(self) -> str:
    if not self.steps:
        return "(无执行轨迹)"
    lines: list[str] = []
    for r in self.steps:
        lines.append(f"=== Step {r.step} ===")
        if r.thought:
            lines.append(f"[Thought]  {r.thought}")
        for i, call in enumerate(r.tool_calls):
            fn = call.get("function", {})
            lines.append(f"[Action]   {fn.get('name')}: {fn.get('arguments')}")
            if i < len(r.observations):
                lines.append(f"[Observe]  {r.observations[i]}")
    return "\n".join(lines)
```

### 输出示例

```
=== Step 1 ===
[Thought]  我需要先获取当前时间，再计算 365 * 24
[Action]   get_current_time: {}
[Observe]  2026-08-11 18:18:00
=== Step 2 ===
[Thought]  现在我来计算 365 * 24
[Action]   calculator: {"expression": "365 * 24"}
[Observe]  365 * 24 = 8760
=== Step 3 ===
[Thought]  现在是 2026-08-11 18:18，365 * 24 = 8760
```

### 设计决策

1. **按步聚合**：StepRecord 把一步的 Thought/Action/Observe 聚合在一起，而不是分散存储
2. **__str__ 友好输出**：直接 print(trace) 就能看到完整执行过程，便于调试
3. **不依赖外部格式**：不用 JSON/YAML，纯文本输出即可读
4. **observations 与 tool_calls 按索引对应**：第 i 个 observation 对应第 i 个 tool_call 的结果

## 与 multi_agent.py 的关系

multi_agent.py 的 MultiAgentTrace 是多 Agent 协作专用的轨迹，结构不同于单 Agent 的 Trace：
- plan：子任务列表
- worker_runs：Worker 每次执行记录
- critic_reviews：Critic 每次审查记录
- final_summary：Planner 最终汇总

两者独立，不共享基类，因为单 Agent 与多 Agent 的执行结构差异较大，强行抽象会增加复杂度。

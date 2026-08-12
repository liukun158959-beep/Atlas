---
title: Atlas 并行子 Agent 实现
---

# Atlas 并行子 Agent 实现

## 背景

单 Agent 循环能处理串行任务，但当多个子任务互相独立时，串行执行效率低下。Atlas Phase 2 的目标之一是实现**基于 `spawn_subagent` + `durable_gather` 的并行多 Agent 协作**。

本文档描述当前 demo 实现（位于 `examples/parallel_subagent_demo.py`），为 Phase 2 内核原语落地提供参考。

## 两个核心原语

### spawn_subagent

父 Agent 通过 function calling 调用 `spawn_subagents` 工具，一次性启动多个子 Agent。

```python
@dataclass
class SubagentSpec:
    role: str           # 角色名（必须在 YAML roles 中声明）
    task: str           # 子任务描述
    # 可选覆盖
    max_steps: int | None = None
    extra_system_prompt: str | None = None

@dataclass
class SubagentResult:
    role: str
    task: str
    artifact: str       # 子 Agent 输出的结构化结论
    duration_ms: int
    error: str | None = None
```

**关键特性**：
- **context 隔离**：每个子 Agent 独立 `Memory`，不继承父对话
- **artifact 返回**：子 Agent 只返回最终结论，不返回内部对话历史
- **工具白名单**：按 Role 过滤工具，子 Agent 只能看到声明的工具
- **串行防重**：同一 Role 名的子 Agent 串行执行，不同 Role 可并行

### durable_gather

并行 fan-out 多个子 Agent，gather 汇总结果。

```python
async def durable_gather(
    specs: list[SubagentSpec],
    llm: LLM,
    role_configs: dict[str, RoleConfig],
    all_tool_specs: list[dict],
) -> list[SubagentResult]:
    """并行执行多个子 Agent，返回结果列表。"""
    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        coros = [
            loop.run_in_executor(pool, _run_subagent, spec, ...)
            for spec in specs
        ]
        results = await asyncio.gather(*coros, return_exceptions=False)
    return results
```

**为什么叫 durable**：
- 每个子 Agent 的执行记录可写入 Journal（Phase 2 落地时）
- 幂等键防止重复执行
- 崩溃后可从断点续跑

## 执行流程

```
父 Agent (ParallelParentAgent)
    │
    ├── 1. 分析用户任务，识别独立子任务
    │
    ├── 2. LLM 调用 spawn_subagents 工具
    │       │  inputSchema: { specs: [{role, task}...] }
    │       │  role 字段带 enum 约束（从 YAML roles 自动生成）
    │
    ├── 3. spawn_subagents 工具内部
    │       │
    │       ├── 3a. 校验 role 是否存在于 role_configs
    │       ├── 3b. durable_gather 并行执行子 Agent
    │       │       ├── subagent_1 (researcher) ──┤
    │       │       ├── subagent_2 (calculator) ──┤── asyncio.gather
    │       │       └── subagent_3 (researcher) ──┤
    │       │
    │       └── 3c. 汇总 SubagentResult 列表返回
    │
    └── 4. 父 Agent 收到结构化 artifact 列表
            → 汇总生成最终答案
```

## 实测效果

**硬件环境**：Windows 11 / Python 3.11 / 单 LLM 实例

**测试场景**：3 个独立子任务（GIL 调研 / 数值计算 / asyncio 对比）

| 指标 | 数值 |
|---|---|
| 子 Agent 数 | 3 |
| 串行理论耗时 | 18325ms |
| 并行实际耗时 | 7780ms |
| **加速比** | **2.36x** |

**加速比分析**：
- 理想情况下 3 个子 Agent 并行应接近 3x 加速
- 实际 2.36x，原因：LLM 调用是 IO 密集型，受网络延迟波动影响
- 加速比 > 1 即证明并行生效

## 输出结构

### 最终答案

父 Agent 汇总 3 个子 Agent 的 artifact，生成统一回答：

```
三件事虽独立，但共同指向 Python 并发编程的核心权衡：
如何在 GIL 约束下高效利用系统资源。

- GIL 解释了为何多线程无法加速 CPU 密集型任务...
- 数值结果 7,016,651 虽属中等规模...
- asyncio.gather 与 ThreadPoolExecutor 的差异...
```

### 执行轨迹

```
[Parent] 共 2 步:
  - 调用工具 spawn_subagents
  - 给出最终答案

[Subagents] 共 3 个子 Agent:
  - [researcher] 调研 GIL ...     耗时: 6207ms  状态: OK
  - [calculator_analyst] 计算 ...  耗时: 5182ms  状态: OK
  - [researcher] 对比 asyncio ... 耗时: 7331ms  状态: OK

并行总耗时: 7780ms
```

### 并行效果指标

| 指标 | 值 |
|---|---|
| 串行理论耗时 | 18325ms |
| 并行实际耗时 | 7780ms |
| 加速比 | 2.36x |
| 父 Agent 总耗时 | 18543ms |

## 代码结构

```
examples/
├── parallel_subagent_demo.py          # 主 demo 脚本
├── parallel_subagent_config.yaml      # 声明式 Role + 父 Agent + 任务配置
```

核心类：

| 类 | 职责 |
|---|---|
| `SubagentResult` | 子 Agent 执行结果（dataclass） |
| `SubagentSpec` | 子 Agent 启动规格（dataclass） |
| `RoleConfig` | Role 三元组（system_prompt + tools + llm_config） |
| `ParallelTrace` | 并行执行轨迹（含 parent_steps + subagent_results） |
| `ParallelParentAgent` | 父 Agent 编排器 |

## 与内核原语的映射

当前 demo 实现是 Phase 2 内核原语的预演：

| Demo 组件 | Phase 2 内核原语 | 说明 |
|---|---|---|
| `SubagentSpec` | `atlas.core.spawn_subagent` | 内核内置工具 |
| `durable_gather` | `atlas.core.durable_gather` | 基于 Journal 的持久化并行 |
| `RoleConfig` | `atlas.core.role.Role` | Role 抽象 |
| `role_configs` 字典 | 内核注册表 | 运行时注册的 Role |
| `ParallelTrace` | `atlas.core.Trace` 扩展 | 多 Agent 轨迹 |

## 局限性

当前 demo 有以下局限，Phase 2 落地时需解决：

1. **无 Journal 持久化**：子 Agent 执行记录未写入 Journal，崩溃无法断点续跑
2. **无 Saga 补偿**：子 Agent 失败无补偿机制
3. **串行防重未实现**：同 Role 名的子 Agent 未做串行约束
4. **无上下文传递**：子 Agent 之间无法传递 artifact（当前只有父→子单向）
5. **Artifact schema 校验**：当前是自由文本，Phase 2 需结构化 Schema 校验

## 运行方式

```bash
# 默认配置
python examples/parallel_subagent_demo.py

# 指定其他配置文件
python examples/parallel_subagent_demo.py path/to/your_config.yaml

# 纯文本模式（重定向到文件）
python examples/parallel_subagent_demo.py --plain > result.log
```

## 与项目原则的一致性

- EP-2 解耦：Role YAML 配置 + 内核原语 + 上层组装
- EP-3 组装式：声明式 YAML + 注入式 Renderer
- EP-4 用户简单：一行命令运行，配置修改无需改代码
- 用户规则 7：Role enum 约束从源头消除角色填写错误
- 用户规则 8：方案清晰，demo 验证后再下沉内核

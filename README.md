<div align="center">

# Atlas

**一套 Agent 内核，搭出你自己的 AI Agent。**

单 Agent 循环、多 Agent 协作、工作流编排 —— 用同一套原语表达，自带崩溃恢复、护栏与可视化。

[![Python](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![Version](https://img.shields.io/badge/version-0.1.0-orange.svg)](./pyproject.toml)
[![Status](https://img.shields.io/badge/status-alpha-yellow.svg)](#路线与状态)

[快速开始](#快速开始) · [示例](#示例一览) · [能力](#能力一览) · [架构文档](./docs/ARCHITECTURE.md)

</div>

---

## Atlas 是什么

Atlas 是一个 **Python Agent 框架**。你用它提供的积木（内核原语 + 现成的多 Agent 模式）搭建自己的 AI Agent，而不是从零处理循环控制、崩溃恢复、并发隔离、副作用补偿这些底层麻烦事。

它的定位是「控制平面」：把确定性的骨架和护栏交给内核，把「智能」（角色、提示词、领域工具）留给你。

- **不必自己造轮子** —— 终止条件、检查点、幂等、熔断、重试、Saga 补偿默认开启，不靠你手动兜底。
- **崩溃能续跑** —— 每一步落盘，进程挂了用 `resume(run_id)` 从断点继续，副作用不会重复执行。
- **能看到发生了什么** —— 内置网页 Dashboard，实时看执行流程图、工具调用、熔断与重试状态。
- **模块可插拔** —— LLM / 工具 / 记忆 / 持久化后端 / 渲染都是协议，按需替换。

> 谁适合用：想快速搭一个可靠的 Agent 或多 Agent 系统、又不想被底层稳定性问题拖住的开发者。

## 三种形态，一套原语

同一套内核，覆盖三类常见需求，不用为每种形态学一套新东西：

| 形态 | 用来做什么 | 上手示例 |
|---|---|---|
| **单 Agent** | 一个会调用工具、多轮对话、能崩溃恢复的助手 | `examples/quickstart.py` |
| **多 Agent 协作** | 规划-执行-审查闭环，或动态分派 + 并行处理 | `examples/pwc_demo.py`、`examples/orchestrator_workers_demo.py` |
| **工作流编排** | 条件分支、并行汇聚、人工审批挂起的流程图 | `examples/edge_demo.py`、`examples/suspend_resume_demo.py` |

## 快速开始

Atlas 目前是 alpha（0.1.0），尚未发布到 PyPI，通过克隆仓库本地安装使用。

```bash
git clone https://github.com/liukun158959-beep/Atlas.git
cd Atlas
python -m atlas init
```

`init` 会一步到位：创建 `.venv` 虚拟环境 → 安装 atlas → 引导你选择模型厂商（Qwen / OpenAI / Deepseek / Moonshot / 智谱）并填入 API Key，写进 `.env`。

> 需要自带一个兼容 OpenAI 协议的 LLM API Key。没有 Key 时，大部分示例可以加 `--mock` 用假模型跑通逻辑。

装好后，跑一个真实示例验证：

```bash
python examples/quickstart.py          # 终端 Markdown 渲染
python examples/quickstart.py --plain  # 纯文本输出
```

## 最小示例

十几行代码，一个会自动调用工具、且崩溃可恢复的 Agent：

```python
from dotenv import load_dotenv
from atlas import Agent, LLM, Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS

load_dotenv()  # 读取 .env 里的 API Key

tools = Toolkit()
for spec in BUILTIN_TOOLS:      # 内置 calculator、get_current_time
    tools.register(**spec)

agent = Agent(
    llm=LLM(),
    tools=tools,
    system_prompt="你是通用助手 Atlas，遇到计算或时间问题请主动调用工具。",
)

print(agent.run("先告诉我现在几点，然后计算 365 * 24 等于多少"))
# 进程崩溃后想续跑上一次任务：agent.resume(run_id)
```

不传 `journal` 时默认落盘到 `./.atlas/journal.db`，这就是崩溃恢复的底座。

## 不写代码：用 YAML + CLI

把 Agent 的行为声明在 YAML 里，用命令行跑，改行为就是改配置：

```yaml
# quickstart.yaml
agent:
  system_prompt: "你是通用助手 Atlas，遇到需要计算或获取时间的问题请主动调用工具。"
  tools: [calculator, get_current_time]
  max_steps: 10
llm:
  provider: qwen
  model: qwen-plus
task: "先告诉我现在几点，然后计算 365 * 24 等于多少"
```

```bash
python -m atlas run --config examples/quickstart.yaml
python -m atlas run --config examples/quickstart.yaml --resume <run_id>   # 断点续跑
```

## 能力一览

内核（`atlas.core`）默认帮你处理好这些，不用自己实现：

- **崩溃恢复** —— 每步落盘（默认 SQLite），`resume(run_id)` 从断点续跑，副作用靠幂等键防重复。
- **稳定性护栏** —— 终止条件组合器（步数 / 超时 / Token / 成本 / 复读检测）、三态熔断器、指数退避重试，默认开启。
- **副作用安全** —— 非幂等工具（扣款、发邮件）必须注册补偿动作，失败时按 Saga 逆序补偿。
- **长对话不爆** —— 分级 Compact：先裁旧工具结果，再滑窗，最后才让 LLM 生成摘要。
- **人在环中** —— 工作流可在关键点挂起等待人工审批，崩溃了审批状态也不丢。

上层现成模式（`atlas.patterns`），拿来即用：

- **PlannerWorkerCritic** —— 规划 → 执行 → 审查，不合格打回重做，追求稳。
- **OrchestratorWorkers** —— 动态分派 + 批间并行处理，追求快。

可插拔：5 个 LLM 厂商预设、工具三分类 ABI、19 个插件生命周期钩子、终端 Rich 渲染 / 纯文本二选一。

## 可视化 Dashboard

一条命令启动网页面板，实时看 Agent 在做什么：

```bash
python -m atlas dashboard --port 8765
# 浏览器打开 http://localhost:8765
```

面板包含：运行列表、单次运行的流程图（DAG 节点状态 / 耗时）、工具调用详情、日志，以及熔断器 / 重试 / 告警的稳定性横幅。

> Dashboard 面向本地开发观测，默认监听本地端口且不带鉴权，请勿直接暴露到公网。

## 示例一览

每个能力都有可直接运行的示例，大多支持 `--mock`（假模型跑逻辑）和 `--plain`（纯文本）：

| 示例 | 演示什么 |
|---|---|
| `quickstart.py` | 单 Agent + 工具调用 + 多步任务 |
| `agent_resume_demo.py` | 崩溃后 `resume`，副作用只执行一次 |
| `saga_demo.py` | 副作用工具失败 → Saga 逆序补偿清零 |
| `circuit_retry_demo.py` | 熔断器三态 + 指数退避重试 |
| `pwc_demo.py` | PlannerWorkerCritic 质量闭环（Critic 打回重做） |
| `orchestrator_workers_demo.py` | OrchestratorWorkers 动态分派 + 并行 |
| `edge_demo.py` / `parallel_edge_demo.py` | 工作流条件分支 / 并行汇聚 |
| `suspend_resume_demo.py` | 工作流挂起等人工审批 + 崩溃恢复 |
| `dashboard_demo.py` | 后台起 Dashboard 看实时流程图 |
| `compact_demo.py` | 长对话分级压缩 |

完整清单见 `examples/` 目录。

## 适合 / 不适合

**适合**
- 想快速搭一个可靠的单 Agent 或多 Agent 系统，不想自己处理循环控制、崩溃恢复、并发隔离。
- 需要副作用安全（扣款 / 下单 / 发邮件类操作要能补偿、不重复执行）。
- 想在开发期把 Agent 的执行过程可视化地看清楚。

**暂不适合**
- 想开箱即用的终端产品 —— Atlas 是框架，你需要写代码把它组装成你的应用。
- 需要生产级持久化后端 —— 目前只有 SQLite / 内存 Journal，Postgres / Temporal 未做。
- 需要复杂图编排的大型工作流 —— 建议直接集成 LangGraph，Atlas 工作流原语只做最小集。

## 路线与状态

Atlas 处于 **alpha（0.1.0）**，单人开发自用，API 可能变动。

- **已落地**：单 Agent 循环、两种多 Agent 模式、工作流 Graph（分支 / 并行 / 挂起恢复）、稳定性护栏全套、Dashboard、RSI 自进化基础设施。
- **已收口**：Prompt 自进化闭环 —— 真跑验证后发现它改的是离线提示词，对实际需求不对口，主动搁置。
- **未做**：Postgres / Temporal 后端、发布到 PyPI。

## 文档

- **[架构与技术参考](./docs/ARCHITECTURE.md)** —— 设计原则、9 大内核原语、各能力实现细节、完整示例清单。
- 想理解内部机制或参与内核开发，从上面这份文档开始。

## 技术栈

Python 3.11+ · OpenAI 兼容协议（任意兼容模型可接入）· `pip` + `venv` · SQLite（默认 Journal）· `rich`（终端渲染）· `PyYAML`（声明式配置）· `pytest`（测试）· OpenTelemetry（可选观测）。

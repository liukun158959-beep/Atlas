# Atlas

一个面向通用任务的 Agent 框架。Atlas 致力于提供一个简洁、可扩展、可控的通用 Agent 内核，让 LLM 能够通过工具调用、记忆与任务编排完成复杂的多步骤任务，而不是只做单轮问答。

## 设计目标

- **通用**：不绑定特定领域，工具、记忆、编排均可插拔，能承接问答、检索、代码执行、数据处理等多种任务。
- **可控**：Agent 的每一步推理与动作都可观测、可中断、可回放，避免黑盒执行。
- **简洁**：内核只做必要的事（LLM 调用、工具调度、上下文管理），不堆砌冗余抽象。
- **可扩展**：通过实现统一的工具/记忆/LLM 接口接入新能力，而不是修改核心流程。

## 架构

Atlas 的核心由四个解耦的层组成：

```
+----------------------------------------------------------+
|                       Orchestrator                       |
|        (任务编排 / ReAct 或 Plan-Execute 循环)            |
+----------------------------------------------------------+
        |               |               |               |
+---------------+ +-------------+ +-------------+ +-----------+
|      LLM      | |    Tools    | |   Memory    | |  Context  |
| (模型接入层)   | | (工具注册)  | | (短/长记忆)  | | (会话状态) |
+---------------+ +-------------+ +-------------+ +-----------+
```

- **LLM 层**：统一封装模型调用，屏蔽不同厂商（OpenAI、Qwen、本地模型等）的差异。
- **Tools 层**：工具以统一接口注册，Agent 通过函数调用机制按需选择执行。
- **Memory 层**：短期会话记忆 + 可选的长期向量记忆，避免上下文无限增长。
- **Context 层**：管理 prompt 拼装、消息裁剪与状态持久化。
- **Orchestrator**：驱动 Agent 的思考-行动循环，支持 ReAct、Plan-Execute 等模式。

## 核心特性

- 统一的 LLM / Tool / Memory 接口，能力可插拔
- 基于 Function Calling 的工具调用与结果回填
- 多轮会话记忆与上下文裁剪
- 结构化的执行轨迹（trace）输出，便于调试与回放
- 支持单 Agent 与多 Agent 协作两种编排模式

## 模块组成

| 模块 | 路径 | 职责 |
|---|---|---|
| core | atlas/core/ | Agent 内核：ReAct 循环 + 多 Agent 协作 |
| llm | atlas/llm/ | 模型接入层：OpenAI 兼容协议 |
| tools | atlas/tools/ | 工具层：注册调度 + 内置工具 |
| memory | atlas/memory/ | 记忆层：会话消息管理 |
| context | atlas/context/ | 上下文层：执行轨迹记录 |
| cli | atlas/cli/ | 命令行工具：init 项目初始化 |
| providers | atlas/providers.py | LLM provider 预设表 |

## 技术栈

- Python 3.11+
- 依赖管理：pip + venv
- LLM 接入：兼容 OpenAI API 协议的模型均可接入
- 向量记忆（可选）：FAISS / Chroma

## 安装与配置

### 一键初始化（推荐）

```bash
git clone git@github.com:liukun158959-beep/Atlas.git
cd Atlas
python -m atlas init
```

init 会依次完成：
1. 在项目根目录创建 .venv 虚拟环境
2. 用 venv 内的 python 执行 pip install -e . 安装 atlas
3. 引导选择 LLM provider，输入 API Key，自动写入 .env

### 手动配置（备选）

```bash
cp .env.example .env
```

编辑 .env 填入真实值：

```env
ATLAS_API_KEY=sk-xxxx
ATLAS_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
ATLAS_MODEL=qwen-plus
```

## 快速开始

```python
from dotenv import load_dotenv
from atlas import Agent, LLM, Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS

load_dotenv()

llm = LLM()
tools = Toolkit()
for spec in BUILTIN_TOOLS:
    tools.register(**spec)

agent = Agent(
    llm=llm,
    tools=tools,
    system_prompt="你是通用助手 Atlas，遇到需要计算或获取时间的问题请主动调用工具。",
)
print(agent.run("先告诉我现在几点，然后计算 365 * 24 等于多少"))

# 查看执行轨迹
print(agent.trace)
```

## 路线图

- 内核：Agent 主循环与 ReAct 模式（已完成）
- LLM 层：OpenAI 协议兼容接入（已完成）
- Tools 层：工具注册与函数调用调度（已完成）
- Memory 层：会话记忆（已完成）
- 执行轨迹：结构化 trace 输出（已完成）
- 多 Agent 协作：Planner-Worker-Critic 模式（已完成）
- 项目初始化：atlas init 一键引导（已完成）
- Memory 层：上下文裁剪与长期向量记忆（规划中）
- 自定义工具扩展与第三方接入示例（规划中）
- atlas doctor 诊断命令（规划中）

## License

MIT

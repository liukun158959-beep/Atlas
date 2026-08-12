---
title: Atlas Renderer 渲染层设计
---

# Atlas Renderer 渲染层设计

## 定位

Renderer 是 Atlas 内核的**输出抽象层**，负责将 Agent 运行时产生的文本、表格、面板等结构化内容渲染到终端（或其他输出目标）。它解决的核心问题是：**上层代码不关心输出格式细节，内核自动渲染最终答案**。

## 设计原则

- **Protocol 先行**：先定义 `Renderer` 协议，再写具体实现
- **零依赖默认实现**：`PlainRenderer` 用纯 `print`，任何环境都能跑
- **可选增强实现**：`RichRenderer` 基于 rich 库，按需注入即可启用 Markdown 渲染
- **依赖单向**：`atlas.core.Agent` → `atlas.render.Renderer`，渲染层不反向 import 内核
- **消除 fallback**：`rich` 已声明为依赖，不做 try/except 兜底

## 架构

```
Agent.run()
    │
    ├── self.renderer.markdown(final, title="最终答案")
    │        ├── PlainRenderer: print(text)                    ← 默认
    │        └── RichRenderer:   console.print(Markdown(text))  ← 注入后
    │
    ├── trace.render(renderer)   ← 多 Agent 场景
    │
    └── renderer.table(rows, headers)   ← 指标展示
```

## 核心接口

`atlas/render/base.py` 中的 `Renderer` Protocol 定义了 5 个方法：

| 方法 | 作用 | PlainRenderer | RichRenderer |
|---|---|---|---|
| `print(*args)` | 通用文本输出 | 原生 `print` | `console.print` |
| `rule(title)` | 分隔线 | `=====` | 居中 `──── 标题 ────` |
| `markdown(text, title)` | Markdown 渲染 | 退化 `print` | `console.print(Markdown(text))` |
| `panel(content, title, **meta)` | 带标题面板 | 纯文本块 | `console.print(Panel(Markdown(...)))` |
| `table(rows, headers, title)` | 表格 | 简易对齐 | `console.print(Table(...))` |

## 两个实现

### PlainRenderer（默认）

零依赖实现，所有方法退化为 `print`。适用于：
- 不支持 ANSI 的终端
- 输出重定向到文件（`python demo.py --plain > result.log`）
- CI/CD 环境

### RichRenderer（增强）

基于 `rich>=13.0`，支持：
- **Markdown 渲染**：`**粗体**`、`*斜体*`、`` `行内代码` ``、列表、代码块
- **LaTeX 近似**：`$10^6$` → `10⁶`（Unicode 上标）、`$\times$` → `×`
- **表格**：对齐的框线表格
- **面板**：带边框的 artifact 展示
- **颜色状态**：加速比 >1.5x 绿色、1-1.5x 黄色、<1x 红色

## 注入方式

Agent 构造时传入 `renderer` 参数：

```python
# 默认纯文本
agent = Agent(llm=llm, tools=tools)

# 终端 Markdown 渲染
from atlas.render import RichRenderer
agent = Agent(llm=llm, tools=tools, renderer=RichRenderer())
```

运行时切换：

```python
# 命令行 --plain 参数切换
renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()
agent = Agent(..., renderer=renderer)
```

## 文件结构

```
atlas/render/
├── base.py      # Renderer Protocol
├── plain.py     # PlainRenderer（默认，零依赖）
├── rich.py      # RichRenderer（基于 rich 的 Markdown 渲染）
└── __init__.py  # 导出 Renderer / PlainRenderer / RichRenderer
```

## 为什么选 Renderer 而非 Plugin

| 维度 | Renderer | Plugin |
|---|---|---|
| 关注点 | 输出层 | Agent 生命周期事件 |
| 调用方式 | Agent **主动**调用 `renderer.markdown()` | Plugin **被动**监听事件 |
| 覆盖范围 | 最终答案 / artifact / trace / 指标 | 仅 `on_agent_finish` |
| 类比 | LLM / Toolkit / Memory | 事件钩子 |

渲染是"被动消费输出"，不是"监听生命周期事件"，更适合作为可注入组件而非插件。

## 与项目原则的一致性

- EP-1 可用性优先：rich 纯 Python，无 server/Rust/Go
- EP-2 解耦可插拔：Protocol 先行、构造参数注入、可独立替换
- EP-3 组装式：注入一行代码即生效，无需改其他代码
- 用户规则 7 消除 fallback：无条件 import RichRenderer，缺 rich 直接抛 `ModuleNotFoundError`

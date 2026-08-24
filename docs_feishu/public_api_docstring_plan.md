# 实施计划：Atlas 公开 API 中文 docstring

> grill-me 对齐后冻结。2026-08-17。只加注释，不改行为。

## 0. 决策

| 项 | 选择 |
|----|------|
| 范围 | 公开 API：`atlas/__init__.py` 与 `atlas.rsi` / `atlas.patterns` 导出；各 Protocol 方法；Agent / Runtime / Journal / LLM / Graph / RSI 对外方法 |
| 格式 | 中文 docstring：首行作用 + `Args` + `Returns` + `依赖` |
| 不做 | `tests/`、`examples/`、`atlas.testing`、私有 `_` 函数、改逻辑 |

`依赖` 写模块/原语名（如 `JournalProtocol.append`、`LLM.chat`），不写调用图。

## 1. 模板

```python
def run(self, user_input: str, run_id: str | None = None) -> str:
    """跑一轮 Agent，返回最终文本答案。

    Args:
        user_input: 用户任务。
        run_id: 可选；省略则新建 UUID。

    Returns:
        最终 assistant 文本。挂起时见 ``run_detailed``.

    依赖:
        ``AgentLoop.run``、``Runtime.journal``、``LLM.chat``。
    """
```

无参数可省略 `Args`；`-> None` 可省略 `Returns`。已有中文说明的扩成四段，不另起一套。

## 2. 覆盖清单

- core：Agent、Runtime、RunResult、Permission、PluginProtocol/Manager、Graph、Suspend、Scope、Evaluator、Terminator、CircuitBreaker、retry、multi_agent、Tracer、AgentLoop.run/resume
- llm：LLM
- journal：Protocol、InMemory、SQLite、restore
- context / memory / tools
- rsi 公开函数与 Protocol
- patterns：PWC / OW 的构造与 run
- plugins、render、utils 对外函数、cli 入口

## 3. 验证

`python -m pytest tests -q` 仍全绿（只加字符串，不改签名）。

# memory - 记忆层

atlas/memory/ 模块管理会话消息历史，按 OpenAI 消息格式存储。

## 文件结构

```
atlas/memory/
└── base.py     # Memory：会话级消息历史
```

## base.py - 会话记忆

### 模块职责

会话级记忆，按 OpenAI 消息格式存储。管理 system / user / assistant / tool 四类消息的追加与清理。

### 核心类：Memory

```python
class Memory:
    def __init__(self, system_prompt: str | None = None) -> None:
        self.messages: list[dict] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})
```

### 核心方法

#### add_user(content: str) -> None

追加 user 消息：

```python
self.messages.append({"role": "user", "content": content})
```

#### add_assistant(message: dict) -> None

追加 assistant 消息。直接存入 LLM 返回的 dict（可能含 content / tool_calls 字段）：

```python
self.messages.append(message)
```

#### add_tool(content: str, tool_call_id: str) -> None

追加 tool 消息（工具执行结果回填）：

```python
self.messages.append(
    {"role": "tool", "content": content, "tool_call_id": tool_call_id}
)
```

#### clear() -> None

清空对话历史，**保留 system prompt**：

```python
self.messages = [m for m in self.messages if m["role"] == "system"]
```

### 消息格式示例

一次完整的 ReAct 循环后，memory.messages 可能长这样：

```python
[
    {"role": "system", "content": "你是通用助手 Atlas..."},
    {"role": "user", "content": "现在几点？"},
    {"role": "assistant", "content": null, "tool_calls": [
        {"id": "call_xxx", "function": {"name": "get_current_time", "arguments": "{}"}}
    ]},
    {"role": "tool", "content": "2026-08-11 18:18:00", "tool_call_id": "call_xxx"},
    {"role": "assistant", "content": "现在是 2026-08-11 18:18。"}
]
```

### 设计决策

1. **按 OpenAI 消息格式存储**：直接传给 LLM 无需转换，减少中间层
2. **clear 保留 system prompt**：重置会话不丢失角色设定
3. **不自动裁剪**：当前版本只做会话级记忆，上下文裁剪与长期向量记忆在路线图中
4. **不引入抽象基类**：Memory 只有一个实现，不需要 MemoryBase 抽象

## 路线图

- 上下文裁剪：当消息超过 token 上限时自动压缩历史
- 长期向量记忆：跨会话经验积累，基于 FAISS / Chroma
- 记忆摘要：用 LLM 把长对话压缩成摘要再存入

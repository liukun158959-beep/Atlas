# llm - 模型接入层

atlas/llm/ 与 atlas/providers.py 共同实现 LLM 接入层，基于 OpenAI 兼容协议，可对接 Qwen / Deepseek / OpenAI / Moonshot / 智谱 等。

## 文件结构

```
atlas/
├── llm/
│   └── base.py     # LLM 客户端：OpenAI 兼容协议封装
└── providers.py    # provider 预设表
```

## base.py - LLM 客户端

### 模块职责

基于 OpenAI 兼容协议封装模型调用，屏蔽不同厂商的差异。所有预设 provider 均兼容 OpenAI 协议，接入成本一致。

### 核心类：LLM

```python
class LLM:
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> None:
        ...
```

**配置优先级：** 显式参数 > 环境变量 > provider 预设

**参数说明：**
- model：模型名（覆盖 provider 预设）
- api_key：模型服务 API Key
- base_url：服务地址（覆盖 provider 预设）
- provider：预设 provider key（如 "qwen"），自动填充 base_url 与默认 model

**环境变量（由 python -m atlas init 写入 .env）：**
- ATLAS_API_KEY：模型服务 API Key
- ATLAS_BASE_URL：服务地址
- ATLAS_MODEL：默认模型名

### 核心方法

#### chat(messages: list[dict], tools: list[dict] | None = None) -> dict

调用对话接口，返回 assistant 消息（dict 形式，便于直接存入 memory）。

```python
kwargs = {"model": self.model, "messages": messages}
if tools:
    kwargs["tools"] = tools
response = self.client.chat.completions.create(**kwargs)
return response.choices[0].message.model_dump(exclude_none=True)
```

**设计要点：** 用 model_dump(exclude_none=True) 去掉空字段，避免污染消息历史。

### 设计决策

1. **基于 OpenAI SDK**：不自己实现 HTTP 客户端，复用官方 SDK 的稳定性
2. **统一 OpenAI 协议**：国内主流模型（Qwen/智谱/Kimi/Deepseek）都提供 OpenAI 兼容端点，一套代码通吃
3. **配置三层优先级**：显式参数 > 环境变量 > provider 预设，灵活且可复现
4. **返回 dict 而非对象**：直接存入 memory，避免类型转换

## providers.py - Provider 预设表

### 模块职责

存储常见 LLM provider 的 base_url 和默认模型名，新增 provider 只需在此表追加一项。

### 预设列表

| key | 显示名 | base_url | 默认模型 |
|---|---|---|---|
| qwen | Qwen (DashScope 兼容模式) | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-plus |
| openai | OpenAI | https://api.openai.com/v1 | gpt-4o-mini |
| deepseek | Deepseek | https://api.deepseek.com/v1 | deepseek-chat |
| moonshot | Moonshot (Kimi) | https://api.moonshot.cn/v1 | moonshot-v1-8k |
| zhipu | 智谱 (GLM) | https://open.bigmodel.cn/api/paas/v4 | glm-4-flash |

### 数据结构

```python
PROVIDERS: dict[str, dict[str, str]] = {
    "qwen": {
        "name": "Qwen (DashScope 兼容模式)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
    ...
}
```

### 辅助函数

#### list_providers() -> list[tuple[str, str]]

返回 (key, 显示名) 列表，用于交互式菜单展示。

### 设计决策

1. **纯数据表**：不引入抽象基类、配置框架，加一家就是加一个 dict
2. **只选 OpenAI 兼容的 provider**：非兼容协议（如 Anthropic 原生）不纳入，保持接入层单一
3. **覆盖国内三大 + 国际两大**：Qwen/智谱/Kimi + OpenAI/Deepseek，命中率 90%+
4. **自定义兜底**：不在预设里的 provider 通过 init 的"自定义"选项手动填写

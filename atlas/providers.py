"""LLM Provider 预设表。

所有预设 provider 均兼容 OpenAI 协议，接入成本一致。
新增 provider 只需在此表追加一项即可。
"""
from __future__ import annotations

# provider 预设：key → (显示名, base_url, 默认模型)
PROVIDERS: dict[str, dict[str, str]] = {
    "qwen": {
        "name": "Qwen (DashScope 兼容模式)",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "default_model": "qwen-plus",
    },
    "openai": {
        "name": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o-mini",
    },
    "deepseek": {
        "name": "Deepseek",
        "base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
    },
    "moonshot": {
        "name": "Moonshot (Kimi)",
        "base_url": "https://api.moonshot.cn/v1",
        "default_model": "moonshot-v1-8k",
    },
    "zhipu": {
        "name": "智谱 (GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "default_model": "glm-4-flash",
    },
}


def list_providers() -> list[tuple[str, str]]:
    """返回 (key, 显示名) 列表，用于交互式菜单展示。"""
    return [(key, info["name"]) for key, info in PROVIDERS.items()]

"""LLM 接入层：基于 OpenAI 兼容协议，可对接 Qwen / Deepseek / OpenAI 等。"""
from __future__ import annotations

import os
import time
from typing import Any

from openai import OpenAI

from ..providers import PROVIDERS


class LLM:
    """OpenAI 兼容协议的模型客户端。

    配置优先级：显式参数 > provider 预设 > 环境变量
        provider       - 预设 provider key（如 "qwen"），自动填充 base_url 与默认 model
        api_key        - 模型服务 API Key
        base_url       - 服务地址（覆盖 provider 预设）
        model          - 模型名（覆盖 provider 预设）

    环境变量（由 `python -m atlas init` 写入 .env）：
        ATLAS_API_KEY  - 模型服务 API Key
        ATLAS_BASE_URL - 服务地址
        ATLAS_MODEL    - 默认模型名
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
    ) -> None:
        # provider 预设作为初值，显式参数与环境变量优先级更高
        preset = PROVIDERS.get(provider or "", {})
        self.model = (
            model
            or os.getenv("ATLAS_MODEL")
            or preset.get("default_model")
            or "qwen-plus"
        )
        self.client = OpenAI(
            api_key=api_key or os.getenv("ATLAS_API_KEY"),
            base_url=base_url or os.getenv("ATLAS_BASE_URL") or preset.get("base_url"),
        )

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """调用对话接口，返回 {message, usage, duration_ms}。

        - message: assistant 消息 dict（可直接存入 memory）
        - usage: token 用量 dict（prompt_tokens/completion_tokens/total_tokens），可能为 None
        - duration_ms: 本次调用耗时（毫秒）
        """
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        start_ts = time.time()
        response = self.client.chat.completions.create(**kwargs)
        duration_ms = int((time.time() - start_ts) * 1000)
        message = response.choices[0].message.model_dump(exclude_none=True)
        usage = None
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
        return {"message": message, "usage": usage, "duration_ms": duration_ms}

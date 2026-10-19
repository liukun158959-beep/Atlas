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

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Any | None = None,
    ) -> dict:
        """调用对话接口，返回 {message, usage, duration_ms}。

        Args:
            messages: OpenAI 格式消息列表（通常来自 ``Memory.messages``）。
            tools: OpenAI tools schema；None 表示不传工具。
            on_delta: 若传入则 stream=True，每段 content 回调一次；
                tool_calls 只在内部拼完整。

        Returns:
            dict：message / usage（可能 None）/ duration_ms。

        Raises:
            RuntimeError: 流结束时缺少 finish_reason，片段不会作为完整结果返回。

        依赖:
            OpenAI 兼容 HTTP；流式时 ``stream_options.include_usage``。
            由 ``AgentLoop`` 与 RSI ``propose_diff`` / ``evolve_prompt`` 调用。
        """
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        start_ts = time.time()
        if on_delta is None:
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
        return self._chat_stream(kwargs, on_delta, start_ts)

    def _chat_stream(
        self,
        kwargs: dict[str, Any],
        on_delta: Any,
        start_ts: float,
    ) -> dict:
        stream = self.client.chat.completions.create(
            **kwargs,
            stream=True,
            stream_options={"include_usage": True},
        )
        content_parts: list[str] = []
        tool_acc: dict[int, dict] = {}
        usage = None
        finished = False
        with stream:
            for chunk in stream:
                chunk_usage = getattr(chunk, "usage", None)
                if chunk_usage:
                    usage = {
                        "prompt_tokens": chunk_usage.prompt_tokens,
                        "completion_tokens": chunk_usage.completion_tokens,
                        "total_tokens": chunk_usage.total_tokens,
                    }
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason is not None:
                    finished = True
                delta = choice.delta
                piece = getattr(delta, "content", None)
                if piece:
                    content_parts.append(piece)
                    on_delta(piece)
                for call in getattr(delta, "tool_calls", None) or []:
                    slot = tool_acc.setdefault(
                        call.index,
                        {
                            "id": "",
                            "type": "function",
                            "function": {"name": "", "arguments": ""},
                        },
                    )
                    if call.id:
                        slot["id"] = call.id
                    fn = call.function
                    if fn is None:
                        continue
                    if fn.name:
                        slot["function"]["name"] += fn.name
                    if fn.arguments:
                        slot["function"]["arguments"] += fn.arguments
        if not finished:
            raise RuntimeError(
                "模型流提前结束，未收到 finish_reason 完成信息；已接收片段不能视为完整结果。"
            )
        duration_ms = int((time.time() - start_ts) * 1000)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": "".join(content_parts) or None,
        }
        if tool_acc:
            message["tool_calls"] = [tool_acc[i] for i in sorted(tool_acc)]
        return {"message": message, "usage": usage, "duration_ms": duration_ms}

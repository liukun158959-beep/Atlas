"""OpenAI-compatible transport and response assembly; no Agent dependencies."""
from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from openai import OpenAI

from ..providers import PROVIDERS
from .contract import (ChatCallbacks, ChatCancelledError, ChatOptions, ChatResult,
                       IncompleteResponseError)


class LLM:
    """Model client. Explicit configuration overrides environment and presets.

    Injected SDK clients are borrowed. Internally created clients disable retries
    and are owned by this instance. Use bind_client for explicit replacement.
    The provider SDK is loaded only when creating an internal client; importing
    contracts or running an offline Agent does not initialize it.
    """

    def __init__(self, model: str | None = None, api_key: str | None = None,
                 base_url: str | None = None, provider: str | None = None,
                 *, client: OpenAI | None = None) -> None:
        preset = PROVIDERS.get(provider or "", {})
        self.model = model or os.getenv("ATLAS_MODEL") or preset.get("default_model") or "qwen-plus"
        if client is None:
            # Contracts and offline Agents must not initialize the provider SDK.
            from openai import OpenAI
            client = OpenAI(
                api_key=api_key or os.getenv("ATLAS_API_KEY"),
                base_url=base_url or os.getenv("ATLAS_BASE_URL") or preset.get("base_url"),
                max_retries=0,
            )
            self._owns_client = True
        else:
            self._owns_client = False
        self.client = client
        self._closed = False
        # Backwards-compatible defaults for existing single-owner callers.
        # New integrations pass per-call options/callbacks instead.
        self.sampling: dict[str, Any] | None = None
        self.on_reasoning = None
        self.on_progress = None
        self.on_tool_delta = None

    def bind_client(self, client: OpenAI, *, model: str | None = None,
                    owned: bool = False) -> None:
        if client is not self.client and self._owns_client:
            self.client.close()
        self.client = client
        self._owns_client = owned
        self._closed = False
        if model is not None:
            self.model = model

    def close(self) -> None:
        if not self._closed and self._owns_client:
            self.client.close()
        self._closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             on_delta: Any | None = None, *, options: ChatOptions | None = None,
             callbacks: ChatCallbacks | None = None) -> ChatResult:
        """Return {message, usage, duration_ms} only after stop/tool_calls.

        The three-argument API is unchanged: a text callback enables streaming.
        Options/callbacks replace instance defaults for this call only. Explicit
        stream=True supports tool-only streams without a text callback.
        IncompleteResponseError/ChatCancelledError carry a partial ChatResult.
        SDK, transport and callback exceptions propagate without retries.
        """
        if self._closed:
            raise RuntimeError("LLM client is closed")
        options = options if options is not None else ChatOptions(sampling=self.sampling)
        callbacks = callbacks if callbacks is not None else ChatCallbacks(
            on_delta=on_delta, on_reasoning=self.on_reasoning,
            on_progress=self.on_progress, on_tool_delta=self.on_tool_delta,
        )
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages}
        if tools:
            kwargs["tools"] = tools
        kwargs.update(_sampling_kwargs(options))
        start = time.monotonic()
        empty = _result({"role": "assistant", "content": None}, None, start)
        _check_cancel(options, empty)
        streaming = options.stream if options.stream is not None else callbacks.on_delta is not None
        if streaming:
            return self._chat_stream(kwargs, options, callbacks, start)
        response = self.client.chat.completions.create(**kwargs)
        choice = next((c for c in response.choices if c.index in (None, 0)), None)
        message = choice.message.model_dump(exclude_none=True) if choice else empty["message"]
        if not options.preserve_reasoning:
            message.pop("reasoning_content", None)
        result = _result(message, _usage(response), start)
        _check_cancel(options, result)
        _check_finish(choice.finish_reason if choice else None, result)
        return result

    def _chat_stream(self, kwargs: dict[str, Any], options: ChatOptions,
                     callbacks: ChatCallbacks, start: float) -> ChatResult:
        content: list[str] = []
        reasoning: list[str] = []
        calls: dict[int, dict] = {}
        usage = None
        finish = None

        def result():
            message: dict[str, Any] = {"role": "assistant", "content": "".join(content) or None}
            if calls:
                message["tool_calls"] = [dict(id=calls[i]["id"], type="function",
                                              function=dict(calls[i]["function"])) for i in sorted(calls)]
            if options.preserve_reasoning and reasoning:
                message["reasoning_content"] = "".join(reasoning)
            return _result(message, usage, start)

        def check_cancel():
            if options.should_cancel is not None and options.should_cancel():
                raise ChatCancelledError(result())

        with self.client.chat.completions.create(
            **kwargs, stream=True, stream_options={"include_usage": True},
        ) as stream:
            for chunk in stream:
                check_cancel()
                if getattr(chunk, "usage", None):
                    usage = _usage(chunk)
                for choice in chunk.choices:
                    if choice.index not in (None, 0):
                        continue
                    if choice.finish_reason is not None:
                        finish = choice.finish_reason
                    delta = choice.delta
                    thought = _delta_text(delta, "reasoning_content")
                    if thought:
                        if options.preserve_reasoning:
                            reasoning.append(thought)
                        if callbacks.on_reasoning:
                            callbacks.on_reasoning(thought)
                    piece = _delta_text(delta, "content")
                    if piece:
                        content.append(piece)
                        if callbacks.on_delta:
                            callbacks.on_delta(piece)
                    for call in getattr(delta, "tool_calls", None) or []:
                        if callbacks.on_progress:
                            callbacks.on_progress()
                        slot = calls.setdefault(call.index, dict(
                            id="", type="function", function=dict(name="", arguments=""),
                        ))
                        if call.id:
                            slot["id"] = call.id
                        if call.function:
                            for field in ("name", "arguments"):
                                slot["function"][field] += getattr(call.function, field, None) or ""
                        if callbacks.on_tool_delta:
                            callbacks.on_tool_delta(call.index, slot["function"]["name"],
                                                    slot["function"]["arguments"])
                    check_cancel()
            complete = result()
            _check_cancel(options, complete)
            _check_finish(finish, complete)
        return complete


def _check_finish(finish: str | None, partial: ChatResult) -> None:
    if finish not in {"stop", "tool_calls"}:
        raise IncompleteResponseError(finish, partial)


def _check_cancel(options: ChatOptions, partial: ChatResult) -> None:
    if options.should_cancel is not None and options.should_cancel():
        raise ChatCancelledError(partial)


def _result(message: dict, usage: dict | None, start: float) -> ChatResult:
    return dict(message=message, usage=usage, duration_ms=int((time.monotonic() - start) * 1000))


def _usage(response: Any) -> dict | None:
    usage = getattr(response, "usage", None)
    return usage.model_dump(exclude_none=True) if usage is not None else None


def _sampling_kwargs(options: ChatOptions) -> dict[str, Any]:
    sampling = options.sampling or {}
    kwargs = {key: sampling[key] for key in ("temperature", "top_p") if key in sampling}
    body: dict[str, Any] = {}
    if "reasoning_effort" in sampling:
        body.update(thinking={"type": "enabled"}, reasoning_effort=sampling["reasoning_effort"])
    for key, value in (options.extra_body or {}).items():
        body[key] = {**body.get(key, {}), **value} if key == "thinking" and isinstance(value, dict) else value
    if body:
        kwargs["extra_body"] = body
    return kwargs


def _delta_text(delta: Any, key: str) -> str | None:
    direct = getattr(delta, key, None)
    return direct if isinstance(direct, str) and direct else None

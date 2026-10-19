"""Small model boundary, independent of Agent, tools and product state."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Protocol, TypedDict


class ChatResult(TypedDict):
    message: dict[str, Any]
    usage: dict[str, Any] | None
    duration_ms: int


class ChatModel(Protocol):
    """The existing three-argument boundary also supported by FakeLLM."""

    def chat(self, messages: list[dict], tools: list[dict] | None = None,
             on_delta: Callable[[str], None] | None = None) -> ChatResult: ...


@dataclass(frozen=True)
class ChatCallbacks:
    on_delta: Callable[[str], None] | None = None
    on_reasoning: Callable[[str], None] | None = None
    on_progress: Callable[[], None] | None = None
    # Tool callbacks receive the accumulated name and arguments, not fragments.
    on_tool_delta: Callable[[int, str, str], None] | None = None


@dataclass(frozen=True)
class ChatOptions:
    sampling: Mapping[str, Any] | None = None
    extra_body: Mapping[str, Any] | None = None
    stream: bool | None = None
    preserve_reasoning: bool = False
    # Cooperative check; blocking reads still require a transport timeout.
    should_cancel: Callable[[], bool] | None = None


class IncompleteResponseError(RuntimeError):
    """Partial output is observable, but must never be executed as a result."""

    def __init__(self, finish_reason: str | None, partial: ChatResult):
        self.finish_reason = finish_reason
        self.partial = partial
        super().__init__(
            f"模型响应未完整完成，finish_reason={finish_reason!r}；已接收片段不能视为完整结果。"
        )


class ChatCancelledError(IncompleteResponseError):
    def __init__(self, partial: ChatResult):
        super().__init__("cancelled", partial)

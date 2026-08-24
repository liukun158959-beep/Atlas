"""OTEL GenAI tracer。未安装 opentelemetry 时为 no-op，禁止自造 span 协议。"""
from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Iterator, Protocol, runtime_checkable


@runtime_checkable
class TracerProtocol(Protocol):
    """可选 tracing 后端。未装 OTEL 时用 ``NoOpTracer``。"""

    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Any:
        """打开一个 span（context manager）。

        Args:
            name: span 名，如 ``gen_ai.invoke_agent``。
            attributes: 可选键值。
        """
        ...


class NoOpTracer:
    """零依赖空实现。"""

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
        """空操作 span，yield None。"""
        yield None


class OtelTracer:
    """opt-in：需要 extras atlas[otel]。"""

    def __init__(self) -> None:
        try:
            from opentelemetry import trace
        except ImportError as e:
            raise ImportError(
                "OTEL tracer 需要安装 atlas[otel]（opentelemetry-api / opentelemetry-sdk）"
            ) from e
        self._tracer = trace.get_tracer("atlas")

    @contextmanager
    def span(self, name: str, attributes: dict[str, Any] | None = None) -> Iterator[Any]:
        """打开 OpenTelemetry span。

        依赖:
            ``opentelemetry.trace``（``atlas[otel]`` extras）。
        """
        with self._tracer.start_as_current_span(name) as span:
            if attributes:
                for k, v in attributes.items():
                    if v is not None:
                        span.set_attribute(k, v)
            yield span

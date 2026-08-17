"""Wave 4：无 otel 依赖时 NoOpTracer 可用。"""
from __future__ import annotations

from atlas.core.tracer import NoOpTracer


def test_noop_tracer_span() -> None:
    tracer = NoOpTracer()
    with tracer.span("gen_ai.invoke_agent", {"gen_ai.operation.name": "invoke_agent"}):
        pass

"""Runtime 组装面：Journal / 终止 / 插件 / compact / 熔断 / 权限 / tracer。"""
from __future__ import annotations

from dataclasses import dataclass, field

from atlas.context.attachment import AttachmentRegistry
from atlas.context.compact import CompactManager
from atlas.core.circuit_breaker import CircuitBreakerRegistry
from atlas.core.permission import NeverConfirm, PermissionPolicy
from atlas.core.plugin import PluginManager
from atlas.core.terminator import Terminator
from atlas.core.tracer import NoOpTracer, TracerProtocol
from atlas.journal.base import JournalProtocol


@dataclass
class Runtime:
    """内核运行时：Journal 必选，其余护栏可插拔。

    Agent 循环、熔断、权限、trace 都从这里取，不在 Agent 上再藏一份。

    依赖:
        ``JournalProtocol``（必选）；``Terminator`` / ``PluginManager`` /
        ``CompactManager`` / ``CircuitBreakerRegistry`` / ``PermissionPolicy`` /
        ``TracerProtocol`` / ``AttachmentRegistry`` 可选。
    """

    journal: JournalProtocol
    terminator: Terminator | None = None
    plugins: PluginManager | None = None
    compact: CompactManager | None = None
    circuit: CircuitBreakerRegistry | None = None
    permission: PermissionPolicy | None = None
    tracer: TracerProtocol | None = None
    attachment_registry: AttachmentRegistry | None = None

    def __post_init__(self) -> None:
        """补齐可选默认值；``journal is None`` 直接失败。"""
        if self.journal is None:
            raise ValueError("Runtime.journal 必选，禁止 journal=None")
        if self.plugins is None:
            self.plugins = PluginManager()
        if self.permission is None:
            self.permission = NeverConfirm()
        if self.tracer is None:
            self.tracer = NoOpTracer()
        if self.attachment_registry is None:
            self.attachment_registry = AttachmentRegistry()

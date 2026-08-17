"""工具执行前的确认策略（对照 OpenHands ConfirmationPolicy）。"""
from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from atlas.tools.base import Tool


class SecurityRisk(Enum):
    LOW = 0
    MEDIUM = 1
    HIGH = 2
    UNKNOWN = 3

    def is_riskier(self, other: "SecurityRisk") -> bool:
        if self is SecurityRisk.UNKNOWN or other is SecurityRisk.UNKNOWN:
            return False
        return self.value >= other.value


def risk_from_tool(tool: Tool) -> SecurityRisk:
    """用 Tool 三分类推断风险：只读 LOW，非幂等副作用 HIGH，其余 MEDIUM。"""
    if tool.isReadOnly:
        return SecurityRisk.LOW
    if tool.hasSideEffects and not tool.isIdempotent:
        return SecurityRisk.HIGH
    return SecurityRisk.MEDIUM


@runtime_checkable
class PermissionPolicy(Protocol):
    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        ...


class NeverConfirm:
    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        return False


class AlwaysConfirm:
    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        return True


class ConfirmRisky:
    def __init__(
        self,
        threshold: SecurityRisk = SecurityRisk.HIGH,
        confirm_unknown: bool = True,
    ) -> None:
        if threshold is SecurityRisk.UNKNOWN:
            raise ValueError("threshold 不能是 UNKNOWN")
        self.threshold = threshold
        self.confirm_unknown = confirm_unknown

    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        if risk is SecurityRisk.UNKNOWN:
            return self.confirm_unknown
        return risk.is_riskier(self.threshold)

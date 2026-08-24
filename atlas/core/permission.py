"""工具执行前的确认策略（对照 OpenHands ConfirmationPolicy）。"""
from __future__ import annotations

from enum import Enum
from typing import Protocol, runtime_checkable

from atlas.tools.base import Tool


class SecurityRisk(Enum):
    """工具风险等级：只读 LOW，一般 MEDIUM，非幂等副作用 HIGH。"""

    LOW = 0
    MEDIUM = 1
    HIGH = 2
    UNKNOWN = 3

    def is_riskier(self, other: "SecurityRisk") -> bool:
        """判断 self 是否达到或超过 other；任一侧 UNKNOWN 则不算更险。

        Args:
            other: 比较基准（如 ConfirmRisky 的 threshold）。

        Returns:
            True 表示应当按「更危险」处理。
        """
        if self is SecurityRisk.UNKNOWN or other is SecurityRisk.UNKNOWN:
            return False
        return self.value >= other.value


def risk_from_tool(tool: Tool) -> SecurityRisk:
    """用 Tool 三分类推断风险：只读 LOW，非幂等副作用 HIGH，其余 MEDIUM。

    Args:
        tool: 已注册工具。

    Returns:
        ``SecurityRisk``。

    依赖:
        ``atlas.tools.base.Tool`` 的 isReadOnly / hasSideEffects / isIdempotent。
    """
    if tool.isReadOnly:
        return SecurityRisk.LOW
    if tool.hasSideEffects and not tool.isIdempotent:
        return SecurityRisk.HIGH
    return SecurityRisk.MEDIUM


@runtime_checkable
class PermissionPolicy(Protocol):
    """工具执行前是否向人确认。由 Runtime.permission 注入。"""

    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        """是否挂起等待调用方 resume。

        Args:
            risk: ``risk_from_tool`` 的结果。
        """
        ...


class NeverConfirm:
    """从不确认，工具直接执行。Runtime 默认策略。"""

    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        """永远返回 False。"""
        return False


class AlwaysConfirm:
    """每个工具调用都挂起。"""

    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        """永远返回 True。"""
        return True


class ConfirmRisky:
    """风险达到阈值（默认 HIGH）或 UNKNOWN 时挂起。"""

    def __init__(
        self,
        threshold: SecurityRisk = SecurityRisk.HIGH,
        confirm_unknown: bool = True,
    ) -> None:
        """Args:
            threshold: 达到该等级即确认；不能是 UNKNOWN。
            confirm_unknown: UNKNOWN 是否也确认。
        """
        if threshold is SecurityRisk.UNKNOWN:
            raise ValueError("threshold 不能是 UNKNOWN")
        self.threshold = threshold
        self.confirm_unknown = confirm_unknown

    def should_confirm(self, risk: SecurityRisk = SecurityRisk.UNKNOWN) -> bool:
        """Args:
            risk: 当前工具风险。
        """
        if risk is SecurityRisk.UNKNOWN:
            return self.confirm_unknown
        return risk.is_riskier(self.threshold)

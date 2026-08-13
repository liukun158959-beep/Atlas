"""分层权限模型 + 提案归档存储。

Q8=A：PermissionLevel 枚举（CORE 只读 / BUFFERED 受控 / FREE 预留）+ check_permission()
Q5=A：ArchiveStore 归档失败提案 + 下次 propose_diff 传给 LLM 避免重复
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum


class PermissionLevel(Enum):
    """分层权限模型三层级。"""

    CORE = "core"            # 核心层：Agent/Graph/Tool 定义/Plugin Protocol，只读
    BUFFERED = "buffered"    # 缓冲层：Skill code / system prompt，受控（propose_diff 可修改）
    FREE = "free"            # 自由层：预留（4.2b prompt 变异沙箱）


# 缓冲层允许修改的 target_type 集合
_BUFFERED_TARGETS = {"skill", "system_prompt"}


def check_permission(target_type: str) -> PermissionLevel:
    """检查 target_type 的权限层级。

    核心层（graph/agent/tool/plugin 等）只读——propose_diff 不可修改。
    缓冲层（skill/system_prompt）受控——propose_diff 可修改，但必须经 evaluate 通过。
    """
    if target_type in _BUFFERED_TARGETS:
        return PermissionLevel.BUFFERED
    return PermissionLevel.CORE


@dataclass
class ArchivedProposal:
    """已归档的自进化提案（含评估结果）。"""

    proposal: object          # ProposedDiff（避免循环引用，用 object）
    evaluation: object        # EvaluationResult
    archived_at: float = 0.0


class ArchiveStore:
    """提案归档存储。

    记录所有提案（通过 + 未通过），用于：
    1. 下次 propose_diff 时把失败提案摘要传给 LLM 避免重复提议
    2. 进化历史审计追踪
    """

    def __init__(self) -> None:
        self._archive: list[ArchivedProposal] = []

    def add(self, proposal: object, evaluation: object) -> None:
        """归档一条提案（含评估结果）。"""
        self._archive.append(ArchivedProposal(
            proposal=proposal,
            evaluation=evaluation,
            archived_at=time.time(),
        ))

    def list_recent(self, n: int = 10) -> list[ArchivedProposal]:
        """按时间倒序返回最近 N 条归档。"""
        return list(reversed(self._archive[-n:])) if n < len(self._archive) else list(reversed(self._archive))

    def list_failed(self) -> list[ArchivedProposal]:
        """返回所有未通过的提案。"""
        return [a for a in self._archive if not a.evaluation.passed]

    def list_passed(self) -> list[ArchivedProposal]:
        """返回所有通过的提案。"""
        return [a for a in self._archive if a.evaluation.passed]

    def count(self) -> int:
        """当前归档总数。"""
        return len(self._archive)

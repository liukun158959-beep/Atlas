"""RSI 自进化模块：Skill Library + 失败记忆 + inspect_self + 自进化闭环。

Phase 4.1 基础设施（感知+记忆层）：
- EmbeddingProtocol + FakeEmbedding / OpenAIEmbedding
- VectorStoreProtocol + SQLiteVectorStore
- Skill dataclass + SkillLibrary（注册/检索/版本化）
- FailureCase + FailureMemory + FailureMemoryCollector（Plugin 采集）
- SelfSnapshot + build_self_snapshot（Agent 自省）

Phase 4.2a 自进化闭环（修改层）：
- PermissionLevel + check_permission + ArchiveStore（分层权限 + 提案归档）
- ProposedDiff + EvaluationResult dataclass
- propose_diff / evaluate / apply_diff / self_evolve
"""
from __future__ import annotations

from .archive import ArchiveStore, ArchivedProposal, PermissionLevel, check_permission
from .embedding import EmbeddingProtocol, FakeEmbedding, OpenAIEmbedding
from .evolver import (
    EvaluationResult,
    ProposedDiff,
    apply_diff,
    evaluate,
    propose_diff,
    self_evolve,
)
from .failure import FailureCase, FailureMemory, FailureMemoryCollector
from .inspector import SelfSnapshot, build_self_snapshot
from .skill import Skill, SkillLibrary
from .vector_store import SQLiteVectorStore, VectorHit, VectorStoreProtocol

__all__ = [
    # Embedding
    "EmbeddingProtocol",
    "FakeEmbedding",
    "OpenAIEmbedding",
    # VectorStore
    "VectorStoreProtocol",
    "VectorHit",
    "SQLiteVectorStore",
    # Skill
    "Skill",
    "SkillLibrary",
    # Failure
    "FailureCase",
    "FailureMemory",
    "FailureMemoryCollector",
    # Inspector
    "SelfSnapshot",
    "build_self_snapshot",
    # Phase 4.2a：权限 + 归档
    "PermissionLevel",
    "check_permission",
    "ArchivedProposal",
    "ArchiveStore",
    # Phase 4.2a：自进化闭环
    "ProposedDiff",
    "EvaluationResult",
    "propose_diff",
    "evaluate",
    "apply_diff",
    "self_evolve",
]

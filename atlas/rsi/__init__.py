"""RSI 自进化模块：Skill Library + 失败记忆 + inspect_self。

Phase 4.1 基础设施（感知+记忆层）：
- EmbeddingProtocol + FakeEmbedding / OpenAIEmbedding
- VectorStoreProtocol + SQLiteVectorStore
- Skill dataclass + SkillLibrary（注册/检索/版本化）
- FailureCase + FailureMemory + FailureMemoryCollector（Plugin 采集）
- SelfSnapshot + build_self_snapshot（Agent 自省）
"""
from __future__ import annotations

from .embedding import EmbeddingProtocol, FakeEmbedding, OpenAIEmbedding
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
]

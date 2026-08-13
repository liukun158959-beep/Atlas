"""RSI 自进化模块：Skill Library + 失败记忆 + inspect_self + 自进化闭环 + Prompt 进化。

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

Phase 4.2b Prompt 进化（反思式变异 + 适应度 + Leakage Guardrails）：
- FitnessEvaluator + FitnessResult + FakeFitnessEvaluator（适应度评估）
- LeakageGuard + LeakageCheckResult + RegexLeakageGuard（越狱防护）
- EvolutionResult + evolve_prompt（Prompt 进化闭环）
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
from .fitness import FakeFitnessEvaluator, FitnessEvaluator, FitnessResult, TaskSetFitnessEvaluator
from .inspector import SelfSnapshot, build_self_snapshot
from .prompt_evolver import (
    EvolutionResult,
    LeakageCheckResult,
    LeakageGuard,
    RegexLeakageGuard,
    evolve_prompt,
)
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
    # Phase 4.2b：适应度评估
    "FitnessEvaluator",
    "FitnessResult",
    "FakeFitnessEvaluator",
    "TaskSetFitnessEvaluator",
    # Phase 4.2b：Leakage Guardrails
    "LeakageGuard",
    "LeakageCheckResult",
    "RegexLeakageGuard",
    # Phase 4.2b：Prompt 进化闭环
    "EvolutionResult",
    "evolve_prompt",
]

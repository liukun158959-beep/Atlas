"""Skill Library：代码即数据的技能注册/检索/版本化。

to_tool() 经 subprocess 沙箱执行 skill code，禁止进程内 exec。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from atlas.tools.base import Tool

from .embedding import EmbeddingProtocol
from .vector_store import VectorStoreProtocol


@dataclass
class Skill:
    """技能定义：代码 + 描述 + embedding 三件套。"""

    name: str
    description: str
    code: str                        # 完整 def 语句字符串
    embedding: list[float]           # description 的 embedding 向量
    version: int = 1
    fitness_score: float = 0.0       # 适应度（Phase 4.2 用，4.1 预留）
    created_at: float = 0.0
    metadata: dict = field(default_factory=dict)

    def to_tool(self) -> Tool:
        """把 skill code 包成 Tool；执行走 subprocess 沙箱。

        Returns:
            ``Tool``，func 内部调 ``run_skill_in_sandbox``。

        依赖:
            ``atlas.rsi.sandbox.run_skill_in_sandbox``、``atlas.tools.base.Tool``。
        """
        from .sandbox import run_skill_in_sandbox

        code = self.code
        func_name = self.name

        def _sandboxed(args: dict) -> str:
            return run_skill_in_sandbox(code, func_name, args)

        return Tool(
            name=self.name,
            description=self.description,
            func=_sandboxed,
            parameters=self.metadata.get("parameters", {"type": "object", "properties": {}}),
        )


class SkillLibrary:
    """技能库：注册 / 语义检索 / 版本化。

    持有 VectorStoreProtocol + EmbeddingProtocol 引用。
    register 时自动 embed(description) → 存 VectorStore + 内存 dict。
    retrieve 时 embed(query) → VectorStore.query → 映射回 Skill。
    """

    def __init__(
        self,
        embedding: EmbeddingProtocol,
        vector_store: VectorStoreProtocol,
    ) -> None:
        """Args:
            embedding: 文本向量。
            vector_store: 可插拔向量库。
        """
        self._embedding = embedding
        self._vs = vector_store
        # 内存索引：skill_id -> Skill（用于 get/list_all 快速访问）
        self._skills: dict[str, Skill] = {}
        # name -> 最新 version 号（用于版本递增）
        self._versions: dict[str, int] = {}

    def register(self, skill: Skill) -> str:
        """入库一个 Skill，同名自动升版本。

        Args:
            skill: 技能；embedding 为空则用 description 现算。

        Returns:
            skill_id，格式 ``{name}_v{version}``。

        依赖:
            ``EmbeddingProtocol.embed``、``VectorStoreProtocol.add``。
        """
        # 版本递增
        if skill.name in self._versions:
            skill.version = self._versions[skill.name] + 1
        else:
            skill.version = 1
        self._versions[skill.name] = skill.version

        if skill.created_at == 0.0:
            skill.created_at = time.time()

        # 如果 embedding 为空，自动计算
        if not skill.embedding:
            skill.embedding = self._embedding.embed(skill.description)

        # skill_id 格式：{name}_v{version}
        skill_id = f"{skill.name}_v{skill.version}"

        # 存入 VectorStore + 内存 dict
        self._vs.add(skill_id, skill.embedding, {
            "name": skill.name,
            "description": skill.description,
            "version": skill.version,
        })
        self._skills[skill_id] = skill

        return skill_id

    def retrieve(self, query: str, top_k: int = 5) -> list[Skill]:
        """按描述语义检索技能。

        Args:
            query: 查询文本。
            top_k: 返回条数。

        依赖:
            ``EmbeddingProtocol.embed``、``VectorStoreProtocol.query``。
        """
        query_emb = self._embedding.embed(query)
        hits = self._vs.query(query_emb, top_k)
        results: list[Skill] = []
        for hit in hits:
            skill = self._skills.get(hit.id)
            if skill is not None:
                results.append(skill)
        return results

    def get(self, skill_id: str) -> Skill | None:
        """按 ID 精确取。"""
        return self._skills.get(skill_id)

    def list_all(self) -> list[Skill]:
        """列出全部 Skill（用于 inspect_self 的 skill_summaries）。"""
        return list(self._skills.values())

    def latest_version(self, name: str) -> int:
        """查某个 name 的最新版本号。"""
        return self._versions.get(name, 0)

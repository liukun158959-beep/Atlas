"""Embedding 层：文本向量化，支持 Fake 和 OpenAI 两种实现。

Q3=A：FakeEmbedding（确定性 hash，demo 用）+ OpenAIEmbedding（复用 LLM client，真实用）
Q9=A：EmbeddingProtocol 三方法 embed / embed_batch / dimension
Q17=A：FakeEmbedding 用 MD5 hash → 64 维归一化向量（跨进程确定性）
"""
from __future__ import annotations

import hashlib
import struct
from typing import Protocol, runtime_checkable


@runtime_checkable
class EmbeddingProtocol(Protocol):
    """文本向量化协议。"""

    def embed(self, text: str) -> list[float]:
        """文本 → 向量。

        Args:
            text: 任意字符串。
        """
        ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量嵌入（API 场景减少网络调用）。"""
        ...

    @property
    def dimension(self) -> int:
        """向量维度。"""
        ...


class FakeEmbedding:
    """确定性假嵌入：MD5 hash -> 归一化向量。

    相同文本永远产生相同向量（跨进程确定性），demo 100% 可复现。
    不同文本产生不同向量（MD5 碰撞概率极低）。
    """

    def __init__(self, dimension: int = 64) -> None:
        self._dimension = dimension

    @property
    def dimension(self) -> int:
        return self._dimension

    def embed(self, text: str) -> list[float]:
        # MD5 hash -> 16 bytes -> 4 个 float
        digest = hashlib.md5(text.encode("utf-8")).digest()
        floats = [struct.unpack("f", digest[i:i + 4])[0] for i in range(0, 16, 4)]
        # 归一化到 [-1, 1] 区间
        normalized = [f / abs(f) if f != 0 else 0.0 for f in floats]
        # 填充到指定维度（重复 + 截断）
        result = (normalized * (self._dimension // len(normalized) + 1))[:self._dimension]
        return result

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]


class OpenAIEmbedding:
    """OpenAI 兼容协议的 embedding 客户端。

    复用 LLM 的 OpenAI client（同 base_url / api_key）。
    默认 model: text-embedding-3-small（1536 维）。
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str = "text-embedding-3-small",
    ) -> None:
        import os
        from openai import OpenAI

        self._client = OpenAI(
            api_key=api_key or os.getenv("ATLAS_API_KEY"),
            base_url=base_url or os.getenv("ATLAS_BASE_URL"),
        )
        self._model = model
        # 常见模型维度表
        self._dim_map = {
            "text-embedding-3-small": 1536,
            "text-embedding-3-large": 3072,
            "text-embedding-ada-002": 1536,
        }

    @property
    def dimension(self) -> int:
        return self._dim_map.get(self._model, 1536)

    def embed(self, text: str) -> list[float]:
        resp = self._client.embeddings.create(model=self._model, input=text)
        return resp.data[0].embedding

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        resp = self._client.embeddings.create(model=self._model, input=texts)
        # OpenAI 按 input 顺序返回
        return [item.embedding for item in sorted(resp.data, key=lambda x: x.index)]

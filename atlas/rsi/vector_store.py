"""向量存储层：持久化 embedding + 语义检索。

Q2=D：可插拔 VectorStoreProtocol + SQLiteVectorStore 默认实现（不引入 Chroma 重依赖）
Q8=A：add / query / get / delete / count + VectorHit(id, score, metadata)

SQLiteVectorStore：embedding 序列化为 bytes 存 SQLite，检索时纯 Python 余弦相似度 top-k。
"""
from __future__ import annotations

import json
import math
import sqlite3
import struct
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass
class VectorHit:
    """向量检索命中结果。"""

    id: str
    score: float           # 相似度分数（0~1，余弦相似度）
    metadata: dict


@runtime_checkable
class VectorStoreProtocol(Protocol):
    """向量存储协议。"""

    def add(self, id: str, embedding: list[float], metadata: dict) -> None: ...
    def query(self, embedding: list[float], top_k: int = 5) -> list[VectorHit]: ...
    def get(self, id: str) -> VectorHit | None: ...
    def delete(self, id: str) -> None: ...
    def count(self) -> int: ...


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度（不依赖 numpy）。"""
    if len(a) != len(b):
        # 长度不一致时截取较短的
        min_len = min(len(a), len(b))
        a, b = a[:min_len], b[:min_len]
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _embedding_to_bytes(embedding: list[float]) -> bytes:
    """float list -> bytes（struct pack）。"""
    return struct.pack(f"{len(embedding)}f", *embedding)


def _bytes_to_embedding(data: bytes) -> list[float]:
    """bytes -> float list（struct unpack）。"""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


class SQLiteVectorStore:
    """SQLite 持久化向量存储。

    表结构：vectors(id TEXT PK, embedding BLOB, metadata TEXT)
    检索时全量加载做余弦相似度 top-k（demo 规模 < 1000 条足够）。
    线程安全：单 threading.Lock 包裹读写（和 SQLiteJournal 一致）。
    """

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            db_path = Path(".atlas/vectors.db")
        else:
            db_path = Path(db_path)

        db_path.parent.mkdir(parents=True, exist_ok=True)

        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS vectors (
                id TEXT PRIMARY KEY,
                embedding BLOB NOT NULL,
                metadata TEXT NOT NULL DEFAULT '{}'
            )
        """)
        self._conn.commit()

    def add(self, id: str, embedding: list[float], metadata: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO vectors (id, embedding, metadata) VALUES (?, ?, ?)",
                (id, _embedding_to_bytes(embedding), json.dumps(metadata, ensure_ascii=False)),
            )
            self._conn.commit()

    def query(self, embedding: list[float], top_k: int = 5) -> list[VectorHit]:
        with self._lock:
            rows = self._conn.execute("SELECT id, embedding, metadata FROM vectors").fetchall()

        if not rows:
            return []

        hits: list[VectorHit] = []
        for row in rows:
            row_id, emb_bytes, meta_json = row
            row_emb = _bytes_to_embedding(emb_bytes)
            score = _cosine_similarity(embedding, row_emb)
            hits.append(VectorHit(id=row_id, score=score, metadata=json.loads(meta_json)))

        # 按相似度降序取 top_k
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def get(self, id: str) -> VectorHit | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT id, embedding, metadata FROM vectors WHERE id = ?", (id,)
            ).fetchone()

        if row is None:
            return None
        row_id, emb_bytes, meta_json = row
        return VectorHit(id=row_id, score=1.0, metadata=json.loads(meta_json))

    def delete(self, id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM vectors WHERE id = ?", (id,))
            self._conn.commit()

    def count(self) -> int:
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) FROM vectors").fetchone()
        return row[0] if row else 0

    def close(self) -> None:
        with self._lock:
            self._conn.close()

"""按 key 隔离 Agent 实例（参考 Restate Virtual Object）。

核心设计：
- per-key threading.Lock：同一个 key 的 acquire 串行化；不同 key 之间完全并行
- 内存状态字典 `_states[key]`：在锁持有期间读写 state，退出锁后不可见外部修改
- 未来 SQLite 持久化后端预留：可替换 _states/_locks 的存储层，API 不破坏
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, Iterator


class Scope:
    """按 key 隔离执行的进程内作用域：同 key 串行，跨 key 并行。

    不跨进程、不跨机器。状态只在 ``acquire`` 持锁期间可写。

    依赖:
        标准库 threading；不依赖 Journal。
    """

    def __init__(self) -> None:
        """创建空的锁表与状态表。"""
        self._locks: dict[str, threading.Lock] = {}
        self._states: dict[str, dict[str, Any]] = {}
        self._guards: dict[str, threading.Lock] = {
            "locks": threading.Lock(),
            "states": threading.Lock(),
        }

    def _get_or_create_lock(self, key: str) -> threading.Lock:
        """懒创建 per-key 锁（有 global guard，避免并发 create 丢失）。"""
        guard = self._guards["locks"]
        with guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def get_state(self, key: str) -> dict[str, Any]:
        """读取指定 key 的 state 拷贝（不持业务锁）。

        Args:
            key: 隔离键。
        """
        guard = self._guards["states"]
        with guard:
            return dict(self._states.get(key, {}))

    def _set_state_internal(self, key: str, state: dict[str, Any]) -> None:
        """持有锁时内部写入（不做拷贝，信任调用方 context manager 内部安全）。"""
        guard = self._guards["states"]
        with guard:
            self._states[key] = state

    @contextmanager
    def acquire(self, key: str) -> Iterator[SimpleNamespace]:
        """获取指定 key 的作用域（阻塞直到拿到锁）。

        Args:
            key: 非空字符串。

        Yields:
            SimpleNamespace（.key + .state）；退出时写回 state。
        """
        if not isinstance(key, str) or not key:
            raise ValueError("scope key 必须是非空字符串")

        lock = self._get_or_create_lock(key)
        lock.acquire()
        try:
            state = self.get_state(key)
            ctx = SimpleNamespace(key=key, state=state)
            yield ctx
        finally:
            try:
                # 无论 yield 是否抛异常，都写回 state（异常时也保留到目前为止的修改，不兜底回滚）
                self._set_state_internal(key, ctx.state)
            finally:
                lock.release()

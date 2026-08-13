"""指数退避 + Full Jitter 重试延迟计算。

Full Jitter 公式：delay = random.uniform(0, min(cap, base * 2^attempt))
减少多个客户端同时重试导致的"重试风暴"。
"""
from __future__ import annotations

import random
import time

DEFAULT_BASE_SECONDS = 0.1
DEFAULT_CAP_SECONDS = 10.0


def full_jitter_delay_ms(
    attempt: int,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    cap_seconds: float = DEFAULT_CAP_SECONDS,
) -> int:
    """计算第 attempt 次重试的延迟毫秒数（Full Jitter）。

    attempt 从 1 开始计数（第 1 次重试=第一次失败后）。
    """
    if attempt < 1:
        raise ValueError(f"attempt 必须 >=1，实际 {attempt}")
    ceiling = min(cap_seconds, base_seconds * (2 ** attempt))
    delay_seconds = random.uniform(0, ceiling)
    return int(delay_seconds * 1000)


def sleep_ms(ms: int) -> None:
    """按毫秒睡眠（封装 time.sleep，便于将来 mock 或 替换为 asyncio.sleep）。"""
    if ms < 0:
        raise ValueError(f"ms 必须 >=0，实际 {ms}")
    time.sleep(ms / 1000.0)

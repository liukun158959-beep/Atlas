"""幂等键派生工具：run_id + tool_name + sorted(JSON 化 input) → SHA-1 十六进制。"""
from __future__ import annotations

import hashlib
import json

IDEMPOTENCY_USER_KEY = "_idempotency_key"  # 用户显式传幂等键的字段名


def derive(run_id: str, tool_name: str, input_dict: dict) -> str:
    """从 run + 工具 + 输入稳定派生幂等键（SHA-1 hex）。

    Args:
        run_id: 当前运行。
        tool_name: 工具名。
        input_dict: 工具入参（会 sort_keys JSON）。

    Returns:
        40 位十六进制。用户也可在 input 里显式传 ``_idempotency_key``。

    依赖:
        由 ``AgentLoop`` 在副作用工具执行前调用，配合 ``Journal.cache_get``。
    """
    serialized = json.dumps(input_dict, sort_keys=True, ensure_ascii=False)
    payload = f"{run_id}\n{tool_name}\n{serialized}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

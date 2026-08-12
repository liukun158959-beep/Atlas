"""幂等键派生工具：run_id + tool_name + sorted(JSON 化 input) → SHA-1 十六进制。"""
from __future__ import annotations

import hashlib
import json

IDEMPOTENCY_USER_KEY = "_idempotency_key"  # 用户显式传幂等键的字段名


def derive(run_id: str, tool_name: str, input_dict: dict) -> str:
    """稳定派生幂等键。

    用 sort_keys 的 JSON 序列化消除 dict key 顺序歧义，
    用 run_id\ntool_name\nserialized_input 拼接后 SHA-1 输出 40 字符十六进制。
    """
    serialized = json.dumps(input_dict, sort_keys=True, ensure_ascii=False)
    payload = f"{run_id}\n{tool_name}\n{serialized}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()

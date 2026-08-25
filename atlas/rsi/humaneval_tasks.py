"""HumanEval 子集（MIT 改编）：编码 Prompt 打分与 atlas ab_test --preset humaneval。

题意来自 OpenAI HumanEval（MIT License），手抄进仓库。
不安装 human-eval：官方 check 会在进程内 exec。
"""
from __future__ import annotations

from typing import Any

# --mock 强 prompt 必须含此句，弱 prompt 不得含此句；不要写进题面
HUMANEVAL_STRONG_MARK = "只输出一个 python 代码块"


def _task(func_name: str, signature: str, doc: str, cases: list[dict], hint: str) -> dict:
    return {
        "task": f"完成下面的 Python 函数。\n\n{signature}\n    \"\"\"{doc}\"\"\"\n",
        "func_name": func_name,
        "cases": cases,
        "failure_hint": hint,
    }


HUMANEVAL_TRAIN: list[dict] = [
    _task(
        "has_close_elements",
        "def has_close_elements(numbers: list[float], threshold: float) -> bool:",
        "判断数字列表中是否存在两个数，其差的绝对值小于 threshold。",
        [
            {"args": [[1.0, 2.0, 3.9, 4.0, 5.0, 2.2], 0.3], "expected": True},
            {"args": [[1.0, 2.0, 3.9, 4.0, 5.0, 2.2], 0.05], "expected": False},
            {"args": [[1.0, 2.0, 5.9, 4.0, 5.0], 0.8], "expected": False},
            {"args": [[1.1, 2.2, 3.1, 4.1, 5.1], 0.5], "expected": False},
        ],
        "必须给出可调用的 has_close_elements，正确判断是否存在足够接近的两个数。禁止只写分析文字。",
    ),
    _task(
        "truncate_number",
        "def truncate_number(number: float) -> float:",
        "给定正浮点数，返回其小数部分（小于 1 的剩余部分）。",
        [
            {"args": [3.5], "expected": 0.5},
            {"args": [1.25], "expected": 0.25},
            {"args": [4.75], "expected": 0.75},
        ],
        "必须给出可调用的 truncate_number，返回正浮点数的小数部分。禁止只写分析文字。",
    ),
    _task(
        "below_zero",
        "def below_zero(operations: list[int]) -> bool:",
        "账户从 0 开始，operations 为正为存、为负为取。若过程中余额曾低于 0 返回 True，否则 False。",
        [
            {"args": [[]], "expected": False},
            {"args": [[1, 2, -3, 1, 2, -3]], "expected": False},
            {"args": [[1, 2, -4, 5, 6]], "expected": True},
            {"args": [[1, -2, 2, -2, 5, -5, 4, -4]], "expected": True},
        ],
        "必须给出可调用的 below_zero，按流水模拟余额，曾低于 0 才为真。禁止只写分析文字。",
    ),
]

HUMANEVAL_VAL: list[dict] = [
    _task(
        "intersperse",
        "def intersperse(numbers: list[int], delimeter: int) -> list[int]:",
        "在列表每两个相邻元素之间插入 delimeter。空列表仍返回空列表。",
        [
            {"args": [[], 7], "expected": []},
            {"args": [[5, 6, 3, 2], 8], "expected": [5, 8, 6, 8, 3, 8, 2]},
            {"args": [[2, 2, 2], 2], "expected": [2, 2, 2, 2, 2]},
        ],
        "必须给出可调用的 intersperse，在相邻元素之间插入分隔值。禁止只写分析文字。",
    ),
    _task(
        "string_xor",
        "def string_xor(a: str, b: str) -> str:",
        "a 和 b 只含 0 和 1，做逐位异或，结果仍用 0/1 字符串返回。",
        [
            {"args": ["111000", "101010"], "expected": "010010"},
            {"args": ["1", "1"], "expected": "0"},
            {"args": ["0101", "0000"], "expected": "0101"},
        ],
        "必须给出可调用的 string_xor，对等长 0/1 串做异或。禁止只写分析文字。",
    ),
    _task(
        "count_distinct_characters",
        "def count_distinct_characters(string: str) -> int:",
        "统计字符串中不区分大小写的不同字符个数。",
        [
            {"args": [""], "expected": 0},
            {"args": ["abcde"], "expected": 5},
            {"args": ["aaaaAAAAaaaa"], "expected": 1},
            {"args": ["Jerry jERRY JeRRRY"], "expected": 5},
        ],
        "必须给出可调用的 count_distinct_characters，按忽略大小写统计不同字符。禁止只写分析文字。",
    ),
]


def humaneval_all_tasks() -> list[dict]:
    """train + val 合成一套，供 ab_test 使用。"""
    return list(HUMANEVAL_TRAIN) + list(HUMANEVAL_VAL)


_SOLUTIONS: dict[str, str] = {
    "has_close_elements": '''```python
def has_close_elements(numbers: list[float], threshold: float) -> bool:
    for i in range(len(numbers)):
        for j in range(i + 1, len(numbers)):
            if abs(numbers[i] - numbers[j]) < threshold:
                return True
    return False
```''',
    "truncate_number": '''```python
def truncate_number(number: float) -> float:
    return number - int(number)
```''',
    "below_zero": '''```python
def below_zero(operations: list[int]) -> bool:
    balance = 0
    for op in operations:
        balance += op
        if balance < 0:
            return True
    return False
```''',
    "intersperse": '''```python
def intersperse(numbers: list[int], delimeter: int) -> list[int]:
    if not numbers:
        return []
    out = [numbers[0]]
    for n in numbers[1:]:
        out.append(delimeter)
        out.append(n)
    return out
```''',
    "string_xor": '''```python
def string_xor(a: str, b: str) -> str:
    bits = []
    for x, y in zip(a, b):
        bits.append("0" if x == y else "1")
    return "".join(bits)
```''',
    "count_distinct_characters": '''```python
def count_distinct_characters(string: str) -> int:
    return len(set(string.lower()))
```''',
}


def humaneval_mock_answer(system_prompt: str, user_task: str) -> str:
    """--mock 答题：弱 prompt 散文；强 prompt 按函数名给正确实现。"""
    if HUMANEVAL_STRONG_MARK not in system_prompt:
        return "这道题需要仔细分析函数语义，下面用自然语言说明思路，不直接给代码。"
    for name, code in _SOLUTIONS.items():
        if f"def {name}" in user_task:
            return code
    raise RuntimeError(
        f"HumanEval mock 无法匹配题面。恢复：为该题补充函数名分类。原文: {user_task[:80]}"
    )


class HumanEvalMockLLM:
    """按 system prompt 是否含 HUMANEVAL_STRONG_MARK 答编码题。"""

    model = "mock-humaneval-ab"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Any = None,
    ) -> dict:
        system = ""
        user = ""
        for m in messages:
            role = m.get("role")
            content = m.get("content") or ""
            if role == "system":
                system = content
            elif role == "user":
                user = content
        text = humaneval_mock_answer(system, user)
        if on_delta:
            on_delta(text)
        return {
            "message": {"role": "assistant", "content": text},
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "duration_ms": 1,
        }

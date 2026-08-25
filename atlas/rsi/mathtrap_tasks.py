"""MathTrap 公开陷阱题（中文改写）：供 --real 进化与 atlas ab_test 共用。"""
from __future__ import annotations

MATHTRAP_TRAIN: list[dict] = [
    {
        "task": (
            "直角三角形 XYZ 中，角 YXZ 是 90 度，XY=24，YZ=25。"
            "求 tan 角 X 的值。"
        ),
        "expected_keywords": ["无定义"],
        "failure_hint": (
            "先确认所求三角函数在该角是否成立，再决定能不能套公式。"
            "禁止把题目当成普通求值题直接计算。"
        ),
    },
    {
        "task": (
            "娜塔莉亚四月卖了 48 个夹子，五月卖的是四月的一半。"
            "她四月和六月一共卖了多少个夹子？"
        ),
        "expected_keywords": ["无法确定"],
        "failure_hint": (
            "先核对题目问到的每个月份是否都给出了销量，缺的月份不能用其它月代替。"
            "禁止把能算的那几个月加总后当成最终答案。"
        ),
    },
    {
        "task": (
            "一个等边三角形周长 30 厘米，同时高是 10 厘米。"
            "求这个三角形的面积。"
        ),
        "expected_keywords": ["矛盾"],
        "failure_hint": (
            "先检查边长和高能不能同时成立，再决定能不能套面积公式。"
            "禁止忽略条件冲突硬算。"
        ),
    },
]

MATHTRAP_VAL: list[dict] = [
    {
        "task": (
            "直角三角形 ABC 中，角 BAC 是 90 度，AB=8，BC=17。"
            "求 tan 角 A 的值。"
        ),
        "expected_keywords": ["无定义"],
        "failure_hint": (
            "先确认所求三角函数在该角是否成立，再决定能不能套公式。"
            "禁止把题目当成普通求值题直接计算。"
        ),
    },
    {
        "task": (
            "小明一月存了 120 元，二月存的是一月的一半。"
            "他一月和三月一共存了多少元？"
        ),
        "expected_keywords": ["无法确定"],
        "failure_hint": (
            "先核对题目问到的每个月份是否都给出了金额，缺的月份不能用其它月代替。"
            "禁止把能算的那几个月加总后当成最终答案。"
        ),
    },
    {
        "task": (
            "一个等边三角形周长 36 厘米，同时高是 12 厘米。"
            "求这个三角形的面积。"
        ),
        "expected_keywords": ["矛盾"],
        "failure_hint": (
            "先检查边长和高能不能同时成立，再决定能不能套面积公式。"
            "禁止忽略条件冲突硬算。"
        ),
    },
]


def mathtrap_all_tasks() -> list[dict]:
    """train + val 合成一套，供 ab_test 使用。"""
    return list(MATHTRAP_TRAIN) + list(MATHTRAP_VAL)

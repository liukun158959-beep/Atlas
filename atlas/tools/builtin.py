"""内置工具集合：calculator 与 get_current_time。"""
from __future__ import annotations

import ast
import operator
from datetime import datetime


# 仅允许数值与下列运算符，避免使用 eval 带来的安全风险
_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _safe_eval(node: ast.AST):
    """递归求值 AST 节点，遇到非白名单节点直接抛错。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp):
        op = _OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        op = _OPERATORS.get(type(node.op))
        if op is None:
            raise ValueError(f"不支持的运算符: {type(node.op).__name__}")
        return op(_safe_eval(node.operand))
    raise ValueError(f"不支持的表达式: {ast.dump(node)}")


def calculator(args: dict) -> str:
    """安全算术计算器，支持四则运算、幂、取模。"""
    expression = args["expression"]
    tree = ast.parse(expression, mode="eval")
    result = _safe_eval(tree.body)
    return f"{expression} = {result}"


def get_current_time(args: dict) -> str:
    """返回当前时间，可指定时区（默认本地）。"""
    tz = args.get("timezone")
    if tz:
        from zoneinfo import ZoneInfo

        now = datetime.now(ZoneInfo(tz))
    else:
        now = datetime.now()
    return now.strftime("%Y-%m-%d %H:%M:%S")


# 工具规格定义，供 Toolkit.register(**spec) 批量注册
BUILTIN_TOOLS = [
    {
        "func": calculator,
        "name": "calculator",
        "description": "执行算术计算，支持加减乘除、幂、取模、整除。输入数学表达式字符串。",
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，例如 (1+2)*3",
                }
            },
            "required": ["expression"],
        },
    },
    {
        "func": get_current_time,
        "name": "get_current_time",
        "description": "获取当前时间。",
        "parameters": {
            "type": "object",
            "properties": {
                "timezone": {
                    "type": "string",
                    "description": "时区名称，例如 Asia/Shanghai，可选",
                }
            },
            "required": [],
        },
    },
]

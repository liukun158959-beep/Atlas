"""Tool ABI 增强演示：三分类标志 + compensate + validate_input。

纯工具层验证，不涉及 LLM。展示 Tool ABI 增强：
- isReadOnly / isIdempotent / hasSideEffects 三分类标志
- isReadOnly=True 自动强制 isIdempotent=True
- hasSideEffects=True 且 isIdempotent=False 必须注册 compensate()
- validate_input 用 JSON Schema 校验输入

用法：
    python examples/tool_abi_demo.py
    python examples/tool_abi_demo.py --plain   # 纯文本渲染
"""
from __future__ import annotations

import sys

import jsonschema

from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Tool, Toolkit
from atlas.tools.builtin import calculator as _calc_func


def _write_file(args: dict) -> str:
    """模拟写文件工具：有副作用、非幂等（重放不安全，需 Saga 补偿）。"""
    return f"已写入 {args['path']}（{len(args.get('content', ''))} 字符）"


def _write_file_compensate(compensate_input, original_result, context) -> dict:
    """写文件的补偿函数（demo 版，无实际副作用）：删除 compensate_input["path"] 对应文件。"""
    # 这里只做占位演示，真实环境下应 os.remove(compensate_input["path"])
    return {}


def _set_flag(args: dict) -> str:
    """幂等设置标志位：重复调用结果一致，重放安全。"""
    return f"flag={args['name']} 已设为 {args['value']}（幂等）"


def main() -> None:
    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()
    renderer.rule("Tool ABI 增强演示")

    tk = Toolkit()
    # 1. 纯查询工具：isReadOnly=True，框架自动强制 isIdempotent=True
    tk.register(
        func=_calc_func, name="calculator", description="算术计算器",
        parameters={
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "数学表达式"}},
            "required": ["expression"],
        },
        isReadOnly=True,
    )
    # 2. 有副作用且非幂等：写文件（Phase 2.1 必须带 compensate）
    tk.register(
        func=_write_file, name="write_file", description="写文件（有副作用）",
        compensate=_write_file_compensate,
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["path", "content"],
        },
        hasSideEffects=True, isIdempotent=False,
    )
    # 3. 幂等工具：set_flag（有副作用但幂等，重放安全）
    tk.register(
        func=_set_flag, name="set_flag", description="幂等设置标志位",
        parameters={
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
            "required": ["name", "value"],
        },
        isIdempotent=True, hasSideEffects=True,
    )

    # 打印每个工具的三分类标志
    rows = []
    for name in ("calculator", "write_file", "set_flag"):
        t: Tool = tk.get(name)
        rows.append({
            "name": t.name,
            "isReadOnly": t.isReadOnly,
            "isIdempotent": t.isIdempotent,
            "hasSideEffects": t.hasSideEffects,
        })
    renderer.table(rows, ["name", "isReadOnly", "isIdempotent", "hasSideEffects"],
                   title="三分类标志")

    # validate_input：正确输入通过
    calc = tk.get("calculator")
    calc.validate_input({"expression": "1+2"})
    renderer.print("validate_input 正确输入通过 OK")

    # validate_input：缺必填字段抛 ValidationError
    try:
        calc.validate_input({})  # 缺 required 字段 expression
        renderer.print("ERROR: 应抛异常却未抛")
    except jsonschema.ValidationError as e:
        renderer.print(f"validate_input 缺字段抛异常 OK: {e.message}")

    # 执行一次工具，验证 func 正常工作
    renderer.print(tk.execute("calculator", {"expression": "123 + 456"}))


if __name__ == "__main__":
    main()

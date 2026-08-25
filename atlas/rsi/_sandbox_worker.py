"""Skill 沙箱 worker：受限 builtins，由 subprocess 调用。"""
from __future__ import annotations

import json
import sys

_ALLOWED = {
    "len": len,
    "range": range,
    "dict": dict,
    "list": list,
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "min": min,
    "max": max,
    "sum": sum,
    "abs": abs,
    "print": print,
    "enumerate": enumerate,
    "zip": zip,
    "tuple": tuple,
    "set": set,
    "round": round,
    "sorted": sorted,
    "isinstance": isinstance,
    "all": all,
    "any": any,
    "map": map,
    "filter": filter,
    "reversed": reversed,
    "ord": ord,
    "chr": chr,
    "iter": iter,
    "next": next,
    "Exception": Exception,
    "ValueError": ValueError,
    "TypeError": TypeError,
    "IndexError": IndexError,
    "KeyError": KeyError,
    "ZeroDivisionError": ZeroDivisionError,
    "StopIteration": StopIteration,
    "None": None,
    "True": True,
    "False": False,
}


def _equal(got, expected) -> bool:
    if got == expected:
        return True
    if isinstance(got, tuple) and isinstance(expected, list):
        return list(got) == expected
    return False


def main() -> int:
    raw = sys.stdin.read()
    payload = json.loads(raw)
    code = payload["code"]
    func_name = payload["func_name"]
    args = payload.get("args") or {}
    cases = payload.get("cases")
    safe_builtins = dict(_ALLOWED)
    namespace: dict = {"__builtins__": safe_builtins}
    try:
        exec(code, namespace)  # noqa: S102  — 已在受限 builtins 下执行
        func = namespace.get(func_name)
        if func is None:
            for v in namespace.values():
                if callable(v) and getattr(v, "__name__", None) == func_name:
                    func = v
                    break
        if func is None:
            print(json.dumps({"ok": False, "error": f"未找到函数 {func_name}"}), file=sys.stderr)
            return 2
        if cases is not None:
            if not isinstance(cases, list) or not cases:
                print(json.dumps({"ok": False, "error": "cases 必须是非空 list"}), file=sys.stderr)
                return 2
            for i, case in enumerate(cases):
                got = func(*case["args"])
                if not _equal(got, case["expected"]):
                    print(json.dumps({
                        "ok": False,
                        "error": f"用例 {i} 失败：got={got!r} expected={case['expected']!r}",
                    }), file=sys.stderr)
                    return 3
            sys.stdout.write(json.dumps({"ok": True, "result": "pass"}, ensure_ascii=False))
            return 0
        result = func(args) if isinstance(args, dict) else func(*args)
        sys.stdout.write(json.dumps({"ok": True, "result": result}, ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 4


if __name__ == "__main__":
    raise SystemExit(main())

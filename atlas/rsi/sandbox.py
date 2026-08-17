"""Skill 代码在 subprocess 受限解释器中执行，禁止进程内 exec。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_WORKER = Path(__file__).resolve().parent / "_sandbox_worker.py"


def run_skill_in_sandbox(
    code: str,
    func_name: str,
    args: dict,
    timeout: float = 5.0,
) -> str:
    """在子进程中执行 skill code。失败直接 ValueError，不回退 exec。"""
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    payload = json.dumps({"code": code, "func_name": func_name, "args": args}, ensure_ascii=False)
    with tempfile.TemporaryDirectory() as tmp:
        try:
            proc = subprocess.run(
                [sys.executable, str(_WORKER)],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=timeout,
                cwd=tmp,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise ValueError(f"Skill {func_name} 执行超时（{timeout}s）") from e
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise ValueError(f"Skill {func_name} 沙箱执行失败: {err or proc.returncode}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise ValueError(f"Skill {func_name} 沙箱输出无法解析: {proc.stdout!r}") from e
    if not payload.get("ok"):
        raise ValueError(payload.get("error") or "sandbox failed")
    result = payload.get("result")
    return result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)

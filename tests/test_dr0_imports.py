"""DR-0：atlas.llm 不得导入上层。"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("atlas.core", "atlas.rsi", "atlas.patterns", "atlas.cli")


def _iter_py(dir_path: Path):
    for p in dir_path.rglob("*.py"):
        yield p


def test_llm_does_not_import_upper_layers() -> None:
    llm_dir = ROOT / "atlas" / "llm"
    offenders: list[str] = []
    for path in _iter_py(llm_dir):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if any(alias.name.startswith(f) for f in FORBIDDEN):
                        offenders.append(f"{path.name}: import {alias.name}")
            elif isinstance(node, ast.ImportFrom) and node.module:
                if any(node.module.startswith(f) for f in FORBIDDEN):
                    offenders.append(f"{path.name}: from {node.module}")
    assert not offenders, "DR-0 违反:\n" + "\n".join(offenders)

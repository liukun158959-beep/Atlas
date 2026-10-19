"""DR-0：atlas.llm 不得导入上层。"""
from __future__ import annotations

import ast
from pathlib import Path
import subprocess
import sys

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


def test_model_import_does_not_load_execution_stack() -> None:
    result = subprocess.run([sys.executable, '-c', """
import sys
from atlas.llm import LLM, ChatModel
assert not any(name.startswith(('atlas.core', 'atlas.rsi', 'atlas.patterns', 'atlas.cli'))
               for name in sys.modules)
"""], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr


def test_existing_top_level_exports_remain_available() -> None:
    import atlas
    for name in atlas.__all__:
        assert name in dir(atlas)
        assert getattr(atlas, name) is not None

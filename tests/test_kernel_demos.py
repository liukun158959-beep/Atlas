"""把 6 条内核 demo 的 --mock 断言收进 pytest。"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def _run_demo(filename: str, args: list[str]) -> None:
    path = EXAMPLES / filename
    old = sys.argv[:]
    sys.argv = [str(path), *args]
    sys.path.insert(0, str(EXAMPLES))
    try:
        try:
            runpy.run_path(str(path), run_name="__main__")
        except SystemExit as error:
            if error.code not in (None, 0):
                raise
    finally:
        sys.argv = old
        if str(EXAMPLES) in sys.path:
            sys.path.remove(str(EXAMPLES))


@pytest.mark.parametrize(
    ("filename", "args"),
    [
        ("terminator_demo.py", ["--plain"]),
        ("saga_demo.py", ["--mock", "--plain"]),
        ("journal_replay_demo.py", ["--mock", "--plain"]),
        ("compact_demo.py", ["--mock", "--plain"]),
        ("circuit_retry_demo.py", ["--mock", "--plain"]),
        ("suspend_resume_demo.py", ["--plain"]),
        ("agent_resume_demo.py", []),
        ("llm_contract_demo.py", ["--mock"]),
    ],
)
def test_core_demos(filename: str, args: list[str], monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    _run_demo(filename, args)

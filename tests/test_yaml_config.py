"""Wave 6：YAML Agent 配置。"""
from __future__ import annotations

from pathlib import Path

from atlas.utils.yaml_loader import load_agent_config

ROOT = Path(__file__).resolve().parents[1]


def test_load_quickstart_yaml() -> None:
    cfg = load_agent_config(str(ROOT / "examples" / "quickstart.yaml"))
    assert "现在几点" in cfg["task"]
    assert cfg["agent"]["tools"] == ["calculator", "get_current_time"]
    assert cfg["llm"]["provider"] == "qwen"

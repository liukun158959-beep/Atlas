"""atlas run：从 YAML 组装 Agent 并执行 / resume。"""
from __future__ import annotations

from pathlib import Path

from atlas.core.agent import Agent
from atlas.journal.sqlite import SQLiteJournal
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS
from atlas.utils.yaml_loader import load_agent_config


def build_agent_from_config(config: dict, *, plain: bool = False) -> Agent:
    """从 ``load_agent_config`` 的 dict 组装 Agent。

    Args:
        config: 含 agent / llm / task，可选 journal.path。
        plain: True 用 PlainRenderer。

    Returns:
        已接 SQLiteJournal 的 Agent。

    依赖:
        ``LLM``、``Toolkit`` + ``BUILTIN_TOOLS``、``SQLiteJournal``、``Agent``。
    """
    llm_cfg = config["llm"]
    agent_cfg = config["agent"]
    llm = LLM(
        provider=llm_cfg.get("provider"),
        model=llm_cfg.get("model"),
        api_key=llm_cfg.get("api_key"),
        base_url=llm_cfg.get("base_url"),
    )
    toolkit = Toolkit()
    name_to_spec = {s["name"]: s for s in BUILTIN_TOOLS}
    for tool_name in agent_cfg["tools"]:
        if tool_name not in name_to_spec:
            raise RuntimeError(f"未知内置工具: {tool_name}")
        toolkit.register(**name_to_spec[tool_name])
    journal_cfg = config.get("journal") or {}
    journal_path = journal_cfg.get("path")
    journal = SQLiteJournal(save_full_payload=True, path=journal_path)
    renderer = PlainRenderer() if plain else RichRenderer()
    return Agent(
        llm=llm,
        tools=toolkit,
        system_prompt=agent_cfg["system_prompt"],
        max_steps=int(agent_cfg.get("max_steps", 10)),
        compact_threshold=agent_cfg.get("compact_threshold", 8000),
        renderer=renderer,
        journal=journal,
    )


def run(argv: list[str]) -> int:
    """``python -m atlas run`` 入口：跑 YAML 任务或 ``--resume``。

    Args:
        argv: 子命令参数（不含 ``run`` 本身）。

    Returns:
        进程退出码；success/terminated/suspended 为 0。

    依赖:
        ``load_agent_config``、``Agent.run_detailed`` / ``resume_detailed``。
    """
    import argparse

    from dotenv import load_dotenv

    load_dotenv()
    ap = argparse.ArgumentParser(prog="atlas run")
    ap.add_argument("--config", required=True, help="Agent YAML 配置路径")
    ap.add_argument("--resume", dest="run_id", default=None, help="从 run_id 续跑")
    ap.add_argument("--plain", action="store_true")
    args = ap.parse_args(argv)

    config = load_agent_config(args.config)
    agent = build_agent_from_config(config, plain=args.plain)
    if args.run_id:
        result = agent.resume_detailed(args.run_id)
    else:
        result = agent.run_detailed(config["task"])
    print(f"run_id={result.run_id}")
    print(f"status={result.status}")
    print(result.final_answer)
    return 0 if result.status in ("success", "terminated", "suspended") else 1

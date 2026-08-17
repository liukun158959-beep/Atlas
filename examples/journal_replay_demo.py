"""Journal event sourcing 演示：用 FakeLLM 跑一轮工具调用 + 一轮答案，重放事件日志。

用法：
    python examples/journal_replay_demo.py --mock --journal-backend=inmemory  # 内存版（默认）
    python examples/journal_replay_demo.py --mock --journal-backend=sqlite    # SQLite 持久化版（:memory:）
    python examples/journal_replay_demo.py --mock --journal-backend=sqlite --plain
    python examples/journal_replay_demo.py          # 真实 LLM + Rich
"""
from __future__ import annotations

import argparse
import sys

from dotenv import load_dotenv

from atlas.core.agent import Agent
from atlas.journal import InMemoryJournal, SQLiteJournal
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS


class FakeLLM:
    """假 LLM：第 1 轮返回工具调用，第 2 轮返回最终答案。"""

    def __init__(self) -> None:
        self._call = 0

    def chat(self, messages, tools=None, on_delta=None):
        self._call += 1
        if self._call == 1:
            msg = {
                "role": "assistant", "content": "我先算一下。",
                "tool_calls": [{
                    "id": "tc1", "type": "function",
                    "function": {"name": "calculator", "arguments": '{"expression": "123 + 456"}'},
                }],
            }
        else:
            msg = {"role": "assistant", "content": "123 + 456 = 579，计算完成。"}
        usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        return {"message": msg, "usage": usage, "duration_ms": 5}


def main() -> None:
    ap = argparse.ArgumentParser(description="Journal event sourcing 演示")
    ap.add_argument("--mock", action="store_true", help="使用 FakeLLM（不发真实请求）")
    ap.add_argument("--plain", action="store_true", help="纯文本渲染")
    ap.add_argument(
        "--journal-backend",
        choices=["inmemory", "sqlite"],
        default="inmemory",
        help="journal 持久化后端：inmemory=内存版（默认），sqlite=SQLite 内存库",
    )
    args = ap.parse_args()

    renderer = PlainRenderer() if args.plain or not RichRenderer.available() else RichRenderer()
    renderer.rule("Journal Event Sourcing 演示")
    renderer.print(f"[后端] {args.journal_backend.upper()}")

    if args.mock:
        llm = FakeLLM()
    else:
        load_dotenv()
        llm = LLM()

    toolkit = Toolkit()
    for spec in BUILTIN_TOOLS:
        toolkit.register(**spec)

    # 全量模式保存完整 payload，便于重放检查
    if args.journal_backend == "sqlite":
        # SQLite backend: use :memory: 保证每次运行隔离（断言模式）
        journal = SQLiteJournal(save_full_payload=True, path=":memory:")
    else:
        journal = InMemoryJournal(save_full_payload=True)
    agent = Agent(
        llm=llm, tools=toolkit,
        system_prompt="你是计算助手，遇到算术问题调用 calculator。",
        renderer=renderer, journal=journal, max_steps=5,
    )

    result = agent.run("帮我算 123 + 456")
    renderer.print(f"最终答案: {result}")

    # 打印 Journal 中所有事件的 seq 与 event_type
    run_id = agent.trace.meta["run_id"]
    events = journal.get_events(run_id)
    renderer.print(f"\nJournal 事件数: {len(events)}")
    rows = [{"seq": e.seq, "event_type": e.event_type} for e in events]
    renderer.table(rows, ["seq", "event_type"], title="事件序列")

    # 重放并打印结果
    replay = journal.replay(run_id)
    renderer.print(f"\n重放结果: event_count={replay['event_count']}, status={replay['status']}")
    renderer.print(f"type_counts: {replay['type_counts']}")

    # 验证 event_count 与实际事件数一致，status 合法
    assert replay["event_count"] == len(events), "event_count 应与实际事件数一致"
    assert replay["status"] in ("success", "terminated"), f"status 异常: {replay['status']}"
    renderer.print("验证通过：event_count 与 status 正确")


if __name__ == "__main__":
    main()

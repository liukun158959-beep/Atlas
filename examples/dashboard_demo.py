"""Dashboard demo：后台起 Dashboard Server（HTTP + WebSocket），主流程跑 3 轮 quickstart mock，每步 publish。

启动后:
    浏览器打开  http://localhost:8765
    左面板: 3 个 run（run-calc-1/2/3）+ running/finished 状态
    中面板: Trace DAG（每轮 Step 卡片 + 缩进）
    右面板: Events（最近 200 条，按类型着色）

10 秒后程序自动退出（demo 级，用户手动跑 `python -m atlas dashboard 8765` 可长期驻留）。
"""
from __future__ import annotations

import json
import sys
import threading
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas import Agent, Toolkit
from atlas.cli.dashboard import publish, run_server
from atlas.journal import InMemoryJournal
from atlas.llm.base import LLM
from atlas.tools.builtin import BUILTIN_TOOLS


class QuickstartMockLLM(LLM):
    """Mock LLM：三轮对话 → 先 calculator 工具调用 → 最终答案。"""

    def __init__(self) -> None:
        super().__init__(model="mock-dashboard-demo", api_key="mock")
        self._step: dict[str, int] = {}

    def chat(self, messages, tools=None, **_kwargs):
        run_id = getattr(self, "_current_run_id", "default")
        self._step[run_id] = self._step.get(run_id, 0) + 1
        step = self._step[run_id]

        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "hello",
        )

        # Step 1：发起 calculator 工具调用（把用户提问里的数字抠出来，a + b）
        if step == 1:
            digits = [int(x) for x in filter(str.isdigit, str(last_user))] if isinstance(last_user, str) else []
            a, b = (digits + [1, 2])[:2]
            tool_calls = [{
                "id": f"call_{run_id}_{step}",
                "type": "function",
                "function": {
                    "name": "calculator",
                    "arguments": json.dumps({"expression": f"{a} + {b}"}),
                },
            }]
            return {
                "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
                "duration_ms": 15,
            }

        # Step 2：拿到 tool 结果 → 返回最终答案
        result_val = a + b if 'digits' in locals() else 3
        last_tool = next(
            (m for m in reversed(messages) if m.get("role") == "tool"),
            None,
        )
        if last_tool:
            try:
                result_val = int(last_tool.get("content", str(result_val)) or result_val)
            except Exception:
                pass

        return {
            "message": {
                "role": "assistant",
                "content": f"根据计算结果，答案是 {result_val}。（mock final answer，run={run_id} step={step}）",
            },
            "usage": {"prompt_tokens": 140, "completion_tokens": 60, "total_tokens": 200},
            "duration_ms": 12,
        }


def main() -> None:
    args = set(sys.argv[1:])
    http_port = 8765
    for a in sys.argv[1:]:
        if a.startswith("--port="):
            http_port = int(a.split("=", 1)[1])

    # ---------- 后台线程：Dashboard server ----------
    def server_thread_entry() -> None:
        try:
            run_server(http_port=http_port)
        except KeyboardInterrupt:
            pass

    server_thread = threading.Thread(target=server_thread_entry, daemon=True, name="dashboard-server")
    server_thread.start()
    time.sleep(0.3)  # 等 server 启动

    print()
    print(f"  🖥  浏览器打开:  http://localhost:{http_port}")
    print("     （WebSocket 自动连接 http_port + 1，刷新页面即可实时看到 DAG 与 Events）")
    print()

    # ---------- 3 轮 quickstart mock ----------
    prompts = [
        "请计算 123 + 45 等于多少？",
        "再算一下 200 + 38 的结果",
        "最后算 7 + 9",
    ]

    mock_llm = QuickstartMockLLM()
    journal = InMemoryJournal()

    for i, prompt in enumerate(prompts, start=1):
        run_id = f"run-calc-{i}"
        mock_llm._current_run_id = run_id  # type: ignore[attr-defined]  # noqa: SLF001
        toolkit = Toolkit()
        for spec in BUILTIN_TOOLS:
            toolkit.register(**spec)
        agent = Agent(
            llm=mock_llm,
            tools=toolkit,
            system_prompt="你是一个计算助手，优先调用 calculator 工具。",
            journal=journal,
        )

        # 开始前 publish 一次（空 trace + 初步 run 状态）
        publish(run_id, journal, agent.trace)
        print(f"[{run_id}] running: {prompt}")

        try:
            answer = agent.run(prompt)
            print(f"[{run_id}] answer : {answer[:80]}")
        except Exception as exc:
            print(f"[{run_id}] FAILED : {exc}")

        # 跑完再 publish 一次
        publish(run_id, journal, agent.trace)
        time.sleep(1.2)  # 给浏览器看 DAG 切换的视觉效果

    print()
    print("[dashboard_demo] 3 轮 quickstart 跑完，再驻留 10 秒（可随时 Ctrl+C 退出）。请刷新浏览器查看最终快照。")
    try:
        time.sleep(10)
    except KeyboardInterrupt:
        print("\n[dashboard_demo] 用户中断，退出")
        return

    print("[dashboard_demo] 结束（daemon 线程自动回收）")


if __name__ == "__main__":
    main()

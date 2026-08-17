"""Compact 机制演示：多轮对话 token 超阈值 → 自动 compact → Summary + Boundary 写入。

用法：
    python examples/compact_demo.py --mock --plain   # FakeLLM 100% 可复现（推荐验证用）
    python examples/compact_demo.py --mock            # FakeLLM + Rich 渲染
    python examples/compact_demo.py --plain           # 真实 LLM（不保证 100% 触发 compact）

验证项：
    1. _runtime_messages 中存在 type=compact_summary 消息
    2. _runtime_messages 中存在 type=compact_boundary 消息
    3. project() 后 messages 条数 < 原始 _runtime_messages 条数
    4. project() 后无 isMeta=True 消息
    5. tool_calls 与 tool 消息配对完整（本 demo 无工具调用，此项 N/A）
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from atlas.core.agent import Agent
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Toolkit


# ---------------------------------------------------------------- FakeLLM
class CompactFakeLLM:
    """模拟多轮对话，token 逐轮递增，第 4 轮超阈值触发 compact。

    正常对话 5 轮：3000 → 5000 → 7000 → 8500（触发 compact）→ 3000（compact 后）
    compact 调用 1 次：返回 9 段结构化 Summary
    """

    def __init__(self) -> None:
        self.normal_call_count = 0
        # 5 轮正常对话回复 + 对应 prompt_tokens
        self.normal_responses = [
            ("第一轮回复：收到你的消息，正在处理。", 3000),
            ("第二轮回复：继续分析中，已收集部分信息。", 5000),
            ("第三轮回复：发现关键信息，准备汇总。", 7000),
            ("第四轮回复：分析完成，这是目前的结论。", 8500),
            ("第五轮回复（compact 后）：基于之前的摘要继续工作。", 3000),
        ]

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        # 检测是否是 compact 调用（CompactManager 的 system prompt）
        first_msg = messages[0] if messages else {}
        if "对话摘要助手" in first_msg.get("content", ""):
            return {
                "message": {
                    "role": "assistant",
                    "content": (
                        "1. 用户目标：进行多轮对话测试\n"
                        "2. 已完成步骤：已完成 4 轮对话\n"
                        "3. 关键发现：token 逐轮递增，第 4 轮超 8000 阈值\n"
                        "4. 当前状态：已触发 compact，历史已压缩\n"
                        "5. 待办事项：继续第 5 轮对话\n"
                        "6. 关键文件/资源：无\n"
                        "7. 错误与教训：无\n"
                        "8. 决策记录：threshold=8000，触发自动 compact\n"
                        "9. 下一步建议：验证 compact 后 messages 条数减少"
                    ),
                },
                "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300},
                "duration_ms": 10,
            }

        # 正常对话回复
        idx = self.normal_call_count
        self.normal_call_count += 1
        content, tokens = self.normal_responses[idx]
        return {
            "message": {"role": "assistant", "content": content},
            "usage": {"prompt_tokens": tokens, "completion_tokens": 100, "total_tokens": tokens + 100},
            "duration_ms": 10,
        }


# ---------------------------------------------------------------- main
def main() -> None:
    args = set(sys.argv[1:])
    use_mock = "--mock" in args
    use_plain = "--plain" in args

    renderer = PlainRenderer() if use_plain else RichRenderer()

    if use_mock:
        llm = CompactFakeLLM()
    else:
        load_dotenv()
        llm = LLM()

    # compact_threshold=8000：第 4 轮 token=8500 时自动触发
    agent = Agent(
        llm=llm,
        tools=Toolkit(),
        system_prompt="你是测试助手，配合用户完成多轮对话测试。",
        max_steps=5,
        renderer=renderer,
        compact_threshold=8000,
    )

    renderer.rule("Compact 机制演示")

    # 5 轮对话
    messages = [
        "第一轮：请帮我记录这条消息",
        "第二轮：补充一些信息",
        "第三轮：继续补充",
        "第四轮：触发 compact 的消息",
        "第五轮：compact 后的新消息",
    ]

    for i, msg in enumerate(messages):
        renderer.rule(f"第 {i + 1} 轮对话")
        result = agent.run(msg)
        token_count = agent.memory.get_token_count()
        renderer.print(f"→ prompt_tokens={token_count}, runtime={len(agent.memory.runtime_messages)} 条")

    # ---------------------------------------------------------------- 验证
    renderer.rule("验证")

    runtime = agent.memory.runtime_messages
    summary_msgs = [m for m in runtime if m.get("type") == "compact_summary"]
    boundary_msgs = [m for m in runtime if m.get("type") == "compact_boundary"]
    compact_happened = len(summary_msgs) >= 1 and len(boundary_msgs) >= 1

    if use_mock:
        # Mock 模式：FakeLLM 固定逐轮递增 token，第 4 轮必触发 → 严格断言
        assert compact_happened, (
            f"Mock 模式下 compact 应已触发: summary={len(summary_msgs)}, "
            f"boundary={len(boundary_msgs)}（未加 --mock 时真实 LLM 短对话 token 不超阈值属正常）"
        )
        renderer.print(f"[1] compact_summary 消息存在 OK ({len(summary_msgs)} 条)")
        renderer.print(f"[2] compact_boundary 消息存在 OK ({len(boundary_msgs)} 条)")

        # 3. project() 后 messages 条数 < runtime 条数
        api_msgs = agent.memory.messages
        assert len(api_msgs) < len(runtime), (
            f"project 后应更少: api={len(api_msgs)}, runtime={len(runtime)}"
        )
        renderer.print(f"[3] project 压缩 OK: runtime={len(runtime)} → api={len(api_msgs)}")

        # 4. project() 后无 isMeta
        meta_in_api = [m for m in api_msgs if m.get("isMeta")]
        assert len(meta_in_api) == 0, f"API messages 不应含 isMeta, 实际 {len(meta_in_api)}"
        renderer.print(f"[4] project 无 isMeta OK")

        # 5. Transcript 记录完整（5 user + 5 assistant + 1 compact event）
        transcript_events = agent.transcript.replay()
        user_events = [e for e in transcript_events if e.get("role") == "user"]
        assistant_events = [e for e in transcript_events if e.get("role") == "assistant"]
        compact_events = [e for e in transcript_events if e.get("type") == "compact"]
        assert len(user_events) == 5, f"Transcript 应有 5 条 user 事件, 实际 {len(user_events)}"
        assert len(assistant_events) == 5, f"Transcript 应有 5 条 assistant 事件, 实际 {len(assistant_events)}"
        assert len(compact_events) == 1, f"Transcript 应有 1 条 compact 事件, 实际 {len(compact_events)}"
        renderer.print(f"[5] Transcript 双写 OK: {len(transcript_events)} 条事件 (5 user + 5 assistant + 1 compact)")

        # 打印 Summary 内容
        renderer.rule("Compact Summary 内容")
        renderer.markdown(summary_msgs[0]["content"])

        renderer.rule("验证通过")
        print("=== compact_demo.py --mock 验证通过 ===")
    else:
        # 真实 LLM 模式：compact 触发取决于实际 token 消耗，soft 检查不抛异常
        renderer.print(f"[状态] 真实 LLM 模式，compact 是否触发视实际 token 数而定")
        if compact_happened:
            renderer.print(f"[✓] compact 已触发: summary={len(summary_msgs)} 条, boundary={len(boundary_msgs)} 条")
            api_msgs = agent.memory.messages
            meta_in_api = [m for m in api_msgs if m.get("isMeta")]
            renderer.print(f"[✓] project 压缩: runtime={len(runtime)} → api={len(api_msgs)}, 无 isMeta={len(meta_in_api)==0}")
            renderer.rule("Compact Summary 内容")
            renderer.markdown(summary_msgs[0]["content"])
        else:
            renderer.print(
                "[i] compact 未触发（5 轮短对话真实 token 未超 8000 阈值，属正常）。\n"
                "    请加 --mock 参数运行：python examples/compact_demo.py --mock --plain\n"
                "    或拉长对话轮次 + token 消耗观察 compact 自动触发。"
            )
        renderer.rule("演示结束")


if __name__ == "__main__":
    main()

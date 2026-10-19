"""Compact 机制：token 超阈值时分级压缩。

1. 先裁剪过旧 tool_result 正文
2. 仍超则滑窗丢掉最旧 user/assistant 对
3. 仍超才调 LLM 生成 9 段 Summary + Boundary
compact 后由 AgentLoop 调用 AttachmentRegistry.rebuild_all 从活状态重注入。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atlas.context.trace import Trace
    from atlas.llm.contract import ChatModel


# 9 段 Summary 模板
_SUMMARY_PROMPT = """请将以下对话历史压缩为 9 段结构化摘要，每段用「段名：内容」格式输出：

1. 用户目标：用户想完成什么
2. 已完成步骤：已经做了哪些操作
3. 关键发现：过程中发现了什么重要信息
4. 当前状态：任务进展到哪一步
5. 待办事项：还有哪些没做
6. 关键文件/资源：涉及的文件、工具、数据
7. 错误与教训：遇到了什么错误，有什么经验
8. 决策记录：做了哪些关键决策
9. 下一步建议：接下来应该做什么

对话历史：
{conversation}
"""


@dataclass
class CompactOutcome:
    """分级 compact 结果。"""

    snipped: bool = False
    summary: str | None = None
    used_llm: bool = False


class CompactManager:
    """Compact 管理器：检测阈值 + 分级压缩（snip → 滑窗 → LLM Summary）。

    由 AgentLoop 在每次 LLM 调用后调用 apply()。
    """

    def __init__(self, threshold: int = 8000, llm: "ChatModel | None" = None) -> None:
        self.threshold = threshold
        self.llm = llm
        self.llm_calls: int = 0

    def should_compact(self, token_count: int) -> bool:
        """是否超过 compact 阈值。

        Args:
            token_count: 当前 prompt tokens。
        """
        return token_count > self.threshold

    def apply(self, runtime_messages: list[dict], token_count: int) -> tuple[list[dict], CompactOutcome]:
        """分级 compact。返回（可能改写后的 runtime_messages, outcome）。

        Args:
            runtime_messages: ``Memory.runtime_messages``。
            token_count: 当前 prompt tokens。

        Returns:
            新消息列表与 ``CompactOutcome``（是否 snip / 是否需要 LLM summary）。

        依赖:
            可选 ``LLM.chat``（最后一档 Summary）；由 ``AgentLoop`` 调用后写 Boundary。
        """
        outcome = CompactOutcome()
        if token_count <= self.threshold:
            return runtime_messages, outcome

        messages = self.snip_tool_results(runtime_messages)
        outcome.snipped = messages != runtime_messages

        if outcome.snipped:
            # 廉价层已生效：略超阈值不再调 LLM
            return messages, outcome

        outcome.summary = self.compact(messages)
        outcome.used_llm = self.llm is not None
        self.llm_calls += 1 if outcome.used_llm else 0
        return messages, outcome

    def snip_tool_results(self, runtime_messages: list[dict], keep_recent: int = 2, max_chars: int = 200) -> list[dict]:
        """保留最近 keep_recent 条 tool 结果全文，更早的截断。"""
        tool_indices = [i for i, m in enumerate(runtime_messages) if m.get("role") == "tool"]
        if len(tool_indices) <= keep_recent:
            return runtime_messages
        drop = set(tool_indices[:-keep_recent])
        out: list[dict] = []
        changed = False
        for i, msg in enumerate(runtime_messages):
            if i in drop and isinstance(msg.get("content"), str) and len(msg["content"]) > max_chars:
                new_msg = dict(msg)
                new_msg["content"] = msg["content"][:max_chars] + "...[snipped]"
                out.append(new_msg)
                changed = True
            else:
                out.append(msg)
        return out if changed else runtime_messages

    def compact(self, runtime_messages: list[dict], trace: "Trace | None" = None) -> str:
        """生成 compact Summary 文本。

        只压缩最后一个 Boundary 之后的活跃消息（避免重复压缩已压缩的历史）。
        调用 LLM 生成 9 段结构化摘要。
        """
        # 找最后一个 boundary，只压缩其后的消息
        active_messages = self._get_active_messages(runtime_messages)

        # 格式化对话历史
        conversation = self._format_messages(active_messages)

        # 构造 summary prompt
        prompt = _SUMMARY_PROMPT.format(conversation=conversation)
        summary_messages = [
            {"role": "system", "content": "你是一个对话摘要助手，负责将长对话压缩为结构化摘要。"},
            {"role": "user", "content": prompt},
        ]

        # 调用 LLM 生成摘要
        if self.llm is None:
            # 无 LLM 时用规则提取（仅用于测试）
            return self._rule_based_summary(active_messages)

        result = self.llm.chat(summary_messages)
        summary_text = result["message"].get("content", "")
        return summary_text

    def _get_active_messages(self, runtime_messages: list[dict]) -> list[dict]:
        """获取最后一个 Boundary 之后的活跃消息（不含 system 和 isMeta）。"""
        boundary_idx = -1
        for i in range(len(runtime_messages) - 1, -1, -1):
            if runtime_messages[i].get("type") == "compact_boundary":
                boundary_idx = i
                break

        if boundary_idx >= 0:
            return [m for m in runtime_messages[boundary_idx + 1:] if not m.get("isMeta")]
        else:
            # 无 Boundary → 取所有非 system 非 isMeta 消息
            return [
                m for m in runtime_messages
                if m["role"] != "system" and not m.get("isMeta")
            ]

    def _format_messages(self, messages: list[dict]) -> str:
        """格式化消息列表为可读文本。"""
        lines: list[str] = []
        for msg in messages:
            role = msg["role"]
            content = msg.get("content", "")
            if role == "assistant" and msg.get("tool_calls"):
                calls = [tc["function"]["name"] for tc in msg["tool_calls"]]
                lines.append(f"[{role}] {content or ''} (工具调用: {', '.join(calls)})")
            elif role == "tool":
                lines.append(f"[tool结果] {content[:200]}")
            else:
                lines.append(f"[{role}] {content or '(空)'}")
        return "\n".join(lines)

    def _rule_based_summary(self, messages: list[dict]) -> str:
        """无 LLM 时的规则提取（仅用于测试/demo）。"""
        user_msgs = [m for m in messages if m["role"] == "user"]
        assistant_msgs = [m for m in messages if m["role"] == "assistant"]
        tool_msgs = [m for m in messages if m["role"] == "tool"]

        parts = [
            f"1. 用户目标：{user_msgs[0]['content'][:100] if user_msgs else '未知'}",
            f"2. 已完成步骤：共 {len(assistant_msgs)} 轮对话，{len(tool_msgs)} 次工具调用",
            f"3. 关键发现：(规则提取模式，未调用 LLM)",
            f"4. 当前状态：已压缩 {len(messages)} 条消息",
            f"5. 待办事项：(需 LLM 提取)",
            f"6. 关键文件/资源：(需 LLM 提取)",
            f"7. 错误与教训：(需 LLM 提取)",
            f"8. 决策记录：(需 LLM 提取)",
            f"9. 下一步建议：继续对话",
        ]
        return "\n".join(parts)

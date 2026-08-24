"""记忆层：管理会话消息历史，含分层投影与 compact 支持。

Phase 1 改造：
- _runtime_messages 存原始数据（含 isMeta Attachment / Boundary / Summary）
- messages @property 返回 project() 投影后的 API messages
- token 跟踪：update_token_count / get_token_count
- compact 支持：add_summary 写入 Summary + Boundary 消息对
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from atlas.context.attachment import Attachment


class Memory:
    """会话级记忆，按 OpenAI 消息格式存储。

    对外接口不变：memory.messages 返回可直接传给 LLM 的 API messages。
    内部分层：_runtime_messages 含全量消息（含 isMeta 元消息），
    project() 负责投影为干净的 API messages。
    """

    def __init__(
        self,
        system_prompt: str | None = None,
        compact_threshold: int | None = None,
    ) -> None:
        self._runtime_messages: list[dict] = []
        self._token_count: int = 0
        self._compact_threshold = compact_threshold
        if system_prompt:
            self._runtime_messages.append({"role": "system", "content": system_prompt})

    # ------------------------------------------------------------------
    # 对外属性：messages 返回 project() 投影结果
    # ------------------------------------------------------------------
    @property
    def messages(self) -> list[dict]:
        """返回投影后的 API messages（不含 isMeta 元消息，tool_call 配对完整）。"""
        return self.project(self._runtime_messages)

    @property
    def runtime_messages(self) -> list[dict]:
        """返回原始 Runtime messages（含 isMeta 元消息），用于 compact 和 transcript。"""
        return self._runtime_messages

    # ------------------------------------------------------------------
    # 消息写入：全部写入 _runtime_messages
    # ------------------------------------------------------------------
    def add_user(self, content: str) -> None:
        """追加一条 user 消息。

        Args:
            content: 用户文本。
        """
        self._runtime_messages.append({"role": "user", "content": content})

    def add_assistant(self, message: dict) -> None:
        """追加一条 assistant 消息（可含 tool_calls）。

        Args:
            message: OpenAI assistant 消息 dict。
        """
        self._runtime_messages.append(message)

    def add_tool(self, content: str, tool_call_id: str) -> None:
        """追加一条 tool 结果。

        Args:
            content: 工具输出文本。
            tool_call_id: 对应 assistant.tool_calls[].id。
        """
        self._runtime_messages.append(
            {"role": "tool", "content": content, "tool_call_id": tool_call_id}
        )

    def add_attachment(self, attachment: "Attachment") -> None:
        """写入 Attachment 消息（isMeta=True，project 时转 system-reminder）。"""
        self._runtime_messages.append(attachment.to_message())

    def add_summary(self, summary_text: str) -> None:
        """写入 compact Summary + Boundary 消息对。

        Summary 在前（保留压缩后的历史），Boundary 在后（project 从此切分）。
        """
        self._runtime_messages.append({
            "role": "assistant",
            "content": summary_text,
            "isMeta": True,
            "type": "compact_summary",
        })
        self._runtime_messages.append({
            "role": "system",
            "content": "[compact_boundary]",
            "isMeta": True,
            "type": "compact_boundary",
        })

    # ------------------------------------------------------------------
    # token 跟踪
    # ------------------------------------------------------------------
    def update_token_count(self, prompt_tokens: int) -> None:
        """更新累计 token 数（从 LLM.chat() 返回的 usage.prompt_tokens 读取）。"""
        self._token_count = prompt_tokens

    def get_token_count(self) -> int:
        """最近一次 LLM usage 写入的 prompt token 数。"""
        return self._token_count

    def reset_token_count(self) -> None:
        """compact 后重置 token 计数（下次从 Boundary 后重新累计）。"""
        self._token_count = 0

    # ------------------------------------------------------------------
    # 清空
    # ------------------------------------------------------------------
    def load_runtime_messages(self, messages: list[dict]) -> None:
        """用 Journal 恢复的消息替换运行时历史。"""
        self._runtime_messages = [dict(m) for m in messages]

    def replace_runtime_messages(self, messages: list[dict]) -> None:
        self.load_runtime_messages(messages)

    def clear(self) -> None:
        """清空对话历史，保留 system prompt。"""
        self._runtime_messages = [
            m for m in self._runtime_messages if m["role"] == "system" and not m.get("isMeta")
        ]
        self._token_count = 0

    # ------------------------------------------------------------------
    # project()：Runtime messages → API messages 五步投影
    # ------------------------------------------------------------------
    def project(self, messages: list[dict]) -> list[dict]:
        """将 Runtime messages 投影为可发给 LLM 的 API messages。

        五步：
        1. 从后往前找最后一个 compact_boundary
        2. 只保留 Boundary 之后的消息 + 之前的 Summary
        3. 过滤 isMeta Attachment → 转为普通 system 消息（去 isMeta/type 字段）
        4. 修 tool_call 配对（删除无配对 tool_calls 的 assistant 或无配对的 tool 消息）
        5. 如仍超阈值，从最旧 user/assistant 对裁剪（目标 = 阈值 × 0.7）
        """
        if not messages:
            return []

        result: list[dict] = []

        # Step 1: 找最后一个 compact_boundary
        boundary_idx = -1
        summary_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            msg = messages[i]
            if msg.get("type") == "compact_boundary":
                boundary_idx = i
                # Summary 在 boundary 前一条
                if i > 0 and messages[i - 1].get("type") == "compact_summary":
                    summary_idx = i - 1
                break

        # Step 2: 保留 system prompt + Summary + Boundary 之后的消息
        if boundary_idx >= 0:
            # system prompt 永远保留（第一条 role=system 且非 isMeta 的消息）
            for msg in messages:
                if msg["role"] == "system" and not msg.get("isMeta"):
                    result.append(dict(msg))
                    break
            if summary_idx >= 0:
                result.append(dict(messages[summary_idx]))
            result.extend(dict(m) for m in messages[boundary_idx + 1:])
        else:
            result.extend(dict(m) for m in messages)

        # Step 3: 过滤 isMeta → Attachment 转 system 消息，Boundary/Summary 清理
        cleaned: list[dict] = []
        for msg in result:
            if not msg.get("isMeta"):
                cleaned.append(msg)
                continue
            msg_type = msg.get("type")
            if msg_type == "attachment":
                # Attachment → 转 system-reminder（content 已是 to_system_reminder() 结果）
                cleaned.append({"role": "system", "content": msg["content"]})
            # compact_boundary / compact_summary 的 isMeta 消息直接跳过
            # (Summary 已在 step 2 处理，但这里 isMeta=True 的 summary 也要转成普通 assistant)
            elif msg_type == "compact_summary":
                cleaned.append({"role": "assistant", "content": msg["content"]})
            # compact_boundary 跳过
        result = cleaned

        # Step 4: 修 tool_call 配对
        result = self._fix_tool_call_pairing(result)

        # Step 5: 超阈值裁剪
        if self._compact_threshold and len(result) > self._compact_threshold:
            result = self._trim_messages(result)

        return result

    def _fix_tool_call_pairing(self, messages: list[dict]) -> list[dict]:
        """修 tool_call 配对：删除无配对 tool 消息和断裂的 tool_calls。

        OpenAI API 要求：assistant 的 tool_calls 必须有对应的 tool role 消息，
        tool role 消息也必须有对应的 assistant tool_calls。
        """
        # 收集所有 tool_call_id
        assistant_call_ids: set[str] = set()
        tool_response_ids: set[str] = set()

        for msg in messages:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    assistant_call_ids.add(tc["id"])
            elif msg["role"] == "tool":
                tool_response_ids.add(msg.get("tool_call_id", ""))

        # 只保留有配对的消息
        result: list[dict] = []
        for msg in messages:
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                # 过滤掉没有 tool 响应的 tool_calls
                paired_calls = [
                    tc for tc in msg["tool_calls"] if tc["id"] in tool_response_ids
                ]
                if paired_calls:
                    new_msg = dict(msg)
                    new_msg["tool_calls"] = paired_calls
                    result.append(new_msg)
                elif msg.get("content"):
                    # 有 content 但 tool_calls 全无配对 → 保留 content
                    new_msg = dict(msg)
                    new_msg.pop("tool_calls", None)
                    result.append(new_msg)
            elif msg["role"] == "tool":
                # 只保留有对应 assistant tool_calls 的 tool 消息
                if msg.get("tool_call_id", "") in assistant_call_ids:
                    result.append(msg)
            else:
                result.append(msg)

        return result

    def _trim_messages(self, messages: list[dict]) -> list[dict]:
        """从最旧 user/assistant 对开始裁剪，目标 = 阈值 × 0.7。

        保留：system 消息 + compact_summary + 最近消息。
        裁剪：从最旧的 user/assistant 对开始删，直到低于目标条数。
        """
        target = int(self._compact_threshold * 0.7)  # type: ignore[operator]

        # 分离 system 消息和其余消息
        system_msgs: list[dict] = []
        other_msgs: list[dict] = []
        for msg in messages:
            if msg["role"] == "system":
                system_msgs.append(msg)
            else:
                other_msgs.append(msg)

        # 从尾部保留最近的消息，裁剪最旧的
        if len(other_msgs) > target:
            other_msgs = other_msgs[-target:]

        # 修配对（裁剪可能破坏 tool_call 配对）
        result = system_msgs + other_msgs
        result = self._fix_tool_call_pairing(result)

        return result

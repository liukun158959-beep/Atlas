"""记忆层：管理会话消息历史。"""
from __future__ import annotations


class Memory:
    """会话级记忆，按 OpenAI 消息格式存储。"""

    def __init__(self, system_prompt: str | None = None) -> None:
        self.messages: list[dict] = []
        if system_prompt:
            self.messages.append({"role": "system", "content": system_prompt})

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, message: dict) -> None:
        self.messages.append(message)

    def add_tool(self, content: str, tool_call_id: str) -> None:
        self.messages.append(
            {"role": "tool", "content": content, "tool_call_id": tool_call_id}
        )

    def clear(self) -> None:
        """清空对话历史，保留 system prompt。"""
        self.messages = [m for m in self.messages if m["role"] == "system"]

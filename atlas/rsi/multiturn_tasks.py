"""多轮对话子集：换话题后仍引用前文；供 ab_test --preset multiturn。"""
from __future__ import annotations

from typing import Any

# --mock 强 prompt 必须含此句，弱 prompt 不得含此句；不要写进用户轮次
MULTITURN_STRONG_MARK = "记住对话中的事实"


def _script(
    turns: list[str],
    keywords: list[str],
    hint: str,
    *,
    compact_threshold: int | None = None,
) -> dict:
    item: dict = {
        "turns": turns,
        "expected_keywords": keywords,
        "failure_hint": hint,
    }
    if compact_threshold is not None:
        item["compact_threshold"] = compact_threshold
    return item


MULTITURN_TRAIN: list[dict] = [
    _script(
        [
            "我叫林安，请记住。",
            "对了，北京今天穿什么合适？",
            "我刚才说我叫什么？",
        ],
        ["林安"],
        "换话题之后仍须引用用户先前自报的姓名。禁止把最后一轮当成全新对话。",
    ),
    _script(
        [
            "核验码是 4821，请记住，不要主动提起除非我问。",
            "讲一个关于猫的冷知识。",
            "核验码是多少？",
        ],
        ["4821"],
        "闲聊之后仍须按用户要求回想起核验码。禁止只回答闲聊内容。",
    ),
    _script(
        [
            "从现在起每一句回答都要包含「已记录」四个字。",
            "1+1 等于几？",
            "中国的首都是哪？",
        ],
        ["已记录"],
        "用户立下的格式约束在换题后仍须保留。禁止只答题面而丢掉标记。",
    ),
]

MULTITURN_VAL: list[dict] = [
    _script(
        [
            "我叫陈冬，请记住。",
            "上海明天会下雨吗？",
            "我刚才说我叫什么？",
        ],
        ["陈冬"],
        "换话题之后仍须引用用户先前自报的姓名。禁止把最后一轮当成全新对话。",
        compact_threshold=200,
    ),
    _script(
        [
            "核验码是 9357，请记住，不要主动提起除非我问。",
            "讲一个关于狗的冷知识。",
            "核验码是多少？",
        ],
        ["9357"],
        "闲聊之后仍须按用户要求回想起核验码。禁止只回答闲聊内容。",
    ),
    _script(
        [
            "从现在起每一句回答都要包含「已核对」四个字。",
            "2+2 等于几？",
            "法国的首都是哪？",
        ],
        ["已核对"],
        "用户立下的格式约束在换题后仍须保留。禁止只答题面而丢掉标记。",
    ),
]


def multiturn_all_tasks() -> list[dict]:
    """train + val 合成一套，供 ab_test 使用。"""
    return list(MULTITURN_TRAIN) + list(MULTITURN_VAL)


def _conversation_blob(messages: list[dict]) -> str:
    parts: list[str] = []
    for m in messages:
        content = m.get("content") or ""
        if content:
            parts.append(content)
    return "\n".join(parts)


def _compact_summary(conversation: str) -> str:
    facts: list[str] = []
    if "林安" in conversation:
        facts.append("用户自称林安")
    if "陈冬" in conversation:
        facts.append("用户自称陈冬")
    if "4821" in conversation:
        facts.append("用户给出核验码 4821")
    if "9357" in conversation:
        facts.append("用户给出核验码 9357")
    if "已记录" in conversation:
        facts.append("用户要求每句包含已记录")
    if "已核对" in conversation:
        facts.append("用户要求每句包含已核对")
    found = "；".join(facts) if facts else "无额外关键信息"
    return (
        "1. 用户目标：多轮对话\n"
        f"2. 已完成步骤：已进行若干轮\n"
        f"3. 关键发现：{found}\n"
        "4. 当前状态：历史已压缩\n"
        "5. 待办事项：继续回答用户\n"
        "6. 关键文件/资源：无\n"
        "7. 错误与教训：无\n"
        "8. 决策记录：已 compact\n"
        "9. 下一步建议：根据摘要继续对话"
    )


def _is_distractor(user: str) -> bool:
    return any(
        key in user
        for key in ("穿什么", "冷知识", "1+1", "下雨", "2+2")
    )


def multiturn_mock_answer(system_prompt: str, messages: list[dict]) -> tuple[str, int]:
    """返回 (正文, prompt_tokens)。弱 prompt 不引用前文；强 prompt 按历史给关键字。"""
    first = messages[0] if messages else {}
    if "对话摘要助手" in (first.get("content") or ""):
        conv = messages[-1].get("content") or "" if messages else ""
        return _compact_summary(conv), 200

    user = ""
    for m in messages:
        if m.get("role") == "user":
            user = m.get("content") or ""
    blob = _conversation_blob(messages)
    tokens = 8500 if _is_distractor(user) else 100

    if MULTITURN_STRONG_MARK not in system_prompt:
        return "这是新问题，我没有前文可以引用。", tokens

    if "我叫什么" in user:
        if "林安" in blob:
            return "你叫林安。", 300
        if "陈冬" in blob:
            return "你叫陈冬。", 300
        raise RuntimeError("多轮 mock 召回姓名失败。恢复：摘要或历史须含自报姓名")
    if "核验码是多少" in user:
        if "4821" in blob:
            return "核验码是 4821。", 300
        if "9357" in blob:
            return "核验码是 9357。", 300
        raise RuntimeError("多轮 mock 召回核验码失败。恢复：摘要或历史须含核验码")
    if "中国的首都" in user:
        return "已记录。北京。", 300
    if "法国的首都" in user:
        return "已核对。巴黎。", 300
    if _is_distractor(user):
        if "已记录" in blob:
            return "已记录。收到。", tokens
        if "已核对" in blob:
            return "已核对。收到。", tokens
        return "收到，我们换个话题。", tokens
    return "好的，我记住了。", tokens


class MultiTurnMockLLM:
    """按 system prompt 是否含 MULTITURN_STRONG_MARK 答多轮题。"""

    model = "mock-multiturn-ab"

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        on_delta: Any = None,
    ) -> dict:
        system = ""
        for m in messages:
            if m.get("role") == "system":
                system = m.get("content") or ""
                break
        text, prompt_tokens = multiturn_mock_answer(system, messages)
        if on_delta:
            on_delta(text)
        return {
            "message": {"role": "assistant", "content": text},
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": 8,
                "total_tokens": prompt_tokens + 8,
            },
            "duration_ms": 1,
        }

"""通过真实 SDK 与模拟 HTTP SSE 验证流完整性，不访问模型服务。"""
from __future__ import annotations

import json

import httpx
import pytest
from openai import OpenAI

from atlas import Agent, Toolkit
from atlas.journal import InMemoryJournal
from atlas.llm.base import LLM
from atlas.render.plain import SilentRenderer


class EventStream(httpx.SyncByteStream):
    def __init__(self, events: list[dict | str], error: Exception | None = None):
        self.events = events
        self.error = error
        self.closed = False

    def __iter__(self):
        for event in self.events:
            data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
            wire = ("data: " + data + "\n\n").encode("utf-8")
            # 分割 UTF-8 字符与 SSE 行，经过真实 SDK 的增量解码。
            for offset in range(0, len(wire), 17):
                yield wire[offset:offset + 17]
        if self.error is not None:
            raise self.error

    def close(self):
        self.closed = True


def chunk(delta: dict, finish_reason: str | None = None) -> dict:
    return {
        "id": "chat-test", "object": "chat.completion.chunk", "created": 0,
        "model": "fake", "choices": [{
            "index": 0, "delta": delta, "finish_reason": finish_reason,
        }],
    }


@pytest.fixture
def model_stream():
    clients = []

    def make(events, error=None):
        body = EventStream(events, error)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(
                200, headers={"Content-Type": "text/event-stream"}, stream=body,
            )

        llm = LLM(model="fake", api_key="test-only", base_url="https://example.invalid/v1")
        llm.client.close()
        llm.client = OpenAI(
            api_key="test-only", base_url="https://example.invalid/v1", max_retries=0,
            http_client=httpx.Client(transport=httpx.MockTransport(handle)),
        )
        clients.append(llm.client)
        return llm, body, requests

    yield make
    for client in clients:
        client.close()


@pytest.mark.parametrize("done", [False, True])
def test_complete_text_keeps_trailing_usage(model_stream, done):
    usage = {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}
    events = [chunk({"content": "完整"}), chunk({"content": "回复"}, "stop"),
              {"id": "chat-test", "choices": [], "usage": usage}]
    if done:
        events.append("[DONE]")
    llm, body, requests = model_stream(events)
    pieces = []

    result = llm.chat([{"role": "user", "content": "test"}], on_delta=pieces.append)

    assert result["message"] == {"role": "assistant", "content": "完整回复"}
    assert result["usage"] == usage
    assert pieces == ["完整", "回复"]
    assert len(requests) == 1
    assert body.closed


@pytest.mark.parametrize("done", [False, True])
def test_partial_text_without_finish_reason_is_rejected(model_stream, done):
    events = [chunk({"content": "半截回复"})]
    if done:
        events.append("[DONE]")
    llm, body, requests = model_stream(events)
    pieces = []

    with pytest.raises(RuntimeError, match="finish_reason"):
        llm.chat([{"role": "user", "content": "test"}], on_delta=pieces.append)

    assert pieces == ["半截回复"]
    assert len(requests) == 1, "部分响应不可自动重试"
    assert body.closed


def test_empty_stream_is_rejected(model_stream):
    llm, body, _ = model_stream(["[DONE]"])
    with pytest.raises(RuntimeError, match="finish_reason"):
        llm.chat([], on_delta=lambda _: None)
    assert body.closed


def test_complete_tool_call_preserves_fragmented_arguments(model_stream):
    llm, body, _ = model_stream([
        chunk({"tool_calls": [{"index": 0, "id": "call-1", "type": "function",
                              "function": {"name": "lookup", "arguments": '{"name":'}}]}),
        chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"中文"}'}}]},
              "tool_calls"),
        "[DONE]",
    ])
    result = llm.chat([], on_delta=lambda _: None)
    assert result["message"] == {
        "role": "assistant", "content": None, "tool_calls": [{
            "id": "call-1", "type": "function",
            "function": {"name": "lookup", "arguments": '{"name":"中文"}'},
        }],
    }
    assert body.closed


@pytest.mark.parametrize("arguments", ['{"value":', '{"value":1}'])
@pytest.mark.parametrize("finish", [None, "length", "content_filter"])
def test_unfinished_tool_stream_cannot_execute_or_finish_agent(model_stream, arguments, finish):
    llm, body, requests = model_stream([chunk({"tool_calls": [{
        "index": 0, "id": "call-1", "type": "function",
        "function": {"name": "record", "arguments": arguments},
    }]}, finish)])
    calls = []
    tools = Toolkit()
    tools.register(
        name="record", description="记录一次查询", parameters={"type": "object"},
        func=lambda args: calls.append(args) or "recorded", isReadOnly=True,
    )
    journal = InMemoryJournal(save_full_payload=True)
    agent = Agent(llm=llm, tools=tools, journal=journal, renderer=SilentRenderer())

    with pytest.raises(RuntimeError, match="finish_reason"):
        agent.run_detailed("query", run_id="incomplete-tool")

    assert calls == []
    assert len(requests) == 1
    assert not any(event.event_type == "agent_finish"
                   for event in journal.get_events("incomplete-tool"))
    assert body.closed


def test_transport_failure_preserves_error_and_closes_stream(model_stream):
    llm, body, requests = model_stream(
        [chunk({"content": "片段"})], error=httpx.ReadError("stream interrupted"),
    )
    with pytest.raises(httpx.ReadError, match="stream interrupted"):
        llm.chat([], on_delta=lambda _: None)
    assert len(requests) == 1
    assert body.closed


def test_callback_failure_closes_stream_without_consuming_the_rest(model_stream):
    llm, body, requests = model_stream([
        chunk({"content": "第一段"}), chunk({"content": "不应继续消费"}, "stop"),
    ])

    def cancelled(_):
        raise ValueError("consumer cancelled")

    with pytest.raises(ValueError, match="consumer cancelled"):
        llm.chat([], on_delta=cancelled)
    assert len(requests) == 1
    assert body.closed

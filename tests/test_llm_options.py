import json
from unittest.mock import Mock

import httpx
from openai import OpenAI
import pytest

from atlas.llm import (LLM, ChatCallbacks, ChatCancelledError, ChatOptions,
                       IncompleteResponseError)


def completion(finish='stop'):
    return dict(id='fixture', object='chat.completion', created=0, model='fixture',
                choices=[dict(index=0, finish_reason=finish, message=dict(
                    role='assistant', content='answer', reasoning_content='private'))],
                usage=dict(prompt_tokens=2, completion_tokens=1, total_tokens=3))


@pytest.fixture
def model():
    seen, response = [], [completion()]
    def handle(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response[0])
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        with OpenAI(api_key='fixture', max_retries=0, http_client=transport) as sdk:
            yield LLM(model='fixture', client=sdk), seen, response


def test_nonstream_legacy_shape_and_per_call_options_do_not_leak(model):
    llm, seen, _ = model
    llm.sampling = dict(temperature=.4, top_p=.8, reasoning_effort='low')
    result = llm.chat([])
    assert result['message'] == dict(role='assistant', content='answer')
    assert result['usage']['total_tokens'] == 3
    assert 'stream' not in seen[-1]
    llm.chat([], options=ChatOptions(sampling=dict(temperature=.1), preserve_reasoning=True))
    assert seen[-1]['temperature'] == .1 and 'thinking' not in seen[-1]
    assert llm.sampling['temperature'] == .4
    llm.chat([])
    assert seen[-1]['temperature'] == .4 and seen[-1]['thinking'] == dict(type='enabled')


@pytest.mark.parametrize('finish', [None, 'length', 'content_filter', 'unknown'])
def test_nonstream_rejects_incomplete_and_preserves_partial(model, finish):
    llm, seen, response = model
    response[0] = completion(finish)
    with pytest.raises(IncompleteResponseError) as caught:
        llm.chat([])
    assert caught.value.finish_reason == finish
    assert caught.value.partial['message']['content'] == 'answer'
    assert len(seen) == 1


def test_nonstream_preserves_reasoning_only_when_requested_and_can_cancel(model):
    llm, _, _ = model
    result = llm.chat([], options=ChatOptions(preserve_reasoning=True))
    assert result['message']['reasoning_content'] == 'private'
    checks = iter([False, True])
    with pytest.raises(ChatCancelledError) as caught:
        llm.chat([], options=ChatOptions(should_cancel=lambda: next(checks)))
    assert caught.value.partial['message']['content'] == 'answer'


def test_nonstream_empty_choices_are_incomplete(model):
    llm, _, response = model
    response[0] = dict(choices=[])
    with pytest.raises(IncompleteResponseError): llm.chat([])


def test_callbacks_and_provider_options_are_scoped_to_one_call():
    seen = []
    def handle(request):
        seen.append(json.loads(request.content))
        event = dict(choices=[dict(index=0, finish_reason='stop', delta=dict(content='ok', reasoning_content='secret'))])
        return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\n')
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        sdk = OpenAI(api_key='fixture', max_retries=0, http_client=transport)
        llm = LLM(model='fixture', client=sdk)
        legacy, explicit = [], []
        llm.on_reasoning = legacy.append
        result = llm.chat([], options=ChatOptions(stream=True, preserve_reasoning=True,
            sampling=dict(reasoning_effort='low'), extra_body=dict(thinking=dict(clear_thinking=False), tool_stream=True)),
            callbacks=ChatCallbacks(on_reasoning=explicit.append))
        assert legacy == [] and explicit == ['secret']
        assert result['message']['reasoning_content'] == 'secret'
        assert seen[-1]['thinking'] == dict(type='enabled', clear_thinking=False)
        llm.chat([], on_delta=lambda _: None)
        assert legacy == ['secret'] and 'tool_stream' not in seen[-1] and 'thinking' not in seen[-1]


def test_client_replacement_closes_only_owned_clients():
    borrowed, owned, replacement = Mock(), Mock(), Mock()
    llm = LLM(model='fixture', client=borrowed)
    llm.bind_client(owned, model='new', owned=True)
    borrowed.close.assert_not_called()
    assert llm.model == 'new'
    llm.bind_client(owned, owned=True)
    owned.close.assert_not_called()
    llm.bind_client(replacement)
    owned.close.assert_called_once()
    llm.close(); llm.close()
    replacement.close.assert_not_called()
    with pytest.raises(RuntimeError, match='closed'): llm.chat([])


def test_internally_created_client_has_no_hidden_retries():
    with LLM(api_key='fixture') as llm:
        assert llm.client.max_retries == 0
    assert llm.client.is_closed()

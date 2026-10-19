"""Reusable offline behavior suite for OpenAI-compatible model adapters.

Subclass StreamContract together with unittest.TestCase and implement make_model
and call_model. Both Atlas and product adapters run these exact assertions.
"""
import json

import httpx
from openai import APIError, RateLimitError

from atlas.llm import ChatCancelledError, IncompleteResponseError


def chunk(delta=None, finish=None, index=0):
    return dict(id='fixture', object='chat.completion.chunk', created=0, model='fixture',
                choices=[dict(index=index, delta=delta or {}, finish_reason=finish)])


class EventBody(httpx.SyncByteStream):
    def __init__(self, events, error=None):
        self.events, self.error, self.closed = events, error, False

    def __iter__(self):
        yield b': keepalive\n\n'
        for event in self.events:
            data = event if isinstance(event, str) else json.dumps(event, ensure_ascii=False)
            wire = ('data: ' + data + '\n\n').encode('utf-8')
            for offset in range(0, len(wire), 13):
                yield wire[offset:offset + 13]
        if self.error:
            raise self.error

    def close(self):
        self.closed = True


class StreamContract:
    """make_model borrows a client; call_model must explicitly stream."""

    def stream_model(self, events, *, error=None, status=200):
        body = EventBody(events, error)
        requests = []

        def handle(request):
            requests.append(json.loads(request.content))
            return httpx.Response(status, headers={'Content-Type': 'text/event-stream'}, stream=body)

        transport = httpx.Client(transport=httpx.MockTransport(handle))
        self.addCleanup(transport.close)
        model = self.make_model(transport)
        self.addCleanup(model.close)
        return model, body, requests, transport

    def test_contract_text_reasoning_and_usage_trailer(self):
        usage = dict(prompt_tokens=2, completion_tokens=3, total_tokens=5,
                     completion_tokens_details=dict(reasoning_tokens=1))
        model, body, requests, _ = self.stream_model([
            chunk(dict(reasoning_content='private')), chunk(dict(content='你好')),
            chunk(dict(content='，世界'), 'stop'), dict(choices=[], usage=usage), '[DONE]',
        ])
        thoughts, text = [], []
        model.on_reasoning = thoughts.append
        result = self.call_model(model, on_delta=text.append)
        self.assertEqual(result['message'], dict(role='assistant', content='你好，世界'))
        self.assertEqual(result['usage']['total_tokens'], 5)
        self.assertEqual(result['usage']['completion_tokens_details']['reasoning_tokens'], 1)
        self.assertEqual(text, ['你好', '，世界'])
        self.assertEqual(thoughts, ['private'])
        self.assertEqual(len(requests), 1)
        self.assertTrue(body.closed)

    def test_contract_explicit_finish_without_done(self):
        model, body, _, _ = self.stream_model([chunk(dict(content='ok'), 'stop')])
        self.assertEqual(self.call_model(model)['message']['content'], 'ok')
        self.assertTrue(body.closed)

    def test_contract_eof_and_done_alone_are_incomplete(self):
        for events in ([], ['[DONE]'], [chunk(dict(content='partial'))],
                       [chunk(dict(content='partial')), '[DONE]']):
            with self.subTest(events=events):
                model, body, requests, _ = self.stream_model(events)
                pieces = []
                with self.assertRaises(IncompleteResponseError) as caught:
                    self.call_model(model, on_delta=pieces.append)
                self.assertIsNone(caught.exception.finish_reason)
                self.assertEqual(caught.exception.partial['message']['content'], ''.join(pieces) or None)
                self.assertEqual(len(requests), 1)
                self.assertTrue(body.closed)

    def test_contract_limit_filter_and_unknown_finish_retain_partial_tools(self):
        for finish in ('length', 'content_filter', 'unexpected'):
            with self.subTest(finish=finish):
                model, body, requests, _ = self.stream_model([chunk(dict(
                    content='partial', tool_calls=[dict(index=0, id='call-1',
                    function=dict(name='write', arguments='{"x":'))]), finish), '[DONE]'])
                with self.assertRaises(IncompleteResponseError) as caught:
                    self.call_model(model)
                self.assertEqual(caught.exception.finish_reason, finish)
                self.assertEqual(caught.exception.partial['message']['tool_calls'][0]['function']['arguments'], '{"x":')
                self.assertEqual(caught.exception.partial['message']['content'], 'partial')
                self.assertEqual(len(requests), 1)
                self.assertTrue(body.closed)

    def test_contract_tools_are_sorted_and_callbacks_are_accumulated(self):
        model, body, _, _ = self.stream_model([
            chunk(dict(tool_calls=[dict(index=1, id='second', function=dict(name='read', arguments='{}'))])),
            chunk(dict(tool_calls=[dict(index=0, id='first', function=dict(name='web_', arguments='{"q":'))])),
            chunk(dict(tool_calls=[dict(index=0, function=dict(name='search', arguments='"中文"}'))]), 'tool_calls'),
        ])
        updates, progress = [], []
        model.on_tool_delta = lambda *args: updates.append(args)
        model.on_progress = lambda: progress.append(True)
        result = self.call_model(model)
        self.assertEqual([c['id'] for c in result['message']['tool_calls']], ['first', 'second'])
        self.assertEqual(updates[-1], (0, 'web_search', '{"q":"中文"}'))
        self.assertEqual(len(progress), 3)
        self.assertTrue(body.closed)

    def test_contract_other_choice_cannot_complete_first_choice(self):
        model, body, _, _ = self.stream_model([chunk(dict(content='ignored'), 'stop', index=1)])
        with self.assertRaises(IncompleteResponseError): self.call_model(model)
        self.assertTrue(body.closed)

    def test_contract_http_error_is_not_retried(self):
        model, body, requests, _ = self.stream_model([], status=429)
        with self.assertRaises(RateLimitError): self.call_model(model)
        self.assertEqual(len(requests), 1)
        self.assertTrue(body.closed)

    def test_contract_transport_error_and_callback_error_close_response(self):
        for failure in ('transport', 'callback'):
            with self.subTest(failure=failure):
                model, body, requests, _ = self.stream_model(
                    [chunk(dict(content='partial'))],
                    error=httpx.ReadError('broken stream') if failure == 'transport' else None,
                )
                def consume(_):
                    if failure == 'callback': raise ValueError('consumer failed')
                with self.assertRaises(httpx.ReadError if failure == 'transport' else ValueError):
                    self.call_model(model, on_delta=consume)
                self.assertEqual(len(requests), 1)
                self.assertTrue(body.closed)

    def test_contract_stream_error_event_is_not_success(self):
        model, body, requests, _ = self.stream_model([dict(error=dict(message='provider failed'))])
        with self.assertRaises(APIError): self.call_model(model)
        self.assertEqual(len(requests), 1)
        self.assertTrue(body.closed)

    def test_contract_cancel_before_request(self):
        model, _, requests, _ = self.stream_model([chunk(dict(content='unused'), 'stop')])
        with self.assertRaises(ChatCancelledError) as caught:
            self.call_model(model, should_cancel=lambda: True)
        self.assertIsNone(caught.exception.partial['message']['content'])
        self.assertEqual(requests, [])

    def test_contract_cancel_during_output_retains_partial_and_closes(self):
        model, body, requests, _ = self.stream_model([
            chunk(dict(content='partial')), chunk(dict(content='must not arrive'), 'stop'),
        ])
        pieces = []
        with self.assertRaises(ChatCancelledError) as caught:
            self.call_model(model, on_delta=pieces.append, should_cancel=lambda: bool(pieces))
        self.assertEqual(pieces, ['partial'])
        self.assertEqual(caught.exception.partial['message']['content'], 'partial')
        self.assertEqual(len(requests), 1)
        self.assertTrue(body.closed)

    def test_contract_borrowed_client_survives_success_and_failure(self):
        for events in ([chunk(dict(content='ok'), 'stop')], ['[DONE]']):
            with self.subTest(events=events):
                model, _, _, client = self.stream_model(events)
                try: self.call_model(model)
                except IncompleteResponseError: pass
                model.close()
                self.assertFalse(client.is_closed)

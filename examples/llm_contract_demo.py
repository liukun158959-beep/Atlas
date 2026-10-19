"""Offline SDK/HTTP example: python examples/llm_contract_demo.py --mock."""
import argparse
import json

import httpx
from openai import OpenAI

from atlas.llm import LLM, ChatCallbacks, ChatOptions, IncompleteResponseError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mock', action='store_true', required=True)
    parser.add_argument('--incomplete', action='store_true')
    args = parser.parse_args()
    events = [dict(choices=[dict(index=0, delta=dict(reasoning_content='模拟思考'))]),
              dict(choices=[dict(index=0, delta=dict(content='模拟回答'),
                                 finish_reason=None if args.incomplete else 'stop')]),
              dict(choices=[], usage=dict(prompt_tokens=2, completion_tokens=1, total_tokens=3))]
    def handle(_):
        return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
                              + 'data: [DONE]\n\n')
    with httpx.Client(transport=httpx.MockTransport(handle)) as transport:
        with OpenAI(api_key='mock-only', max_retries=0, http_client=transport) as sdk:
            llm = LLM(model='mock', client=sdk)
            text, thoughts = [], []
            try:
                result = llm.chat([], options=ChatOptions(stream=True), callbacks=ChatCallbacks(
                    on_delta=text.append, on_reasoning=thoughts.append))
                assert result['message']['content'] == '模拟回答' and thoughts == ['模拟思考']
                print(json.dumps(dict(result=result, text=text, reasoning=thoughts), ensure_ascii=False))
            except IncompleteResponseError as error:
                print(json.dumps(dict(error=str(error), partial=error.partial), ensure_ascii=False))
                return 2
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

import unittest

from openai import OpenAI

from atlas.llm import LLM, ChatOptions
from atlas.testing.llm_contract import StreamContract


class AtlasStreamContract(StreamContract, unittest.TestCase):
    def make_model(self, transport):
        return LLM(model='fixture', client=OpenAI(api_key='fixture', base_url='https://example.invalid/v1',
                                               max_retries=0, http_client=transport))

    def call_model(self, model, on_delta=None, should_cancel=None):
        return model.chat([], on_delta=on_delta, options=ChatOptions(stream=True, should_cancel=should_cancel))

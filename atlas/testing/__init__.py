"""测试辅助（不进 atlas 顶层导出）。"""
from .fake_llm import FakeLLM, LoopingFakeLLM

__all__ = ["FakeLLM", "LoopingFakeLLM"]

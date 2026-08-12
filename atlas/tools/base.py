"""工具层：统一的工具定义与注册调度。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Callable


@dataclass
class Tool:
    """单个工具的定义。"""

    name: str
    description: str
    parameters: dict  # JSON Schema 描述参数（静态默认值，首次注册占位）
    func: Callable[[dict], str]  # 接收参数字典，返回字符串结果
    # Step 2.3 新增：Q19 schema 懒加载 factory。
    # 非 None 时，Toolkit.to_openai_tools() 每轮调用重新生成 parameters（如 role enum 动态更新）。
    parameters_factory: Callable[[], dict] | None = None


class Toolkit:
    """工具集合，负责注册、导出 OpenAI 协议、按名执行。"""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(
        self,
        func: Callable[[dict], str],
        name: str,
        description: str,
        parameters: dict,
    ) -> Tool:
        """注册一个工具，重名时直接报错（消除命名冲突的模糊性）。"""
        if name in self._tools:
            raise ValueError(f"工具已存在: {name}")
        tool = Tool(name=name, description=description, parameters=parameters, func=func)
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        if name not in self._tools:
            raise KeyError(f"未注册的工具: {name}")
        return self._tools[name]

    def to_openai_tools(self) -> list[dict]:
        """转换为 OpenAI function calling 协议。Q19：支持 parameters_factory 懒加载。"""
        result: list[dict] = []
        for t in self._tools.values():
            params = t.parameters_factory() if t.parameters_factory else t.parameters
            result.append({
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": params,
                },
            })
        return result

    def execute(self, name: str, arguments: str | dict) -> str:
        """按名称执行工具，arguments 支持 JSON 字符串或字典。"""
        tool = self.get(name)
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        return tool.func(arguments)

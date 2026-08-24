"""工具层：统一的工具定义与注册调度。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import jsonschema

_log = logging.getLogger(__name__)


@dataclass
class Tool:
    """单个工具的定义。"""

    name: str
    description: str
    parameters: dict  # JSON Schema 描述参数（静态默认值，首次注册占位）
    func: Callable[[dict], str]  # 接收参数字典，返回字符串结果
    # Q19 schema 懒加载 factory。
    # 非 None 时，Toolkit.to_openai_tools() 每轮调用重新生成 parameters（如 role enum 动态更新）。
    parameters_factory: Callable[[], dict] | None = None
    # Phase 1 Tool ABI 增强：三分类标志，用于 Saga 补偿分类决策
    isReadOnly: bool = False  # 纯查询，无副作用
    isIdempotent: bool = False  # 天然幂等（重放安全）
    hasSideEffects: bool = False  # 有副作用，需要补偿管理
    compensate: Callable[[dict, Any, dict], dict] | None = None
    """补偿函数（仅当 hasSideEffects=True 且 isIdempotent=False 时必须非 None）。
    参数 1: compensate_input - 原工具执行时的输入 dict（含 _idempotency_key）
    参数 2: original_result - 原工具成功时的返回结果字符串（失败时为 None 或错误描述）
    参数 3: context        - dict，包含 run_id/step_id/tool_name 三个上下文字段
    返回值: 不做要求，内核只关心是否抛异常。返回非 None 的带 error 键 dict 视为业务失败。
    """

    circuit_key: str | None = None
    """熔断器隔离键。None 时运行时 fallback 为 tool_name。不同工具共享相同 circuit_key 可共用一个熔断器（如同一外部服务的多个接口）。"""

    retry_on: tuple[type[Exception], ...] = (TimeoutError, ConnectionError)
    """触发自动重试的异常白名单。空 tuple = 永不重试（即便 retry_max>0 也不重试）。仅当 isIdempotent=True 时生效，非幂等工具 retry_max>0 会在注册时被拒绝。"""

    retry_max: int = 3
    """最大重试次数。0 = 永不重试（总尝试次数 = 1 次原执行 + retry_max 次重试）。"""

    def validate_input(self, input: dict) -> None:
        """用 JSON Schema 校验输入参数，校验失败抛 jsonschema.ValidationError。"""
        schema = self.parameters
        if not schema and self.parameters_factory:
            schema = self.parameters_factory()
        if schema:
            jsonschema.validate(instance=input, schema=schema)


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
        isReadOnly: bool = False,
        isIdempotent: bool = False,
        hasSideEffects: bool = False,
        compensate: Callable[[dict, Any, dict], dict] | None = None,
        circuit_key: str | None = None,
        retry_on: tuple[type[Exception], ...] = (TimeoutError, ConnectionError),
        retry_max: int = 3,
    ) -> Tool:
        """注册一个工具，重名直接报错。

        Args:
            func: ``dict -> str`` 实现。
            name / description / parameters: OpenAI function schema。
            isReadOnly / isIdempotent / hasSideEffects: Tool ABI 三分类。
            compensate: 非幂等副作用必须提供。
            circuit_key / retry_on / retry_max: 熔断与重试；非幂等禁止 retry_max>0。

        Returns:
            已登记的 ``Tool``。

        依赖:
            ``jsonschema``（validate_input）；由 ``AgentLoop`` 按名 ``execute``。
        """
        if name in self._tools:
            raise ValueError(f"工具已存在: {name}")
        # 纯查询天然幂等，自动修正
        if isReadOnly and not isIdempotent:
            isIdempotent = True
        # 有副作用且非幂等，必须注册 compensate()，否则直接拒绝
        if hasSideEffects and not isIdempotent and compensate is None:
            raise ValueError(
                f"Tool {name}: hasSideEffects=True 且非幂等，必须注册 compensate() 函数"
            )
        # 非幂等工具禁止自动重试（从源头消除 fallback 触发场景）
        if not isIdempotent and retry_max > 0:
            raise ValueError(
                f"Tool {name}: 非幂等工具禁止重试（retry_max 必须=0，请在调用处手动处理失败）"
            )
        tool = Tool(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            isReadOnly=isReadOnly,
            isIdempotent=isIdempotent,
            hasSideEffects=hasSideEffects,
            compensate=compensate,
            circuit_key=circuit_key,
            retry_on=retry_on,
            retry_max=retry_max,
        )
        self._tools[name] = tool
        return tool

    def get(self, name: str) -> Tool:
        """按名取工具；未注册则 KeyError。

        Args:
            name: 注册时的工具名。
        """
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

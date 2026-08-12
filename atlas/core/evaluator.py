"""Evaluator：独立的代理输出验证层，内核外调用。

最小实现：ArtifactSchemaEvaluator 用 jsonschema 校验 subagent 结果。
Protocol 对外开放，用户可注入自定义 Evaluator（如打分、规则校验等）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import jsonschema

from atlas.journal.base import JournalEvent


@runtime_checkable
class Evaluator(Protocol):
    """Evaluator 抽象接口：用户侧独立在 agent.run() 之后调用。"""

    def evaluate(
        self,
        result: str,
        events: list[JournalEvent],
        contracts: list | None = None,
    ) -> tuple[bool, list[str]]:
        """评估结果是否达标。

        Args:
            result: agent.run() 返回的最终答案字符串
            events: journal.get_events(run_id) 的事件列表
            contracts: 临时覆盖的契约列表，不传就用实例化时传入的

        Returns:
            (是否通过, 未通过原因列表（空列表=通过）)
        """
        ...


@dataclass
class ArtifactSchemaEvaluator:
    """内置 JSON Schema 契约校验 Evaluator。

    contracts 每项是 (artifact_key, json_schema_dict)：
    - 如果 result JSON 解析后是 list，每个 item 中含该 key 的子项逐个校验
    - 如果是 dict，直接找该 key 校验
    - 找不到 key 的跳过（不报错，因为可能多个契约对应多个 subagent）
    """

    contracts: list[tuple[str, dict]] = field(default_factory=list)

    def evaluate(
        self,
        result: str,
        events: list[JournalEvent],
        contracts: list[tuple[str, dict]] | None = None,
    ) -> tuple[bool, list[str]]:
        contracts = contracts or self.contracts
        reasons: list[str] = []
        # 1. 没有契约 → 直接通过（Evaluator 是 opt-in 校验）
        if not contracts:
            return (True, [])
        # 2. 解析 result：先当 JSON 解析（phase2-1 parallel_gather 返回 JSON 列表）
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return (False, ["无法解析 result 为 JSON，但提供了 contracts"])
        # 3. 统一 items 成列表
        items = parsed if isinstance(parsed, list) else [parsed]
        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for key, schema in contracts:
                if key in item:
                    try:
                        jsonschema.validate(instance=item[key], schema=schema)
                    except jsonschema.ValidationError as e:
                        reasons.append(f"item[{idx}].{key}: {e.message}")
        return (len(reasons) == 0, reasons)

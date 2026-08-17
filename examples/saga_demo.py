"""Saga 补偿机制演示：2 个副作用工具执行成功 → ship 失败 → Saga LIFO 补偿清零。

用法：
    python examples/saga_demo.py --mock --plain   # FakeLLM 100% 可复现（推荐验证用）
    python examples/saga_demo.py --mock            # FakeLLM + Rich 渲染
    python examples/saga_demo.py --plain           # 真实 LLM（不保证 100% 触发 Saga）
"""
from __future__ import annotations

import sys

from dotenv import load_dotenv

from atlas.core.agent import Agent
from atlas.core.terminator import MaxSteps
from atlas.journal import InMemoryJournal
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Toolkit
from atlas.utils.tracker import SideEffectTracker

# 全局副作用追踪器：3 个工具 + 3 个 compensate 共用，断言补偿完全清零
tracker = SideEffectTracker()

# ---------------------------------------------------------------- 工具定义
def write_purchase_order(params: dict) -> str:
    po_id, items = params["po_id"], params["items"]
    tracker.record(po_id, {"type": "po", "items": items})
    return f"PO[{po_id}]已创建，items={items}"

def delete_purchase_order(ci, result, ctx) -> dict:
    tracker.erase(ci["po_id"])
    return {}

def charge_payment(params: dict) -> str:
    txn_id, amount = params["txn_id"], params["amount_usd"]
    tracker.record(txn_id, {"type": "payment", "amount": amount})
    return f"支付[{txn_id}]扣款 ${amount} 成功"

def refund_payment(ci, result, ctx) -> dict:
    tracker.erase(ci["txn_id"])
    return {}

def ship_goods(params: dict) -> str:
    ship_id = params["ship_id"]
    # ship_id 以 FAIL 开头 → 模拟发货服务 5xx，故意让本轮失败触发 Saga
    if ship_id.startswith("FAIL"):
        raise RuntimeError(f"SHIP-SVC-500: 发货调度失败 ship_id={ship_id}")
    tracker.record(ship_id, {"type": "ship", "status": "dispatched"})
    return f"发货[{ship_id}]已出库"

def cancel_shipment(ci, result, ctx) -> dict:
    tracker.erase(ci["ship_id"])
    return {}


# ---------------------------------------------------------------- FakeLLM
class SagaFakeLLM:
    """第 1 轮 write_po + charge_payment（均成功入 saga_stack），
    第 2 轮 ship_goods(FAIL-SHIP-001) → 抛异常，之后 max_steps 终止触发 Saga。"""

    def __init__(self) -> None:
        self._call = 0

    def chat(self, messages, tools=None, on_delta=None):
        self._call += 1
        if self._call == 1:
            tool_calls = [
                {"id": "tc1", "type": "function",
                 "function": {"name": "write_purchase_order",
                              "arguments": '{"po_id":"PO-2026-0001","items":["SKU-A","SKU-B"]}'}},
                {"id": "tc2", "type": "function",
                 "function": {"name": "charge_payment",
                              "arguments": '{"txn_id":"TXN-PAY-8888","amount_usd":299.99}'}},
            ]
            msg: dict = {"role": "assistant", "content": "先创建采购单并扣款，然后发货。", "tool_calls": tool_calls}
        else:
            # 第 2+ 轮只调 FAIL-SHIP，让工具抛异常，最终 Agent 因 MaxSteps(2) 终止
            tool_calls = [{"id": "tc3", "type": "function",
                           "function": {"name": "ship_goods",
                                        "arguments": '{"ship_id":"FAIL-SHIP-001"}'}}]
            msg = {"role": "assistant", "content": "尝试发货。", "tool_calls": tool_calls}
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        return {"message": msg, "usage": usage, "duration_ms": 5}


def main() -> None:
    mock = "--mock" in sys.argv
    renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()
    renderer.rule("Saga 补偿机制演示 · Phase 2.1 收尾")

    # 1. 注册 3 个 非幂等有副作用 工具，全部带 compensate
    toolkit = Toolkit()
    toolkit.register(
        func=write_purchase_order, name="write_purchase_order",
        description="创建采购单（非幂等，有副作用）", compensate=delete_purchase_order,
        parameters={"type": "object", "required": ["po_id", "items"],
                    "properties": {"po_id": {"type": "string"}, "items": {"type": "array"}}},
        hasSideEffects=True, isIdempotent=False, retry_max=0,
    )
    toolkit.register(
        func=charge_payment, name="charge_payment",
        description="扣款支付（非幂等，有副作用）", compensate=refund_payment,
        parameters={"type": "object", "required": ["txn_id", "amount_usd"],
                    "properties": {"txn_id": {"type": "string"}, "amount_usd": {"type": "number"}}},
        hasSideEffects=True, isIdempotent=False, retry_max=0,
    )
    toolkit.register(
        func=ship_goods, name="ship_goods",
        description="发货出库（非幂等，有副作用；ship_id=FAIL-* 抛 5xx）", compensate=cancel_shipment,
        parameters={"type": "object", "required": ["ship_id"],
                    "properties": {"ship_id": {"type": "string"}}},
        hasSideEffects=True, isIdempotent=False, retry_max=0,
    )

    # 2. 构造 Agent（--mock 用 SagaFakeLLM，max_steps=2 确保两轮后触发 Saga）
    if mock:
        llm = SagaFakeLLM()
    else:
        load_dotenv()
        llm = LLM()
    journal = InMemoryJournal(save_full_payload=True)
    agent = Agent(
        llm=llm, tools=toolkit, journal=journal, renderer=renderer,
        system_prompt="你是订单履约助手，流程是：创建采购单 → 扣款 → 发货。",
        terminator=MaxSteps(3),
    )

    # 3. 执行任务（--mock 一定会触发 Saga）
    result = agent.run("请处理订单 PO-2026-0001：SKU-A/SKU-B，金额 $299.99")
    _ = result

    # 4. 断言 SideEffectTracker 最终为空（所有执行过的副作用都已补偿）
    renderer.rule("验证")
    snap = tracker.snapshot()
    renderer.print(f"SideEffectTracker 当前记录: {snap if snap else '空'}")
    assert tracker.is_empty(), (
        f"Bug: 仍有 {len(tracker)} 条副作用未补偿! snapshot={snap}"
    )
    renderer.print("[SideEffectTracker 最终 0 条记录，补偿全部成功]")
    # 补偿失败列表如果有的话，包含 ship_goods 是没真正执行成功的，所以不应该出现
    if "补偿失败列表" in result:
        renderer.print(f"[提示] 存在补偿失败项（人工介入）：{result}")
    renderer.print("=== saga_demo.py --mock 验证通过 ===")


if __name__ == "__main__":
    main()

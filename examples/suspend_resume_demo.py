"""Phase 3.2 Suspend/Resume Durable HITL 演示：审批流程 + 崩溃恢复（双 Graph 实例）。

图结构：
    submit_order → wait_approval(SuspendNode)
         │
         ├─(approved=True)──► ship_order ──► END
         └─(approved=False)─► cancel_order ──► END

7 项断言：
  ① Part 1 挂起：status="suspended" + prompt 含 PO-999/金额/商品
  ② Part 1 resume(approved=True) → 最终 status=shipped + shipment_id=SHIP-PO-999
  ③ Part 1 state 保留 + 注入：order_id + approved 都在 final_state
  ④ Part 2 run 新 run → resume(approved=False + reject_reason) → status=cancelled + reason
  ⑤ Part 1 Journal 事件链：≥4 条（graph_node_exec*? + graph_suspend + graph_resume + graph_node_exec*?）
  ⑥ Part 3 双实例崩溃恢复：del graph1 → 新建 graph2（同 Journal + 同结构）→ graph2.resume → 结果和 Part 1 完全一致
  ⑦ graph_resume 事件的 payload.resumed_from_seq = graph_suspend 事件的 seq（审计链路）

运行方式：
    python examples/suspend_resume_demo.py --plain   # 纯文本渲染，断言驱动（推荐）
    python examples/suspend_resume_demo.py           # Rich 渲染（若可用）
"""
from __future__ import annotations

import argparse
import sys
from typing import Callable

from atlas import (
    Graph,
    InMemoryJournal,
    SuspendNode,
    SuspendToken,
)
from atlas.core.graph import END
from atlas.render import PlainRenderer, RichRenderer


# =============================================================
# 节点定义（纯 Python，无 LLM，无外部依赖）
# =============================================================
def submit_order(state: dict) -> dict:
    """提交订单：生成订单号、金额、商品清单。"""
    return {
        "order_id": "PO-999",
        "amount": 199.99,
        "items": ["SKU-A", "SKU-B"],
        "submitted": True,
    }


def ship_order(state: dict) -> dict:
    """审批通过：生成发货单。"""
    return {
        "status": "shipped",
        "shipment_id": f"SHIP-{state['order_id']}",
    }


def cancel_order(state: dict) -> dict:
    """审批拒绝：取消订单。"""
    return {
        "status": "cancelled",
        "reason": f"审批拒绝: {state.get('reject_reason', '未提供拒绝理由')}",
    }


def build_graph(journal: InMemoryJournal, name: str = "approval_flow") -> Graph:
    """构建审批流程图（graph1 和 graph2 共用构建函数，保证结构一致 Q19=A）。"""
    g = Graph(name=name, journal=journal)
    g.add_node("submit_order", submit_order)
    g.add_node(
        "wait_approval",
        SuspendNode(
            lambda s: (
                f"请审批订单 {s['order_id']}，金额 ${s['amount']}，"
                f"商品 {', '.join(s['items'])}。"
                f" resume 用法：graph.resume(token, {{'approved': True/False [, 'reject_reason': str]}})"
            )
        ),
    )
    g.add_edge("submit_order", "wait_approval")
    g.add_conditional_edges(
        "wait_approval",
        lambda s: "ship" if s.get("approved") else "cancel",
        {"ship": "ship_order", "cancel": "cancel_order"},
    )
    g.add_node("ship_order", ship_order)
    g.add_node("cancel_order", cancel_order)
    g.add_edge("ship_order", END)
    g.add_edge("cancel_order", END)
    g.set_entry_point("submit_order")
    return g


# =============================================================
# Part 1：approved=True 完整流程
# =============================================================
def part1_approved(journal: InMemoryJournal, renderer) -> tuple[SuspendToken, dict, int]:
    """Part 1：挂起 → resume approved=True → ship。

    返回 (suspend_token, part1_final_state, graph_suspend_seq) 用于后续断言。
    """
    renderer.rule("Part 1：审批通过流程（approved=True → 发货）")
    graph1 = build_graph(journal, name="approval_part1")

    # 1. run 到 SuspendNode 挂起
    r1 = graph1.run({})
    renderer.print(f"挂起态 status: {r1.status}")
    renderer.print(f"挂起 prompt: {r1.suspend_prompt}")
    renderer.print(f"suspend_token.node_name: {r1.suspend_token.node_name if r1.suspend_token else None}")
    assert r1.status == "suspended", f"① 失败：status 应为 suspended，实际 {r1.status}"
    assert r1.suspend_prompt is not None and "PO-999" in r1.suspend_prompt and "$199.99" in r1.suspend_prompt and "SKU-A" in r1.suspend_prompt, (
        f"① 失败：prompt 应含 PO-999/$199.99/SKU-A，实际 {r1.suspend_prompt!r}"
    )
    token1: SuspendToken = r1.suspend_token  # type: ignore[assignment]
    assert token1.node_name == "wait_approval"
    renderer.print("assert ①: OK（挂起态 + prompt 内容）")

    # 2. resume approved=True
    r2 = graph1.resume(token1, {"approved": True})
    renderer.print(f"resume 后 status: {r2.status}")
    renderer.print(f"最终状态: status={r2.final_state.get('status')}, shipment_id={r2.final_state.get('shipment_id')}")
    assert r2.status == "completed", f"② 失败：resume 后 status 应为 completed，实际 {r2.status}"
    assert r2.final_state.get("status") == "shipped", f"② 失败：最终 status 应为 shipped，实际 {r2.final_state}"
    assert r2.final_state.get("shipment_id") == "SHIP-PO-999", f"② 失败：shipment_id 应为 SHIP-PO-999"
    renderer.print("assert ②: OK（approved=True → shipped + shipment_id）")

    # 3. state 保留 + 注入验证
    final_state = r2.final_state
    assert final_state["order_id"] == "PO-999", f"③ 失败：order_id 丢失，实际 {final_state.get('order_id')}"
    assert final_state["amount"] == 199.99, f"③ 失败：amount 丢失，实际 {final_state.get('amount')}"
    assert final_state["approved"] is True, f"③ 失败：approved 注入失败，实际 {final_state.get('approved')}"
    renderer.print("assert ③: OK（state 保留 + resume 注入）")

    # 记录 graph_suspend 事件 seq（后面 Part 3 断言用）
    events = journal.get_events(token1.run_id)
    suspend_seq = next(e.seq for e in events if e.event_type == "graph_suspend")
    return token1, final_state, suspend_seq


# =============================================================
# Part 2：approved=False 完整流程（新 run）
# =============================================================
def part2_rejected(journal: InMemoryJournal, renderer) -> None:
    """Part 2：新 run 挂起 → resume approved=False + reject_reason → cancel。"""
    renderer.rule("Part 2：审批拒绝流程（approved=False → 取消）")
    graph_p2 = build_graph(journal, name="approval_part2")

    r1 = graph_p2.run({})
    assert r1.status == "suspended"
    token = r1.suspend_token
    assert token is not None
    renderer.print(f"Part 2 run_id: {token.run_id}")

    r2 = graph_p2.resume(token, {"approved": False, "reject_reason": "库存不足"})
    renderer.print(f"最终状态: status={r2.final_state.get('status')}, reason={r2.final_state.get('reason')}")
    assert r2.final_state.get("status") == "cancelled", (
        f"④ 失败：最终 status 应为 cancelled，实际 {r2.final_state.get('status')}"
    )
    assert "库存不足" in r2.final_state.get("reason", ""), (
        f"④ 失败：reason 应含 '库存不足'，实际 {r2.final_state.get('reason')}"
    )
    renderer.print("assert ④: OK（approved=False → cancelled + 拒绝理由）")


# =============================================================
# Part 3：双 Graph 实例崩溃恢复（Q19=A）
# =============================================================
def part3_crash_recovery(
    journal: InMemoryJournal,
    renderer,
    part1_final_state: dict,
) -> None:
    """Part 3：模拟进程崩溃后 durable 恢复。

    - graph_p3_first 运行到挂起，返回 suspend_token → del graph_p3_first（模拟崩溃）
    - graph_p3_second 新建同结构 Graph（同一 Journal 实例）→ graph_p3_second.resume(token, approved=True)
    - 断言：final_state 和 Part 1 完全一致（对比关键 key：order_id/approved/status/shipment_id/submitted/amount）
    """
    renderer.rule("Part 3：双 Graph 实例崩溃恢复（模拟进程重启）")

    # 第一实例：run 到挂起
    graph_p3_first = build_graph(journal, name="approval_crash_first")
    r1 = graph_p3_first.run({})
    assert r1.status == "suspended" and r1.suspend_token is not None
    token3: SuspendToken = r1.suspend_token
    renderer.print(f"崩溃前 run_id: {token3.run_id}")
    renderer.print(f"崩溃前节点: {token3.node_name}")

    # 模拟崩溃：丢弃 graph_p3_first 实例
    del graph_p3_first
    renderer.print("模拟崩溃: del graph_p3_first（进程重启）")

    # 第二实例：同 Journal + 同图结构（调用 build_graph 重建结构）
    graph_p3_second = build_graph(journal, name="approval_crash_second")
    renderer.print("重启后实例: graph_p3_second（同 Journal + 同图结构）")

    # 从崩溃点 resume（崩溃时 approved=True 没来得及传，现在补）
    r2 = graph_p3_second.resume(token3, {"approved": True})
    renderer.print(f"恢复后最终状态: status={r2.final_state.get('status')}, shipment_id={r2.final_state.get('shipment_id')}")

    # 对比 Part 1 关键 key（Q19=A 验证 durable 恢复）
    recovery_state = r2.final_state
    assert recovery_state["order_id"] == part1_final_state["order_id"], f"⑥ 失败：order_id 不一致"
    assert recovery_state["approved"] == part1_final_state["approved"], f"⑥ 失败：approved 不一致"
    assert recovery_state["status"] == part1_final_state["status"], f"⑥ 失败：status 不一致"
    assert recovery_state["shipment_id"] == part1_final_state["shipment_id"], f"⑥ 失败：shipment_id 不一致"
    assert recovery_state["submitted"] == part1_final_state["submitted"], f"⑥ 失败：submitted 不一致"
    assert recovery_state["amount"] == part1_final_state["amount"], f"⑥ 失败：amount 不一致"
    renderer.print("assert ⑥: OK（双实例崩溃恢复结果 = Part 1 结果）")


# =============================================================
# Part 5：事件链 + 审计链路断言
# =============================================================
def part5_journal_asserts(
    journal: InMemoryJournal,
    token1: SuspendToken,
    suspend_seq: int,
    renderer,
) -> None:
    """⑤ Journal 事件链 + ⑦ graph_resume.payload.resumed_from_seq 校验。"""
    renderer.rule("Part 5：Journal 事件链 + 审计链路")
    events = journal.get_events(token1.run_id)
    event_types = [e.event_type for e in events]
    renderer.print(f"run_id={token1.run_id} 事件链: {len(events)} 条: {event_types}")

    # ⑤ 事件链应 ≥ 4 条（submit_exec + wait_exec + suspend + resume + ship_exec = 5 最小 4）
    assert len(events) >= 4, f"⑤ 失败：事件数应为 ≥4，实际 {len(events)}"
    assert "graph_node_exec" in event_types, "⑤ 失败：缺少 graph_node_exec"
    assert "graph_suspend" in event_types, "⑤ 失败：缺少 graph_suspend"
    assert "graph_resume" in event_types, "⑤ 失败：缺少 graph_resume"
    renderer.print(f"assert ⑤: OK（{len(events)} 条事件，三种类型齐全）")

    # ⑦ graph_resume.payload.resumed_from_seq == graph_suspend.seq
    resume_event = next(e for e in events if e.event_type == "graph_resume")
    resumed_from_seq = resume_event.payload.get("resumed_from_seq")
    assert resumed_from_seq == suspend_seq, (
        f"⑦ 失败：graph_resume.payload.resumed_from_seq={resumed_from_seq} "
        f"!= graph_suspend.seq={suspend_seq}"
    )
    renderer.print(f"assert ⑦: OK（resumed_from_seq={resumed_from_seq} == suspend.seq={suspend_seq}）")


# =============================================================
# 主函数
# =============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 3.2 Suspend/Resume 审批 Demo")
    parser.add_argument("--plain", action="store_true", help="PlainRenderer（不依赖 Rich）")
    args = parser.parse_args()

    renderer = PlainRenderer() if args.plain else RichRenderer()
    # 共享 Journal（同一进程内多个 Graph 实例共享，模拟持久化存储）
    journal = InMemoryJournal(save_full_payload=True)

    renderer.rule("Phase 3.2 Suspend/Resume Durable HITL Demo（7 项断言）")
    renderer.print("Journal: InMemoryJournal(save_full_payload=True)")
    renderer.print("图结构: submit_order → wait_approval(SuspendNode) → [ship|cancel] → END")

    # Part 1
    token1, part1_final_state, suspend_seq = part1_approved(journal, renderer)

    # Part 2
    part2_rejected(journal, renderer)

    # Part 3
    part3_crash_recovery(journal, renderer, part1_final_state)

    # Part 5（断言 ⑤ + ⑦）
    part5_journal_asserts(journal, token1, suspend_seq, renderer)

    # 汇总
    renderer.rule("7 项断言全部通过")
    renderer.print("① Part 1 挂起态: OK")
    renderer.print("② Part 1 approved→ship: OK")
    renderer.print("③ Part 1 state 保留+注入: OK")
    renderer.print("④ Part 2 rejected→cancel: OK")
    renderer.print("⑤ Journal 事件链: OK")
    renderer.print("⑥ 双实例崩溃恢复: OK")
    renderer.print("⑦ graph_resume.resumed_from_seq: OK")


if __name__ == "__main__":
    main()

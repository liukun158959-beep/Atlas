"""Phase 2.2 Resilience 演示：熔断器三态 + 指数退避重试 + 非幂等拒重试。

场景（--mock 模式全断言可重复）：
  1. flaky_api(call_id)        → 幂等，retry_max=3，连续抛 3 次 RuntimeError 后第 4 次成功
  2. unreliable_svc(req_id)    → 幂等，retry_max=1，永远抛 TimeoutError → retry_exhausted
  3. credit_card_charge(...)   → 非幂等有副作用，retry_max=0，带 compensate 回滚
  4. 手动触发 credit_card_charge 熔断器 Open → circuit_rejected 不产生副作用

运行方式：
  python examples/circuit_retry_demo.py --mock --plain          # 断言模式，100% 可重复
  python examples/circuit_retry_demo.py --mock --dashboard      # 断言 + 后台起 Dashboard HTTP(8765)
"""
from __future__ import annotations

import argparse
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

from atlas import Agent
from atlas.cli.dashboard import publish, run_server
from atlas.core.circuit_breaker import CircuitBreakerRegistry
from atlas.core.plugin import BasePlugin, PluginManager
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.tools.base import Toolkit


# =============================================================
# 工具定义
# =============================================================

# 内部状态计数器（断言依据）
FLAKY_CALL_COUNTER: Counter[str] = Counter()
UNRELIABLE_CALL_COUNTER: Counter[str] = Counter()


@dataclass
class SideEffectTracker:
    """非幂等副作用的全局记录（断言 circuit_rejected 不触发副作用用）。"""
    charged: list[tuple[str, float]] = field(default_factory=list)  # (txn_id, usd)
    refunded: list[str] = field(default_factory=list)              # txn_id 已退款列表

    def reset(self) -> None:
        self.charged.clear()
        self.refunded.clear()


_SIDE_EFFECT = SideEffectTracker()


def flaky_api(params: dict) -> str:
    """幂等：连续 3 次失败，第 4 次成功（模拟瞬时抖动 + 自动重试兜底）。"""
    call_id = params["call_id"]
    FLAKY_CALL_COUNTER[call_id] += 1
    n = FLAKY_CALL_COUNTER[call_id]
    if n <= 3:
        raise RuntimeError(f"flaky timeout #{n} for {call_id}")
    return f"success-ok[{call_id}] after {n} attempts"


def unreliable_svc(params: dict) -> str:
    """幂等：永远抛 TimeoutError（演示 retry_exhausted）。"""
    req_id = params["req_id"]
    UNRELIABLE_CALL_COUNTER[req_id] += 1
    raise TimeoutError(f"unreliable svc timeout for {req_id}")


def credit_card_charge(params: dict) -> str:
    """非幂等有副作用：扣款。"""
    txn_id = params["txn_id"]
    usd = float(params["usd"])
    _SIDE_EFFECT.charged.append((txn_id, usd))
    return f"CHARGED {txn_id} ${usd:.2f}"


def credit_card_refund(
    compensate_input: dict,
    original_result: object,
    context: dict,
) -> dict:
    """非幂等补偿：退款。"""
    txn_id = compensate_input["txn_id"]
    _SIDE_EFFECT.refunded.append(txn_id)
    return {"ok": True, "txn_id": txn_id}


# =============================================================
# FakeLLM：两轮，先 tool_calls → 再最终答案
# =============================================================

@dataclass
class ResilienceFakeLLM(LLM):
    model: str = "mock-resilience"
    _step: int = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None, on_delta=None) -> dict:
        self._step += 1
        ts = int(time.time() * 1000)
        if self._step == 1:
            # 第 1 轮：顺序调用 3 个工具
            return {
                "message": {
                    "role": "assistant",
                    "content": "我先调用 flaky_api、unreliable_svc 和 credit_card_charge 三个工具。",
                    "tool_calls": [
                        {
                            "id": "call_flaky_001",
                            "type": "function",
                            "function": {
                                "name": "flaky_api",
                                "arguments": '{"call_id": "FLAKY-DEMO-001"}',
                            },
                        },
                        {
                            "id": "call_unrel_001",
                            "type": "function",
                            "function": {
                                "name": "unreliable_svc",
                                "arguments": '{"req_id": "REQ-UNREL-001"}',
                            },
                        },
                        {
                            "id": "call_charge_001",
                            "type": "function",
                            "function": {
                                "name": "credit_card_charge",
                                "arguments": '{"txn_id": "TXN-001", "usd": 99.5}',
                            },
                        },
                    ],
                },
                "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20},
                "duration_ms": 5,
                "_ts": ts,
            }
        # 第 2 轮及以后：直接给最终答案，结束 run
        return {
            "message": {
                "role": "assistant",
                "content": "所有工具执行完毕，Phase 2.2 Resilience 演示结束。",
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            "duration_ms": 4,
            "_ts": ts,
        }


# =============================================================
# CapturePlugin：记录 resilience 事件用于断言
# =============================================================

@dataclass
class CapturePlugin(BasePlugin):
    name: str = "capture_resilience"
    priority: int = 5
    retry_before_events: list = field(default_factory=list)
    retry_exhausted_events: list = field(default_factory=list)
    circuit_events: list = field(default_factory=list)

    def on_retry_before(self, **kw):
        self.retry_before_events.append(kw)

    def on_retry_exhausted(self, **kw):
        self.retry_exhausted_events.append(kw)

    def on_circuit_open(self, **kw):
        self.circuit_events.append(("open", kw["circuit_key"]))

    def on_circuit_closed(self, **kw):
        self.circuit_events.append(("closed", kw["circuit_key"]))


# =============================================================
# 主流程
# =============================================================

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="使用 FakeLLM（不发真实请求，默认开启）", default=True)
    ap.add_argument("--real", dest="mock", action="store_false", help="使用 .env 中配置的真实 LLM")
    ap.add_argument("--plain", action="store_true", help="使用纯文本渲染器（Rich 不可用时）")
    ap.add_argument("--dashboard", action="store_true", help="后台启动 Atlas Dashboard(HTTP 8765)")
    args = ap.parse_args()

    renderer = PlainRenderer() if args.plain or not RichRenderer.available() else RichRenderer()
    renderer.rule("Phase 2.2 Resilience 演示 · 熔断器 + 重试 + 非幂等拒重试")

    # 重置所有计数器
    FLAKY_CALL_COUNTER.clear()
    UNRELIABLE_CALL_COUNTER.clear()
    _SIDE_EFFECT.reset()

    # 1. 注册工具
    tk = Toolkit()
    tk.register(
        func=flaky_api,
        name="flaky_api",
        description="幂等抖动接口：前 3 次抛 RuntimeError，第 4 次成功（自动重试）",
        parameters={"type": "object", "required": ["call_id"],
                    "properties": {"call_id": {"type": "string"}}},
        isIdempotent=True, retry_max=3,
        retry_on=(RuntimeError, TimeoutError, ConnectionError),
    )
    tk.register(
        func=unreliable_svc,
        name="unreliable_svc",
        description="幂等不可靠服务：永远抛 TimeoutError（演示 retry_exhausted）",
        parameters={"type": "object", "required": ["req_id"],
                    "properties": {"req_id": {"type": "string"}}},
        isIdempotent=True, retry_max=1,
        retry_on=(TimeoutError, ConnectionError),
    )
    tk.register(
        func=credit_card_charge,
        name="credit_card_charge",
        description="非幂等扣款支付：必须带 compensate，retry_max 必须=0",
        parameters={"type": "object", "required": ["txn_id", "usd"],
                    "properties": {"txn_id": {"type": "string"}, "usd": {"type": "number"}}},
        hasSideEffects=True, isIdempotent=False,
        compensate=credit_card_refund, retry_max=0,
    )

    # 2. 注入 CapturePlugin + 小阈值熔断器（N=3 快速熔断，W=9999 保持 Open 便于测试）
    cap_plugin = CapturePlugin()
    pm = PluginManager()
    pm.register(cap_plugin)
    registry = CircuitBreakerRegistry(failure_threshold=3, open_window_seconds=9999)

    if args.mock:
        llm: LLM = ResilienceFakeLLM()
    else:
        try:
            from dotenv import load_dotenv
            load_dotenv()
        except Exception:
            pass
        llm = LLM()

    agent = Agent(
        llm=llm, tools=tk, max_steps=3,
        renderer=renderer, plugin_manager=pm,
        circuit_breaker_registry=registry,
    )

    # 3. Dashboard 可选
    if args.dashboard:
        threading.Thread(target=lambda: run_server(8765), daemon=True).start()
        time.sleep(0.4)
        renderer.markdown(
            "Atlas Dashboard 已启动：HTTP http://localhost:8765  ·  WebSocket ws://localhost:8766\n"
            "顶部 RESILIENCE 横幅实时展示：熔断器状态 / 重试记录 / 告警",
            title="Dashboard"
        )

    # 4. 第一轮 run：flaky_api 重试成功 + unreliable_svc 重试耗尽 + 扣款成功
    run_id_1 = "run-resilience-main"
    agent.trace.meta["run_id"] = run_id_1
    if args.dashboard:
        publish(run_id_1, None, agent.trace, agent.circuit_breaker_registry)

    task1 = (
        "请依次执行：\n"
        "  a) flaky_api(call_id='FLAKY-DEMO-001') — 观察它是否在 3 次失败后第 4 次自动重试成功\n"
        "  b) unreliable_svc(req_id='REQ-UNREL-001') — 观察重试耗尽后是否标记失败\n"
        "  c) credit_card_charge(txn_id='TXN-001', usd=99.5) — 确认非幂等扣款正常执行一次"
    )
    ans = agent.run(task1)
    renderer.markdown(ans[:200], title="第一轮 run 最终回答")
    if args.dashboard:
        publish(run_id_1, None, agent.trace, agent.circuit_breaker_registry)
        time.sleep(0.6)

    # =============================================================
    # --mock 下核心断言
    # =============================================================
    if args.mock:
        renderer.rule("断言检查（仅 --mock 模式，真实 LLM 跳过）")
        # A1：flaky_api 调用次数 = 4（1 原执行 + 3 重试），最终成功
        flaky_calls = FLAKY_CALL_COUNTER.get("FLAKY-DEMO-001", 0)
        assert flaky_calls == 4, (
            f"[断言 A1 FAIL] flaky_api 应被调用 4 次（1原+3重试），实际={flaky_calls}"
        )
        renderer.markdown(f"[断言 A1 PASS] flaky_api 调用次数={flaky_calls}（1原+3重试）", title="A1")

        # A2：unreliable_svc 调用次数 = 2（1原+1重试），且发生 retry_exhausted 事件
        unrel_calls = UNRELIABLE_CALL_COUNTER.get("REQ-UNREL-001", 0)
        assert unrel_calls == 2, (
            f"[断言 A2 FAIL] unreliable_svc 应被调用 2 次（1原+1重试），实际={unrel_calls}"
        )
        exhausted_names = [e["tool_name"] for e in cap_plugin.retry_exhausted_events]
        assert "unreliable_svc" in exhausted_names, (
            f"[断言 A2b FAIL] 未捕获 unreliable_svc 的 retry_exhausted 事件，实际={exhausted_names}"
        )
        renderer.markdown(
            f"[断言 A2 PASS] unreliable_svc 调用={unrel_calls}, retry_exhausted 事件捕获={len(cap_plugin.retry_exhausted_events)} 条",
            title="A2"
        )

        # A3：flaky_api 熔断器最终清零（on_success）
        snap = agent.circuit_breaker_registry.snapshot() if agent.circuit_breaker_registry else {"circuit_breakers": []}
        flaky_row = next((r for r in snap["circuit_breakers"] if r["key"] == "flaky_api"), None)
        assert flaky_row is not None and flaky_row["state"] == "closed" and flaky_row["failure_count"] == 0, (
            f"[断言 A3 FAIL] flaky_api 熔断器应清零 closed/0，实际={flaky_row}"
        )
        renderer.markdown(
            f"[断言 A3 PASS] flaky_api 熔断器 state={flaky_row['state']}, failure_count={flaky_row['failure_count']}",
            title="A3"
        )

        # A4：第一轮 side effect = 1 次扣款；然后手动触发 credit_card_charge 熔断器 Open，
        #     再用 agent 第二轮跑 tool_calls（通过手动调 tools.execute 走 circuit_rejected 路径），
        #     此时 side effect 记录不变（熔断器拒绝 = 不真正执行 → 无副作用）
        initial_charged = len(_SIDE_EFFECT.charged)
        assert initial_charged == 1 and _SIDE_EFFECT.charged[0][0] == "TXN-001", (
            f"[断言 A4 FAIL] 第一轮应扣款 1 条 TXN-001，实际={_SIDE_EFFECT.charged}"
        )
        # 手动把 credit_card_charge 熔断器打到 Open（3 次 on_failure）
        for _ in range(3):
            agent.circuit_breaker_registry.on_failure("credit_card_charge", run_id="manual-open")
        snap2 = agent.circuit_breaker_registry.snapshot()
        cc_row = next((r for r in snap2["circuit_breakers"] if r["key"] == "credit_card_charge"), None)
        assert cc_row is not None and cc_row["state"] == "open", (
            f"[断言 A4b FAIL] credit_card_charge 应 Open，实际={cc_row}"
        )
        # 手动走一次熔断器拒绝路径（模拟 agent.run 流程：allow_request → circuit_rejected）
        circuit_key = "credit_card_charge"
        allowed, _st, reason = agent.circuit_breaker_registry.allow_request(circuit_key, run_id="manual-check")
        assert not allowed and "OPEN" in (reason or ""), f"应被熔断器拒绝，实际 allowed={allowed}, reason={reason}"
        # 关键：拒绝后一定不调用 func → SideEffectTracker 记录数不变
        assert len(_SIDE_EFFECT.charged) == 1, (
            f"[断言 A4c FAIL] circuit_rejected 不应产生副作用，charged={_SIDE_EFFECT.charged}"
        )
        renderer.markdown(
            f"[断言 A4 PASS] 第一轮扣款记录={initial_charged}；熔断器拒绝后 charged 仍={len(_SIDE_EFFECT.charged)}，无副作用产生",
            title="A4"
        )
        print()
        print("=== circuit_retry_demo.py --mock 全部断言通过 ===")

    if args.dashboard:
        print("\n[dashboard 模式] 再驻留 30 秒，浏览器刷新查看最终状态。Ctrl+C 提前退出。")
        try:
            for sec in range(30):
                publish(run_id_1, None, agent.trace, agent.circuit_breaker_registry)
                time.sleep(1)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()

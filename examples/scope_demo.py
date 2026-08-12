"""Scope demo：per-key 隔离验证。

3 个断言：
    1. 同 key 两线程 sleep(0.1)：时间戳串行（后 start_ts > 前 end_ts）
    2. 不同 key 两线程 sleep(0.1)：时间戳并行（end_ts - start_ts < 0.18，即重叠）
    3. state 写回：两次 acquire("k1") 后第二次能读到第一次写入的值
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from atlas.core.scope import Scope


def worker_sequential(
    scope: Scope,
    key: str,
    results: list[dict],
    idx: int,
    sleep_s: float,
) -> None:
    with scope.acquire(key) as ctx:
        start_ts = time.perf_counter()
        time.sleep(sleep_s)
        ctx.state[f"worker_{idx}"] = True
        end_ts = time.perf_counter()
        results.append({"idx": idx, "start": start_ts, "end": end_ts})


def main() -> None:
    scope = Scope()

    # ---------- Case 1：同 key 串行 ----------
    seq_results: list[dict] = []
    t1 = threading.Thread(target=worker_sequential, args=(scope, "same-key", seq_results, 1, 0.1))
    t2 = threading.Thread(target=worker_sequential, args=(scope, "same-key", seq_results, 2, 0.1))
    t1.start()
    t2.start()
    t1.join()
    t2.join()
    seq_results.sort(key=lambda r: r["idx"])

    second_start_after_first_end = seq_results[1]["start"] >= seq_results[0]["end"]
    assert second_start_after_first_end, (
        f"Case 1 FAIL：同 key 应串行（idx={seq_results[0]['idx']} end={seq_results[0]['end']:.6f},"
        f" 但 idx={seq_results[1]['idx']} start={seq_results[1]['start']:.6f}）"
    )
    print(f"[Scope Case 1 PASS] 同 key 串行: idx1 end={seq_results[0]['end']:.6f} <= idx2 start={seq_results[1]['start']:.6f}")

    # ---------- Case 2：不同 key 并行 ----------
    par_results: list[dict] = []
    t3 = threading.Thread(target=worker_sequential, args=(scope, "key-A", par_results, 3, 0.1))
    t4 = threading.Thread(target=worker_sequential, args=(scope, "key-B", par_results, 4, 0.1))
    start_all = time.perf_counter()
    t3.start()
    t4.start()
    t3.join()
    t4.join()
    end_all = time.perf_counter()
    total = end_all - start_all

    # 各 sleep 0.1 秒，并行总耗时应 < 0.18（串行则 ≥ 0.2）
    assert total < 0.18, (
        f"Case 2 FAIL：不同 key 应并行（总耗时 {total:.3f}s，并行应 < 0.18s）"
    )
    print(f"[Scope Case 2 PASS] 不同 key 并行：总耗时 {total:.3f}s < 0.18s")

    # ---------- Case 3：state 写回持久化 ----------
    with scope.acquire("k1") as ctx:
        ctx.state["first_write"] = 42

    state_after_first = scope.get_state("k1")
    assert state_after_first.get("first_write") == 42, (
        f"Case 3 FAIL：第一次 write 后 state 应为 42, 实际 {state_after_first}"
    )

    with scope.acquire("k1") as ctx:
        assert ctx.state.get("first_write") == 42, (
            f"Case 3 FAIL：第二次 acquire 应读到 first_write=42, 实际 {ctx.state}"
        )
        ctx.state["second_write"] = "hello"

    state_after_second = scope.get_state("k1")
    assert state_after_second == {"first_write": 42, "second_write": "hello"}, (
        f"Case 3 FAIL：最终 state 不匹配, 实际 {state_after_second}"
    )
    print(f"[Scope Case 3 PASS] state 写回: 最终 state={state_after_second}")

    print("\n[Scope demo 全部通过]")


if __name__ == "__main__":
    main()

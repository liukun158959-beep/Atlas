"""三态熔断器：Closed → Open → Half-Open → Closed。

算法：连续失败计数 N（默认 5），冷却窗口 W（默认 30 秒）。
- Closed：正常放行，每次失败 +1，达到 N → Open
- Open：拒绝所有请求，W 秒后 → Half-Open
- Half-Open：只放 1 个探测请求，成功 → Closed；失败 → Open 重计时

对外 API：allow_request / on_success / on_failure / snapshot / append_retry_log。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

# Open 状态超过该秒数视为"严重告警"（Dashboard 红色高亮）
_ALERT_OPEN_THRESHOLD_SECONDS = 60
# retry_logs 最大条数（ring buffer）
_RETRY_LOGS_CAPACITY = 200


@dataclass
class _State:
    state: str = CLOSED
    failure_count: int = 0
    open_since_ts: float = 0.0
    half_open_inflight: int = 0
    last_state_change_ts: float | None = None


@dataclass
class CircuitBreakerRegistry:
    """熔断器注册中心：按 circuit_key 管理独立三态状态机。线程安全。"""

    failure_threshold: int = 5
    open_window_seconds: int = 30
    _states: dict[str, _State] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _plugin_manager: Any = None  # 可选懒注入，用于状态变更时 emit 插件事件
    _retry_logs: list[dict] = field(default_factory=list)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _get(self, circuit_key: str) -> _State:
        if circuit_key not in self._states:
            self._states[circuit_key] = _State()
        return self._states[circuit_key]

    def _safe_emit(self, event: str, **kwargs: Any) -> None:
        """安全分发插件事件（PluginManager 未注入时 no-op）。"""
        if self._plugin_manager is None:
            return
        try:
            self._plugin_manager.emit(event, **kwargs)
        except Exception:
            # 插件分发失败绝不影响熔断器主逻辑
            pass

    def set_plugin_manager(self, pm: Any) -> None:
        """运行时注入 PluginManager（避免模块级循环引用）。"""
        self._plugin_manager = pm

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    def allow_request(
        self, circuit_key: str, run_id: str | None = None
    ) -> tuple[bool, str | None, str | None]:
        """请求是否放行。

        Args:
            circuit_key: 熔断隔离键（常为 tool.circuit_key 或工具名）。
            run_id: 可选，写入插件事件。

        Returns:
            (是否放行, 放行后状态, 拒绝原因)。

        依赖:
            可选 ``PluginManager.emit``（circuit_half_open 等）。
        """
        with self._lock:
            s = self._get(circuit_key)
            now = time.time()
            if s.state == OPEN:
                remain = self.open_window_seconds - (now - s.open_since_ts)
                if remain <= 0:
                    # 冷却结束 → Half-Open
                    s.state = HALF_OPEN
                    s.half_open_inflight = 1
                    s.last_state_change_ts = now
                    self._safe_emit(
                        "circuit_half_open",
                        run_id=run_id,
                        circuit_key=circuit_key,
                    )
                    return (True, HALF_OPEN, "Open 冷却结束，半开放首次探测放行")
                return (
                    False,
                    None,
                    f"熔断器 OPEN。剩余 {int(remain) + 1} 秒冷却",
                )
            if s.state == HALF_OPEN:
                if s.half_open_inflight >= 1:
                    return (
                        False,
                        None,
                        "半开放仅放行 1 个探测请求，其余请求拒绝（避免雪崩）",
                    )
                s.half_open_inflight += 1
                return (True, HALF_OPEN, "半开放探测放行")
            # CLOSED 直接放行
            return (True, CLOSED, None)

    def on_success(self, circuit_key: str, run_id: str | None = None) -> None:
        """记一次成功：CLOSED 清零失败；Half-Open 探测成功则回到 CLOSED。

        Args:
            circuit_key: 隔离键。
            run_id: 可选，写入插件事件。
        """
        with self._lock:
            s = self._get(circuit_key)
            now = time.time()
            if s.state == HALF_OPEN:
                s.state = CLOSED
                s.failure_count = 0
                s.half_open_inflight = 0
                s.last_state_change_ts = now
                self._safe_emit(
                    "circuit_closed",
                    run_id=run_id,
                    circuit_key=circuit_key,
                    probe_success=True,
                )
            elif s.state == CLOSED:
                # CLOSED 成功直接清零，不 emit（避免高频）
                s.failure_count = 0
            # OPEN 状态出现 on_success = 异常路径（理论上 Open 不会放行），按 CLOSED 处理
            else:
                s.state = CLOSED
                s.failure_count = 0
                s.last_state_change_ts = now

    def on_failure(self, circuit_key: str, run_id: str | None = None) -> None:
        """记一次失败：计数达阈值则 Open；Half-Open 探测失败则重新 Open。

        Args:
            circuit_key: 隔离键。
            run_id: 可选。
        """
        with self._lock:
            s = self._get(circuit_key)
            now = time.time()
            if s.state == HALF_OPEN:
                # 半开放探测失败 → 直接 Open，重计冷却
                s.state = OPEN
                s.open_since_ts = now
                s.half_open_inflight = 0
                s.last_state_change_ts = now
                self._safe_emit(
                    "circuit_open",
                    run_id=run_id,
                    circuit_key=circuit_key,
                    failure_count=s.failure_count,
                    open_window_seconds=self.open_window_seconds,
                )
                return
            if s.state == CLOSED:
                s.failure_count += 1
                if s.failure_count >= self.failure_threshold:
                    s.state = OPEN
                    s.open_since_ts = now
                    s.last_state_change_ts = now
                    self._safe_emit(
                        "circuit_open",
                        run_id=run_id,
                        circuit_key=circuit_key,
                        failure_count=s.failure_count,
                        open_window_seconds=self.open_window_seconds,
                    )
            # OPEN 状态继续失败不计（等待冷却）

    def append_retry_log(self, entry: dict) -> None:
        """追加一条重试日志（ring buffer，最多保留 200 条）。"""
        with self._lock:
            self._retry_logs.append(entry)
            if len(self._retry_logs) > _RETRY_LOGS_CAPACITY:
                # 切尾（比 deque 省 import）
                self._retry_logs = self._retry_logs[-_RETRY_LOGS_CAPACITY:]

    # ------------------------------------------------------------------
    # Dashboard snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, list[dict]]:
        """给 Dashboard 的熔断器 / 重试日志 / 告警快照。"""
        with self._lock:
            now = time.time()
            circuit_rows: list[dict] = []
            alerts: list[dict] = []
            for key, s in self._states.items():
                remain = 0
                if s.state == OPEN:
                    remain = max(
                        0,
                        int(self.open_window_seconds - (now - s.open_since_ts)),
                    )
                circuit_rows.append({
                    "key": key,
                    "state": s.state,
                    "failure_count": s.failure_count,
                    "open_remain_secs": remain,
                    "last_state_change_ts": s.last_state_change_ts,
                })
                # 告警 1：Open 状态超过阈值
                if s.state == OPEN and s.open_since_ts > 0:
                    open_duration = now - s.open_since_ts
                    if open_duration >= _ALERT_OPEN_THRESHOLD_SECONDS:
                        alerts.append({
                            "level": "critical",
                            "message": (
                                f"熔断器 {key} OPEN 状态已持续 "
                                f"{int(open_duration)}s，超过 "
                                f"{_ALERT_OPEN_THRESHOLD_SECONDS}s 未恢复"
                            ),
                            "since_ts": s.open_since_ts,
                        })
                # 告警 2：连续失败计数 >= threshold-1 且仍 CLOSED（接近熔断预警）
                if (
                    s.state == CLOSED
                    and s.failure_count >= max(1, self.failure_threshold - 1)
                ):
                    alerts.append({
                        "level": "warning",
                        "message": (
                            f"熔断器 {key} 连续失败 {s.failure_count} 次，"
                            f"距离熔断阈值 {self.failure_threshold} 还差 "
                            f"{self.failure_threshold - s.failure_count} 次"
                        ),
                        "since_ts": now,
                    })
            retry_logs_copy = list(self._retry_logs)
        return {
            "circuit_breakers": circuit_rows,
            "retry_logs": retry_logs_copy,
            "alerts": alerts,
        }

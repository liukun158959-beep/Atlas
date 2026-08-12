"""FileLoggerPlugin：将所有生命周期事件结构化落盘。

订阅全部 10 个钩子，每条事件写成一行 JSON。
目的：为 Dashboard 展示、离线分析、Saga 补偿排查提供统一的时间顺序数据源。

遵循 DR-5：输出格式完全结构化（事件名 + 关键字参数 → JSON 行），
绝不拼字符串，绝不打印到 stdout。
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from atlas.core.plugin import BasePlugin

log = logging.getLogger("atlas.plugins.logger")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _safe_serialize(obj: Any) -> Any:
    """把 JSON 不可序列化的对象转成可存储的预览字符串。"""
    if isinstance(obj, (dict, list, tuple, str, int, float, bool, type(None))):
        return obj
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    # 其他对象：用 repr 截断，保证不抛异常也不泄露过多数据
    s = repr(obj)
    return f"<{type(obj).__name__} {s[:200]}...>" if len(s) > 220 else f"<{type(obj).__name__} {s}>"


@dataclass
class FileLoggerPlugin(BasePlugin):
    """结构化落盘插件。

    每条事件写成标准 JSONL 格式（一行一个 JSON）：
    {
      "ts": "2026-08-11T18:30:00.123Z",
      "event": "llm_after_call",
      "plugin": "logger",
      "run_id": "run_xxx",
      "payload": { "model": "qwen-plus", "input_tokens": 100, ... }
    }

    Parameters
    ----------
    log_dir:
        日志存放目录，默认 ./runs/logs
    filename_pattern:
        文件名模板，支持 {date} 占位符（按天切分）
    include_payload:
        是否记录完整 payload（调试时开，生产上可能只记录事件名+run_id）
    """

    name: str = "logger"
    priority: int = 50  # 比较靠前：先写盘，再交给后面插件处理

    log_dir: str | Path = "./runs/logs"
    filename_pattern: str = "atlas-{date}.jsonl"
    include_payload: bool = True

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _today: str | None = field(default=None, repr=False)
    _fp: Any = field(default=None, repr=False)

    # ---- 内部 ------------------------------------------------------------

    def _ensure_file(self) -> None:
        """按天打开/切换日志文件。懒加载，不是 __init__ 就打开。"""
        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        if self._today == today and self._fp is not None:
            return
        if self._fp is not None:
            try:
                self._fp.flush()
                self._fp.close()
            except Exception:
                pass

        log_dir = Path(self.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        filename = self.filename_pattern.format(date=today)
        path = log_dir / filename
        self._fp = open(path, "a", encoding="utf-8")  # noqa: SIM115
        self._today = today
        log.info("file_logger.opened", path=str(path))

    def _write(self, event: str, payload: dict[str, Any]) -> None:
        """线程安全地写一行 JSONL。"""
        row: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "event": event,
            "plugin": self.name,
        }
        if self.include_payload:
            row["payload"] = {k: _safe_serialize(v) for k, v in payload.items()}
        else:
            # 最少保留 run_id，便于关联
            row["payload"] = {"run_id": payload.get("run_id")}

        with self._lock:
            try:
                self._ensure_file()
                self._fp.write(json.dumps(row, ensure_ascii=False) + "\n")
                self._fp.flush()
            except Exception as exc:  # noqa: BLE001
                # 写日志失败不能影响主流程
                log.error(
                    "file_logger.write_failed",
                    event=event,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )

    def close(self) -> None:
        """显式关闭文件描述符（进程退出前可调用，不强制）。"""
        with self._lock:
            if self._fp is not None:
                try:
                    self._fp.flush()
                    self._fp.close()
                finally:
                    self._fp = None
                    self._today = None

    # ---- Agent 生命周期 --------------------------------------------------

    def on_agent_start(self, *, run_id: str, task: str, context: dict[str, Any]) -> None:
        self._write("agent_start", {"run_id": run_id, "task": task, "context": context})

    def on_agent_finish(
        self,
        *,
        run_id: str,
        final_result: str,
        status: str,
        context: dict[str, Any],
    ) -> None:
        self._write("agent_finish", {
            "run_id": run_id, "final_result": final_result,
            "status": status, "context": context,
        })

    # ---- Turn 生命周期 ---------------------------------------------------

    def on_turn_start(self, *, run_id: str, turn_idx: int, context: dict[str, Any]) -> None:
        self._write("turn_start", {"run_id": run_id, "turn_idx": turn_idx, "context": context})

    def on_turn_finish(
        self,
        *,
        run_id: str,
        turn_idx: int,
        decisions: list[Any],
        context: dict[str, Any],
    ) -> None:
        self._write("turn_finish", {
            "run_id": run_id, "turn_idx": turn_idx,
            "decisions_count": len(decisions), "context": context,
        })

    # ---- LLM 调用 -------------------------------------------------------

    def on_llm_before_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        model: str,
        messages: list[dict[str, Any]],
        kwargs: dict[str, Any],
    ) -> None:
        # messages 可能很长，这里只记数量 + 角色序列，避免日志爆炸
        role_seq = [m.get("role", "?") for m in messages]
        self._write("llm_before_call", {
            "run_id": run_id, "turn_idx": turn_idx,
            "model": model, "message_count": len(messages), "roles": role_seq,
            "kwargs_keys": list(kwargs.keys()),
        })

    def on_llm_after_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        model: str,
        response: dict[str, Any],
        usage: dict[str, int] | None,
        duration_ms: int,
    ) -> None:
        self._write("llm_after_call", {
            "run_id": run_id, "turn_idx": turn_idx,
            "model": model, "usage": usage, "duration_ms": duration_ms,
            "finish_reason": response.get("finish_reason") if isinstance(response, dict) else None,
        })

    # ---- 工具调用 -------------------------------------------------------

    def on_tool_before_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        input: dict[str, Any],
    ) -> None:
        self._write("tool_before_call", {
            "run_id": run_id, "turn_idx": turn_idx,
            "step_id": step_id, "tool_name": tool_name, "input": input,
        })

    def on_tool_after_call(
        self,
        *,
        run_id: str,
        turn_idx: int,
        step_id: str,
        tool_name: str,
        input: dict[str, Any],
        result: dict[str, Any],
        duration_ms: int,
        status: str,
    ) -> None:
        self._write("tool_after_call", {
            "run_id": run_id, "turn_idx": turn_idx,
            "step_id": step_id, "tool_name": tool_name,
            "duration_ms": duration_ms, "status": status,
        })

    # ---- Saga 补偿 ------------------------------------------------------

    def on_compensate_start(
        self,
        *,
        run_id: str,
        failed_step_id: str,
        compensate_count: int,
    ) -> None:
        self._write("compensate_start", {
            "run_id": run_id,
            "failed_step_id": failed_step_id,
            "compensate_count": compensate_count,
        })

    def on_compensate_failed(
        self,
        *,
        run_id: str,
        step_id: str,
        tool_name: str,
        error: str,
        retry_count: int,
        next_retry_at: str | None,
    ) -> None:
        self._write("compensate_failed", {
            "run_id": run_id, "step_id": step_id, "tool_name": tool_name,
            "error": error, "retry_count": retry_count,
            "next_retry_at": next_retry_at,
        })

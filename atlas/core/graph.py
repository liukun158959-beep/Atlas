"""工作流原语：Edge 控制流边 + Parallel 并行汇聚（Phase 3.1）。

设计（grill-me 28 项冻结）：
  Q2=A 解释器模式：GraphInterpreter 运行时遍历，不预编译
  Q3=B 节点是 Callable[[dict], dict]：Agent 和普通函数都支持
  Q4=A 统一 dict in/out + merge 语义（和 LangGraph 对齐）
  Q11=A 两类边：Edge 普通边 + ConditionalEdge 条件边
  Q13=B ParallelNode 单节点内嵌 fan_out + and_ 汇聚
  Q17=A 特殊节点名 END 表示终止
  Q26=A 复用现有 on_turn_start/on_turn_finish 钩子（不新增 Graph 专属钩子）

和 Agent 的关系（Q8=C）：Graph 和 Agent 并列，不改造现有 Agent.run；
  react_graph() 桥接函数验证"Agent 循环能用 Graph 语法表达"。
"""
from __future__ import annotations

import copy
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Callable

from ..context.trace import Trace
from ..journal.base import JournalEvent, JournalProtocol
from ..render import PlainRenderer, Renderer
from .circuit_breaker import CircuitBreakerRegistry
from .plugin import PluginManager
from .suspend import SuspendToken
from .terminator import Terminator

if TYPE_CHECKING:
    from .agent import Agent


# =============================================================
# 常量（Q17=A：特殊节点名 END）
# =============================================================
END = "END"


# =============================================================
# 两类边（Q11=A）
# =============================================================
@dataclass
class Edge:
    """普通边：from_node -> to_node，无条件跳转。"""

    from_node: str
    to_node: str


@dataclass
class ConditionalEdge:
    """条件边：from_node -> condition(state) 返回 key -> mapping[key] 决定 to_node。

    condition 返回的 key 必须在 mapping 里，mapping 的值是目标节点名或 END。
    """

    from_node: str
    condition: Callable[[dict], str]  # 返回 mapping 的 key
    mapping: dict[str, str]  # key -> to_node（值可以是 END）


# =============================================================
# Trace 子结构（Q23=A）
# =============================================================
@dataclass
class NodeExecution:
    """单个节点执行记录。"""

    node_name: str
    input_state: dict  # 执行前的 state 快照（深拷贝）
    output_state: dict  # 节点返回的 dict（merge 前的原始返回）
    duration_ms: int
    error: str | None = None  # 非 None 表示该节点执行抛异常


@dataclass
class EdgeRouting:
    """单次边路由记录。"""

    from_node: str
    condition_key: str | None  # None=普通边，非 None=条件边返回的 key
    to_node: str


@dataclass
class GraphTrace:
    """Graph 全程扁平可观测存档（Q23=A）。"""

    graph_name: str = ""
    node_executions: list[NodeExecution] = field(default_factory=list)
    edge_routings: list[EdgeRouting] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0
    total_duration_ms: int = 0
    iterations: int = 0  # 实际执行的节点数（含重复节点）
    terminated_by: str = ""  # "END" / "max_iterations" / "terminator" / "error"


@dataclass
class GraphResult:
    """Graph.run() / resume() 的返回值（Q9=A）。"""

    final_state: dict
    trace: GraphTrace
    # ── Phase 3.2 新增：status/suspend_prompt/suspend_token（老代码忽略行为不变） ──
    status: str = "completed"  # "completed" / "suspended"
    suspend_prompt: str | None = None  # status=suspended 时的提示文本
    suspend_token: SuspendToken | None = None  # status=suspended 时的恢复 token


# =============================================================
# parallel_run 辅助（Q14=B）
# =============================================================
def parallel_run(fns: list[Callable[[dict], dict]], state: dict) -> list[dict]:
    """并行执行多个节点函数，fail-fast 错误传播。

    每个 fn 收到同一份 state 的深拷贝（防互相污染）。
    返回结果列表，顺序与 fns 一致（不是完成顺序）。
    任一 fn 抛异常 -> 取消所有未完成的 + raise（不静默吞错，DR-8）。
    """
    results: list[dict | None] = [None] * len(fns)
    with ThreadPoolExecutor(max_workers=len(fns)) as pool:
        future_to_idx = {
            pool.submit(fn, copy.deepcopy(state)): idx
            for idx, fn in enumerate(fns)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            # result() 会抛出 fn 内的异常（fail-fast）
            results[idx] = future.result()
    # fail-fast 模式下 results 不会有 None，这里只是类型安全收尾
    return [r for r in results if r is not None]


# =============================================================
# Graph 类（Q21=A：8 项构造函数）
# =============================================================
@dataclass
class Graph:
    """工作流图（解释器模式，Q2=A）。

    构造函数字段：0 必填 + 8 可选 = 8 项（<= DR-2 15 限额）。
    """

    # ── 可选（8 项） ──
    name: str | None = None
    max_iterations: int = 50  # Q6=C 兜底终止
    renderer: Renderer | None = None  # None 时用 PlainRenderer
    journal: JournalProtocol | None = None  # Q15=B 不强依赖，允许注入做 trace 关联
    plugin_manager: PluginManager | None = None  # Q26=A 复用现有钩子
    terminator: Terminator | None = None  # 全局终止条件（和 Agent 对齐）
    circuit_breaker_registry: CircuitBreakerRegistry | None = None  # 熔断器共享
    raise_on_error: bool = True  # True=fail-fast raise（Q14=B），False=记录到 trace 继续

    # ── 内部状态（post_init 初始化，不进构造函数签名） ──
    _nodes: dict[str, Callable[[dict], dict]] = field(default_factory=dict, init=False, repr=False)
    _edges: list[Edge | ConditionalEdge] = field(default_factory=list, init=False, repr=False)
    _entry_point: str | None = field(default=None, init=False, repr=False)
    _exit_points: set[str] = field(default_factory=set, init=False, repr=False)
    _renderer: Renderer = field(default_factory=PlainRenderer, init=False, repr=False)

    def __post_init__(self) -> None:
        self._renderer = self.renderer or PlainRenderer()
        # plugin_manager None 时用空 PluginManager（emit 遍历空列表无副作用，和 Agent 一致）
        if self.plugin_manager is None:
            self.plugin_manager = PluginManager()

    # =============================================================
    # 命令式构造 API（Q9=A，和 LangGraph 对齐）
    # =============================================================
    def add_node(self, name: str, fn: Callable[[dict], dict]) -> None:
        """注册节点。name 重名 ValueError（不静默覆盖，DR-0 红线）。"""
        if name in self._nodes:
            raise ValueError(f"节点 {name} 已存在，不可重复注册")
        if name == END:
            raise ValueError(f"节点名 {END} 是保留字，不可用作节点名")
        self._nodes[name] = fn

    def add_edge(self, from_node: str, to_node: str) -> None:
        """加普通边。add 时不验证节点存在性（Q22=A lazy 验证留到 run）。"""
        self._edges.append(Edge(from_node=from_node, to_node=to_node))

    def add_conditional_edges(
        self,
        from_node: str,
        condition: Callable[[dict], str],
        mapping: dict[str, str],
    ) -> None:
        """加条件边。condition 返回的 key 必须在 mapping 里，mapping 值是目标节点名或 END。"""
        self._edges.append(ConditionalEdge(
            from_node=from_node,
            condition=condition,
            mapping=mapping,
        ))

    def set_entry_point(self, name: str) -> None:
        """设置入口节点。"""
        self._entry_point = name

    def set_exit_point(self, name: str) -> None:
        """标记退出节点（到该节点时停止）。多个 exit_point 用 set 保存。"""
        self._exit_points.add(name)

    # =============================================================
    # 图验证（Q22=A：run 入口 lazy 验证）
    # =============================================================
    def _validate(self) -> None:
        """run() 入口集中验证图结构。失败 raise ValueError。"""
        if self._entry_point is None:
            raise ValueError("Graph 缺少 entry_point，请调 set_entry_point()")
        if self._entry_point not in self._nodes:
            raise ValueError(f"entry_point '{self._entry_point}' 未在 nodes 中注册")
        for edge in self._edges:
            if edge.from_node not in self._nodes:
                raise ValueError(f"边 from_node '{edge.from_node}' 未在 nodes 中注册")
            if isinstance(edge, Edge):
                if edge.to_node != END and edge.to_node not in self._nodes:
                    raise ValueError(f"边 to_node '{edge.to_node}' 未在 nodes 中注册且非 END")
            elif isinstance(edge, ConditionalEdge):
                for key, target in edge.mapping.items():
                    if target != END and target not in self._nodes:
                        raise ValueError(f"条件边 mapping[{key}]='{target}' 未在 nodes 中注册且非 END")
        # 环检测：允许环（循环边），但必须有 END 可达或显式 exit_point
        # 简化检查：至少有一条边的 to_node 是 END 或条件边 mapping 含 END，或有 exit_points
        has_end_reachable = any(
            (isinstance(e, Edge) and e.to_node == END)
            or (isinstance(e, ConditionalEdge) and END in e.mapping.values())
            for e in self._edges
        )
        if not has_end_reachable and not self._exit_points:
            raise ValueError("Graph 无 END 可达路径且无 exit_point，会死循环")

    # =============================================================
    # 主循环（Q2=A 解释器模式）
    # =============================================================
    def run(
        self,
        initial_state: dict | None = None,
        *,
        suspend_timeout: float | None = None,
    ) -> GraphResult:
        """运行图（Phase 3.2 新增 suspend_timeout 预留参数，当前不阻塞线程）。

        流程：
          1. _validate() 验证图结构
          2. 从 entry_point 开始
          3. 循环：执行节点 → 记录 Trace + 可选 Journal 事件 → 判断出边 → 路由
          4. 终止：到 END / max_iterations / terminator / error / SuspendNode 挂起
        """
        self._validate()
        state: dict = dict(initial_state or {})
        _start = time.time()
        run_id = f"graph_{int(_start * 1000)}"
        trace = GraphTrace(graph_name=self.name or "unnamed", start_ts=_start)
        return self._run_internal(
            state=state,
            start_node=self._entry_point or "",
            run_id=run_id,
            trace=trace,
            term_start_ts=_start,
        )

    def resume(self, token: SuspendToken, user_input: dict) -> GraphResult:
        """从挂起点恢复执行（Q7=A + Q14=A）。

        流程：
          1. 校验 user_input 是 dict（Q13=(2)B merge 语义）
          2. 从 journal.get_events(token.run_id) 找 seq=token.seq 且 event_type=graph_suspend 的事件
          3. 取事件 payload 的完整 state_snapshot（Q12=A + Q8=C 挂起点存完整快照）
          4. state.update(user_input) 注入外部输入
          5. append graph_resume 事件（审计链路 Q11=A）
          6. emit on_resume 钩子（Q15=B）
          7. 从 token.node_name 的下一条边开始路由（挂起节点不重跑 Q14=A）
        """
        if not isinstance(user_input, dict):
            raise ValueError(
                f"user_input 必须是 dict（Q13=(2)B merge 语义），实际 {type(user_input)}"
            )
        if self.journal is None:
            raise ValueError("resume() 需要 Graph 注入 journal 实例（Q8=C durable 恢复依赖 Journal）")

        events = self.journal.get_events(token.run_id)
        suspend_event: JournalEvent | None = None
        for e in events:
            if e.seq == token.seq and e.event_type == "graph_suspend":
                suspend_event = e
                break
        if suspend_event is None:
            raise ValueError(
                f"token 对应的 graph_suspend 事件不存在（run_id={token.run_id}, seq={token.seq}）。"
                f" 请确认 Graph 使用了同一个 Journal 实例，且 run_id/seq 未被清理。"
            )
        if suspend_event.payload.get("node_name") != token.node_name:
            raise ValueError(
                f"token.node_name={token.node_name} 与 graph_suspend 事件 payload.node_name="
                f"{suspend_event.payload.get('node_name')!r} 不匹配"
            )

        # Q12=A：从 graph_suspend 事件 payload 取完整 state 快照
        state: dict = copy.deepcopy(suspend_event.payload["state_snapshot"])
        # Q13=(2)B：merge user_input 到 state
        state.update(user_input)

        _start = time.time()
        # 重建 trace（延续此前的 node_executions 记录可从 events 重建，这里简化：新建空 trace 只记 resume 后的执行）
        # 若要完整 trace 合并，可遍历 events 把 graph_node_exec 转成 NodeExecution，此处留简化版
        trace = GraphTrace(graph_name=self.name or "unnamed", start_ts=_start)

        # append graph_resume 事件（审计链路）
        resume_seq = self._journal_next_seq(token.run_id)
        self.journal.append(JournalEvent(
            run_id=token.run_id,
            seq=resume_seq,
            ts=self._journal_now_iso(),
            event_type="graph_resume",
            payload={
                "node_name": token.node_name,
                "user_input": copy.deepcopy(user_input),
                "resumed_from_seq": token.seq,
            },
        ))

        # emit on_resume（Q15=B 新增钩子）
        self.plugin_manager.emit(
            "resume",
            run_id=token.run_id,
            node_name=token.node_name,
            user_input=copy.deepcopy(user_input),
            resumed_from_seq=token.seq,
        )

        # 从 token.node_name 的下一条边开始（Q14=A 不重跑挂起节点）
        return self._run_internal(
            state=state,
            start_node=None,  # None = 走"先路由 token.node_name 出边再执行下一节点"模式
            run_id=token.run_id,
            trace=trace,
            term_start_ts=_start,
            resume_from_node=token.node_name,
        )

    # =============================================================
    # 内部：run / resume 共享的主循环
    # =============================================================
    def _run_internal(
        self,
        *,
        state: dict,
        start_node: str | None,
        run_id: str,
        trace: GraphTrace,
        term_start_ts: float,
        resume_from_node: str | None = None,
    ) -> GraphResult:
        """共享主循环。

        - 首次 run：start_node=entry_point，resume_from_node=None → 先执行 start_node
        - resume：start_node=None，resume_from_node=挂起节点名 → 先路由挂起节点出边再执行下一节点
        """
        # 构建出边索引
        out_edges: dict[str, list[Edge | ConditionalEdge]] = {}
        for edge in self._edges:
            out_edges.setdefault(edge.from_node, []).append(edge)

        term_ctx = SimpleNamespace(
            step_count=0,
            start_time=term_start_ts * 1000,
            total_tokens=0,
            cost_usd=0.0,
            recent_outputs=[],
        ) if self.terminator is not None else None

        # Phase 3.2：SuspendNode 挂起时填充
        suspended_result: GraphResult | None = None

        current_node: str | None
        iterations = trace.iterations
        terminated_by = ""

        if resume_from_node is not None:
            # resume 流程：先路由 resume_from_node 的出边，得到 current_node
            # （resume 不重跑挂起节点 Q14=A）
            edges_current = out_edges.get(resume_from_node, [])
            if not edges_current:
                if resume_from_node in self._exit_points:
                    terminated_by = "END"
                else:
                    raise ValueError(f"resume_from_node {resume_from_node} 无出边且非 exit_point")
                current_node = END
            else:
                edge = edges_current[0]
                if isinstance(edge, Edge):
                    current_node = edge.to_node
                    trace.edge_routings.append(EdgeRouting(
                        from_node=resume_from_node,
                        condition_key=None,
                        to_node=current_node,
                    ))
                else:  # ConditionalEdge
                    condition_key = edge.condition(state)
                    if condition_key not in edge.mapping:
                        raise ValueError(
                            f"resume 条件边 condition 返回 '{condition_key}' 不在 mapping {list(edge.mapping.keys())}"
                        )
                    current_node = edge.mapping[condition_key]
                    trace.edge_routings.append(EdgeRouting(
                        from_node=resume_from_node,
                        condition_key=condition_key,
                        to_node=current_node,
                    ))
        else:
            # run 流程：从 start_node 执行
            current_node = start_node or ""
            if not current_node:
                raise ValueError("_run_internal 首次 run 必须传 start_node")

        try:
            while current_node != END and iterations < self.max_iterations:
                # terminator 兜底
                if self.terminator is not None and term_ctx is not None:
                    should_stop, reasons = self.terminator.should_stop(term_ctx)
                    if should_stop:
                        terminated_by = "terminator"
                        break

                iterations += 1
                if term_ctx is not None:
                    term_ctx.step_count = iterations
                assert current_node is not None
                fn = self._nodes[current_node]

                # 复用 on_turn_start（Q26=A）
                self.plugin_manager.emit(
                    "turn_start",
                    run_id=run_id,
                    turn_idx=iterations,
                    context={"node_name": current_node, "graph_name": trace.graph_name},
                )

                t0 = time.time()
                input_snapshot = copy.deepcopy(state)
                error_msg: str | None = None
                output_state: dict = {}
                try:
                    output_state = fn(state) or {}
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {e}"
                    if self.raise_on_error:
                        raise
                    output_state = {"__error__": error_msg}
                duration_ms = int((time.time() - t0) * 1000)

                # merge 到 state（Q4=A）
                state.update(output_state)

                # 记录 Trace NodeExecution
                node_exec = NodeExecution(
                    node_name=current_node,
                    input_state=input_snapshot,
                    output_state=output_state,
                    duration_ms=duration_ms,
                    error=error_msg,
                )
                trace.node_executions.append(node_exec)

                # Phase 3.2：graph_node_exec Journal 事件（journal 非 None 时才 append）
                if self.journal is not None:
                    seq = self._journal_next_seq(run_id)
                    self.journal.append(JournalEvent(
                        run_id=run_id,
                        seq=seq,
                        ts=self._journal_now_iso(),
                        event_type="graph_node_exec",
                        payload={
                            "node_name": current_node,
                            "input_state": input_snapshot,
                            "output_state": copy.deepcopy(output_state),
                            "duration_ms": duration_ms,
                            "error": error_msg,
                        },
                    ))

                # Phase 3.2：检测 SuspendNode 返回的 __suspend__ 标记（Q5=B）
                if "__suspend__" in output_state:
                    prompt = output_state["__suspend__"]
                    # SuspendNode 必须配 Journal（消除 fallback，不允许 durable=false 误挂起）
                    if self.journal is None:
                        raise ValueError(
                            "SuspendNode 需要注入 Journal 才能保证 durable。"
                            "请在 Graph 构造函数传 journal=InMemoryJournal(save_full_payload=True)"
                        )
                    # Q8=C：graph_suspend 事件存完整 state 快照（=挂起时完整 state）
                    state_snapshot = copy.deepcopy(state)
                    suspend_seq = self._journal_next_seq(run_id)
                    self.journal.append(JournalEvent(
                        run_id=run_id,
                        seq=suspend_seq,
                        ts=self._journal_now_iso(),
                        event_type="graph_suspend",
                        payload={
                            "node_name": current_node,
                            "state_snapshot": state_snapshot,
                            "prompt": prompt,
                        },
                    ))

                    # Q6=A 构造 SuspendToken
                    token = SuspendToken(run_id=run_id, node_name=current_node, seq=suspend_seq)

                    # Q15=B emit on_suspend 钩子
                    self.plugin_manager.emit(
                        "suspend",
                        run_id=run_id,
                        node_name=current_node,
                        prompt=prompt,
                        token={"run_id": token.run_id, "node_name": token.node_name, "seq": token.seq},
                    )

                    # emit on_turn_finish（挂起也是一种 turn 结束）
                    self.plugin_manager.emit(
                        "turn_finish",
                        run_id=run_id,
                        turn_idx=iterations,
                        decisions=[],
                        context={"node_name": current_node, "duration_ms": duration_ms, "suspended": True},
                    )

                    # 构造挂起态 GraphResult（Q9=A）
                    trace.end_ts = time.time()
                    trace.total_duration_ms = int((trace.end_ts - (trace.start_ts or term_start_ts)) * 1000)
                    trace.iterations = iterations
                    trace.terminated_by = "suspended"
                    suspended_result = GraphResult(
                        final_state=state,
                        trace=trace,
                        status="suspended",
                        suspend_prompt=prompt,
                        suspend_token=token,
                    )
                    break  # 跳出循环，挂起

                if term_ctx is not None and isinstance(output_state, dict):
                    term_ctx.recent_outputs.append(str(output_state)[:200])

                # on_turn_finish（非挂起路径）
                self.plugin_manager.emit(
                    "turn_finish",
                    run_id=run_id,
                    turn_idx=iterations,
                    decisions=[],
                    context={"node_name": current_node, "duration_ms": duration_ms},
                )

                # 边路由（正常节点）
                edges_from_current = out_edges.get(current_node, [])
                if not edges_from_current:
                    if current_node in self._exit_points:
                        terminated_by = "END"
                        break
                    raise ValueError(f"节点 {current_node} 无出边且非 exit_point")

                edge = edges_from_current[0]
                if isinstance(edge, Edge):
                    next_node = edge.to_node
                    trace.edge_routings.append(EdgeRouting(
                        from_node=current_node,
                        condition_key=None,
                        to_node=next_node,
                    ))
                else:
                    condition_key = edge.condition(state)
                    if condition_key not in edge.mapping:
                        raise ValueError(
                            f"条件边 condition 返回 '{condition_key}' 不在 mapping {list(edge.mapping.keys())} 中"
                        )
                    next_node = edge.mapping[condition_key]
                    trace.edge_routings.append(EdgeRouting(
                        from_node=current_node,
                        condition_key=condition_key,
                        to_node=next_node,
                    ))

                current_node = next_node

            if suspended_result is not None:
                return suspended_result

            if not terminated_by:
                if current_node == END:
                    terminated_by = "END"
                elif iterations >= self.max_iterations:
                    terminated_by = "max_iterations"
                else:
                    terminated_by = "unknown"

        except Exception:
            terminated_by = "error"
            trace.end_ts = time.time()
            trace.total_duration_ms = int((trace.end_ts - (trace.start_ts or term_start_ts)) * 1000)
            trace.iterations = iterations
            trace.terminated_by = terminated_by
            raise

        trace.end_ts = time.time()
        trace.total_duration_ms = int((trace.end_ts - (trace.start_ts or term_start_ts)) * 1000)
        trace.iterations = iterations
        trace.terminated_by = terminated_by
        return GraphResult(final_state=state, trace=trace, status="completed")

    # =============================================================
    # 内部：Journal 辅助（对齐 atlas.journal.base 的 make_event 风格）
    # =============================================================
    def _journal_next_seq(self, run_id: str) -> int:
        """通过 journal 动态拿 next_seq。

        InMemoryJournal 有 next_seq() 方法；若 journal 是其他实现，
        退化为 get_events 取最后一条 seq+1。
        """
        j = self.journal
        assert j is not None
        if hasattr(j, "next_seq") and callable(getattr(j, "next_seq")):
            return j.next_seq(run_id)  # type: ignore[attr-defined]
        events = j.get_events(run_id)
        return (events[-1].seq + 1) if events else 1

    def _journal_now_iso(self) -> str:
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()


# =============================================================
# ParallelNode（Q13=B 单节点内嵌 fan_out + and_ 汇聚）
# =============================================================
class ParallelNode:
    """并行节点（Q13=B 单节点内嵌 fan_out + and_ 汇聚）。

    用法：
        g.add_node('parallel_1', ParallelNode([fn_a, fn_b, fn_c]))
        # 执行时：3 个 fn 并行跑，结果合并成 list 放到 state['parallel_results']

    语义（Q27=A）：
        - 每个 fn 收到同一份 state 的深拷贝（防互相污染）
        - 结果按 fns 顺序合并成 list（不是完成顺序）
        - fail-fast：任一 fn 抛异常 -> 全部取消 + raise（Q14=B）
    """

    def __init__(self, fns: list[Callable[[dict], dict]]) -> None:
        if not fns:
            raise ValueError("ParallelNode 至少需要 1 个 fn")
        self.fns = fns

    def __call__(self, state: dict) -> dict:
        """节点签名 Callable[[dict], dict]，和普通函数节点对齐。"""
        results = parallel_run(self.fns, state)
        return {"parallel_results": results}


# =============================================================
# react_graph 桥接函数（Q8=C）
# =============================================================
def react_graph(agent: "Agent", max_iterations: int = 10) -> Graph:
    """把 Agent 包装成单节点 Graph（Q8=C 桥接函数）。

    验证"Agent 循环能用 Graph 语法表达"设计假设。
    最小形态：单节点 Graph（agent.run(state['input']) -> {'answer': ...}），无循环。

    用户要"自我修正循环"可以用 add_conditional_edges 自己写。
    """
    def agent_node(state: dict) -> dict:
        user_input = state.get("input", "")
        answer = agent.run(user_input)
        return {"answer": answer}

    g = Graph(name=f"react_{getattr(agent, 'max_steps', 'agent')}", max_iterations=max_iterations)
    g.add_node("agent", agent_node)
    g.add_edge("agent", END)
    g.set_entry_point("agent")
    return g

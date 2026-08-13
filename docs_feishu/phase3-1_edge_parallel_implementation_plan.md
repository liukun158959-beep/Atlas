# Phase 3.1 实施计划：工作流原语 Edge + Parallel

> grill-me 3 轮共享理解冻结（28 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 Phase 2.3 / Journal SQLite 成功范式 + 用户规则）：
> 1. 每步独立验证：完成后**立即**有一行 `python -c` 验证，不做"半成品等下一步"
> 2. 严格单向依赖：Step 编号越大依赖越多，绝不反向；graph.py 属于 atlas-core，不 import patterns
> 3. Windows 纯 Python 验证命令（不用 head/curl 类 Unix 工具）
> 4. 不考虑 fallback / 旧兼容，没有老用户：直接新建 graph.py，不改造现有 Agent.run
> 5. 代码迁移先 copy 再改写：parallel_run 从 multi_agent.py 的 parallel_gather copy ThreadPoolExecutor 用法
> 6. 不折中方案：Edge/Parallel 用解释器模式 + Callable 节点，对齐 LangGraph 事实标准

---

## 0. 决策速查表（28 项 grill-me 冻结）

### 0.1 范围与顺序

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | Phase 3.1 做几个原语 | C：Edge + Parallel 一起（Suspend/Resume 后移 Phase 3.2） |
| Q10 | demo 数量 | B：2 个（edge_demo + parallel_edge_demo） |
| Q28 | 断言清单最终确认 | B：edge_demo 5 断言 + parallel_edge_demo 4 断言 |

### 0.2 执行模型

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q2 | Edge 执行模型 | A：解释器模式（GraphInterpreter 运行时遍历，不预编译） |
| Q8 | GraphInterpreter vs Agent 关系 | C：并列 + react_graph() 桥接（不改造现有 Agent.run） |

### 0.3 节点抽象

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q3 | 节点类型 | B：`Callable[[dict], dict]`（Agent 和普通函数都支持） |
| Q4 | 节点签名 + State 传递 | A：统一 dict in/out + merge 语义（和 LangGraph 对齐） |
| Q12 | LLM 决策条件边形态 | A：LLM 决策 = 普通 Python 条件边 + Agent 调用（不专门抽象） |

### 0.4 边

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q5 | 条件边判断位置 | C：Python 函数 + LLM 决策都支持 |
| Q6 | 循环终止控制 | C：节点主动结束 + max_iterations 兜底 |
| Q11 | Edge dataclass | A：两类（Edge 普通边 + ConditionalEdge 条件边） |
| Q17 | 终止语义 | A：特殊节点名 END（和 LangGraph 对齐） |

### 0.5 Parallel

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q7 | 扇出汇聚语义 | A：fan_out + and_ 汇聚（不做 or_ race） |
| Q13 | Parallel API 形状 | B：单个 ParallelNode + 内嵌 fan_out/and_ |
| Q14 | 线程池 + 错误传播 | B：新建 parallel_run，fail-fast raise |
| Q27 | ParallelNode 输入输出 | A：共享 state，输出合并 list（放到 state["parallel_results"]） |

### 0.6 持久化

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q15 | 执行点持久化 | B：只在 Trace 留痕（不单独 emit Journal 事件，为 Phase 3.2 铺路但不提前设计） |

### 0.7 位置与导出

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q16 | 代码位置 | A：`atlas/core/graph.py` 单文件 |
| Q24 | react_graph() 位置 | A：graph.py 内部，不顶层导出 |
| Q25 | atlas 顶层导出 | A：导出 4 项（Graph / GraphResult / GraphTrace / ParallelNode） |

### 0.8 Graph 类

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q21 | 构造函数字段 | A：8 项（0 必填 + 8 可选 ≤ 15 DR-2 限额） |
| Q22 | 图验证时机 | A：run() 入口 lazy 验证（entry/边引用/条件边 mapping/环检测） |
| Q26 | Plugin 钩子集成 | A：复用现有 on_turn_start/on_turn_finish（不新增 Graph 专属钩子） |

### 0.9 Trace

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q19 | Graph.run() 返回值 | B：`GraphResult(final_state, trace: GraphTrace)` |
| Q23 | GraphTrace 字段 | A：含 NodeExecution + EdgeRouting 两个子 dataclass + terminated_by |

### 0.10 Demo

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q20 | FakeLLM 设计 | A+B：edge_demo 2 次 LLM 调用（refine→END）；parallel_edge_demo 纯 Python Worker |
| Q9 | Graph API 形状 | A：命令式 add_node/add_edge（和 LangGraph 对齐） |

---

## 0.5 依赖顺序与整体验证

```
Step 1：新建 atlas/core/graph.py 骨架
    （Edge / ConditionalEdge / NodeExecution / EdgeRouting / GraphTrace / GraphResult
     6 个 dataclass + Graph 类 8 项构造函数 + parallel_run 辅助函数）
    ─── 无依赖（纯新建）
        │
        ▼
Step 2：Graph 类的 add_node / add_edge / add_conditional_edges / set_entry_point / set_exit_point API
    （命令式构造图，add 时不验证，run 时集中验证）
        │
        ▼
Step 3：GraphInterpreter 主循环（Graph.run 方法实现）
    （解释器模式：从 entry_point 开始 → 执行节点 → 判断出边 → 条件边路由 → 直到 END / max_iterations / terminator）
    + Plugin 钩子复用 on_turn_start/on_turn_finish（Q26=A）
    + Trace 记录 NodeExecution + EdgeRouting（Q23=A）
        │
        ▼
Step 4：ParallelNode 类实现
    （内嵌 fan_out + and_ 汇聚，调 parallel_run，fail-fast raise，输出合并到 state["parallel_results"]）
        │
        ▼
Step 5：react_graph() 桥接函数
    （把 Agent 包装成单节点 Graph，验证"Agent 循环能用 Graph 语法表达"设计假设）
        │
        ▼
Step 6：examples/edge_demo.py 新建（5 项断言 FakeLLM 可复现）
        │
        ▼
Step 7：examples/parallel_edge_demo.py 新建（4 项断言纯 Python Worker 可复现）
        │
        ▼
Step 8：atlas/__init__.py 顶层 re-export + README 更新
```

验证层（全部完成后）：
  V1：每步独立 `python -c` 导入验证
  V2：edge_demo --mock（5 断言）+ parallel_edge_demo --mock（4 断言）
  V3：10 个已有 mock demo 回归（tool_abi / journal_replay / terminator / plugin_cost / saga / compact / circuit_retry / parallel_subagent / pwc / scope）

---

## Step 1：graph.py 骨架（6 dataclass + Graph 构造函数 + parallel_run）

**目的**：建立文件骨架，定义所有数据结构，不实现主循环逻辑。

**改动文件**（1 新建）：

| 文件 | 内容分块 | 行数估算 |
|------|---------|---------|
| `atlas/core/graph.py`（**新建**） | 1. 模块文档字符串（Graph 解释器模式 + Callable 节点 + Edge/ConditionalEdge 两类边 + ParallelNode 并行汇聚 + react_graph 桥接）<br>2. 6 个 dataclass：`Edge` / `ConditionalEdge` / `NodeExecution` / `EdgeRouting` / `GraphTrace` / `GraphResult`<br>3. `parallel_run(fns, state) -> list[dict]` 辅助函数（Q14=B）<br>4. `@dataclass class Graph` 8 项构造函数（Q21=A） | +120 行 |

### 1.1 6 个 dataclass + parallel_run 骨架

```python
# atlas/core/graph.py
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

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable

from ..context.trace import Trace
from ..journal.base import JournalProtocol
from ..render import PlainRenderer, Renderer
from .circuit_breaker import CircuitBreakerRegistry
from .plugin import PluginManager
from .terminator import Terminator


# =============================================================
# 常量
# =============================================================
END = "END"  # Q17=A：特殊节点名，GraphInterpreter 遇到则停止


# =============================================================
# 两类边（Q11=A）
# =============================================================
@dataclass
class Edge:
    """普通边：from_node → to_node，无条件跳转。"""

    from_node: str
    to_node: str


@dataclass
class ConditionalEdge:
    """条件边：from_node → condition(state) 返回 key → mapping[key] 决定 to_node。

    condition 返回的 key 必须在 mapping 里，mapping 的值是目标节点名或 END。
    """

    from_node: str
    condition: Callable[[dict], str]  # 返回 mapping 的 key
    mapping: dict[str, str]  # key → to_node（值可以是 END）


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
    """Graph.run() 的返回值（Q19=B）。"""

    final_state: dict
    trace: GraphTrace


# =============================================================
# parallel_run 辅助（Q14=B）
# =============================================================
def parallel_run(fns: list[Callable[[dict], dict]], state: dict) -> list[dict]:
    """并行执行多个节点函数，fail-fast 错误传播。

    每个 fn 收到同一份 state 的浅拷贝（防互相污染）。
    返回结果列表，顺序与 fns 一致（不是完成顺序）。
    任一 fn 抛异常 → 取消所有未完成的 + raise（不静默吞错，DR-8）。
    """
    import copy
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
    return [r for r in results if r is not None]  # 理论上不会 None，fail-fast 已 raise


# =============================================================
# Graph 类（Q21=A：8 项构造函数）
# =============================================================
@dataclass
class Graph:
    """工作流图（解释器模式，Q2=A）。

    构造函数字段：0 必填 + 8 可选 = 8 项（≤ DR-2 15 限额）。
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
            from .plugin import PluginManager
            self.plugin_manager = PluginManager()
```

**验证 Step 1.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.graph import Graph, Edge, ConditionalEdge, NodeExecution, EdgeRouting, GraphTrace, GraphResult, parallel_run, END
print('import OK')
g = Graph(name='test', max_iterations=10)
assert g.max_iterations == 10 and g.name == 'test'
assert g._nodes == {} and g._edges == []
assert g._entry_point is None
print('Graph 构造 8 项可选 OK')
# parallel_run 3 个纯函数并行
def fn1(s): return {'r': 'a'}
def fn2(s): return {'r': 'b'}
def fn3(s): return {'r': 'c'}
results = parallel_run([fn1, fn2, fn3], {'input': 1})
assert [r['r'] for r in results] == ['a', 'b', 'c']  # 顺序保持
print('parallel_run 3 fn 顺序保持 OK')
# parallel_run fail-fast
def bad(s): raise ValueError('boom')
try:
    parallel_run([fn1, bad, fn3], {})
    assert False, '应抛 ValueError'
except ValueError as e:
    assert 'boom' in str(e)
print('parallel_run fail-fast OK')
print('=== Step 1.V 通过 ===')
"
```

**回滚方式**：删 `atlas/core/graph.py`
**风险**：0（纯新建，无现有代码依赖）

---

## Step 2：Graph 类的 add_node / add_edge / add_conditional_edges / set_entry_point / set_exit_point API

**目的**：命令式 API（Q9=A），add 时不验证（Q22=A lazy 验证留到 run 入口）。

**改动文件**（1 修改）：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/graph.py` | 在 Graph 类里追加 5 个方法 | +60 行 |

### 2.1 命令式 API 骨架

```python
# 接 Graph 类 __post_init__ 之后

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
```

**验证 Step 2.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.graph import Graph, Edge, ConditionalEdge, END
g = Graph()
g.add_node('start', lambda s: {'step': 1})
g.add_node('worker', lambda s: {'step': s.get('step', 0) + 1})
g.add_edge('start', 'worker')
g.add_conditional_edges('worker', lambda s: 'end' if s['step'] >= 3 else 'loop', {'end': END, 'loop': 'worker'})
g.set_entry_point('start')
assert g._entry_point == 'start'
assert len(g._nodes) == 2
assert len(g._edges) == 2
assert isinstance(g._edges[0], Edge)
assert isinstance(g._edges[1], ConditionalEdge)
# 重名 ValueError
try:
    g.add_node('start', lambda s: {})
    assert False, '应抛 ValueError'
except ValueError:
    pass
# END 保留字
try:
    g.add_node(END, lambda s: {})
    assert False, '应抛 ValueError'
except ValueError:
    pass
print('=== Step 2.V 通过 ===')
"
```

---

## Step 3：GraphInterpreter 主循环（Graph.run 方法 + 验证 + Plugin + Trace）

**目的**：解释器模式主循环（Q2=A）+ run 入口 lazy 验证（Q22=A）+ Plugin 复用 on_turn 钩子（Q26=A）+ Trace 记录（Q23=A）。

**改动文件**（1 修改）：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/graph.py` | 追加 `_validate()` + `run(initial_state)` 两个方法 | +150 行 |

### 3.1 验证 + 主循环骨架

```python
# 接 Graph 类 set_exit_point 之后

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
        # 环检测：允许环（循环边），但必须有 END 可达（否则死循环）
        # 简化检查：至少有一条边的 to_node 是 END 或条件边 mapping 含 END
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
    def run(self, initial_state: dict | None = None) -> GraphResult:
        """运行图。返回 GraphResult(final_state, trace)。

        流程：
          1. _validate() 验证图结构
          2. 从 entry_point 开始
          3. 循环：执行节点 → 记录 Trace → 判断出边 → 路由到下一节点
          4. 终止条件：到 END / max_iterations / terminator / error
        """
        import copy
        self._validate()
        state: dict = dict(initial_state or {})
        trace = GraphTrace(graph_name=self.name or "unnamed", start_ts=time.time())
        _start = time.time()
        run_id = f"graph_{int(_start * 1000)}"

        current_node = self._entry_point
        iterations = 0
        terminated_by = ""

        # 构建出边索引：from_node → list[Edge | ConditionalEdge]
        out_edges: dict[str, list[Edge | ConditionalEdge]] = {}
        for edge in self._edges:
            out_edges.setdefault(edge.from_node, []).append(edge)

        try:
            while current_node != END and iterations < self.max_iterations:
                # Q6=C：terminator 兜底（和 Agent 对齐）
                if self.terminator is not None:
                    # terminator 接收一个简化 state 做判断
                    if hasattr(self.terminator, 'should_stop'):
                        if self.terminator.should_stop({"iterations": iterations, "state": state}):
                            terminated_by = "terminator"
                            break

                iterations += 1
                fn = self._nodes[current_node]

                # Q26=A：复用 on_turn_start（把节点执行映射为 turn 语义）
                self.plugin_manager.emit(
                    "on_turn_start",
                    run_id=run_id,
                    turn_idx=iterations,
                    payload={"node_name": current_node, "graph_name": trace.graph_name},
                )

                # 执行节点
                t0 = time.time()
                input_snapshot = copy.deepcopy(state)
                error_msg: str | None = None
                output_state: dict = {}
                try:
                    output_state = fn(state) or {}
                except Exception as e:
                    error_msg = f"{type(e).__name__}: {e}"
                    if self.raise_on_error:
                        raise  # fail-fast（Q14=B 对齐）
                    # raise_on_error=False：记录到 trace 继续
                    output_state = {"__error__": error_msg}
                duration_ms = int((time.time() - t0) * 1000)

                # merge output_state 到 state（Q4=A merge 语义）
                state.update(output_state)

                # 记录 NodeExecution（Q23=A）
                trace.node_executions.append(NodeExecution(
                    node_name=current_node,
                    input_state=input_snapshot,
                    output_state=output_state,
                    duration_ms=duration_ms,
                    error=error_msg,
                ))

                # Q26=A：复用 on_turn_finish
                self.plugin_manager.emit(
                    "on_turn_finish",
                    run_id=run_id,
                    turn_idx=iterations,
                    payload={"node_name": current_node, "duration_ms": duration_ms},
                )

                # 判断出边
                edges_from_current = out_edges.get(current_node, [])
                if not edges_from_current:
                    # 无出边：检查是否 exit_point
                    if current_node in self._exit_points:
                        terminated_by = "END"
                        break
                    # 无出边且非 exit_point：报错（不应发生，_validate 已防）
                    raise ValueError(f"节点 {current_node} 无出边且非 exit_point")

                # 取第一条边（简化：每节点只支持一条出边，多出边用条件边表达）
                edge = edges_from_current[0]
                if isinstance(edge, Edge):
                    next_node = edge.to_node
                    trace.edge_routings.append(EdgeRouting(
                        from_node=current_node,
                        condition_key=None,
                        to_node=next_node,
                    ))
                else:  # ConditionalEdge
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

            if not terminated_by:
                if current_node == END:
                    terminated_by = "END"
                elif iterations >= self.max_iterations:
                    terminated_by = "max_iterations"
                else:
                    terminated_by = "unknown"

        except Exception as e:
            terminated_by = "error"
            trace.end_ts = time.time()
            trace.total_duration_ms = int((trace.end_ts - _start) * 1000)
            trace.iterations = iterations
            trace.terminated_by = terminated_by
            raise

        trace.end_ts = time.time()
        trace.total_duration_ms = int((trace.end_ts - _start) * 1000)
        trace.iterations = iterations
        trace.terminated_by = terminated_by
        return GraphResult(final_state=state, trace=trace)
```

**验证 Step 3.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.graph import Graph, END
# 简单线性图：start → worker → END
g = Graph(name='linear')
g.add_node('start', lambda s: {'count': 1})
g.add_node('worker', lambda s: {'count': s['count'] + 1, 'msg': 'done'})
g.add_edge('start', 'worker')
g.add_edge('worker', END)
g.set_entry_point('start')
result = g.run({'init': True})
assert result.final_state['count'] == 2
assert result.final_state['msg'] == 'done'
assert result.final_state['init'] is True  # 初始 state 保留
assert result.trace.terminated_by == 'END'
assert len(result.trace.node_executions) == 2
assert result.trace.node_executions[0].node_name == 'start'
assert result.trace.node_executions[1].node_name == 'worker'
assert len(result.trace.edge_routings) == 2
print('线性图 OK')

# 条件边循环图：worker → (step<3 ? loop : end) → END
g2 = Graph(name='loop')
g2.add_node('worker', lambda s: {'step': s.get('step', 0) + 1})
g2.add_conditional_edges('worker', lambda s: 'end' if s['step'] >= 3 else 'loop', {'end': END, 'loop': 'worker'})
g2.set_entry_point('worker')
r2 = g2.run({})
assert r2.final_state['step'] == 3
assert r2.trace.terminated_by == 'END'
assert r2.trace.iterations == 3
# 条件边路由记录
routings = r2.trace.edge_routings
assert len(routings) == 3
assert routings[0].condition_key == 'loop' and routings[0].to_node == 'worker'
assert routings[1].condition_key == 'loop' and routings[1].to_node == 'worker'
assert routings[2].condition_key == 'end' and routings[2].to_node == END
print('条件边循环图 OK')

# max_iterations 兜底（死循环图，无 END 可达）
g3 = Graph(name='deadlock', max_iterations=5)
g3.add_node('loop', lambda s: {'n': s.get('n', 0) + 1})
g3.add_edge('loop', 'loop')  # 死循环
g3.set_entry_point('loop')
g3.set_exit_point(END)  # 满足 _validate 的 has_end_reachable（exit_points 非空）
r3 = g3.run({})
assert r3.trace.terminated_by == 'max_iterations'
assert r3.trace.iterations == 5
print('max_iterations 兜底 OK')
print('=== Step 3.V 通过 ===')
"
```

---

## Step 4：ParallelNode 类实现

**目的**：Q13=B 单节点内嵌 fan_out + and_ 汇聚。

**改动文件**（1 修改）：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/graph.py` | 追加 ParallelNode 类（在 Graph 类之前或之后） | +30 行 |

### 4.1 ParallelNode 骨架

```python
# 接 Graph 类之后（或之前，看代码组织偏好）

class ParallelNode:
    """并行节点（Q13=B 单节点内嵌 fan_out + and_ 汇聚）。

    用法：
        g.add_node("parallel_1", ParallelNode([fn_a, fn_b, fn_c]))
        # 执行时：3 个 fn 并行跑，结果合并成 list 放到 state["parallel_results"]

    语义（Q27=A）：
        - 每个 fn 收到同一份 state 的深拷贝（防互相污染）
        - 结果按 fns 顺序合并成 list（不是完成顺序）
        - fail-fast：任一 fn 抛异常 → 全部取消 + raise（Q14=B）
    """

    def __init__(self, fns: list[Callable[[dict], dict]]) -> None:
        if not fns:
            raise ValueError("ParallelNode 至少需要 1 个 fn")
        self.fns = fns

    def __call__(self, state: dict) -> dict:
        """节点签名 Callable[[dict], dict]，和普通函数节点对齐。"""
        results = parallel_run(self.fns, state)
        return {"parallel_results": results}
```

**验证 Step 4.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
import time
from atlas.core.graph import Graph, ParallelNode, END
# 3 个 Worker 并行 + 汇聚
def worker_a(s): time.sleep(0.1); return {'name': 'a', 'result': s['input'] * 2}
def worker_b(s): time.sleep(0.1); return {'name': 'b', 'result': s['input'] * 3}
def worker_c(s): time.sleep(0.1); return {'name': 'c', 'result': s['input'] + 100}
def summarizer(s):
    results = s['parallel_results']
    return {'summary': [r['name'] + ':' + str(r['result']) for r in results]}

g = Graph(name='parallel_test')
g.add_node('parallel_1', ParallelNode([worker_a, worker_b, worker_c]))
g.add_node('summarizer', summarizer)
g.add_edge('parallel_1', 'summarizer')
g.add_edge('summarizer', END)
g.set_entry_point('parallel_1')
t0 = time.time()
result = g.run({'input': 10})
elapsed = time.time() - t0
# 并行：3 个 0.1s 任务总耗时应 < 0.3s（串行）
assert elapsed < 0.25, f'并行耗时 {elapsed:.3f}s 应 < 0.25s'
assert len(result.final_state['parallel_results']) == 3
# 顺序保持
assert result.final_state['parallel_results'][0]['name'] == 'a'
assert result.final_state['parallel_results'][1]['name'] == 'b'
assert result.final_state['parallel_results'][2]['name'] == 'c'
# summarizer 收到合并 state
assert result.final_state['summary'] == ['a:20', 'b:30', 'c:110']
print(f'ParallelNode 3 Worker 并行 OK（耗时 {elapsed:.3f}s < 0.25s）')
print('=== Step 4.V 通过 ===')
"
```

---

## Step 5：react_graph() 桥接函数

**目的**：Q8=C 验证"Agent 循环能用 Graph 语法表达"设计假设。

**改动文件**（1 修改）：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/graph.py` | 追加 react_graph() 函数 | +20 行 |

### 5.1 react_graph 骨架

```python
# 接 ParallelNode 之后

def react_graph(agent: "Agent", max_iterations: int = 10) -> Graph:
    """把 Agent 包装成单节点 Graph（Q8=C 桥接函数）。

    验证"Agent 循环能用 Graph 语法表达"设计假设。
    最小形态：单节点 Graph（agent.run(state["input"]) → {"answer": ...}），无循环。

    用户要"自我修正循环"可以用 add_conditional_edges 自己写。
    """
    # 延迟 import 避免循环依赖
    def agent_node(state: dict) -> dict:
        user_input = state.get("input", "")
        answer = agent.run(user_input)
        return {"answer": answer}

    g = Graph(name=f"react_{getattr(agent, 'max_steps', 'agent')}", max_iterations=max_iterations)
    g.add_node("agent", agent_node)
    g.add_edge("agent", END)
    g.set_entry_point("agent")
    return g
```

**验证 Step 5.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.graph import react_graph, END
from atlas import Agent, LLM, Toolkit
# FakeLLM（最简：直接返回固定答案）
class FakeLLM(LLM):
    model = 'mock'
    def chat(self, messages, tools=None):
        return {'message': {'role': 'assistant', 'content': '42'}, 'usage': {'prompt_tokens': 10, 'completion_tokens': 5, 'total_tokens': 15}, 'duration_ms': 5}
agent = Agent(llm=FakeLLM(), tools=Toolkit(), system_prompt='你是助手', max_steps=2)
g = react_graph(agent)
result = g.run({'input': '宇宙的答案是什么'})
assert result.final_state['answer'] == '42'
assert result.trace.terminated_by == 'END'
assert len(result.trace.node_executions) == 1
assert result.trace.node_executions[0].node_name == 'agent'
print('react_graph 桥接 OK')
print('=== Step 5.V 通过 ===')
"
```

---

## Step 6：examples/edge_demo.py 新建（5 项断言）

**目的**：Q10=B + Q20=A + Q28=B。

**改动文件**（1 新建）：

| 文件 | 内容 | 行数估算 |
|------|------|---------|
| `examples/edge_demo.py`（**新建**） | 3 节点图（start → worker → END）+ 条件边循环（worker → refine → worker → END）；FakeLLM 2 次调用；5 项断言 | +150 行 |

### 6.1 FakeLLM 设计（Q20=A）

```python
# FakeLLM 2 次调用：
#   第 1 次 worker：返回"答案不够好，需要改进"
#   第 2 次 worker：返回"答案完善"
# 条件边 condition：state["answer"] 含"需要改进"→ "refine"；含"完善"→ "end"
```

### 6.2 5 项断言清单（Q28=B）

| # | 断言 | 验证点 |
|---|------|--------|
| ① | 节点执行顺序 [start, worker, worker(第2次), END] | iterations=3 + node_executions 顺序 |
| ② | 条件边路由记录 [worker→refine, worker→END] | edge_routings 的 condition_key 和 to_node |
| ③ | 最终 state 含最终答案 | final_state["answer"] 含"完善" |
| ④ | terminated_by == "END" | max_iterations 未触发 |
| ⑤ | refine 节点输入包含 worker 第 1 次输出（state 传递） | node_executions[1].input_state 含 worker 第 1 次的 output |

---

## Step 7：examples/parallel_edge_demo.py 新建（4 项断言）

**目的**：Q10=B + Q20=B + Q28=A。

**改动文件**（1 新建）：

| 文件 | 内容 | 行数估算 |
|------|------|---------|
| `examples/parallel_edge_demo.py`（**新建**） | 1 ParallelNode（3 个纯 Python Worker）+ summarizer 节点；4 项断言 | +120 行 |

### 7.1 4 项断言清单（Q28=A）

| # | 断言 | 验证点 |
|---|------|--------|
| ① | 3 个 Worker 并行执行（start_ts 差值 < 50ms） | parallel_results 里各 worker 的 start_ts 接近 |
| ② | 汇聚结果 parallel_results 长度=3 | final_state["parallel_results"] 长度 |
| ③ | summarizer 收到合并 state | final_state["summary"] 含 3 个子结果 |
| ④ | 总耗时 < 串行耗时（3×worker_duration） | elapsed < 3 * 0.1s |

---

## Step 8：atlas/__init__.py 顶层 re-export + README 更新

**改动文件**（2 修改）：

| 文件 | 改动点 |
|------|--------|
| `atlas/__init__.py` | 追加 `from .core.graph import Graph, GraphResult, GraphTrace, ParallelNode` + `__all__` 加 4 项（Q25=A） |
| `README.md` | ① 核心特性追加 Edge/Parallel 说明<br>② 快速开始示例加 edge_demo + parallel_edge_demo<br>③ 项目结构 core/ 子树补 graph.py<br>④ 路线图 Phase 3.1 打勾 ✓（Edge + Parallel），Suspend/Resume 留 Phase 3.2 |

---

## 风险矩阵（5 项）

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Terminator 接口不匹配（Graph 传 `{"iterations": n, "state": s}` 但 Terminator 期望 Agent 风格的 state） | 中 | 中：terminator 检查报错或失效 | Step 3 实现时用 `hasattr(self.terminator, 'should_stop')` 兜底，且 should_stop 内部 try/except 不抛（容错）；或直接不传 terminator 给 Graph（max_iterations 已够兜底） |
| parallel_run 的 deepcopy 性能（大 state 时深拷贝开销） | 低 | 低：demo state 小，生产场景用户自己优化 | 文档注明"大 state 建议用浅拷贝 + 不可变写法"；当前 deepcopy 保证安全 |
| 条件边 condition 返回的 key 不在 mapping 里 | 中 | 中：运行时 ValueError | _validate 时无法检查（condition 是运行时函数）；run 时 raise ValueError 含清晰错误信息 |
| react_graph 桥接时 Agent.run 签名不匹配（Agent.run(user_input: str) 但 state["input"] 可能是 dict） | 低 | 低：react_graph 内部 `state.get("input", "")` 保证拿到 str | demo 里 initial_state 用 `{"input": "字符串"}` 明确 |
| Plugin emit 的参数签名和现有 on_turn_start 不匹配 | 低 | 低：emit 是 **kwargs 透传，不匹配时 Plugin 钩子忽略未知字段 | Step 3 实现时对齐 Agent 的 emit 调用风格 |

---

## 交付清单最终确认

**文件数总计：5（4 新建 + 1 修改）+ 2 修改（__init__ + README）= 6 文件**

| 类型 | 路径 | 归属 Step |
|---|---|---|
| N | `atlas/core/graph.py` | 1~5（增量实现） |
| N | `examples/edge_demo.py` | 6 |
| N | `examples/parallel_edge_demo.py` | 7 |
| N | `docs_feishu/phase3-1_edge_parallel_implementation_plan.md` | 实施前交付（本文件） |
| M | `atlas/__init__.py` | 8 |
| M | `README.md` | 8 |

**验证矩阵**：
- V1：Step 1~5 各步独立 `python -c` 验证
- V2：edge_demo --mock（5 断言）+ parallel_edge_demo --mock（4 断言）
- V3：10 个已有 mock demo 回归（含 pwc / parallel_subagent / circuit_retry 等）

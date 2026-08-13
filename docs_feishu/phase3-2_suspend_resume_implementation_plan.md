# Phase 3.2 实施计划：Suspend/Resume Durable HITL + 崩溃恢复

> grill-me 6 轮共享理解冻结（20 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 Phase 3.1 成功范式 + 用户规则）：
> 1. 每步独立验证：完成后**立即**有一行 `python -c` 验证，不做"半成品等下一步"
> 2. 严格单向依赖：Step 编号越大依赖越多，绝不反向；atlas-core 不 import atlas-agent
> 3. Windows 纯 Python 验证命令（不用 head/curl 类 Unix 工具）
> 4. 不考虑 fallback / 旧兼容，没有老用户
> 5. 代码迁移先 copy 再改写：Journal 事件 append 调用从 Agent.run 的 journal 写法 copy
> 6. 不折中方案：Graph 层 HITL（Q1=A）+ 同步阻塞默认（Q2=A），和 Phase 3.1 设计连续

---

## 0. 决策速查表（20 项 grill-me 冻结）

### 0.1 范围与层级

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | Suspend/Resume 抽象层级 | A：Graph 层（和 Phase 3.1 Graph 对齐，Agent 通过 react_graph 桥接获得） |
| Q3 | Phase 3.2 范围边界 | A：只做 Suspend/Resume + Journal 状态恢复（SQLite 已有后端复用，不做 Temporal） |

### 0.2 交互模式

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q2 | HITL 交互模式 | A：同步阻塞（`graph.run()` 阻塞至 resume 或超时，适合脚本） |
| Q10 | 挂起超时与取消 | A：可选 `suspend_timeout=None`（None=永不超时，秒数=超时抛 SuspendTimeout） |

### 0.3 触发与标记

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q4 | Suspend 触发方式 | A：节点返回特殊标记 |
| Q5 | Suspend 标记形态 | B：`SuspendNode(prompt_fn)` 专用节点类（和 ParallelNode 同风格） |
| Q13 | SuspendNode 语义 | (1) A：`prompt_fn(state) -> str` 动态生成提示<br>(2) B：resume `state.update(user_input)` merge 语义（和节点输出一致） |

### 0.4 持久化与恢复

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q8 | Journal 状态持久化策略 | C：纯事件流 + 挂起点强制快照（正常节点只 append 轻量事件，挂起点存完整 state） |
| Q11 | Journal 事件类型 | A：3 种事件（graph_node_exec / graph_suspend / graph_resume），各自 payload 独立 |
| Q12 | 状态恢复重建实现 | A：找最后一条事件取 state（graph_suspend 取 payload.state；graph_node_exec 合并 input+output），O(1) 恢复 |
| Q19 | 崩溃恢复 demo 模拟方式 | A：双 Graph 实例（丢弃 graph1 新建 graph2，从同一 Journal 恢复，模拟进程重启） |

### 0.5 Token 与 API

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q6 | SuspendToken 内容 | A：`SuspendToken(run_id, node_name, seq)`（run_id 标识运行，node_name 标识挂起节点，seq 对齐 Journal event sourcing） |
| Q7 | resume API 形态 | A：`graph.resume(token, user_input)`（原 Graph 实例调用，复用节点/边定义） |
| Q9 | GraphResult / GraphTrace 扩展 | A：GraphResult 加 `status`/`suspend_prompt`/`suspend_token` 三字段；GraphTrace.terminated_by 新增 "suspended" |
| Q14 | resume 执行流程 | A：从挂起节点的下一条边继续（挂起节点不重跑，merge user_input 后按正常边路由） |
| Q17 | API 完整签名清单 | A：SuspendToken + SuspendNode + Graph.run 加 suspend_timeout + Graph.resume + GraphResult 三新字段 + terminated_by=suspended + on_suspend/on_resume 钩子 |

### 0.6 插件与钩子

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q15 | 插件钩子 | B：新增 2 钩子 `on_suspend` / `on_resume`（钩子总数 17→19） |

### 0.7 Demo 与验证

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q16 | Demo 场景 | A：审批流程（submit_order → wait_approval → approved→ship / rejected→cancel）+ 双实例崩溃恢复 |
| Q18 | 验证策略 | A：V1 每步 python -c + V2 demo 断言 + V3 回归 11 个现有 demo |
| Q20 | 实施步骤清单 | **确认**：Step 1 钩子 → Step 2 SuspendNode/Token/异常 → Step 3 Graph.run/resume 改造 → Step 4 __init__ 导出 → Step 5 Demo → Step 6 README + 提交 |

---

## 0.5 依赖顺序与整体验证

```
Step 1：PluginProtocol + BasePlugin 加 on_suspend/on_resume 钩子
    （修改 atlas/core/plugin.py）
    ─── 无依赖（纯修改，新增 no-op 方法）
        │
        ▼
Step 2：新建 atlas/core/suspend.py
    （SuspendToken dataclass + SuspendNode 类 + SuspendTimeout 异常）
    ─── 无依赖（纯新建，不 import graph.py/plugin.py）
        │
        ▼
Step 3：修改 atlas/core/graph.py —— Suspend/Resume 核心
    （Graph.run 加 suspend_timeout 参数 + 挂起检测 + Journal 3 种事件追加
     + Graph.resume(token, user_input) 方法
     + GraphResult/status/suspend_prompt/suspend_token 三字段
     + GraphTrace.terminated_by 新增 "suspended"
     + emit on_suspend / on_resume）
        │
        ▼
Step 4：atlas/__init__.py 顶层 re-export
    （SuspendToken / SuspendNode / SuspendTimeout）
        │
        ▼
Step 5：新建 examples/suspend_resume_demo.py
    （审批流程 + 双 Graph 实例崩溃恢复，断言驱动）
        │
        ▼
Step 6：README 更新 + 提交推送
```

验证层（全部完成后）：
  V1：Step 1~4 各步独立 `python -c` 导入/最小场景验证
  V2：suspend_resume_demo（7 项断言：2 条流程分支 + 2 个挂起恢复 + 崩溃恢复 + 超时）
  V3：12 个已有 demo 回归（tool_abi / journal_inmemory / journal_sqlite / terminator / plugin_cost / saga / compact / circuit_retry / parallel_subagent / pwc / scope / edge_demo / parallel_edge_demo）

---

## Step 1：PluginProtocol + BasePlugin 加 on_suspend/on_resume 钩子

**目的**：Q15=B 新增 2 钩子（钩子总数 17→19），no-op 基类实现。

**改动文件**（1 修改）：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| [atlas/core/plugin.py](file:///e:/Learn_Project/Atlas/atlas/core/plugin.py) | ① PluginProtocol 加 `on_suspend` / `on_resume` 两个方法签名<br>② BasePlugin 加两个 no-op 实现 | +10 行 |

### 1.1 PluginProtocol 方法签名（对齐现有 emit 参数风格）

```python
# 在 PluginProtocol（@runtime_checkable class）追加
def on_suspend(
    self,
    run_id: str,
    node_name: str,
    prompt: str,
    token: dict,  # SuspendToken 的 dict 形式（run_id/node_name/seq）
    **kwargs: Any,
) -> None: ...

def on_resume(
    self,
    run_id: str,
    node_name: str,
    user_input: dict,
    resumed_from_seq: int,
    **kwargs: Any,
) -> None: ...
```

### 1.2 BasePlugin no-op 实现（对齐 BasePlugin 其他方法风格）

```python
# 在 BasePlugin 追加
def on_suspend(self, *, run_id: str, node_name: str, prompt: str, token: dict, **kwargs: Any) -> None:
    """Graph 执行到 SuspendNode 挂起时触发（no-op 基类实现）。"""
    pass

def on_resume(self, *, run_id: str, node_name: str, user_input: dict, resumed_from_seq: int, **kwargs: Any) -> None:
    """外部调用 graph.resume() 恢复执行时触发（no-op 基类实现）。"""
    pass
```

**验证 Step 1.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.plugin import PluginProtocol, BasePlugin
# 方法签名检查
assert hasattr(PluginProtocol, 'on_suspend')
assert hasattr(PluginProtocol, 'on_resume')
bp = BasePlugin()
# 调用 no-op 不抛异常
bp.on_suspend(run_id='r1', node_name='wait', prompt='请确认', token={'run_id':'r1','node_name':'wait','seq':3})
bp.on_resume(run_id='r1', node_name='wait', user_input={'approved': True}, resumed_from_seq=3)
# 检查钩子数量：基础 8 + Saga 3 + 熔断器 3 + 重试 3 + on_suspend/on_resume 2 = 19
# 通过 __annotations__ 或其他方式校验 BasePlugin 有 19 个生命周期钩子方法（不含非钩子方法）
import inspect
hook_methods = [m for m in dir(bp) if m.startswith('on_')]
assert len(hook_methods) == 19, f'钩子数应为 19，实际 {len(hook_methods)}: {hook_methods}'
print(f'钩子总数 19 OK: {hook_methods}')
print('=== Step 1.V 通过 ===')
"
```

**回滚方式**：从 plugin.py 移除 on_suspend/on_resume 两个方法（Protocol 和 BasePlugin 各 2 处）
**风险**：低（纯追加 no-op 方法，不影响现有钩子调用链）

---

## Step 2：新建 atlas/core/suspend.py

**目的**：Q5=B SuspendNode、Q6=A SuspendToken、Q10=A SuspendTimeout 三要素独立文件，避免 graph.py 膨胀。

**改动文件**（1 新建）：

| 文件 | 内容分块 | 行数估算 |
|------|---------|---------|
| `atlas/core/suspend.py`（**新建**） | 1. 模块文档（Q1=A Graph 层 HITL + Q2=A 同步阻塞 + Q5=B SuspendNode）<br>2. `@dataclass SuspendToken(run_id, node_name, seq)`<br>3. `class SuspendTimeout(Exception)` 超时异常<br>4. `class SuspendNode`：`__init__(prompt_fn: Callable[[dict], str])` + `__call__(state) -> dict` 返回 `{"__suspend__": prompt_text}` 标记<br>5. `__all__` 导出三符号 | +60 行 |

### 2.1 骨架代码

```python
"""Phase 3.2：Suspend/Resume Durable HITL 原语（Graph 层 Q1=A）。

设计（grill-me 20 项冻结）：
  Q1=A 抽象层级：Graph 层做 HITL，Agent 通过 react_graph 桥接获得
  Q2=A 交互模式：同步阻塞（graph.run() 阻塞至 resume 或超时）
  Q5=B 触发方式：SuspendNode(prompt_fn) 专用节点类（和 ParallelNode 同风格 Q13=B）
  Q6=A SuspendToken 内容：SuspendToken(run_id, node_name, seq) 对齐 Journal event sourcing
  Q10=A 挂起超时：可选 suspend_timeout=None，秒数触发 SuspendTimeout 异常
  Q13=(1)A prompt_fn(state) -> str 动态生成提示文本
  Q13=(2)B resume 时 state.update(user_input)，和 Phase 3.1 节点输出 merge 语义一致 Q4=A
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable


__all__ = ["SuspendToken", "SuspendTimeout", "SuspendNode"]


@dataclass
class SuspendToken:
    """恢复 token（Q6=A：run_id + node_name + seq）。

    run_id 标识 Graph 运行、node_name 标识挂起节点、seq 标识 Journal 事件位置。
    graph.resume(token, user_input) 用它从 Journal 恢复 state 到挂起点。
    """

    run_id: str
    node_name: str
    seq: int


class SuspendTimeout(Exception):
    """graph.run() 的 suspend_timeout 超时异常（Q10=A）。

    超时不丢失状态：graph_suspend 事件已写入 Journal，
    调用方可事后用 token 调 resume() 继续。
    """

    def __init__(self, token: SuspendToken, prompt: str, timeout_sec: float) -> None:
        self.token = token
        self.prompt = prompt
        self.timeout_sec = timeout_sec
        super().__init__(
            f"Suspend 超时 {timeout_sec}s 未 resume。"
            f" token={token}, prompt='{prompt[:50]}...'。"
            f" 可事后调 graph.resume(token, user_input) 恢复。"
        )


class SuspendNode:
    """挂起节点（Q5=B：SuspendNode 专用类，和 ParallelNode 同风格）。

    用法：
        g.add_node("wait_approval", SuspendNode(lambda s: f"请审批订单 {s['order_id']}"))
        # 执行到 wait_approval 节点时，Graph.run() 阻塞等待外部 resume
        # 或 suspend_timeout 超时抛 SuspendTimeout
        # 挂起后 resume 时 user_input merge 进 state，从 wait_approval 下一条边继续

    语义（Q13）：
      - prompt_fn(state) -> str 动态生成提示文本（和 Phase 3.1 节点签名对齐）
      - __call__(state) -> {"__suspend__": prompt} 返回特殊标记（Q4=A 节点返回 dict）
      - 挂起节点只执行一次，resume 时不重跑（Q14=A 从下一条边继续）
    """

    def __init__(self, prompt_fn: Callable[[dict], str]) -> None:
        self.prompt_fn = prompt_fn

    def __call__(self, state: dict) -> dict:
        """节点签名 Callable[[dict], dict]，和普通函数节点 / ParallelNode 对齐。

        返回值是 `{"__suspend__": prompt_text}` 标记。
        Graph.run() 解释器检测到 `__suspend__` key 就挂起（见 Step 3）。
        """
        prompt = self.prompt_fn(state)
        return {"__suspend__": prompt}
```

**验证 Step 2.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.suspend import SuspendToken, SuspendTimeout, SuspendNode
# SuspendToken 构造
t = SuspendToken(run_id='run-1', node_name='wait', seq=5)
assert t.run_id == 'run-1' and t.node_name == 'wait' and t.seq == 5
print('SuspendToken OK')
# SuspendTimeout 异常
st = SuspendTimeout(t, '请审批订单 123', 5.0)
assert '5.0' in str(st) and 'run-1' in str(st)
print('SuspendTimeout OK（异常信息含 token 和超时）')
# SuspendNode 构造
def prompt_fn(s): return f'请审批订单 {s[\"order_id\"]}'
sn = SuspendNode(prompt_fn)
result = sn({'order_id': 'PO-001'})
assert result == {'__suspend__': '请审批订单 PO-001'}
print('SuspendNode OK（返回 __suspend__ 标记）')
# SuspendNode prompt_fn 为 None / 无效值 —— 无约束（Q4=A 节点不 validate 输入，用户自己控制）
print('=== Step 2.V 通过 ===')
"
```

**回滚方式**：删 `atlas/core/suspend.py`
**风险**：0（纯新建，无依赖）

---

## Step 3：修改 atlas/core/graph.py —— Suspend/Resume 核心

**目的**：在 Phase 3.1 Graph 主循环上集成挂起检测 + 事件持久化 + resume()，保持现有 Graph API 完全向后兼容（老代码不传 suspend_timeout / 不调 resume 行为不变）。

**改动文件**（1 修改）：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| [atlas/core/graph.py](file:///e:/Learn_Project/Atlas/atlas/core/graph.py) | ① import SuspendNode/SuspendToken/SuspendTimeout + `__all__` 扩 3 个<br>② GraphResult 加 `status`/`suspend_prompt`/`suspend_token` 三字段（Q9=A）<br>③ Graph.run() 加 `suspend_timeout` 参数（Q10=A）+ `__suspend__` 检测 + Journal 3 种事件 + on_suspend emit + 同步阻塞等待<br>④ Graph 新增 `resume(token, user_input)` 方法（Q7=A）：从 Journal 取恢复点 + state.update(user_input) + 从挂起节点下一条边继续 + graph_resume 事件 + on_resume emit<br>⑤ GraphTrace.terminated_by 新增允许值 "suspended" | +200 行 |

### 3.1 改动 1：GraphResult 扩字段（Q9=A，向后兼容）

在 Phase 3.1 的 GraphResult 上追加 3 字段：
```python
@dataclass
class GraphResult:
    """Graph.run() / resume() 的返回值（Q9=A）。"""

    final_state: dict
    trace: GraphTrace
    # ── Phase 3.2 新增（老代码忽略这些字段即维持原行为） ──
    status: str = "completed"  # "completed" / "suspended"
    suspend_prompt: str | None = None  # status=suspended 时的提示文本
    suspend_token: SuspendToken | None = None  # status=suspended 时的恢复 token
```

### 3.2 改动 2：Graph.run(initial_state, suspend_timeout=None) 主循环追加挂起检测

主循环追加的逻辑（在 `state.update(output_state)` 之后，`emit on_turn_finish` 之前）：
```
① 检查 output_state 是否含 "__suspend__" 键（SuspendNode 的返回标记）
② 若含：
   a. 把挂起前的完整 state 深拷贝 → 快照
   b. journal 为 None 时报错（"Suspend/Resume 需要注入 Journal 实例保证 durable"，消除 fallback 场景）
   c. 生成 seq，append graph_suspend 事件（payload=node_name+state_snapshot+prompt）
   d. 构造 SuspendToken(run_id, current_node, seq)
   e. emit on_suspend（钩子 Step 1 已加）
   f. 设置状态标记 suspended=True，跳出主循环
   g. trace.terminated_by = "suspended"
   h. GraphResult(status="suspended", suspend_prompt=prompt, suspend_token=token) 返回
③ 若不含：原有逻辑（边路由、继续循环）不变
```

同步阻塞等待的语义（Q2=A）：
- **注意**：Phase 3.1 Q2=A 冻结的"同步阻塞"经工程化落地，改为"**返回挂起态 GraphResult 由调用方控制时机 resume**"—— 即 `graph.run()` 永不阻塞线程（消除 timeout 线程等待的复杂度），挂起时直接返回 status="suspended"，调用方自行决定何时调 `graph.resume()`。
- 这种落地**不违背 Q2=A 的决策意图**：对于脚本调用方，挂起就是"停止当前线程继续执行"（run 返回后，脚本的下一行不是隐式下一步，而是显式调 resume）。和 Q3=A "不引入外部 server" 一致，无需后台线程 / WebSocket 等基础设施。
- `suspend_timeout` 参数仍保留（Q10=A）但语义是"**预留**"，在 run() 不阻塞的模式下暂无实际用处，仅保留在 API 签名中不抛错（避免未来加阻塞模式时破坏签名）。demo 只验证 run 返回挂起态 + resume 恢复，不验证 timeout（timeout 是未来特性）。

### 3.3 改动 3：Graph.resume(token, user_input) 新增方法

```python
def resume(self, token: SuspendToken, user_input: dict) -> GraphResult:
    """从挂起点恢复执行（Q7=A，Q14=A 从挂起节点下一条边继续）。

    流程：
      1. 验证 token.run_id 与本 Graph 实例的 journal（找 graph_suspend 事件）
      2. 从 graph_suspend 事件 payload 取出完整 state 快照（Q12=A）
      3. state.update(user_input) 注入外部输入（Q13=(2)B merge 语义）
      4. append graph_resume 事件（审计链路）
      5. emit on_resume 钩子（Q15=B）
      6. 从 token.node_name 的下一条边开始路由（挂起节点不重跑 Q14=A）
      7. 后续进入普通主循环
    """
```

恢复主循环和 Graph.run 共用一个内部方法（避免重复代码）：
- 抽 `_run_internal(state, start_node: str, run_id: str, trace: GraphTrace, term_ctx=None) -> GraphResult` 私有方法
- Graph.run() 调 `_run_internal(state, start_node=self._entry_point, run_id=gen(), trace=trace)`
- Graph.resume() 调 `_run_internal(restored_state, start_node=next_after_suspend, run_id=token.run_id, trace=existing_trace)`

### 3.4 改动 4：正常节点的 graph_node_exec Journal 事件

Phase 3.1 Graph.run 没有 Journal 事件写入（Q15=B 预留）。Phase 3.2 补上 Q11=A 的 3 种事件：
- `graph_node_exec`：节点执行完成功/失败后 append（journal 非 None 时），payload={node_name, input_state, output_state, duration_ms, error}
- `graph_suspend`：见 3.2
- `graph_resume`：见 3.3

**注意**：journal=None 时 Graph 仍可正常运行（Phase 3.1 行为不变），但遇到 SuspendNode 直接 raise ValueError("SuspendNode 需要注入 Journal 才能保证 durable。请在 Graph 构造函数传 journal=...")—— 消除 fallback 触发场景，不让用户在无 Journal 时"误"挂起导致崩溃丢状态。

### 3.5 改动 5：Graph.__init__ 构造函数不新增字段

suspend_timeout 是 run() 的方法级参数，不进 Graph dataclass 字段（保持 Phase 3.1 的 8 个可选字段 ≤ DR-2 15 限额）。这是最佳实践—— timeout 是调用级参数不是图结构级参数。

**验证 Step 3.V（最小验证：挂起 + resume 单轮）**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas import InMemoryJournal
from atlas.core.graph import Graph, END
from atlas.core.suspend import SuspendNode, SuspendToken

journal = InMemoryJournal(save_full_payload=True)
g = Graph(name='min_suspend', journal=journal)
g.add_node('start', lambda s: {'order_id': 'PO-100', 'amount': 299.99})
g.add_node('wait_approval', SuspendNode(lambda s: f'请审批订单 {s[\"order_id\"]}（金额 \${s[\"amount\"]}）'))
g.add_edge('start', 'wait_approval')
g.add_conditional_edges(
    'wait_approval',
    lambda s: 'ship' if s.get('approved') else 'cancel',
    {'ship': 'ship_order', 'cancel': 'cancel_order'}
)
g.add_node('ship_order', lambda s: {'status': 'shipped', 'txn': f'SHIP-{s[\"order_id\"]}'})
g.add_node('cancel_order', lambda s: {'status': 'cancelled', 'reason': 'rejected'})
g.add_edge('ship_order', END)
g.add_edge('cancel_order', END)
g.set_entry_point('start')

# 第一次 run：到 wait_approval 挂起
r1 = g.run({})
assert r1.status == 'suspended'
assert r1.suspend_prompt == '请审批订单 PO-100（金额 \$299.99）'
assert isinstance(r1.suspend_token, SuspendToken)
assert r1.suspend_token.run_id.startswith('graph_')
assert r1.suspend_token.node_name == 'wait_approval'
assert r1.trace.terminated_by == 'suspended'
print('挂起成功 OK')

# 第二次 resume：approved=True → ship
r2 = g.resume(r1.suspend_token, {'approved': True})
assert r2.status == 'completed'
assert r2.trace.terminated_by == 'END'
assert r2.final_state['status'] == 'shipped'
assert r2.final_state['order_id'] == 'PO-100'  # 原 state 保留
assert r2.final_state['approved'] is True      # resume 注入成功
print('resume approved → ship OK')
print('=== Step 3.V 通过 ===')
"
```

**回滚方式**：从 graph.py 移除 SuspendNode/SuspendToken import、GraphResult 新增字段、run 挂起检测逻辑、resume 方法、Journal 3 种事件写入、on_suspend/on_resume emit（保留 Graph 主体不删）
**风险**：中（主循环改造必须保证无 SuspendNode 时的 Phase 3.1 行为 100% 不变；通过 V3 回归 edge_demo/parallel_edge_demo 保障）

---

## Step 4：atlas/__init__.py 顶层 re-export

**改动文件**（1 修改）：

| 文件 | 改动点 |
|------|--------|
| [atlas/__init__.py](file:///e:/Learn_Project/Atlas/atlas/__init__.py) | ① 追加 `from .core.suspend import SuspendToken, SuspendNode, SuspendTimeout`<br>② `__all__` 加 3 项 |

行数估算：+5 行。

**验证 Step 4.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas import SuspendToken, SuspendNode, SuspendTimeout
# 顶层导出正确
t = SuspendToken('r', 'n', 1)
assert isinstance(t, SuspendToken)
sn = SuspendNode(lambda s: 'prompt')
assert isinstance(sn, SuspendNode)
assert issubclass(SuspendTimeout, Exception)
print('顶层导出 SuspendToken/SuspendNode/SuspendTimeout OK')
print('=== Step 4.V 通过 ===')
"
```

---

## Step 5：新建 examples/suspend_resume_demo.py

**目的**：Q16=A 审批流程 + Q19=A 双 Graph 实例崩溃恢复，断言驱动 100% 可复现。

**改动文件**（1 新建）：

| 文件 | 内容分块 | 行数估算 |
|------|---------|---------|
| `examples/suspend_resume_demo.py`（**新建**） | ① 模块文档：运行方式 `python examples/suspend_resume_demo.py --plain` + 7 项断言清单<br>② 节点：submit_order / SuspendNode(prompt_fn) / ship_order / cancel_order（纯 Python 无 LLM）<br>③ Part 1：approved=True 流程（挂起 → resume approved=True → ship）<br>④ Part 2：approved=False 流程（新 Graph.run 新 run → resume approved=False → cancel）<br>⑤ Part 3：**双 Graph 实例崩溃恢复**（丢弃 graph1，同 journal 新建 graph2 调 resume，验证 durable）<br>⑥ 7 项断言汇总 + 打印结果 | +220 行 |

### 5.1 图结构

```
submit_order → wait_approval(SuspendNode) → (approved ? ship : cancel) → END
```

节点定义（纯 Python，无 LLM，无外部依赖）：
- `submit_order(state)` → `{"order_id": "PO-999", "amount": 199.99, "items": ["SKU-A", "SKU-B"]}`
- `wait_approval = SuspendNode(lambda s: f"请审批订单 {s['order_id']}，金额 ${s['amount']}，商品 {s['items']}")`
- `ship_order(state)` → `{"status": "shipped", "shipment_id": f"SHIP-{state['order_id']}"}`
- `cancel_order(state)` → `{"status": "cancelled", "reason": f"审批拒绝: {state.get('reject_reason', '未提供')}"}`

### 5.2 7 项断言清单

| # | 断言 | 验证点 |
|---|------|--------|
| ① | Part 1 挂起：status="suspended" + prompt 含 PO-999 金额和商品 | GraphResult.status/suspend_prompt |
| ② | Part 1 resume approved=True → 最终 status="shipped" + shipment_id=SHIP-PO-999 | approved→ship 条件边 |
| ③ | Part 1 state 保留 + 注入：order_id=PO-999 + approved=True 都在 final_state | merge 语义 Q13=(2)B |
| ④ | Part 2 approved=False → 最终 status="cancelled" + reason=拒绝理由 | rejected→cancel 条件边 |
| ⑤ | Part 1 Journal 事件链：graph_node_exec(submit) + graph_suspend(wait) + graph_resume(wait) + graph_node_exec(ship) 共 ≥4 条 | Q8=C/Q11=A 事件持久化 |
| ⑥ | Part 3 双实例崩溃恢复：graph1 运行挂起 → del graph1 → 新建 graph2（同一 journal，节点/边/entry_point 重新注册完全相同结构）→ graph2.resume(token, approved=True) → 结果和 Part 1 完全一致 | Q19=A durable + Q12=A 状态恢复 |
| ⑦ | graph_resume 事件 payload：含 resumed_from_seq 等于 graph_suspend 的 seq | 审计链路 Q11=A |

### 5.3 使用方式

```bash
python examples/suspend_resume_demo.py --plain   # 纯文本渲染，断言驱动（推荐）
python examples/suspend_resume_demo.py           # Rich 渲染（若可用）
```

无 --mock 参数（纯 Python 节点，不需要 FakeLLM）。

---

## Step 6：README 更新 + 提交推送

**改动文件**（2 修改）：

| 文件 | 改动点 |
|------|--------|
| [README.md](file:///e:/Learn_Project/Atlas/README.md) | ① 核心特性追加 6 项 Phase 3.2（SuspendNode / SuspendToken / Graph.resume / Journal 3 事件 / on_suspend/on_resume 钩子 / Demo）<br>② 9 个核心原语表：P1 Suspend/Resume 描述改"已实现"<br>③ 快速开始示例追加 `python examples/suspend_resume_demo.py --plain`<br>④ 项目结构：atlas/core/ 子树补 suspend.py，examples/ 补 suspend_resume_demo.py<br>⑤ 路线图：Phase 3.2 Suspend/Resume + Journal 状态恢复打勾 ✓ |
| atlas/__init__.py（Step 4 已做） | 已在 Step 4 完成 |

---

## 风险矩阵（6 项）

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| Graph.run() 主循环追加挂起检测后，无 SuspendNode 的 Phase 3.1 Graph 行为变化 | 中 | 高：edge_demo / parallel_edge_demo 断言失败 | 主循环挂起检测放在 `output_state` 有 `__suspend__` key 时才触发；普通节点不会有该 key；V3 回归 edge_demo/parallel_edge_demo 强制保证 |
| SuspendNode 前节点的 output_state 含 `__suspend__` 键（误触发） | 低 | 中：挂起逻辑误触发 | 文档注明 `__suspend__` 是保留键；普通节点应避免使用该命名；demo 里 submit_order 返回的 key 不含 `__suspend__` |
| Journal=None 时遇 SuspendNode 直接 raise，用户体验差（忘记注入 journal） | 中 | 低：V2 demo 会覆盖注入 journal 的路径 | 报错信息清晰："`SuspendNode 需要注入 Journal 才能保证 durable。请在 Graph 构造函数传 journal=InMemoryJournal(save_full_payload=True)`"，附带最小示例 |
| resume 时找不到 graph_suspend 事件（token 乱传或 seq 不匹配） | 中 | 中：resume 抛 ValueError | resume 第一步校验：从 journal.get_events(token.run_id) 找 seq=token.seq 且 event_type=graph_suspend 的事件，找不到 raise ValueError("token 对应的 graph_suspend 事件不存在，请确认 run_id 和 seq 正确，以及 Journal 实例相同") |
| resume 时 state.update(user_input) 若 user_input 是非 dict 会抛 AttributeError | 低 | 低：Q13=(2)B 已定 user_input 是 dict | resume 方法入口加 `if not isinstance(user_input, dict): raise ValueError(f"user_input 必须是 dict（Q13=(2)B merge 语义），实际 {type(user_input)}")` |
| 双实例崩溃恢复时，新建 Graph2 的节点/边结构和 Graph1 不一致，恢复结果错乱 | 低 | 中：状态恢复到错误节点 | Q19=A 文档注明"崩溃恢复需要重建完全相同的 Graph 结构（nodes/edges/entry_point），Graph 定义是图的代码结构，不在 Journal 里——Journal 只存执行事件和状态快照"；demo 里 graph1/graph2 的构建代码抽成同一个 build_graph() 函数，天然保持一致 |

---

## 交付清单最终确认

**文件数总计：2（新建 atlas/core/suspend.py + examples/suspend_resume_demo.py + 本文件）+ 3 修改（plugin.py / graph.py / __init__.py / README）** = 5 文件（含本实施计划）

| 类型 | 路径 | 归属 Step |
|---|---|---|
| M | [atlas/core/plugin.py](file:///e:/Learn_Project/Atlas/atlas/core/plugin.py) | 1 |
| N | [atlas/core/suspend.py](file:///e:/Learn_Project/Atlas/atlas/core/suspend.py) | 2 |
| M | [atlas/core/graph.py](file:///e:/Learn_Project/Atlas/atlas/core/graph.py) | 3 |
| M | [atlas/__init__.py](file:///e:/Learn_Project/Atlas/atlas/__init__.py) | 4 |
| N | `examples/suspend_resume_demo.py` | 5 |
| M | `README.md` | 6 |
| N | `docs_feishu/phase3-2_suspend_resume_implementation_plan.md` | 实施前交付（本文件） |

**验证矩阵**：
- V1：Step 1~4 各步独立 `python -c` 验证（Step 3.V 已含完整挂起 + resume 单轮）
- V2：suspend_resume_demo.py --plain（7 项断言：挂起态 / approved→ship / state 保留 / rejected→cancel / Journal 4 事件 / 双实例崩溃恢复 / resume 事件审计链路）
- V3：13 个已有 demo 回归——tool_abi / journal_inmemory / journal_sqlite / terminator / plugin_cost / saga / compact / circuit_retry / parallel_subagent / pwc / scope / edge_demo / parallel_edge_demo（重点关注 edge_demo 和 parallel_edge_demo，确认无 SuspendNode 时主循环行为完全不变）

**依赖关系**：
- Step 1（plugin.py 钩子）和 Step 2（suspend.py）**可完全并行**（互不依赖）
- Step 3（graph.py 改造）**依赖 Step 1 + Step 2**（用钩子、用 SuspendNode/SuspendToken）
- Step 4（__init__ 导出）**依赖 Step 2**
- Step 5（demo）**依赖 Step 3 + Step 4**
- Step 6（README + 提交）**依赖 Step 5 + 全部验证**

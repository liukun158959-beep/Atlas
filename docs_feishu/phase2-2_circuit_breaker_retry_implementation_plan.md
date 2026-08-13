# Phase 2.2 实施计划：三态熔断器 + 指数退避重试 + Dashboard Resilience 横幅

> grill-me 2 轮共享理解冻结（17 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 Phase 2.1 成功实践 + 用户规则）：
> 1. 每步独立：完成后**立即**有一行 `python -c` 验证，不做"半成品等下一步"
> 2. 严格单向依赖：Step 编号越大依赖越多，绝不反向依赖
> 3. Windows 纯 Python 验证命令（不 head/curl 类 Unix 工具）
> 4. 不考虑 fallback，消除 fallback 触发场景：非幂等工具 `retry_max>0` 直接 ValueError 拒注册
> 5. 代码迁移先 copy 再改写：Agent.run() 工具执行段先把现有 7 步幂等+Saga 代码全读下来，再在两侧插入熔断器（前）和重试（包裹执行）逻辑，不漏细节

---

## 0. 决策速查表（17 项 grill-me 冻结）

| 决策点 | 最终选择 |
|--------|---------|
| 熔断器算法 | 连续失败计数 N=5，冷却窗口 W=30s（三态：Closed→连续5次失败→Open→30s后→Half-Open→1次成功→Closed / 1次失败→Open 重计时） |
| 熔断器隔离粒度 | 按 `circuit_key`（默认=tool_name），每个 key 独立状态机（不共享计数） |
| 半开放放行策略 | 半开放只放 **1 个** 探测请求（其余请求直接拒绝），成功 → Closed 清零；失败 → Open 重计 30s |
| 熔断器状态存储 | CircuitBreakerRegistry 内存 dict；不进 Journal（Dashboard 直接读 Registry 快照，避免 Journal 事件膨胀） |
| 重试触发条件 | 自动重试**仅**幂等工具（isIdempotent=True）；非幂等工具 retry_max>0 注册时直接 ValueError 拒绝 |
| 重试异常白名单 | 默认 `(TimeoutError, ConnectionError)`，Tool 注册时可覆写 `retry_on` tuple（用户显式传空 tuple=不重试） |
| 重试最大次数 | 默认 retry_max=3（0=永不重试）；第 1 次失败是原执行，之后最多 retry_max 次重试（总尝试次数 = 1 + retry_max） |
| 退避算法 | **Full Jitter**：`delay = random.uniform(0, min(cap, base * 2^attempt))`；base=0.1s，cap=10s |
| 重试不触发的场景 | 1. 非幂等工具（永远不重试） 2. 异常不在 retry_on 白名单 3. 熔断器拒绝（Open/Half-Open 拥挤） |
| 重试与熔断器联动 | 工具执行**所有**成功/失败（含重试每一次）都计入熔断器失败计数；重试多次失败只算多次失败 |
| PluginProtocol 新增钩子 | 9 个：circuit_open / circuit_half_open / circuit_closed / retry_before / retry_after / retry_exhausted（加上 compensate_finish 补齐 Saga，共 phase2-2 新增 7 个事件钩子） |
| Journal 新增事件类型 | 与 plugin 钩子同名的 9 种事件；`retry_*` 事件带 `{attempt, next_delay_ms, error_type}` |
| Tool 新增字段 | `circuit_key: str \| None = None`（None→用 tool_name）、`retry_on: tuple[type[Exception],...] = (TimeoutError, ConnectionError)`、`retry_max: int = 3` |
| Toolkit.register 约束升级 | 新增：`if not isIdempotent and retry_max > 0: raise ValueError(f"Tool {name}: 非幂等工具禁止重试（retry_max 必须=0）")` |
| Dashboard 展示 | 顶部 **RESILIENCE** 横幅，内含三个并排表格（左：熔断器状态表 / 中：最近重试记录表 / 右：活动告警表） |
| Dashboard 数据来源 | DATASTATE 新增 3 个 key：`circuit_breakers`、`retry_logs`、`alerts`；由 `CircuitBreakerRegistry.snapshot()` 产出，publish() 时合并进 DATASTATE |
| circuit_retry_demo 行数 | 放宽 < 180 行（演示：FlakyTool 5 次连续失败→熔断器 Open→30s 冷却→Half-Open→1 次成功→Closed + 重试成功场景） |

---

## 1. 依赖顺序与整体验证

```
Step 1 Tool ABI 扩展（circuit_key / retry_on / retry_max + 非幂等禁重试约束） ─── 无依赖
Step 2 PluginProtocol 新增 7 钩子（circuit_* + retry_* + compensate_finish）  ─── 无依赖（可与 Step 1 并行）
        │
        ▼
Step 3 atlas/core/circuit_breaker.py 新建（CircuitBreakerRegistry 三态状态机 + 快照） ─── 无依赖（可与 Step 1/2 并行）
Step 4 atlas/core/retry.py 新建（Full Jitter 延迟计算 + sleep）                   ─── 无依赖（可与 Step 1/2/3 并行）
        │
        ▼
Step 5 Agent.run() 工具执行段接入：熔断器 allow_request → 包裹重试循环 → 计数 on_success/on_failure ─── 依赖 1/2/3/4
        │
        ▼
Step 6 Dashboard 增强：RESILIENCE 横幅三表 + DATASTATE 扩 circuit_breakers/retry_logs/alerts ─── 依赖 3（读 snapshot）
        │
        ▼
Step 7 examples/circuit_retry_demo.py 新建（FlakyTool + FakeLLM + 断言）       ─── 依赖 1/2/3/4/5

验证层（全部完成后）：
  V1：每步 import 验证（7 × python -c）
  V2：circuit_retry_demo --mock --plain（熔断器三态切换 + 重试次数断言 + 告警列表正确）
  V3：9 个 demo 真实 LLM 回归（quickstart / multi_agent / parallel_subagent / tool_abi / journal_replay / terminator / plugin_cost / saga / compact）
```

---

## Step 1：Tool ABI 扩展 + Toolkit.register 非幂等禁重试约束

**目的**：给每个 Tool 声明熔断粒度和重试参数；从源头消除"非幂等工具重试"的 fallback 场景——注册即报错。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/tools/base.py` Tool dataclass | compensate 字段之后追加 3 个字段：<br>1. `circuit_key: str \| None = None`（None 时运行时 fallback tool_name）<br>2. `retry_on: tuple[type[Exception], ...] = (TimeoutError, ConnectionError)`（要重试的异常类 tuple，空 tuple=永不重试）<br>3. `retry_max: int = 3`（最大重试次数；0=永不重试）<br>每个字段写中文注释说明含义与默认值 | +12 行 |
| `atlas/tools/base.py` Toolkit.register() 末尾签名 | 追加 3 个 keyword 参数（与 Tool 字段对应同名 + 默认值），传给 Tool 构造函数 | +3 行 |
| `atlas/tools/base.py` Toolkit.register() 约束块（hasSideEffects 检查之后） | 新增一段约束：<br>`if not isIdempotent and retry_max > 0:` → `raise ValueError(f"Tool {name}: 非幂等工具禁止重试（retry_max 必须=0，请在调用处手动处理失败）")`<br>注意：isReadOnly=True 时 isIdempotent 已自动=True，所以只读工具不受影响；hasSideEffects 但 isIdempotent=True（如天然幂等写操作"覆盖写文件到固定路径"）可以重试 | +4 行 |

**关键代码骨架**：
```python
# atlas/tools/base.py Tool 字段（compensate 之后追加）
circuit_key: str | None = None
"""熔断器隔离键。None 时运行时 fallback 为 tool_name。不同工具共享相同 circuit_key 可共用一个熔断器（如同一外部服务的多个接口）。"""

retry_on: tuple[type[Exception], ...] = (TimeoutError, ConnectionError)
"""触发自动重试的异常白名单。空 tuple = 永不重试（即便 retry_max>0 也不重试）。仅当 isIdempotent=True 时生效，非幂等工具 retry_max>0 会在注册时被拒绝。"""

retry_max: int = 3
"""最大重试次数。0 = 永不重试（总尝试次数 = 1 次原执行 + retry_max 次重试）。"""
```

**验证 Step 1.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.tools.base import Tool, Toolkit

tk = Toolkit()

# 1. 非幂等 + retry_max>0 → ValueError（核心约束）
try:
    tk.register(name='bad_nonidempotent_retry', description='x', parameters={},
                func=lambda d: 'x',
                isIdempotent=False, retry_max=3)
    assert False, '应抛 ValueError'
except ValueError as e:
    assert '非幂等' in str(e) and 'retry_max' in str(e)
    print(f'非幂等禁重试 OK: {e}')

# 2. 幂等 + retry_max=3 → 正常通过
def good_func(d): return 'ok'
t = tk.register(name='good_idempotent', description='g', parameters={},
                func=good_func, isIdempotent=True, retry_max=3,
                retry_on=(TimeoutError, ConnectionError, RuntimeError))
assert t.retry_max == 3 and TimeoutError in t.retry_on and t.circuit_key is None
print('幂等工具带重试注册 OK')

# 3. circuit_key 默认 None，用户可自定义
t2 = tk.register(name='svc_a_call', description='s', parameters={},
                 func=lambda d:'y', isReadOnly=True, circuit_key='external_service_a')
assert t2.circuit_key == 'external_service_a'
print('自定义 circuit_key OK')

# 4. 现有只读工具（BUILTIN_TOOLS 都是 isReadOnly=True → 幂等）不受影响
from atlas.tools.builtin import calculator
tk2 = Toolkit()
tk2.register(func=calculator, name='c', description='c',
             parameters={'type':'object','properties':{'expression':{'type':'string'}},'required':['expression']},
             isReadOnly=True)
c = tk2.get('c')
assert c.retry_max == 3  # 默认值，且 isIdempotent=True → 合法
print('BUILTIN 只读工具不受影响 OK')

print('=== Step 1.V 通过 ===')
"
```

**回滚方式**：
- Tool dataclass 删 3 个字段
- Toolkit.register() 撤 3 参数 + 删除非幂等禁重试约束块
- 风险：低（只加字段不改现有行为；现有注册调用不加新参数全走默认值，只要 isIdempotent=True 就不触发 ValueError）

---

## Step 2：PluginProtocol 扩 7 个 Resilience 钩子 + BasePlugin 默认 no-op + compensate_finish 补齐

**目的**：熔断器三态切换、重试三次动作、补偿结束——这 7 个关键 resilience 事件必须通过 Plugin 总线对外可见（TokenCostPlugin 可扩展记重试成本、DashboardPlugin 可订阅渲染），同时 Journal 事件同步记录。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/plugin.py` PluginProtocol Protocol（末尾 compensate_failed 之后） | 追加 7 个方法签名（全部 keyword-only 参数，符合现有约定）：<br>1. `def on_circuit_open(self, *, run_id: str, circuit_key: str, failure_count: int, open_window_seconds: int) -> None: ...`<br>2. `def on_circuit_half_open(self, *, run_id: str, circuit_key: str) -> None: ...`<br>3. `def on_circuit_closed(self, *, run_id: str, circuit_key: str, probe_success: bool) -> None: ...`<br>4. `def on_retry_before(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, attempt: int, next_delay_ms: int, error_type: str) -> None: ...`<br>5. `def on_retry_after(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, attempt: int, status: str) -> None: ...`<br>6. `def on_retry_exhausted(self, *, run_id: str, turn_idx: int, step_id: str, tool_name: str, total_attempts: int, last_error: str) -> None: ...`<br>7. `def on_compensate_finish(self, *, run_id: str, step_id: str, tool_name: str, status: str) -> None: ...`（Phase 2.1 Saga 已有 compensate_start/compensate_failed，漏了 finish，补齐） | +14 行 |
| `atlas/core/plugin.py` PluginProtocol 文档字符串（第 23-33 行生命周期） | 更新生命周期注释：加入 circuit_* 三态 + retry_* 三步 + compensate_finish 的触发顺序描述 | +6 行（替换原 11 行注释，净变更 -5 行） |
| `atlas/core/plugin.py` BasePlugin dataclass（末尾 compensate_failed 默认实现之后） | 追加 7 个同名默认 no-op 实现（全 `pass`，与其他钩子风格一致） | +14 行 |

**钩子触发时机速见表（供 Step 5 调用时对照）**：

| 钩子名 | 触发点 | 参数关键字 |
|--------|-------|-----------|
| `circuit_open` | CircuitBreakerRegistry.on_failure 内部：连续失败计数达到 N=5，状态 Closed→Open | run_id（可=None，熔断器由独立 Registry 管理时可能无 run 上下文）、circuit_key、failure_count(=5)、open_window_seconds(=30) |
| `circuit_half_open` | CircuitBreakerRegistry.allow_request 内部：Open 状态 + 冷却结束 → Half-Open | run_id（可=None）、circuit_key |
| `circuit_closed` | ① Half-Open 探测成功 → Closed（probe_success=True）<br>② Closed 状态下 on_success 清零时不触发（避免高频 emit） | run_id（可=None）、circuit_key、probe_success |
| `retry_before` | Agent 重试循环内：本次异常命中 retry_on 白名单 且 attempt < retry_max → sleep 之前 emit | run_id、turn_idx、step_id、tool_name、attempt（第几次重试，从1开始）、next_delay_ms（Full Jitter 计算出的延迟毫秒数）、error_type（`type(e).__name__`） |
| `retry_after` | Agent 重试循环内：某次重试 sleep 之后 emit（不管重试成功还是失败，只要执行了一次重试就打） | run_id、turn_idx、step_id、tool_name、attempt、status（"success"/"failed"） |
| `retry_exhausted` | Agent 重试循环结束：最后一次重试仍失败 → 放弃重试，工具最终标记 failed 之前 emit | run_id、turn_idx、step_id、tool_name、total_attempts（=1+retry_max）、last_error（最后一次异常 str） |
| `compensate_finish` | Saga 补偿循环内：某个 compensate() 未抛异常 → emit（Agent.run() Phase 2.1 Saga 块已打 `compensate_finish` Journal，现在把 plugin emit 补上） | run_id、step_id、tool_name、status（="success"） |

**验证 Step 2.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.plugin import BasePlugin, PluginProtocol, PluginManager
import dataclasses

# 1. 运行时检查：BasePlugin 是 PluginProtocol
@dataclasses.dataclass
class TestPlugin(BasePlugin):
    name: str = 'test_resilience'
    priority: int = 50
p = TestPlugin()
assert isinstance(p, PluginProtocol)
print('BasePlugin → PluginProtocol OK')

# 2. 7 个钩子方法存在且可调用（no-op 不抛）
p.on_circuit_open(run_id='r1', circuit_key='k1', failure_count=5, open_window_seconds=30)
p.on_circuit_half_open(run_id='r1', circuit_key='k1')
p.on_circuit_closed(run_id='r1', circuit_key='k1', probe_success=True)
p.on_retry_before(run_id='r1', turn_idx=1, step_id='s1', tool_name='t1', attempt=1, next_delay_ms=100, error_type='TimeoutError')
p.on_retry_after(run_id='r1', turn_idx=1, step_id='s1', tool_name='t1', attempt=1, status='failed')
p.on_retry_exhausted(run_id='r1', turn_idx=1, step_id='s1', tool_name='t1', total_attempts=4, last_error='timeout')
p.on_compensate_finish(run_id='r1', step_id='s1', tool_name='t1', status='success')
print('7 个 resilience 钩子 no-op 调用 OK')

# 3. PluginManager.emit 能正确分发（不抛，记日志即可）
class CapturePlugin(BasePlugin):
    name: str = 'cap'
    priority: int = 10
    def __post_init__(self): self.events = []
    def on_circuit_open(self, **kw): self.events.append(('open', kw['circuit_key']))
    def on_retry_exhausted(self, **kw): self.events.append(('exhausted', kw['tool_name']))

cap = CapturePlugin()
pm = PluginManager()
pm.register(cap)
pm.emit('circuit_open', run_id='r1', circuit_key='svc_a', failure_count=5, open_window_seconds=30)
pm.emit('retry_exhausted', run_id='r1', turn_idx=1, step_id='s1', tool_name='flaky', total_attempts=4, last_error='e')
assert cap.events == [('open', 'svc_a'), ('exhausted', 'flaky')]
print('PluginManager.emit 分发 resilience 事件 OK')

print('=== Step 2.V 通过 ===')
"
```

**回滚方式**：
- PluginProtocol 删 7 个方法签名 + 恢复原生命周期注释
- BasePlugin 删 7 个 no-op 默认实现
- 风险：0（纯新增接口，不调用不生效；Phase 2.1 Saga 块漏的 compensate_finish emit 没加的话只是少个事件，不影响功能）

---

## Step 3：atlas/core/circuit_breaker.py 新建 — CircuitBreakerRegistry 三态状态机

**目的**：核心 resilience 基础设施。统一管理所有 circuit_key 的三态（Closed/Open/Half-Open）转换、连续失败计数、冷却窗口计时，对外暴露 `allow_request()` / `on_success()` / `on_failure()` / `snapshot()` 四个 API。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/circuit_breaker.py`（**新建**） | 模块内 3 个常量 + 1 dataclass + 1 Registry 类：<br>常量：`CLOSED = "closed"`、`OPEN = "open"`、`HALF_OPEN = "half_open"`<br>`@dataclass class _State`：`state: str = CLOSED`、`failure_count: int = 0`、`open_since_ts: float = 0.0`、`half_open_inflight: int = 0`（半开放已放行探测请求数，用于保证半开放只放 1 个）<br>`@dataclass class CircuitBreakerRegistry`：<br>  字段：`failure_threshold: int = 5`（N=5）、`open_window_seconds: int = 30`（W=30s）、`_states: dict[str, _State] = field(default_factory=dict)`、`_lock: threading.Lock = field(default_factory=threading.Lock)`、`_external_plugin_manager: PluginManager \| None = None`（可选注入，用于状态变更时 emit 插件事件；None 时不 emit，只改内部状态）<br>  私有辅助 `_get(key) -> _State`：不存在就新建 CLOSED 状态<br>  方法 1 `allow_request(circuit_key: str, run_id: str \| None = None) -> tuple[bool, str \| None, str \| None]`：返回 `(是否放行, 当前状态放行后值或 None, 拒绝原因/放行说明字符串)`。核心逻辑：<br>    ① Open 状态：`now - open_since_ts >= W` → 切 Half-Open + inflight=0 + emit circuit_half_open → 放行（return True, HALF_OPEN, "半开放首次探测放行"）；否则 return False, None, `"熔断器 OPEN，剩余 N 秒"`<br>    ② Half-Open 状态：`half_open_inflight >= 1` → 拒绝（拥挤），return False, None, `"半开放仅放行 1 个探测请求，其余拒绝"`；否则 inflight +=1 → 放行 return True, HALF_OPEN, "半开放探测放行"<br>    ③ Closed 状态：直接放行 return True, CLOSED, None<br>  方法 2 `on_success(circuit_key: str, run_id: str \| None = None) -> None`：<br>    ① Half-Open：切 CLOSED，清零 failure_count、inflight → emit circuit_closed(probe_success=True)<br>    ② Closed：failure_count = 0（清零，不 emit，避免高频）<br>  方法 3 `on_failure(circuit_key: str, run_id: str \| None = None) -> None`：<br>    ① Half-Open：直接切 Open，open_since_ts=now，inflight=0 → emit circuit_open(failure_count, W)<br>    ② Closed：failure_count += 1；达到 threshold → 切 Open，open_since_ts=now → emit circuit_open<br>    ③ Open：不计（已开了，无需再计）<br>  方法 4 `snapshot() -> dict`：返回 Dashboard 需要的 3 块数据：<br>    `{"circuit_breakers": [{key, state, failure_count, open_remain_secs, ...}], "alerts": [...], "retry_logs": []}`（retry_logs 留空，由 Agent 重试循环单独追加，见 Step 5；alerts 当前只包含 "Open 状态超过 1 分钟未恢复" 的 key 列表）<br>  方法 5 `append_retry_log(entry: dict) -> None`：供 Agent 重试循环写 retry_logs，内部维护最多 200 条 ring buffer | +220 行 |
| `atlas/core/__init__.py`（**若当前存在导出**，否则先查看再决定） | 如果 `__init__.py` 里有 `from atlas.core.xxx import ...` 导出核心类，追加 `from atlas.core.circuit_breaker import CircuitBreakerRegistry`；若当前 `atlas/core/__init__.py` 是空文件则不改 | 0 ~ +2 行 |

**关键代码骨架（allow_request 核心）**：
```python
# atlas/core/circuit_breaker.py allow_request
import threading
import time
from dataclasses import dataclass, field
from typing import Any

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

@dataclass
class _State:
    state: str = CLOSED
    failure_count: int = 0
    open_since_ts: float = 0.0
    half_open_inflight: int = 0

@dataclass
class CircuitBreakerRegistry:
    failure_threshold: int = 5
    open_window_seconds: int = 30
    _states: dict[str, _State] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _plugin_manager: Any = None  # 懒注入，避免与 PluginManager 模块级循环引用
    _retry_logs: list[dict] = field(default_factory=list)

    def _get(self, circuit_key: str) -> _State:
        if circuit_key not in self._states:
            self._states[circuit_key] = _State()
        return self._states[circuit_key]

    def allow_request(self, circuit_key: str, run_id: str | None = None) -> tuple[bool, str | None, str | None]:
        with self._lock:
            s = self._get(circuit_key)
            now = time.time()
            if s.state == OPEN:
                remain = self.open_window_seconds - (now - s.open_since_ts)
                if remain <= 0:
                    # 冷却结束 → Half-Open
                    s.state = HALF_OPEN
                    s.half_open_inflight = 0
                    s.half_open_inflight += 1
                    self._safe_emit("circuit_half_open", run_id=run_id, circuit_key=circuit_key)
                    return (True, HALF_OPEN, "Open 冷却结束，半开放首次探测放行")
                return (False, None, f"熔断器 OPEN。剩余 {int(remain)+1} 秒冷却")
            if s.state == HALF_OPEN:
                if s.half_open_inflight >= 1:
                    return (False, None, "半开放仅放行 1 个探测请求，其余请求拒绝（避免雪崩）")
                s.half_open_inflight += 1
                return (True, HALF_OPEN, "半开放探测放行")
            # CLOSED 直接放行
            return (True, CLOSED, None)
```

**验证 Step 3.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.circuit_breaker import CircuitBreakerRegistry, CLOSED, OPEN, HALF_OPEN
import time

r = CircuitBreakerRegistry(failure_threshold=3, open_window_seconds=1)  # 用小阈值+短窗口便于测试

# 1. 初始 CLOSED → 放行
ok, st, reason = r.allow_request('svc_a')
assert ok and st == CLOSED and reason is None
print(f'1. CLOSED 放行 OK, state={st}')

# 2. Closed 连续 3 次失败 → Open
r.on_failure('svc_a')
r.on_failure('svc_a')
r.on_failure('svc_a')  # 第 3 次 → 切 Open
# 立即再 allow_request → 拒绝（Open）
ok, st, reason = r.allow_request('svc_a')
assert not ok and st is None and 'OPEN' in (reason or '')
print(f'2. N=3 失败切 Open 拒绝 OK: reason={reason}')

# 3. 等 1.2s → Open 冷却结束 → Half-Open，放行 1 个
time.sleep(1.2)
ok, st, reason = r.allow_request('svc_a')
assert ok and st == HALF_OPEN
print(f'3. 冷却→Half-Open OK, state={st}, reason={reason}')
# 半开放第 2 个 → 拒绝
ok2, st2, reason2 = r.allow_request('svc_a')
assert not ok2 and '半开放仅放行 1 个' in (reason2 or '')
print(f'   半开放第 2 个拥挤拒绝 OK: reason={reason2}')

# 4. Half-Open 探测成功 → Closed（清零）
r.on_success('svc_a')
ok3, st3, _ = r.allow_request('svc_a')
assert ok3 and st3 == CLOSED
print(f'4. Half-Open 成功→Closed OK, state={st3}')

# 5. Half-Open 探测失败 → 直接 Open（重计时）
r.on_failure('svc_a'); r.on_failure('svc_a'); r.on_failure('svc_a')  # 先到 Open
time.sleep(1.2)
r.allow_request('svc_a')  # Half-Open 放行 1 个
r.on_failure('svc_a')     # Half-Open 探测失败 → Open
ok5, _, _ = r.allow_request('svc_a')
assert not ok5
print(f'5. Half-Open 失败→Open OK')

# 6. snapshot() 返回 circuit_breakers / alerts / retry_logs 三个 key
snap = r.snapshot()
assert 'circuit_breakers' in snap and 'alerts' in snap and 'retry_logs' in snap
print(f'6. snapshot() 结构 OK: circuit_breakers 条目={len(snap[\"circuit_breakers\"])}')

print('=== Step 3.V 通过 ===')
"
```

**回滚方式**：
- 删 `atlas/core/circuit_breaker.py`
- 撤 `atlas/core/__init__.py` 中对应 export（若加了）
- 风险：0（纯新文件，不 import 不生效；与现有代码无耦合）

---

## Step 4：atlas/core/retry.py 新建 — Full Jitter 退避 + sleep 封装

**目的**：重试延迟计算统一函数，不让 Agent.run() 内手写随机 + 指数逻辑（容易写错 base/cap）。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/retry.py`（**新建**） | 2 个模块级常量 + 2 个函数：<br>常量 `DEFAULT_BASE_SECONDS = 0.1`（100ms 起步）、`DEFAULT_CAP_SECONDS = 10.0`（最大 10s）<br>函数 1 `full_jitter_delay_ms(attempt: int, base_seconds: float = DEFAULT_BASE_SECONDS, cap_seconds: float = DEFAULT_CAP_SECONDS) -> int`：<br>  `import random`、`delay = random.uniform(0, min(cap_seconds, base_seconds * (2 ** attempt)))` → `return int(delay * 1000)`（attempt 从 1 开始：第 1 次重试 attempt=1，第 2 次 attempt=2）<br>函数 2 `sleep_ms(ms: int) -> None`：`time.sleep(ms / 1000.0)`（让 Agent.run() 不直接依赖 time.sleep，便于将来 mock）<br>注释中文 | +40 行 |

**关键代码骨架**：
```python
"""指数退避 + Full Jitter 重试延迟计算。

Full Jitter 公式：delay = random.uniform(0, min(cap, base * 2^attempt))
减少多个客户端同时重试导致的"重试风暴"。
"""
import random
import time

DEFAULT_BASE_SECONDS = 0.1
DEFAULT_CAP_SECONDS = 10.0

def full_jitter_delay_ms(
    attempt: int,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    cap_seconds: float = DEFAULT_CAP_SECONDS,
) -> int:
    """计算第 attempt 次重试的延迟毫秒数（Full Jitter）。

    attempt 从 1 开始计数（第 1 次重试=第一次失败后）。
    """
    if attempt < 1:
        raise ValueError(f"attempt 必须 >=1，实际 {attempt}")
    ceiling = min(cap_seconds, base_seconds * (2 ** attempt))
    delay_seconds = random.uniform(0, ceiling)
    return int(delay_seconds * 1000)

def sleep_ms(ms: int) -> None:
    """按毫秒睡眠（封装 time.sleep，便于将来 mock 或 替换为 asyncio.sleep）。"""
    if ms < 0:
        raise ValueError(f"ms 必须 >=0，实际 {ms}")
    time.sleep(ms / 1000.0)
```

**验证 Step 4.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.core.retry import full_jitter_delay_ms, sleep_ms, DEFAULT_BASE_SECONDS, DEFAULT_CAP_SECONDS

# 1. attempt<1 → ValueError
try:
    full_jitter_delay_ms(0)
    assert False
except ValueError as e:
    assert 'attempt' in str(e)
print('1. attempt<1 抛 ValueError OK')

# 2. 1000 次统计：attempt=1 延迟 ∈ [0, 0.2s]（base=0.1, 2^1=0.2），全部 <= cap
delays = [full_jitter_delay_ms(1) for _ in range(1000)]
assert all(0 <= d <= 200 for d in delays)
avg = sum(delays) / 1000
print(f'2. attempt=1: 范围 OK，均值≈{avg:.0f}ms（期望≈100ms 一半）')

# 3. attempt=10（大指数）：全部 <= cap*1000=10000ms
delays10 = [full_jitter_delay_ms(10) for _ in range(500)]
assert all(d <= 10000 for d in delays10)
print(f'3. attempt=10: 全部 <=10000ms OK，最大={max(delays10)}ms')

# 4. sleep_ms(50) 实际睡眠 >30ms（Windows 调度有误差，放宽阈值）
import time as _t
t0 = _t.perf_counter()
sleep_ms(50)
elapsed_ms = (_t.perf_counter() - t0) * 1000
assert elapsed_ms >= 30, f'实际只睡了 {elapsed_ms:.0f}ms'
print(f'4. sleep_ms(50) 实际睡眠 {elapsed_ms:.0f}ms OK')

# 5. ms<0 → ValueError
try:
    sleep_ms(-1)
    assert False
except ValueError:
    pass
print('5. sleep_ms(-1) 抛 ValueError OK')

print('=== Step 4.V 通过 ===')
"
```

**回滚方式**：
- 删 `atlas/core/retry.py`
- 风险：0（纯新文件，无依赖）

---

## Step 5：Agent.run() 工具执行段接入 — 熔断器前置检查 + 包裹重试循环

**目的**：Phase 2.2 最核心的一步——把现有 Agent.run() 工具执行段（7 步幂等+Saga）包裹成：

```
tool_before_call 之前：
  ① 取 circuit_key = tool.circuit_key or tool.name
  ② registry.allow_request(circuit_key) → 拒绝：打 status=circuit_rejected，跳过执行 + emit on_tool_after_call(circuit_rejected)
  ③ 放行：进入 "重试循环包裹 真正执行"

重试循环包裹（仅 isIdempotent=True 时启用，否则直接单次执行）：
  for attempt in range(retry_max + 1):
    try:
      result = execute()
      registry.on_success(circuit_key)
      break
    except Exception as e:
      registry.on_failure(circuit_key)
      if 非幂等 或 type(e) not in retry_on 或 attempt >= retry_max:
        标记失败 + break
      emit retry_before → sleep_ms(delay) → emit retry_after
  全部重试失败 → emit retry_exhausted
之后走原有 幂等 cache_set + saga_stack 入栈 + tool_after_call 逻辑
```

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/agent.py` Agent `__init__` 构造函数末尾参数列表 | 追加 2 个末尾 keyword 参数：<br>1. `circuit_breaker_registry: CircuitBreakerRegistry \| None = None`（None → 自动 `CircuitBreakerRegistry()`；dashboard publish() 时从 agent 上拿这个 registry 做 snapshot）<br>2. `auto_default_registry: bool = True`（保留默认 True 即可）<br>`__init__` 内部：`self.circuit_breaker_registry = circuit_breaker_registry or (CircuitBreakerRegistry() if auto_default_registry else None)` | +6 行 |
| `atlas/core/agent.py` 顶部 imports | 追加 2 个 import：<br>`from atlas.core.circuit_breaker import CircuitBreakerRegistry`<br>`from atlas.core.retry import full_jitter_delay_ms, sleep_ms` | +2 行 |
| `atlas/core/agent.py` Agent.run() 工具执行段（核心改动） | 现有 7 步的**Step 3 幂等 cache_check 之前、Step 2 tool_before_call emit 之后**插入 2 个新步骤 + 包裹 Step 4 真正执行为重试循环。具体结构在下面"关键代码骨架"给出 | +90 行（先 copy 现有 Step 3~5 代码，再改写；原代码不删先注释释放旁边，V 通过再删） |
| `atlas/core/agent.py` Agent.run() Saga 补偿块（compensate 成功分支） | Phase 2.1 已有 `self._journal_append(run_id, "compensate_finish", {...})`；在其旁边补一句 `self.plugin_manager.emit("compensate_finish", run_id=run_id, step_id=sid, tool_name=tname, status="success")`（Step 2 钩子补齐） | +1 行 |

**关键代码骨架（工具执行段插入逻辑，先 copy 原 Step 1~7 代码再改写）**：
```python
# ======== 以下插入位置：Step 2 tool_before_call emit 结束之后，Step 3 幂等 cache_check 之前 ========

# Step NEW-1: 熔断器前置检查
circuit_key = tool.circuit_key or tool_name
circuit_registry = self.circuit_breaker_registry
allowed, circuit_state_after, circuit_reason = True, None, None
if circuit_registry is not None:
    allowed, circuit_state_after, circuit_reason = circuit_registry.allow_request(circuit_key, run_id=run_id)

if not allowed:
    # 熔断器拒绝 → 直接构造失败结果，跳过执行 + 不入 saga_stack + 不 retry
    result = f"工具执行被熔断器拒绝: {circuit_reason or 'circuit open'}"
    tool_status = "circuit_rejected"
    tool_duration_ms = 0
    # 跳转 Step 5 saga_stack 判断（自然跳过，因为 status!='success'）和 Step 6/7 回填
    # 注意：这里不要写幂等 cache（拒绝不是真实执行结果，下次可能恢复）

else:
    # Step NEW-2: 熔断器放行 → 用重试循环包裹真正执行（替换原 Step 4 的单次 try/except）
    tool_start = time.time()
    result = None
    last_error: Exception | None = None
    final_attempt = 0
    actual_retry_max = 0
    if tool.isIdempotent:
        actual_retry_max = tool.retry_max  # 幂等：按 Tool 声明
    # 否则 actual_retry_max=0 → 只跑 1 次（不重试）

    for attempt in range(actual_retry_max + 1):
        final_attempt = attempt  # 1+retry_max 中最后一次 attempt 下标
        try:
            # ---- 真正执行（copy 原 Step 4 的 execute 代码，保持 cleaned_args 逻辑不变）----
            cleaned_args = tool_input if not isinstance(fn["arguments"], str) else json.dumps(tool_input, ensure_ascii=False)
            result = self.tools.execute(tool_name, cleaned_args)
            # 成功 → 熔断器 on_success（不管这次是原执行还是重试成功）
            if circuit_registry is not None:
                circuit_registry.on_success(circuit_key, run_id=run_id)
            tool_status = "success"
            last_error = None
            break  # 成功跳出重试循环
        except Exception as e:
            last_error = e
            # 失败 → 熔断器 on_failure（每一次失败都计，包括重试的每一次）
            if circuit_registry is not None:
                circuit_registry.on_failure(circuit_key, run_id=run_id)

            # 判断是否还能重试：幂等 + 异常在白名单 + 还有剩余重试次数
            is_retryable = (
                tool.isIdempotent
                and any(isinstance(e, et) for et in tool.retry_on)
                and attempt < actual_retry_max  # 注意 <，不是 <=；range 末尾那次不算重试
            )
            if not is_retryable:
                result = f"工具执行失败: {e}"
                tool_status = "failed"
                # 所有重试耗尽？
                if tool.isIdempotent and attempt >= actual_retry_max and actual_retry_max > 0:
                    # emit retry_exhausted
                    self._emit_and_journal(
                        run_id, "retry_exhausted",
                        payload={
                            "turn_idx": step_no,
                            "step_id": step_id,
                            "tool_name": tool_name,
                            "total_attempts": actual_retry_max + 1,
                            "last_error": str(e),
                        },
                        turn_idx=step_no,
                        step_id=step_id,
                        tool_name=tool_name,
                        total_attempts=actual_retry_max + 1,
                        last_error=str(e),
                    )
                break  # 不能重试，跳出循环

            # 可以重试 → emit retry_before + sleep + retry_after（emit 到 plugin + journal + registry.retry_logs）
            attempt_no = attempt + 1  # 给用户看的"第几次重试"，从 1 开始
            delay_ms = full_jitter_delay_ms(attempt_no)
            self._emit_and_journal(
                run_id, "retry_before",
                payload={
                    "turn_idx": step_no, "step_id": step_id, "tool_name": tool_name,
                    "attempt": attempt_no, "next_delay_ms": delay_ms,
                    "error_type": type(e).__name__, "error": str(e),
                },
                turn_idx=step_no, step_id=step_id, tool_name=tool_name,
                attempt=attempt_no, next_delay_ms=delay_ms,
                error_type=type(e).__name__,
            )
            # 写入 registry 的 retry_logs ring buffer（Dashboard 用）
            if circuit_registry is not None and hasattr(circuit_registry, "append_retry_log"):
                circuit_registry.append_retry_log({
                    "ts": time.time(), "run_id": run_id, "step_id": step_id,
                    "tool_name": tool_name, "attempt": attempt_no,
                    "next_delay_ms": delay_ms, "error_type": type(e).__name__,
                })
            sleep_ms(delay_ms)
            # retry_after（状态=这次重试后的结果，下一轮循环就知道了；这里先标 failed 因为刚 catch 异常）
            self._emit_and_journal(
                run_id, "retry_after",
                payload={
                    "turn_idx": step_no, "step_id": step_id, "tool_name": tool_name,
                    "attempt": attempt_no, "status": "failed",
                },
                turn_idx=step_no, step_id=step_id, tool_name=tool_name,
                attempt=attempt_no, status="failed",
            )
            # 下一次 for 循环迭代 → 下一次重试

    tool_duration_ms = int((time.time() - tool_start) * 1000)

    # 执行成功才写幂等 cache（失败/重试耗尽都不写，下次能重新尝试）
    if self.journal and tool_status == "success":
        self.journal.cache_set(key, result)

# ======== 以下恢复原 Step 5 saga_stack 判断（tool.hasSideEffects 那段）+ Step 6 memory回填 + Step 7 tool_after_call emit 逻辑 ========
# 注意：tool_after_call 的 status 现在四态：success / failed / idempotency_hit / circuit_rejected
```

**验证 Step 5.V**：
```powershell
cd e:\Learn_Project\Atlas
# 临时写一个 _step5_verify.py 文件（避免 shell 多行转义），包含：
# 1. CircuitBreakerRegistry(failure_threshold=2, open_window_seconds=1) 注入 Agent
# 2. FakeTool：每次调用抛 TimeoutError（幂等，retry_max=2）→ 验证 retry_before/retry_after 各 2 次 + retry_exhausted 1 次
# 3. 断言：工具最终 status=failed，熔断器 failure_count 达到阈值后切 Open（circuit_rejected）
# 4. 另一个场景：FlakyTool 第 3 次调用才成功 → 重试成功，status=success，熔断器 on_success 计数清零
python -c "
import sys
sys.path.insert(0, '.')
# 详见实现时的 _step5_verify.py，本命令只做 import 层检查
from atlas.core.agent import Agent
from atlas.core.circuit_breaker import CircuitBreakerRegistry
from atlas.core.retry import full_jitter_delay_ms, sleep_ms
print('Step 5 import 层 OK：Agent/CircuitBreakerRegistry/retry 互相引用无循环')
print('=== Step 5.V import 验证通过（详细断言写在 _step5_verify.py）===')
"
```

**回滚方式**：
- Agent `__init__` 撤 circuit_breaker_registry 参数 + 字段
- Agent.run() 工具执行段：回退到原 7 步（幂等+Saga）版本（**实施时先 copy 原代码不删注释放旁边，确认新代码跑通 V2 再删**）
- 顶部撤 2 个 import
- 风险：中（最高）——改动 Agent.run() 工具执行核心段，虽然是包裹插入但涉及多层 try/except 嵌套；必须先单跑 `saga_demo --mock` 确保 Saga 补偿没被破坏，再跑新 demo

---

## Step 6：Dashboard 增强 — RESILIENCE 横幅三表 + DATASTATE 扩 key + publish() 合并 Registry snapshot

**目的**：Dashboard 顶部新增 "RESILIENCE" 横幅横幅区域（三个并排表格：熔断器状态 / 最近重试记录 / 活动告警），实时可视化 resilience 状态。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/cli/dashboard.py` DATASTATE 常量 dict | 扩 3 个 key：`"circuit_breakers": []`、`"retry_logs": []`、`"alerts": []` | +3 行 |
| `atlas/cli/dashboard.py` publish() 函数签名末尾 | 追加可选参数 `circuit_breaker_registry: CircuitBreakerRegistry \| None = None`（默认 None，不破坏现有调用）<br>publish() 末尾（with _DATASTATE_LOCK 块内）追加：<br>`if circuit_breaker_registry: snap = circuit_breaker_registry.snapshot(); DATASTATE["circuit_breakers"] = snap.get("circuit_breakers", []); DATASTATE["retry_logs"] = snap.get("retry_logs", []); DATASTATE["alerts"] = snap.get("alerts", [])` | +8 行 |
| `atlas/cli/dashboard.py` _HTML_TEMPLATE CSS 头部 | 追加 `.banner`（RESILIENCE 横幅容器：顶部 header 下方、columns 上方，背景 #181c28 红橙色警示风格）、`.banner table`（三表并排 33% 宽）、`.banner th`、`.banner td`、`.st-open`（Open 状态红色背景）、`.st-half`（Half-Open 黄色背景）、`.st-closed`（Closed 绿色背景）样式 | +30 行 |
| `atlas/cli/dashboard.py` _HTML_TEMPLATE HTML body 结构 | `<div class="header">` 之后 `<div class="columns">` 之前插入 `<div class="banner">` 区，内含三个 `<table>`：<br>左表 `#tbl-circuit`（熔断器：circuit_key / 状态 / fail_count / 剩余秒 / 最近状态变时间）<br>中表 `#tbl-retry`（最近重试记录：时间 / tool / 第几次 / error_type / 等待 ms）<br>右表 `#tbl-alerts`（活动告警：严重级别 / 描述 / 开始时间） | +35 行 |
| `atlas/cli/dashboard.py` _HTML_TEMPLATE JS 部分 | 追加三个 render 函数 `renderCircuit(data.circuit_breakers)` / `renderRetry(data.retry_logs)` / `renderAlerts(data.alerts)`；在现有 `update(data)` 函数末尾追加调用三个 render | +50 行 |
| `examples/dashboard_demo.py`（若当前存在） | publish() 调用处传 `circuit_breaker_registry=agent.circuit_breaker_registry`；**先读 dashboard_demo.py 现有代码再改** | +1 ~ +3 行 |

**Dashboard payload 契约（WebSocket 推送时 data.* 的字段结构）**：

```typescript
// circuit_breakers[] - 每个熔断器的当前状态
interface CircuitBreakerRow {
  key: string;                 // circuit_key
  state: "closed" | "open" | "half_open";
  failure_count: number;       // 当前连续失败次数
  open_remain_secs: number;    // Open 剩余冷却秒数（Closed/Half-Open 时 = 0）
  last_state_change_ts: number | null;  // 最近状态切换 unix 秒（可空）
}

// retry_logs[] - 最近 200 条重试事件（ring buffer，新的在尾）
interface RetryLogRow {
  ts: number;                  // unix 秒
  run_id: string;
  step_id: string;
  tool_name: string;
  attempt: number;             // 第几次重试（从 1 开始）
  next_delay_ms: number;       // 本次 sleep 毫秒
  error_type: string;          // e.g. "TimeoutError"
}

// alerts[] - 活动告警列表（熔断超过 1 分钟、重试耗尽次数>0 等）
interface AlertRow {
  level: "warning" | "critical";
  message: string;             // e.g. "熔断器 svc_a Open 状态已持续 92s"
  since_ts: number;            // 告警起始 unix 秒
}
```

**验证 Step 6.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.cli.dashboard import publish, DATASTATE, _INDEX_HTML
from atlas.core.circuit_breaker import CircuitBreakerRegistry

# 1. publish() 新参数不传 → 不破坏旧调用（backward compat）
publish('dummy-run', None, None)
assert 'circuit_breakers' in DATASTATE and 'retry_logs' in DATASTATE and 'alerts' in DATASTATE
print('1. publish() 不传 registry → DATASTATE 新 key 存在（空列表）OK')

# 2. publish() 传 registry → 三 key 有数据
r = CircuitBreakerRegistry(failure_threshold=2, open_window_seconds=60)
r.on_failure('slow_svc'); r.on_failure('slow_svc')  # → Open
r.append_retry_log({'ts': 1, 'run_id':'r1','step_id':'s1','tool_name':'t','attempt':1,'next_delay_ms':100,'error_type':'TimeoutError'})
publish('dummy-run2', None, None, circuit_breaker_registry=r)
assert len(DATASTATE['circuit_breakers']) >= 1 and any(x['state']=='open' for x in DATASTATE['circuit_breakers'])
assert len(DATASTATE['retry_logs']) == 1
print(f'2. publish() 传 registry → circuit_breakers={len(DATASTATE[\"circuit_breakers\"])}, retry_logs={len(DATASTATE[\"retry_logs\"])} OK')

# 3. HTML 模板包含 banner 区域
assert 'RESILIENCE' in _INDEX_HTML and 'tbl-circuit' in _INDEX_HTML and 'tbl-retry' in _INDEX_HTML and 'tbl-alerts' in _INDEX_HTML
print('3. _INDEX_HTML 包含 RESILIENCE 横幅 + 三表 ID OK')

print('=== Step 6.V 通过 ===')
"
```

**回滚方式**：
- DATASTATE 删 3 个 key
- publish() 撤 circuit_breaker_registry 参数 + 对应赋值块
- _HTML_TEMPLATE 删 banner 区和三个 render 函数
- dashboard_demo.py 撤对应参数（若加了）
- 风险：低（纯前端展示 + publish 扩参数，不破坏内核；publish() 现有调用不传新参数走默认值 = 空列表，前端空表显示 "(无数据)" 兜底）

---

## Step 7：examples/circuit_retry_demo.py 新建 — FlakyTool 驱动完整链路

**目的**：EP 演示模式 + 自动化断言（--mock 模式 100% 可重复），一个脚本覆盖：熔断器三态切换完整路径 + 幂等工具重试成功/耗尽两场景 + circuit_rejected 状态 + SideEffectTracker 不变（circuit_rejected 没真正执行，不产生副作用）+ Dashboard publish（--dashboard 开关）。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `examples/circuit_retry_demo.py`（**新建，< 180 行**） | 结构：<br>1. argparse：`--mock`（FakeLLM，默认）、`--plain`（渲染器切换）、`--dashboard`（后台起 Dashboard HTTP+WS）<br>2. 工具注册 3 个：<br>   a. `flaky_api(call_id)`：幂等（isIdempotent=True, retry_max=3, retry_on=(RuntimeError,)）；内部 `FLAKY_STATE: dict[str, int]` 记录每个 call_id 失败次数；第 1/2/3 次调用抛 RuntimeError("flaky timeout")，第 4 次返回 "success-ok"（对应 retry_max=3，第四次=原 1 次+重试 3 次？视具体断言调整次数）<br>   b. `unreliable_svc(req_id)`：幂等，retry_max=1，但每 call 永远抛 TimeoutError（用于验证 retry_exhausted）<br>   c. `credit_card_charge(txn_id, usd)`：非幂等有副作用，hasSideEffects=True，isIdempotent=False，带 compensate=reverse_credit（和 saga_demo 一致，用于证明 circuit_rejected 时不产生副作用）<br>3. FakeLLM 构造（--mock）：第 1 轮顺序 tool_calls = [flaky_api, unreliable_svc, credit_card_charge]，第 2 轮 LLM 直接给出最终答案（无 tool_calls），让 Agent 正常 agent_finish 不触发 Saga<br>4. Agent 初始化注入 CircuitBreakerRegistry(failure_threshold=3, open_window_seconds=9999)（长窗口便于测试 Open 后保持状态）<br>5. 调用 agent.run("处理订单 XYZ")<br>6. 断言块（--mock 下）：<br>   ① flaky_api 实际 func 调用次数 = 原 1 次 + 重试 3 次 = 4 次（最终成功）<br>   ② unreliable_svc func 调用次数 = 原 1 次 + 重试 1 次 = 2 次（耗尽，retry_exhausted）<br>   ③ CircuitBreakerRegistry.snapshot() 中对应 flaky_api 的 failure_count=0（on_success 清零）<br>   ④ 再人工调 3 次 unreliable_svc 手动触发熔断→再请求→circuit_rejected，此时 credit_card_charge 的 SideEffectTracker 记录数不变（熔断器拒绝不真正执行）<br>7. `--dashboard` 时：后台线程起 `run_server(8765)`，循环 `publish(run_id, agent.journal, agent.trace, agent.circuit_breaker_registry)` 推送 3 秒后提示用户打开浏览器 | +175 行 |

**验证 Step 7.V**：
```powershell
cd e:\Learn_Project\Atlas
python examples/circuit_retry_demo.py --mock --plain
# 期望输出末尾：
#   [断言] flaky_api 调用次数=4（原1次+重试3次） OK
#   [断言] unreliable_svc retry_exhausted 事件存在 OK
#   [断言] flaky_api 熔断器清零 OK
#   [断言] circuit_rejected 不产生副作用 OK
#   === circuit_retry_demo --mock 全部断言通过 ===
# 退出码 0
```

语法验证：
```powershell
cd e:\Learn_Project\Atlas
python -c "import ast; ast.parse(open('examples/circuit_retry_demo.py', encoding='utf-8').read()); print('语法 OK')"
```

**回滚方式**：
- 删 `examples/circuit_retry_demo.py`
- 风险：0（纯 demo 文件，不影响内核）

---

## 整体验证矩阵

### V1：7 步单独 import + 断言
```powershell
# 每步对应 Step X.V 脚本（7 个）。最后统一再跑：
cd e:\Learn_Project\Atlas
python -c "
import atlas.tools.base
import atlas.core.plugin
import atlas.core.circuit_breaker
import atlas.core.retry
import atlas.core.agent
import atlas.cli.dashboard
import ast; ast.parse(open('examples/circuit_retry_demo.py', encoding='utf-8').read())
print('V1 7 模块全 import + demo 语法 OK')
"
```

### V2：circuit_retry_demo --mock + 4 大断言 + plain 渲染
```powershell
cd e:\Learn_Project\Atlas
python examples/circuit_retry_demo.py --mock --plain
```

### V3：9 个 demo 真实 LLM 回归（确保 resilience 接入不破坏老 demo）
```powershell
cd e:\Learn_Project\Atlas
python examples/quickstart.py --plain
python examples/multi_agent_demo.py --plain
python examples/parallel_subagent_demo.py --plain
python examples/tool_abi_demo.py --plain
python examples/journal_replay_demo.py --plain
python examples/terminator_demo.py --plain
python examples/plugin_cost_demo.py --plain
python examples/saga_demo.py --mock --plain
python examples/compact_demo.py --mock --plain
```

---

## 风险矩阵

| Step | 风险等级 | 内容 | 回滚 / 缓解 |
|------|---------|------|------------|
| 1 | 低 | 只有"非幂等且 retry_max>0"注册才 ValueError；现有 BUILTIN_TOOLS 全 isReadOnly=True→isIdempotent=True，不受影响 | 撤 3 字段 + 撤约束 |
| 2 | 0 | 纯新增 7 个钩子接口，不 emit 不生效 | 删 7 方法签名 + no-op |
| 3 | 0 | 纯新模块 CircuitBreakerRegistry，不 import 不生效 | 删文件 |
| 4 | 0 | 纯新模块 retry 工具函数 | 删文件 |
| 5 | 中（最高） | Agent.run() 工具执行段插入熔断器检查 + 包裹重试循环，与现有幂等+Saga 三重嵌套；try/except 层级变复杂 | **实施时先 copy 原工具执行段 7 步代码，加注释放旁边，新代码并行写；V2 通过再删旧注释代码** + 单独回归 saga_demo --mock 确认 Saga 补偿没被破坏 |
| 6 | 低 | 前端展示 + publish 扩参数；不传新参数 = 空列表兜底，不破坏现有 dashboard_demo | 撤 banner 区 + 撤 publish 新参数 |
| 7 | 低 | 纯 demo 文件 + 内置断言 | 删文件 |

---

## 交付顺序（按依赖）

Step 1 → Step 2 → Step 3 → Step 4（四步之间**互不依赖，可并行实施**）→ Step 5（依赖前四步全 OK）→ Step 6（只依赖 Step 3，可与 Step 5 并行）→ Step 7（依赖 1/2/3/4/5 五步全 OK）。

每步完成立即跑对应 Step X.V；Step 5.V 通过后必须额外回归 `saga_demo --mock --plain` 确保 Saga 补偿没被重试循环破坏，再开 Step 7。

# Phase 2.1 收尾实施计划：幂等键 + Saga 补偿注册 + 独立 Evaluator

> grill-me 3 轮共享理解冻结（Q1×5 + Q2×4 + 最后确认）。2026-08-12 定稿。
>
> 实施原则（经验 1246097 失败教训 + 用户规则）：
> 1. 每步独立：完成后**立即**有一行 `python -c` 验证，不做"半成品等下一步"
> 2. 严格单向依赖：Step 编号越大依赖越多，绝不反向依赖
> 3. Windows 纯 Python 验证命令（不 head/curl 类 Unix 工具）
> 4. 不考虑 fallback，消除 fallback 触发场景：非幂等有副作用无补偿直接 ValueError 拒注册
> 5. 代码迁移先 copy 再改写：Agent.run() 工具执行段先把现有代码全读下来再插幂等+Saga 逻辑，不漏细节

---

## 0. 决策速查表（10 项 grill-me 冻结）

| 决策点 | 最终选择 |
|--------|---------|
| 幂等键生成 | 用户显式传（工具入参加 `_idempotency_key` 字段）→ 不传 fallback `_derive_idempotency_key(run_id, tool_name, tool_input_dict)` = SHA-1 十六进制 |
| 幂等键存储接口 | `JournalProtocol` 扩 `cache_get(key) / cache_set(key, value)`；InMemoryJournal 额外 `_cache: dict[str, str]`；SQLite 后续单独 cache 表 |
| Tool.compensate 注入 | Tool dataclass 追加 `compensate: Callable[[dict, Any, dict], dict] \| None = None` 字段；`Toolkit.register()` 同名参数接收 |
| 注册约束（升级） | Phase 1 只是 warning → 现在直接 **ValueError 拒绝**：`hasSideEffects=True and isIdempotent=False and compensate is None` → 抛 ValueError，消息包含 tool_name |
| Saga 触发范围 | **仅在无最终答案时触发**（Agent.run 因终止条件/工具异常链 导致最后没有返回 content 最终答案时）；Agent 成功产出最终答案时**不补偿** |
| Saga 栈与顺序 | `_saga_stack: list[tuple[step_id, tool_name, compensate_func, compensate_input]]`；触发时 LIFO 遍历逐个 compensate |
| 补偿失败重试 | **不重试一次性**：compensate() 抛或返回错误直接算失败，emit `on_compensate_failed(retry_count=1, next_retry_at=None)`，继续下一个补偿动作。最终答案里包含"补偿失败 step 列表"提示人工介入 |
| Evaluator 实现 | `Evaluator Protocol` + 内置 `ArtifactSchemaEvaluator(contracts: list[tuple[str, dict]])`；Protocol `evaluate(result: str, events: list[JournalEvent], contracts=None)` → `tuple[bool, list[str]]` |
| Evaluator 注入点 | **内核外独立调用**，不进 Agent.run() 主循环：用户 `result = agent.run(task)` 之后自己 `ok, reasons = evaluator.evaluate(result, agent.journal.get_events(run_id))` |
| 顶层导出 | `atlas/__init__.py` 只 export `Evaluator` Protocol + `ArtifactSchemaEvaluator`；其余（`_derive_idempotency_key`/SideEffectTracker/SagaError）留各自内部路径 |
| saga_demo 行数 | **放宽 < 150 行**（要演示 2 个副作用工具 + 2 个 compensate + SideEffectTracker + 断言） |

---

## 1. 依赖顺序与整体验证

```
Step 1 Tool ABI 补偿字段 + 注册约束升级 ─── 无依赖
Step 2 JournalProtocol.cache_get/set + InMemory 实现 ─── 无依赖
Step 3 atlas.utils.idempotency 派生 hash 工具 ─── 无依赖
Step 5 Evaluator Protocol + ArtifactSchemaEvaluator + __init__.py 导出 ─── 无依赖（可与 1/2/3 并行）
        │
        ▼
Step 4 Agent.run() 接入：幂等键 cache 命中跳过执行 + SagaStack LIFO 补偿 ─── 依赖 1/2/3
        │
        ▼
Step 6 saga_demo.py + SideEffectTracker ─── 依赖 1/2/3/4

验证层（全部完成后）：
  V1：每步 import 验证（6 × python -c）
  V2：saga_demo --mock 断言（SideEffectTracker 清空 + 补偿失败列表正确）
  V3：7 个 demo 真实 LLM 回归（quickstart / multi_agent / parallel_subagent / tool_abi / journal_replay / terminator / plugin_cost）
```

---

## Step 1：Tool ABI 追加 compensate 字段 + 注册约束升级

**目的**：Phase 1 里 `hasSideEffects+非幂等` 只打 warning，现在升级成"无补偿直接拒绝注册"——消除 fallback 触发场景。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/tools/base.py` Tool dataclass | 末尾追加字段：<br>`compensate: Callable[[dict, Any, dict], dict] \| None = None`<br>字段注释说明 3 参数含义：<br>1. compensate_input = 原工具 input dict（含 _idempotency_key）<br>2. original_result = 原工具成功返回的字符串<br>3. context = `{run_id, step_id, tool_name}` | +6 行 |
| `atlas/tools/base.py` Toolkit.register() 末尾签名 | 追加参数 `compensate: Callable[[dict, Any, dict], dict] \| None = None`，传给 Tool 构造函数 | +2 行 |
| `atlas/tools/base.py` Toolkit.register() 末尾约束 | 替换 Phase 1 的 warning 逻辑：<br>`if hasSideEffects and not isIdempotent:` 块内：<br>  `if compensate is None: raise ValueError(f"Tool {name}: hasSideEffects=True 且非幂等，必须注册 compensate() 函数")`<br>  若有 compensate 就不 warning 直接放行 | +3 行（覆盖旧 warning 3 行，净变更 0） |

**关键代码骨架**：
```python
# atlas/tools/base.py Tool 字段
compensate: Callable[[dict, Any, dict], dict] | None = None
"""补偿函数（仅当 hasSideEffects=True 且 isIdempotent=False 时必须非 None）。
参数 1: compensate_input - 原工具执行时的输入 dict（含 _idempotency_key）
参数 2: original_result - 原工具成功时的返回结果字符串（失败时为 None 或错误描述）
参数 3: context        - dict，包含 run_id/step_id/tool_name 三个上下文字段
返回值: 不做要求，内核只关心是否抛异常。返回非 None 的带 error 键 dict 视为业务失败。
"""
```

**验证 Step 1.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.tools.base import Tool, Toolkit

# 1. 非幂等有副作用 无 compensate → ValueError
tk = Toolkit()
try:
    tk.register(name='bad', description='b', parameters={},
                func=lambda d: 'x', hasSideEffects=True, isIdempotent=False, compensate=None)
    assert False, '应抛 ValueError'
except ValueError as e:
    assert 'compensate' in str(e) and 'bad' in str(e)
    print(f'无补偿拒绝注册 OK: {e}')

# 2. 非幂等有副作用 有 compensate → 正常通过
called = []
def my_comp(ci, or_, ctx): called.append((ci, or_, ctx)); return {}
tk.register(name='good', description='g', parameters={},
             func=lambda d: 'ok', hasSideEffects=True, isIdempotent=False, compensate=my_comp)
good = tk.get('good')
assert good.compensate is my_comp
print('有补偿注册 OK')

# 3. 只读工具（Phase 1 自动强制幂等）不需要 compensate
from atlas.tools.builtin import calculator
tk2 = Toolkit()
tk2.register(func=calculator, name='c', description='c',
             parameters={'type':'object','properties':{'expression':{'type':'string'}},'required':['expression']},
             isReadOnly=True)
assert tk2.get('c').isIdempotent == True and tk2.get('c').compensate == None
print('只读工具免补偿 OK')

print('=== Step 1.V 通过 ===')
"
```

**回滚方式**：
- Tool dataclass 删 compensate 字段
- Toolkit.register() 撤 compensate 参数 + 恢复 Phase 1 仅 warning 逻辑
- 风险：低（只加字段不改现有行为，现有无 compensate 注册的工具只要是 isReadOnly 或 isIdempotent 都不受影响）

---

## Step 2：JournalProtocol 扩 cache_get/cache_set + InMemoryJournal 实现

**目的**：幂等键 key→result 映射走 Journal（不单独加新存储），将来 SQLiteJournal 直接加 cache 表。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/journal/base.py` JournalProtocol Protocol | 末尾追加 2 个方法签名：<br>`def cache_get(self, key: str) -> str \| None: ...`<br>`def cache_set(self, key: str, value: str) -> None: ...` | +3 行 |
| `atlas/journal/base.py` InMemoryJournal dataclass | 字段追加 `_cache: dict[str, str] = field(default_factory=dict)`<br>末尾追加 `cache_get(key)` → `return self._cache.get(key)`<br>追加 `cache_set(key, value)` → `self._cache[key] = value`<br>append / next_seq / replay 不需要动 | +6 行 |

**验证 Step 2.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.journal import InMemoryJournal, JournalProtocol

j = InMemoryJournal()
# 1. 空 key → None
assert j.cache_get('k1') is None
print('空 cache_get OK')

j.cache_set('k1', 'v1')
assert j.cache_get('k1') == 'v1'
j.cache_set('k1', 'v1-override')
assert j.cache_get('k1') == 'v1-override'
print('cache_set + 覆盖 OK')

# 2. Protocol 检查（新加方法也在 runtime_checkable 里）
assert isinstance(j, JournalProtocol)
print('Protocol OK（含新 cache_* 方法）')

# 3. 新加 cache dict 不干扰原事件逻辑（验证不串）
j.append(j.make_event('r1', 'agent_start', {'task': 't'}))
assert len(j.get_events('r1')) == 1 and j.cache_get('k1') == 'v1-override'
print('事件与 cache 互不干扰 OK')

print('=== Step 2.V 通过 ===')
"
```

**回滚方式**：
- JournalProtocol 删两个 cache_* 方法签名
- InMemoryJournal 删 `_cache` 字段和两个方法
- 风险：0（新加接口，不调用不影响任何现有代码）

---

## Step 3：atlas.utils.idempotency 派生 hash 工具

**目的**：统一"未显式传 _idempotency_key 时 fallback 派生"的算法，不让 Agent.run 和 multi_agent._run_subagent 各自写一套。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/utils/idempotency.py`（**新建**） | 单函数 `derive(run_id: str, tool_name: str, input_dict: dict) -> str`：<br>1. 用 `json.dumps(input_dict, sort_keys=True, ensure_ascii=False)` 稳定序列化输入（sort_keys 去 dict key 顺序歧义，ensure_ascii=False 保留中文）<br>2. `payload = f"{run_id}\n{tool_name}\n{serialized_input}"` 构造稳定字符串<br>3. `hashlib.sha1(payload.encode('utf-8')).hexdigest()` 返回 40 字符十六进制<br>4. 顶层常量 `IDEMPOTENCY_USER_KEY = "_idempotency_key"`（用户显式传参时的字段名，避免魔法字符串） | +25 行 |
| `atlas/utils/__init__.py` | 追加 `from atlas.utils.idempotency import derive as derive_idempotency_key, IDEMPOTENCY_USER_KEY` + `__all__` | +2 行 |

**关键代码骨架**：
```python
# atlas/utils/idempotency.py
import hashlib
import json

IDEMPOTENCY_USER_KEY = "_idempotency_key"  # 用户显式传幂等键的字段名

def derive(run_id: str, tool_name: str, input_dict: dict) -> str:
    """稳定派生幂等键：run_id + tool_name + sorted(JSON 化 input) → SHA-1 十六进制。"""
    serialized = json.dumps(input_dict, sort_keys=True, ensure_ascii=False)
    payload = f"{run_id}\n{tool_name}\n{serialized}"
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()
```

**验证 Step 3.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.utils.idempotency import derive, IDEMPOTENCY_USER_KEY

# 1. 相同输入相同输出（稳定）
k1 = derive('r1', 'calc', {'a': 1, 'b': 2})
k2 = derive('r1', 'calc', {'b': 2, 'a': 1})  # dict 顺序互换
assert k1 == k2 and len(k1) == 40
print(f'稳定派生 OK: len=40, 顺序不敏感, k1[:8]={k1[:8]}')

# 2. 不同 run_id / tool_name / input → 全不同
k3 = derive('r2', 'calc', {'a': 1, 'b': 2})  # 不同 run_id
k4 = derive('r1', 'write', {'a': 1, 'b': 2})  # 不同 tool_name
k5 = derive('r1', 'calc', {'a': 1, 'b': 3})   # 不同 input
assert len({k1, k3, k4, k5}) == 4
print('4 维度全不同 OK')

# 3. 用户魔法字段名常量存在
assert IDEMPOTENCY_USER_KEY == '_idempotency_key'
print('IDEMPOTENCY_USER_KEY 常量 OK')

print('=== Step 3.V 通过 ===')
"
```

**回滚方式**：
- 删除 `atlas/utils/idempotency.py`
- `atlas/utils/__init__.py` 撤对应 import
- 风险：0（纯新工具，不调用不影响）

---

## Step 4：Agent.run() 接入幂等键 cache 命中跳过 + SagaStack LIFO 补偿

**目的**：Phase 2.1 最核心的一步——把现在 Agent.run() 的"工具裸执行"包装成"执行前查幂等 cache + 非幂等有副作用 step 进 saga_stack + 最终失败时 LIFO 补偿"。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/agent.py` 顶部 imports | 追加 3 个：<br>`from atlas.utils.idempotency import derive as derive_idempotency_key, IDEMPOTENCY_USER_KEY`<br>`from atlas.tools.base import Tool`（取 tool 的三标志和 compensate） | +2 行 |
| `atlas/core/agent.py` Agent.run() 开头 run_id 之后 | 初始化 2 个新上下文容器：<br>`_saga_stack: list[tuple[str, str, Callable, dict]] = []` （step_id, tool_name, compensate_func, compensate_input）<br>`_compensate_failed: list[tuple[str, str, str]] = []` （step_id, tool_name, error_str） | +3 行 |
| `atlas/core/agent.py` Agent.run() 循环内工具执行段（改动核心） | 在现有"for call in tool_calls:"块内，把 `try: result = self.tools.execute(...)` 扩成 7 步：<br>1. **解析 tool_input（含用户自定义 _idempotency_key 字段）**<br>2. **取 tool 对象**：`tool = self.tools.get(fn['name'])`（Toolkit 内部现在有 `get()`，没就直接抛）<br>3. **幂等键计算**：`key = tool_input.pop(IDEMPOTENCY_USER_KEY, None) or derive_idempotency_key(run_id, tool.name, tool_input)`（注意先 pop 掉魔法字段，不要把它传给真正 tool.func 执行！）<br>4. **查幂等 cache**：`if self.journal and self.journal.cache_get(key) is not None: result = self.journal.cache_get(key)` 跳过执行，并打 `tool_status = 'idempotency_hit'` 不进 saga_stack（Journal/幂等命中不补偿，因为根本没真正执行）<br>5. **真正执行（cache miss）**：现有 try/except 不变，拿到 result 后 `if self.journal: self.journal.cache_set(key, result)`<br>6. **判断是否进 saga_stack**：`if tool.hasSideEffects and not tool.isIdempotent and tool.compensate and tool_status == 'success':` → `_saga_stack.append( (step_id, tool.name, tool.compensate, dict(tool_input)) )`（补偿要 copy tool_input，防止后续被改）<br>7. **tool_after_call status 升级**：原来只有 success/failed，现在加 'idempotency_hit' 作为第三态（插件 on_tool_after_call 的 status 参数就是它） | +60 行（在现有 for 工具执行段内插入，先 copy 原代码再改写） |
| `atlas/core/agent.py` Agent.run() "最终失败 return" 分支之前（终止条件/未给出答案的最后 return） | 插入 **Saga 补偿执行块**：<br>1. `if _saga_stack:` → 先 emit `on_compensate_start(run_id=run_id, failed_step_id=step_id if step_id else 'unknown', compensate_count=len(_saga_stack))`<br>2. `while _saga_stack:` 弹出 LIFO：`step_id, tool_name, comp_func, ci = _saga_stack.pop()`<br>3. `try: comp_func(ci, last_result, {'run_id': run_id, 'step_id': step_id, 'tool_name': tool_name})`<br>   `except Exception as e:` → 记录 `_compensate_failed.append(...)` + emit `on_compensate_failed(run_id, step_id, tool_name, error=str(e), retry_count=1, next_retry_at=None)`<br>4. Saga 跑完后，若 `_compensate_failed` 不为空：在最终返回字符串前追加一段 "补偿失败列表: [...] 请人工介入" 描述 | +40 行 |
| `atlas/core/multi_agent.py` `_run_subagent`（如果它也有独立工具执行路径） | 和 Agent.run 相同处理：幂等键 + saga_stack。**先读 multi_agent.py 确认其工具执行路径**——当前设计是 _run_subagent 内部调用 Agent.run()，所以只需 Agent.run() 改到位就行；如果 _run_subagent 有自己直接 execute 工具的代码，也要同步改 | 0 ~ +40 行 |

**关键代码骨架（仅工具执行段核心，其余上下文逻辑先 copy 原代码）**：
```python
# Agent.run() 循环内 - 工具执行段（替换原有 "for call in tool_calls:" 的主体）
for tool_idx, call in enumerate(tool_calls):
    fn = call["function"]
    # 1. 解析 tool_input（现有 agent.py 已有 json.loads 逻辑，copy 下来不改）
    tool_input_raw = fn["arguments"]
    if isinstance(tool_input_raw, str):
        try:
            tool_input = json.loads(tool_input_raw)
        except json.JSONDecodeError:
            tool_input = {}
    else:
        tool_input = tool_input_raw or {}

    tool_name = fn["name"]
    tool = self.tools.get(tool_name)  # 拿 Tool 对象，三标志 + compensate 从这取
    step_id = f"{run_id}:{step_no}:{tool_idx}"

    # 2. 幂等键：先 pop 魔法字段（不要传给真正工具执行）
    explicit_key = tool_input.pop(IDEMPOTENCY_USER_KEY, None)
    key = explicit_key or derive_idempotency_key(run_id, tool_name, tool_input)

    # 3. 查幂等 cache（命中 → 跳过执行，不进 saga_stack）
    tool_status = "success"
    cached_result = self.journal.cache_get(key) if self.journal else None
    if cached_result is not None:
        result = cached_result
        tool_status = "idempotency_hit"
    else:
        # 4. 真正执行（copy 原 try/except）
        tool_start = time.time()
        try:
            result = self.tools.execute(tool_name, fn["arguments"])
            tool_status = "success"
        except Exception as e:
            result = f"工具执行失败: {e}"
            tool_status = "failed"
        tool_duration_ms = int((time.time() - tool_start) * 1000)

        # 执行成功才写幂等 cache（失败不写，下次能重试）
        if self.journal and tool_status == "success":
            self.journal.cache_set(key, result)

    # 5. 进 saga_stack：只有 非幂等有副作用 且 有 compensate 且 真正执行成功（不是 idempotency_hit 也不是 failed）时
    if (
        tool.hasSideEffects
        and not tool.isIdempotent
        and tool.compensate is not None
        and tool_status == "success"
    ):
        _saga_stack.append((step_id, tool_name, tool.compensate, dict(tool_input)))

    # 6. 回填 memory + record（copy 原逻辑）
    self.memory.add_tool(result, call["id"])
    record.observations.append(result)

    # 7. 插件 + Journal：tool_after_call（status 现在三态）
    self._emit_and_journal(
        run_id, "tool_after_call",
        payload={...},
        turn_idx=step_no, step_id=step_id, tool_name=tool_name, input=tool_input,
        result={"output": result}, duration_ms=tool_duration_ms if tool_status != "idempotency_hit" else 0,
        status=tool_status,
    )
```

**验证 Step 4.V**（用 FakeLLM 构造完整链路：工具执行成功 → 失败 → Saga LIFO 补偿）：
```powershell
cd e:\Learn_Project\Atlas
python -c "
# 验证脚本：构造 2 个副作用工具，先跑成功（进 saga_stack），然后下一轮让 Agent 失败。
# 预期：saga_stack LIFO 补偿按反顺序执行，SideEffectTracker 内 state 清零
# 详细脚本写在临时文件 _step4_verify.py 里跑（避免 shell f-string 转义）
"
```
> 实际实现时创建 `_step4_verify.py`，包含：
> 1. SideEffectTracker（在 demo 前先在验证脚本里临时定义，不用 atlas.utils）
> 2. 注册 2 个非幂等有副作用工具（都带 compensate）
> 3. FakeLLM：第 1 轮调 2 个工具（都成功），第 2 轮返回一个永远 tool_call 到"不存在的工具（或抛异常）"让 Agent 最终无结果
> 4. 断言：2 个 compensate 都被调用（按逆序），tracker.state 为空
> 5. 幂等命中断言：同一个 key 第二次执行，真正 func 调用次数不增加

**回滚方式**：
- Agent.run() 工具执行段：回退到原来"直接 execute + try/except"版本
- Agent.run() 失败 return 分支：删 Saga 补偿块
- 撤 imports 的 derive_idempotency_key
- 风险：中（改动 Agent.run 核心循环，虽然是插入但逻辑多），所以 Step 4.V 必须验证 saga_demo 才能过

---

## Step 5：Evaluator Protocol + ArtifactSchemaEvaluator + 顶层导出

**目的**：落地"不让 Agent 自评"的验证基础设施。最小实现：只 Schema 校验 Evaluator，Protocol 开放。内核外独立调用，不进 Agent.run() 主循环。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/evaluator.py`（**新建**） | 2 个类：<br>1. `Evaluator Protocol`（@runtime_checkable）：方法 `evaluate(result: str, events: list[JournalEvent], contracts: list | None = None) -> tuple[bool, list[str]]`<br>2. `ArtifactSchemaEvaluator dataclass`：<br>   - 字段 `contracts: list[tuple[str, dict]]`（每个是 (artifact_key, json_schema_dict)，artifact_key 对应 subagent_spec.id 或最终答案段内约定的 key）<br>   - `evaluate(result, events, contracts=None)`：<br>     1. try 把 result json.loads 成 dict（单 artifact 也行或 list也行）<br>     2. 按 contracts 每个 (key, schema)：如果 result 里能找到该 key（或每个 list item 包含该 key 的子项），`jsonschema.validate(instance=value, schema=schema)`<br>     3. 不抛 → ok；抛 ValidationError → 收集 reasons 里加 `f"{key}: {e.message}"`<br>     4. 返回 (len(reasons)==0, reasons) | +90 行 |
| `atlas/__init__.py` | 追加 import：<br>`from atlas.core.evaluator import Evaluator, ArtifactSchemaEvaluator`<br>`__all__` 加 `"Evaluator", "ArtifactSchemaEvaluator"` | +3 行 |

**关键代码骨架**：
```python
# atlas/core/evaluator.py
from __future__ import annotations
import json
import jsonschema
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable, Any
from atlas.journal.base import JournalEvent

@runtime_checkable
class Evaluator(Protocol):
    def evaluate(self, result: str, events: list[JournalEvent], contracts: list | None = None) -> tuple[bool, list[str]]: ...

@dataclass
class ArtifactSchemaEvaluator:
    contracts: list[tuple[str, dict]] = field(default_factory=list)

    def evaluate(self, result: str, events: list[JournalEvent], contracts: list[tuple[str, dict]] | None = None) -> tuple[bool, list[str]]:
        contracts = contracts or self.contracts
        reasons: list[str] = []
        # 1. 解析 result：先当 JSON 解析（phase2-1 parallel_gather 返回 JSON 列表）
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            return (True, []) if not contracts else (False, ["无法解析 result 为 JSON，但提供了 contracts"])
        # 2. 统一 items 成列表
        items = parsed if isinstance(parsed, list) else [parsed]
        for idx, item in enumerate(items):
            for key, schema in contracts:
                if key in item:
                    try:
                        jsonschema.validate(instance=item[key], schema=schema)
                    except jsonschema.ValidationError as e:
                        reasons.append(f"item[{idx}].{key}: {e.message}")
        return (len(reasons) == 0, reasons)
```

**验证 Step 5.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas import Evaluator, ArtifactSchemaEvaluator
from atlas.journal import JournalEvent  # 只是 import 下证明 events 参数类型有

# 1. 顶层导出 OK
print('顶层导出 OK')

# 2. ArtifactSchemaEvaluator 通过场景
eval1 = ArtifactSchemaEvaluator([('content', {'type':'string','minLength':1})])
ok, reasons = eval1.evaluate('[{{\"subagent_id\":\"A\",\"artifact\":\"OK_A\",\"content\":\"ok\"}}]', [])
assert ok and reasons == []
print('Schema 通过 OK')

# 3. 不通过场景（minLength=10，而 content='ok' 只有 2 字）
eval2 = ArtifactSchemaEvaluator([('content', {'type':'string','minLength':10})])
ok2, reasons2 = eval2.evaluate('[{{\"content\":\"ok\"}}]', [])
assert not ok2 and reasons2
print(f'Schema 不通过 OK: reasons={reasons2}')

# 4. Protocol 运行时检查
assert isinstance(eval1, Evaluator)
print('Protocol runtime_checkable OK')

print('=== Step 5.V 通过 ===')
"
```

**回滚方式**：
- 删 `atlas/core/evaluator.py`
- `atlas/__init__.py` 撤对应 export
- 风险：0（纯新增，内核不调用，不破坏现有代码）

---

## Step 6：saga_demo.py + SideEffectTracker 辅助

**目的**：EP-4 的"演示模式"——一个脚本一键跑通失败→补偿→成功。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/utils/tracker.py`（**新建**） | `SideEffectTracker dataclass`：<br>内部 `_records: dict[str, dict]`（key=side_effect_id，value={...}）<br>方法：<br>`record(effect_id, metadata)` 写记录<br>`erase(effect_id)` 删记录<br>`exists(effect_id) -> bool`<br>`is_empty() -> bool`（断言 `len(_records)==0`）<br>`__len__()`<br>代码 < 40 行，注释中文 | +40 行 |
| `atlas/utils/__init__.py` | 追加 `from atlas.utils.tracker import SideEffectTracker`，`__all__` 追加 `"SideEffectTracker"` | +2 行 |
| `examples/saga_demo.py`（**新建，目标 < 150 行**） | 结构：<br>1. argparse：`--mock` 开关，`--plain` 渲染切换<br>2. `--mock` 用 FakeLLM 构造：第 1 轮调 2 个工具（write_purchase_order + charge_payment），第 2 轮故意让 FakeLLM 返回 tool_call 到 **FAIL-SHIP**（故意不存在的工具或抛异常业务工具，让本轮失败），触发 Saga<br>3. 注册两个 非幂等有副作用工具（hasSideEffects=True, isIdempotent=False）：<br>   a. `write_purchase_order(po_id, items)` → compensate=delete_purchase_order<br>   b. `charge_payment(txn_id, amount_usd)` → compensate=refund_payment<br>   c. （可选）再加一个 `ship_goods(ship_id)` → compensate=cancel_shipment，工具函数当 ship_id.startswith('FAIL') 时直接抛 RuntimeError（模拟发货服务 5xx）<br>4. 所有副作用工具调用 SideEffectTracker.record()，补偿 erase()<br>5. 补偿断言：`assert tracker.is_empty()`，失败补偿列表包含 ship_goods 的 FAIL-ID<br>6. `--mock` FakeLLM 保证 100% 可重复；真实 LLM 版本只提示（不保证 100% 触发 Saga，因为真实 LLM 可能回答对）<br>7. 用 PlainRenderer/RichRenderer 打印补偿流程 summary 面板 | +140 行 |

**验证 Step 6.V**：
```powershell
cd e:\Learn_Project\Atlas
python examples/saga_demo.py --mock --plain
# 期望输出（--plain）：包含 "[SideEffectTracker 最终 0 条记录，补偿全部成功]" 字样
# 程序退出码 0（不抛异常）
```

语法验证：
```powershell
cd e:\Learn_Project\Atlas
python -c "import ast; ast.parse(open('examples/saga_demo.py', encoding='utf-8').read()); print('语法 OK')"
python -c "from atlas.utils.tracker import SideEffectTracker; t=SideEffectTracker(); t.record('a',{}); t.erase('a'); assert t.is_empty(); print('SideEffectTracker OK')"
```

**回滚方式**：
- 删 `atlas/utils/tracker.py`
- 删 `examples/saga_demo.py`
- `atlas/utils/__init__.py` 撤 SideEffectTracker 导出
- 风险：0（纯新增，不影响内核）

---

## 整体验证矩阵

### V1：6 步单独 import + 断言
```powershell
# 每步对应一个 Step X.V，上面已写（6 个 python -c 脚本）
# 最后统一再跑：
cd e:\Learn_Project\Atlas
python -c "
import atlas.tools.base
import atlas.journal
import atlas.utils.idempotency
import atlas.core.agent
import atlas.core.evaluator
import atlas.utils.tracker
print('V1 6 模块全 import OK')
"
```

### V2：saga_demo --mock + SideEffectTracker 清空 + 补偿失败列表正确
```powershell
cd e:\Learn_Project\Atlas
python examples/saga_demo.py --mock --plain
```

### V3：7 个 demo 真实 LLM 回归
```powershell
cd e:\Learn_Project\Atlas
python examples/quickstart.py --plain
python examples/multi_agent_demo.py --plain
python examples/parallel_subagent_demo.py --plain
python examples/tool_abi_demo.py --plain
python examples/journal_replay_demo.py --plain
python examples/terminator_demo.py --plain
python examples/plugin_cost_demo.py --plain
```

---

## 风险矩阵

| Step | 风险等级 | 内容 | 回滚 / 缓解 |
|------|---------|------|------------|
| 1 | 低 | 只有 非幂等有副作用无 compensate 注册 才抛 ValueError；现有 BUILTIN_TOOLS 都是 isReadOnly=True，不受影响 | 撤 compensate 字段 + 恢复 warning |
| 2 | 0 | 纯新接口 cache_*，不调用不生效 | 删两个方法 |
| 3 | 0 | 纯 hash 工具 | 删文件 |
| 4 | 中（最高） | Agent.run() 工具执行核心段重写，插入 7 步幂等 + saga 逻辑；另有失败 return 前 Saga 补偿块<br>→ **实施时先 copy 原工具执行代码（不删）加注释放旁边，确认新代码跑通 V2 再删** | 回退工具执行段 + 删 Saga 块 |
| 5 | 0 | 纯新增 Evaluator | 删文件 + 撤 export |
| 6 | 低 | 纯 demo + tracker 辅助 | 删两个新增文件 |

---

## 交付顺序（按依赖）

Step 1 → Step 2 → Step 3 → Step 5（并行 Step 1/2/3）→ Step 4 → Step 6。每步完成立即跑对应 Step X.V。Step 4.V 必须通过再开 Step 6（因为 Step 4 是 Saga 补偿的核心实现）。

# Phase 1 地基实施计划（Scope B：解锁链 + 低悬果实）

> grill-me 2 轮盘问 + 3 轮共享理解确认冻结。2026-08-12 定稿。
>
> 实施原则（来自经验 1246097 失败教训）：
> 1. 每步独立：完成后**立即**有一行命令验证，不做"半成品等下一步才看得出来"
> 2. 严格单向依赖：Step 编号越大依赖越多，绝不反向依赖
> 3. 回滚方式明确：每步列出"若验证失败，恢复原状要撤销哪些改动"
> 4. Windows 环境：不使用类 Unix 工具，验证命令统一 `python -c "..."` 纯 Python

## 0. 决策速查表（grill-me 全共享理解）

| 决策项 | 最终选择 |
|--------|---------|
| 范围 | Scope B（5 项）：终止条件 + Journal + 插件接入 + Tool ABI + YAML loader |
| 终止条件组合 API | `|` 运算符重载（`MaxSteps(10) \| Timeout(60_000)`） |
| 终止条件默认策略 | max_steps=10（沿用）**+** timeout_ms=60_000（新增）。cost_cap/token/repetition 默认关 |
| 插件接入深度 | 完整接入：Agent 持有 PluginManager + run_id UUID + LLM 返回 usage+duration + 工具 status/step_id/duration |
| Journal 存储引擎 | InMemoryJournal 内存实现 + JournalProtocol 接口预留，SQLite 不做 |
| Journal 事件结构 | 折中：结构化壳 dataclass（run_id/seq/ts/event_type）+ 自由 payload dict |
| Journal 存储范围 | 默认摘要模式，`save_full_payload=False`，opt-in 开全量 |
| Tool ABI 字段范围 | 三标志（isReadOnly/isIdempotent/hasSideEffects）+ validate_input（jsonschema），compensate/isConcurrencySafe 留 Phase 2.1 |
| Tool 注册约束 | isReadOnly=True → 强制 isIdempotent=True；hasSideEffects=True+isIdempotent=False → warning |
| 日志框架 | 保持标准 logging，修正文档，**不引入 structlog** |
| YAML loader | `atlas.utils.yaml_loader.load_parallel_subagent_config()` 最小实现，demo 切读 YAML |

---

## 1. 依赖顺序与整体验证

```
Step 1 Tool ABI 增强 ──┐
Step 2 日志文档修正 ── 独立步，无依赖，可与 Step 1 并行
Step 3 JournalProtocol + InMemoryJournal ── 独立步，无依赖
Step 4 终止条件组合器 ── 独立步，无依赖
Step 5 LLM 返回 usage+duration ───────────────────── 插件接入前置
Step 6 插件体系接入 Agent.run() ── 依赖 Step 3/4/5
Step 7 最小 YAML loader + demo 切换 ── 独立步，无依赖

验证层（全部完成后执行）：
  V1：每步 import 验证（7 × python -c "..."）
  V2：插件单测风格验证（TokenCostPlugin 真的能累计 cost）
  V3：三个 demo 回归（quickstart / multi_agent / parallel_subagent）
```

---

## Step 1：Tool ABI 增强

**目的**：Tool 加三分类标志（isReadOnly / isIdempotent / hasSideEffects）+ validate_input 方法，是 Phase 2.1 Saga 补偿分类的前置。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/tools/base.py` Tool dataclass | 末尾追加 3 个字段：<br>`isReadOnly: bool = False`<br>`isIdempotent: bool = False`<br>`hasSideEffects: bool = False` | +3 行 |
| `atlas/tools/base.py` Tool 类 | 末尾追加 `validate_input(input: dict) -> None` 方法：<br>1. schema = self.parameters or (self.parameters_factory() if self.parameters_factory else {})<br>2. 若 schema 非空 `jsonschema.validate(input, schema)`，空 schema 跳过 | +8 行 |
| `atlas/tools/base.py` Toolkit.register() | 末尾追加约束校验（`**spec` 解包后）：<br>1. if tool.isReadOnly and not tool.isIdempotent → 把 tool.isIdempotent 改为 True（静默修正）<br>2. if tool.hasSideEffects and not tool.isIdempotent → `logging.warning(...)`（不阻止注册） | +6 行 |
| `atlas/tools/builtin.py` BUILTIN_TOOLS 每个 spec | calculator 追加 `isReadOnly=True`；get_current_time 追加 `isReadOnly=True` | +2 行 |

**关键代码骨架**：
```python
# atlas/tools/base.py
@dataclass
class Tool:
    name: str
    description: str
    parameters: dict = field(default_factory=dict)
    func: Callable[[dict], str] = field(repr=False)
    parameters_factory: Callable[[], dict] | None = None
    # Step 1 新增
    isReadOnly: bool = False
    isIdempotent: bool = False
    hasSideEffects: bool = False

    def validate_input(self, input: dict) -> None:
        schema = self.parameters
        if not schema and self.parameters_factory:
            schema = self.parameters_factory()
        if schema:
            jsonschema.validate(instance=input, schema=schema)
```

**验证 Step 1.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.tools.base import Tool, Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS
# 1. Tool 字段存在且默认值正确
t = Tool(name='x', description='x', parameters={}, func=lambda d: '')
assert t.isReadOnly == False and t.isIdempotent == False and t.hasSideEffects == False
print('默认值 OK')
# 2. isReadOnly → 强制 isIdempotent=True
tk = Toolkit()
spec = {'name': 'r', 'description': 'r', 'parameters': {}, 'func': lambda d: 'x', 'isReadOnly': True}
tk.register(**spec)
assert tk.get('r').isIdempotent == True
print('isReadOnly 自动强制 isIdempotent OK')
# 3. 内置工具标记正确
for spec in BUILTIN_TOOLS:
    assert spec.get('isReadOnly') == True
print('BUILTIN_TOOLS 标记 OK')
# 4. validate_input：正确通过 / 错误抛 ValidationError
t2 = Tool(name='calc', description='calc',
          parameters={'type':'object','properties':{'a':{'type':'number'}},'required':['a']},
          func=lambda d: '')
t2.validate_input({'a': 3})
print('validate_input 正确通过 OK')
try:
    t2.validate_input({})
    assert False, '应该抛异常'
except Exception:
    print('validate_input 错误抛异常 OK')
print('=== Step 1.V 通过 ===')
"
```

**回滚方式**：仅改 `atlas/tools/base.py` + `atlas/tools/builtin.py`，撤销上述 4 个改动点即可。不影响其它模块。

---

## Step 2：日志框架文档修正

**目的**：消除 project_memory.md / DR-5 与实际代码的不一致——实际用标准 logging，文档错误写的是 structlog。

**改动文件**：

| 文件 | 改动点 |
|------|--------|
| `c:\Users\lenovo\.trae-cn\memory\projects\-e-Learn-Project--p2-bedcb9b653fe08227b86\project_memory.md` | 搜索 "structlog" 行，替换为 "标准 Python logging（配合 `extra={}` 结构化字段）" |
| `e:\Learn_Project\Atlas\README.md` 第 337 行附近（DR-5 示例代码） | 保留示例的结构化风格，仅把示例代码上方描述的 "structlog" 改为 "Python 标准 logging"；`log.info/warn/error` 示例函数名不变（标准 logging 也有这些方法） |

**关键代码骨架**：不涉及代码，纯文档。README DR-5 描述文字改为：
```
所有 `log.info/warn/error` 必须（使用 Python 标准 logging，配合 `extra={}` 传入关键字参数实现结构化）：
```

**验证 Step 2.V**：
```powershell
cd e:\Learn_Project\Atlas
findstr /s /i /n "structlog" README.md atlas\ 2>nul
# 期望：无任何匹配（README 和源码目录全改完）
```

**回滚方式**：把两处描述文字改回 structlog。不涉及代码，零风险。

---

## Step 3：JournalProtocol + InMemoryJournal

**目的**：冻结 Journal 抽象接口（后续 SQLite 实现只写 Protocol 实现类，不改业务逻辑），event sourcing 机制在内存跑通。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/journal/__init__.py`（**新建目录 + 新建文件**） | 包入口，导出 Protocol + 两个实现类 | +5 行 |
| `atlas/journal/base.py`（**新建**） | 新建 3 个类：<br>1. `JournalEvent` dataclass（run_id: str, seq: int, ts: str, event_type: str, payload: dict）<br>2. `JournalProtocol` Protocol（`append(event) / get_events(run_id) / replay(run_id) / list_run_ids()`）<br>3. `InMemoryJournal` dataclass 实现 Protocol：内部 `_events: list[JournalEvent]` + `_seq_counter: dict[str, int]` 按 run_id 计数，append 时自增 seq | +110 行 |

**关键代码骨架**：
```python
# atlas/journal/base.py
@dataclass
class JournalEvent:
    run_id: str
    seq: int
    ts: str
    event_type: str
    payload: dict

class JournalProtocol(Protocol):
    def append(self, event: JournalEvent) -> None: ...
    def get_events(self, run_id: str) -> list[JournalEvent]: ...
    def replay(self, run_id: str) -> dict: ...  # 返回重放后的状态摘要
    def list_run_ids(self) -> list[str]: ...

@dataclass
class InMemoryJournal(JournalProtocol):
    save_full_payload: bool = False
    _events: list[JournalEvent] = field(default_factory=list)
    _counters: dict[str, int] = field(default_factory=dict)

    def _summarize(self, event_type: str, payload: dict) -> dict:
        """默认摘要模式：按 event_type 裁剪到前 200 字符 + mask 明显密钥字段"""
        if self.save_full_payload:
            return payload
        # ... 摘要裁剪逻辑
```

**验证 Step 3.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.journal import InMemoryJournal, JournalEvent
import time
j = InMemoryJournal()  # 默认摘要模式
e1 = JournalEvent('r1', 1, '2026-08-12T00:00:00Z', 'agent_start', {'task': 't'})
j.append(e1)
assert j.list_run_ids() == ['r1']
assert len(j.get_events('r1')) == 1
print('append+list+get OK')
# replay 返回 dict（空实现也行，接口对就好）
res = j.replay('r1')
assert isinstance(res, dict)
print('replay 返回类型 OK')
print('=== Step 3.V 通过 ===')
"
```

**回滚方式**：删除 `atlas/journal/` 整个目录。不影响任何现有模块。

---

## Step 4：终止条件组合器

**目的**：把现在 `for step_no in range(1, self.max_steps + 1)` 单条件替换为可组合 Terminator。默认 max_steps + timeout，和现有行为回归兼容（max_steps=10 保留）。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/terminator.py`（**新建**） | 6 个类 + 1 个 CompositeTerminator + 顶层便捷函数 `make_default_terminator()`：<br>- `Terminator` Protocol（`should_stop(context) -> tuple[bool, str]`  返回（是否停，终止理由）<br>- `MaxSteps(n: int)`：context.step_count >= n → True<br>- `Timeout(timeout_ms: int)`：context.start_time + timeout_ms < now → True<br>- `TokenBudget(max_tokens: int)`：context.total_tokens >= max → True<br>- `CostCap(max_usd: float)`：context.cost_usd >= max → True<br>- `RepetitionDetector(window=5)`：最近 window 轮输出完全相同 → True<br>- `CompositeTerminator(list[Terminator])`：任一命中即停，收集所有理由<br>- 每个 Terminator 实现 `__or__(self, other)` → 返回 CompositeTerminator<br>- `make_default_terminator() = MaxSteps(10) \| Timeout(60_000)` | +220 行 |
| `atlas/core/agent.py` Agent.__init__ | 新增 `terminator` 参数（末尾）：<br>`terminator: Terminator | None = None`，None 时用 `make_default_terminator()` | +3 行 |
| `atlas/core/agent.py` Agent.run() 开头 | 初始化终止 context：`_term_ctx = SimpleNamespace(start_time=time.time()*1000, step_count=0, total_tokens=0, cost_usd=0, recent_outputs=[])` | +3 行 |
| `atlas/core/agent.py` Agent.run() 循环体（替换 for→while） | 把 `for step_no in range(1, self.max_steps+1):` 改为 `while True:`，循环开头：<br>1. `_term_ctx.step_count += 1`<br>2. `should_stop, reasons = self.terminator.should_stop(_term_ctx)`<br>3. 若 should_stop → 构造 `Agent 在 {step_count} 步内...` 最终答案返回，保留 Trace 渲染 | +12 行 |

**关键代码骨架**：
```python
# atlas/core/terminator.py
class Terminator(Protocol):
    def __or__(self, other: "Terminator") -> "CompositeTerminator":
        return CompositeTerminator([self, other])
    def should_stop(self, ctx: SimpleNamespace) -> tuple[bool, list[str]]: ...

@dataclass
class MaxSteps:
    max_steps: int
    def __or__(self, other): return CompositeTerminator([self, other])
    def should_stop(self, ctx):
        if ctx.step_count >= self.max_steps:
            return True, [f"max_steps={self.max_steps} 已用尽"]
        return False, []

@dataclass
class Timeout:
    timeout_ms: int
    def __or__(self, other): return CompositeTerminator([self, other])
    def should_stop(self, ctx):
        if (time.time()*1000 - ctx.start_time) >= self.timeout_ms:
            return True, [f"timeout={self.timeout_ms}ms 已到"]
        return False, []
```

**验证 Step 4.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
import types, time
from atlas.core.terminator import MaxSteps, Timeout, TokenBudget, CostCap, CompositeTerminator, make_default_terminator
# 1. | 运算
t = MaxSteps(3) | Timeout(1000)
assert isinstance(t, CompositeTerminator)
print('| 运算符重载 OK')
# 2. MaxSteps 触发
ctx = types.SimpleNamespace(step_count=3, start_time=time.time()*1000, total_tokens=0, cost_usd=0, recent_outputs=[])
stop, reasons = t.should_stop(ctx)
assert stop and 'max_steps' in reasons[0]
print('MaxSteps 触发 OK')
# 3. make_default_terminator 返回两个条件
dt = make_default_terminator()
assert isinstance(dt, CompositeTerminator)
assert len(dt.terminators) == 2
print('默认 terminator 两件套 OK')
# 4. Agent 接受 terminator 参数
from atlas.tools.base import Toolkit
from atlas.core.agent import Agent
class FakeLLM:
    def chat(self, msgs, tools=None): return {'content': 'answer'}
a = Agent(llm=FakeLLM(), tools=Toolkit(), terminator=MaxSteps(2))
a.run('hi')
print('Agent 自定义 terminator OK')
print('=== Step 4.V 通过 ===')
"
```

**回滚方式**：
- 删除 `atlas/core/terminator.py`
- `Agent.__init__` 撤 `terminator` 参数
- `Agent.run()` 改回 `for step_no in range(1, self.max_steps + 1):` 原始循环
- 回归风险：0（默认 terminator 行为 = max_steps=10，和现有完全一致）

---

## Step 5：LLM.chat() 返回 usage + duration_ms

**目的**：插件体系接入的前置。`on_llm_after_call` 需要 `usage` 和 `duration_ms`，当前 LLM.chat() 丢弃了 response.usage，也不计时。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/llm/base.py` LLM.chat() 函数体 | 重写 4 处：<br>1. 开头 `start_ts = time.time()`<br>2. 末尾 response 取完后：<br>   `message_dict = response.choices[0].message.model_dump(exclude_none=True)`<br>   `usage = getattr(response, 'usage', None)`<br>   `duration_ms = int((time.time() - start_ts) * 1000)`<br>3. **返回格式**（需与所有调用方兼容）：`return {'message': message_dict, 'usage': usage.model_dump() if usage else None, 'duration_ms': duration_ms}`  → 这会**破坏当前 Agent.run() 现有读取**，所以需要配套改 Agent.run()<br>   ⚠️ 见下方"破坏性变更" | +10 行 |
| `atlas/core/agent.py` Agent.run() 内部调用 `self.llm.chat(messages, tools=openai_tools)` 的地方 | 当前代码：<br>`resp = self.llm.chat(messages, tools=openai_tools)`<br>`messages.append(resp)`<br>改为：<br>`llm_result = self.llm.chat(messages, tools=openai_tools)`<br>`resp = llm_result['message']`<br>`_term_ctx.last_llm_usage = llm_result['usage']`<br>`_term_ctx.last_llm_duration_ms = llm_result['duration_ms']`<br>（token_budget/cost_cap 终止条件需要累计 tokens，这里先接住给后续累计用）<br>同时把 CostCap / TokenBudget 累计逻辑塞在这个位置：`_term_ctx.total_tokens += (usage['prompt_tokens'] + usage.get('completion_tokens',0)) if usage else 0` 等（可选，Step 6 再放也行） | +8 行 |

**破坏性变更警告**：LLM.chat() 返回值从 `message_dict`（直接能 `messages.append(resp)`）变为 `{message, usage, duration_ms}` 三层结构。如果 examples/ 下有直接调 LLM.chat() 的代码，需要同步调整。本次 Step 5.V 里必须 grep 全项目确认。

**验证 Step 5.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
# 1. grep 所有直接调用 llm.chat() 位置，确认全部只在 Agent.run()
import subprocess, re
r = subprocess.run(['git', 'grep', '-n', '\\.llm\\.chat('], capture_output=True, text=True)
print('调用 .llm.chat() 的位置：')
print(r.stdout)
# 2. 结构化 Fake LLM 返回格式
class FakeResp:
    usage = None
    class choices0:
        class msg:
            def model_dump(self, exclude_none=None):
                return {'role': 'assistant', 'content': 'hi'}
    choices = [types.SimpleNamespace(message=msg())]
# 这个只验证逻辑，真正要跑连真实 LLM 的是 V3 三个 demo
from atlas.tools.base import Toolkit
from atlas.core.agent import Agent
class FakeLLM:
    def chat(self, msgs, tools=None):
        return {'message': {'role':'assistant','content':'done'}, 'usage': None, 'duration_ms': 123}
a = Agent(llm=FakeLLM(), tools=Toolkit())
a.run('hi')
print('Fake LLM 三元组返回 Agent 处理 OK')
print('=== Step 5.V 通过 ===')
"
```

**回滚方式**：
- LLM.chat() 改回 `return response.choices[0].message.model_dump(exclude_none=True)`
- Agent.run() 改回 `resp = self.llm.chat(...)` 直接接
- 风险：低，纯返回结构变更 + 调用方同步改

---

## Step 6：插件体系接入 Agent.run()

**目的**：把"孤岛"的 PluginProtocol/PluginManager/FileLoggerPlugin/TokenCostPlugin 真正接入 Agent 生命周期。插件体系是预算护栏（cost_cap 通过 TokenCostPlugin 提供累计 cost_usd）和 FileLogger 的基础。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/core/agent.py` Agent.__init__ | 末尾追加 2 参数：<br>`plugin_manager: PluginManager \| None = None`（None 时用 PluginManager() 空实例）<br>`journal: JournalProtocol \| None = None`（预留后续 InMemoryJournal 注入，None 时不 Journal 记录，不报错）<br>字段挂到 self：`self.plugin_manager / self.journal` | +5 行 |
| `atlas/core/agent.py` Agent.__init__ 顶部 imports | 追加 `from atlas.core.plugin import PluginManager` + `from atlas.journal.base import JournalProtocol, JournalEvent` + `import uuid, datetime` | +3 行 |
| `atlas/core/agent.py` Agent.run() 入口 | 在 load_dotenv 之后，循环之前：<br>1. `run_id = str(uuid.uuid4())`<br>2. `self.plugin_manager.emit('agent_start', run_id=run_id, task=task, context={})`<br>3. `if self.journal: self.journal.append(JournalEvent(run_id, seq=..., ts=datetime.utcnow().isoformat()+'Z', event_type='agent_start', payload={'task': task}))`<br>4. `turn_idx = 0` | +8 行 |
| `atlas/core/agent.py` Agent.run() 循环内 | 每个工具调用位置：<br>1. before：`emit('tool_before_call', run_id, turn_idx, step_id=step_no, tool_name, input)`<br>2. after：`emit('tool_after_call', run_id, turn_idx, step_id, tool_name, input, result, duration_ms, status)` | +8 行 |
| `atlas/core/agent.py` Agent.run() 每个 LLM 调用位置 | before/after：`emit('llm_before_call')` / `emit('llm_after_call', usage=llm_result['usage'], duration_ms=llm_result['duration_ms'])` | +6 行 |
| `atlas/core/agent.py` Agent.run() 循环开头 | `turn_idx += 1` + `emit('turn_start')`；循环末尾 `emit('turn_finish')` | +5 行 |
| `atlas/core/agent.py` Agent.run() 两个 return 前 | `emit('agent_finish', run_id, final_result, status=...)`；若有 journal 追加 agent_finish event | +5 行 |
| `atlas/core/agent.py` Agent.run() 终止条件累计 cost_usd | 在 emit('llm_after_call') 之后从 TokenCostPlugin.get_summary(run_id) 拿 cost_usd 累计到 `_term_ctx.cost_usd`（若该 plugin 已注册） | +4 行 |

**关键代码骨架**：
```python
# Agent.run() 开头
run_id = str(uuid.uuid4())
self.trace.meta['run_id'] = run_id
try:
    self.plugin_manager.emit(
        'agent_start', run_id=run_id, task=task,
        context={'system_prompt': self.system_prompt},
    )
except Exception:
    pass  # 插件异常永不中断主流程（PluginManager 自己吞）

# 循环开头
while True:
    _term_ctx.step_count += 1
    turn_idx += 1
    self.plugin_manager.emit('turn_start', run_id=run_id, turn_idx=turn_idx, context={})
    should_stop, reasons = self.terminator.should_stop(_term_ctx)
    if should_stop:
        final = f"Agent 终止：{'; '.join(reasons)}"
        self.plugin_manager.emit('agent_finish', run_id=run_id, final_result=final, status='terminated', context={})
        self.trace.render(self.renderer, depth=0)
        return final
```

**验证 Step 6.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.tools.base import Toolkit
from atlas.core.agent import Agent
from atlas.core.plugin import PluginManager
from atlas.plugins.token_cost import TokenCostPlugin
from atlas.journal import InMemoryJournal

pm = PluginManager()
tc = TokenCostPlugin()
pm.register(tc)

class FakeLLM:
    call_count = 0
    def chat(self, msgs, tools=None):
        self.call_count += 1
        FakeLLM.call_count = self.call_count
        usage = {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150}
        # 第一次 LLM 用工具，第二次出答案（给 token_cost 统计 2 轮）
        if self.call_count == 1:
            msg = {'role':'assistant','content':'th',
                   'tool_calls':[{'id':'tc1','type':'function','function':{'name':'calculator','arguments':'{\"a\":1,\"b\":2,\"op\":\"+\"}'}}]}
        else:
            msg = {'role':'assistant','content':'final answer'}
        return {'message': msg, 'usage': usage, 'duration_ms': 100}

tk = Toolkit()
# calculator
import operator
def calc(a, b, op):
    return str({'add': operator.add, 'sub': operator.sub, 'mul': operator.mul, 'div': operator.truediv}[op](a, b))
tk.register(name='calculator', description='math',
            parameters={'type':'object','properties':{'a':{'type':'number'},'b':{'type':'number'},'op':{'type':'string'}},'required':['a','b','op']},
            func=lambda d: calc(d['a'], d['b'], d['op']),
            isReadOnly=True)

j = InMemoryJournal(save_full_payload=True)
a = Agent(llm=FakeLLM(), tools=tk, plugin_manager=pm, journal=j, max_steps=3)
# 注入 terminator 的默认 make_default_terminator()，Agent.__init__ 会自己处理

result = a.run('1+2=?')
print(f'最终答案: {result}')

# 断言
s = tc.get_summary(list(tc._per_run.keys())[0])
assert s.get('total_tokens', 0) >= 150, f'TokenCostPlugin 应该累计到 tokens，实际 {s}'
print(f'TokenCostPlugin 累计 OK: total_tokens={s.get(\"total_tokens\")}')

# Journal: 至少 agent_start + agent_finish 两个事件
rid = a.trace.meta['run_id']
events = j.get_events(rid)
types = [e.event_type for e in events]
assert 'agent_start' in types and 'agent_finish' in types
print(f'Journal 事件类型 OK: {set(types)}')

print('=== Step 6.V 通过 ===')
"
```

**回滚方式**：
- Agent.__init__ 撤 plugin_manager/journal 参数
- Agent.run() 内删除所有 emit 调用 + run_id 生成
- 风险：低，所有 emit 都 try/except 隔离，删除后 Agent.run 行为和 Step 4 完全一致

---

## Step 7：最小 YAML loader + parallel_subagent_demo.py 切换

**目的**：让 examples/parallel_subagent_config.yaml 从"示意文档"变成"实际被读取的配置"。用户改 YAML 即改角色/task，不用改 Python。

**改动文件**：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/utils/__init__.py`（**新建目录 + 新建文件**） | 空 `__init__.py` | +0 行 |
| `atlas/utils/yaml_loader.py`（**新建**） | 单函数 `load_parallel_subagent_config(path: str) -> tuple[dict[str, RoleConfig], dict, str]`：<br>1. yaml.safe_load 读文件<br>2. 遍历 `roles:`，每个 → RoleConfig(name=角色key, system_prompt=..., tool_names=role_yaml['tools'], temperature=role_yaml['llm_config']['temperature'], max_steps=role_yaml['llm_config']['max_steps'])<br>3. 返回 (roles_dict, parent_cfg_dict, task_str)<br>4. schema 错误（缺 tools/system_prompt 等）抛 RuntimeError 带具体字段名 | +90 行 |
| `examples/parallel_subagent_demo.py` | **重写加载逻辑**（保留 Agent.run 调用和 sys.argv --plain 处理）：<br>1. argparse.ArgumentParser，加 `--config` 参数，默认 `examples/parallel_subagent_config.yaml`<br>2. `roles, parent, task = load_parallel_subagent_config(args.config)`<br>3. 用 parent['llm_config']['temperature'] / parent['max_steps'] 构造 Agent<br>4. parent['system_prompt_template'].format(known_roles=...) 作为 system_prompt | -450 +230 |

**关键代码骨架**：
```python
# atlas/utils/yaml_loader.py
def load_parallel_subagent_config(path: str):
    with open(path, encoding='utf-8') as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise RuntimeError(f'YAML 根应为 dict，实际 {type(raw).__name__}')
    if 'roles' not in raw:
        raise RuntimeError('YAML 缺少必填字段: roles')
    roles = {}
    for name, cfg in raw['roles'].items():
        # 字段校验 + RoleConfig 构造
        if 'system_prompt' not in cfg or 'tools' not in cfg or 'llm_config' not in cfg:
            raise RuntimeError(f'Role {name} 缺字段: system_prompt/tools/llm_config 必有')
        llm_cfg = cfg['llm_config']
        roles[name] = RoleConfig(
            name=name, system_prompt=cfg['system_prompt'],
            tool_names=cfg['tools'],
            temperature=llm_cfg.get('temperature', 0.3),
            max_steps=llm_cfg.get('max_steps', 5),
            timeout_ms=cfg.get('timeout_ms', 60_000),
        )
    return roles, raw.get('parent', {}), raw.get('task', '')
```

**验证 Step 7.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.utils.yaml_loader import load_parallel_subagent_config
roles, parent, task = load_parallel_subagent_config('examples/parallel_subagent_config.yaml')
# roles 至少 2 个（分析员 + 研究员，按 YAML 里的实际数量）
assert len(roles) >= 2, f'YAML roles 数量应为 >=2，实际 {len(roles)}'
# 第一个 RoleConfig 字段全
r = list(roles.values())[0]
assert r.name and r.system_prompt and r.tool_names and r.temperature and r.max_steps and r.timeout_ms
print(f'解析成功: roles={list(roles.keys())}, parent 有 keys={list(parent.keys())}, task 前 20 字={task[:20]}...')
# 语法检查
import ast
ast.parse(open('examples/parallel_subagent_demo.py', encoding='utf-8').read())
print('demo 语法 OK')
print('=== Step 7.V 通过 ===')
"
```

**回滚方式**：
- 删除 `atlas/utils/` 目录
- parallel_subagent_demo.py 回退到当前 Python 硬构造 RoleConfig 版本
- 风险：0（YAML loader 是纯新增功能，不回退也不影响其它模块）

---

## 整体验证矩阵（7 步全部完成后）

### V1：每步 import 验证（复用上面每步的 Step X.V，一口气全跑）
```powershell
cd e:\Learn_Project\Atlas
python -c "
# 把 Step 1.V / 3.V / 4.V / 5.V / 6.V / 7.V 的 assert 全放这里
# 不写重复，只需确保 import 全不崩
import atlas.tools.base, atlas.journal, atlas.core.terminator, atlas.llm.base, atlas.core.agent, atlas.utils.yaml_loader
print('所有模块 import OK')
"
```

### V2：TokenCostPlugin 真统计（插件接入最关键的价值验证）
- 复用 Step 6.V 完整脚本，断言 `cost_usd >= 0` 且 `total_tokens >= 300`（FakeLLM 返回两轮，每轮 150）

### V3：3 个 demo 真实连 LLM 回归（.env 已配置时）
```powershell
cd e:\Learn_Project\Atlas
python examples/quickstart.py --plain          # 断言：最后一行不是 "Agent 终止"（预算默认不触发）
python examples/multi_agent_demo.py --plain    # 断言：Critic 审查全部通过
python examples/parallel_subagent_demo.py --plain  # 断言：3 子 Agent 并行成功，加速比 >= 1.5x
```

---

## 风险矩阵与回滚

| Step | 风险等级 | 风险内容 | 缓解 / 回滚 |
|------|---------|---------|------------|
| 1 | 低 | 字段默认值 False，不破坏现有 Toolkit.register 调用 | 回滚 Step 1 4 处改动 |
| 2 | 0 | 纯文档改动 | 撤销文档修改 |
| 3 | 0 | 全新目录，零反向引用 | 删除 `atlas/journal/` |
| 4 | 低 | 默认 terminator 两件套行为 = 现状 max_steps=10，行为完全一致；Agent.run() 从 for 改 while 是唯一重写点 | 回滚 while→for，删 terminator.py |
| 5 | 中 | LLM.chat() 返回结构变了，grep 全项目调用点。若 examples 有裸调用会崩 | git grep -n `.llm.chat(` 先确认所有调用点只在 Agent.run()，不通过就不提交 Step 5 |
| 6 | 中 | emit 多处插入，若某个 emit 参数与 PluginProtocol 签名不符 → 该插件异常被 PluginManager 吞不影响主流程，但日志空。6.V 断言 TokenCostPlugin 真统计就可发现 | 6.V 失败时回滚 emit 插入 + __init__ 参数 |
| 7 | 低 | 纯新增。demo 回滚只需 git checkout | 不回滚也不影响其它功能 |

---

## 交付顺序（按依赖）

实现时按 Step 1 → 3 → 4 → 2（文档并行）→ 5 → 6 → 7，每步完成后立即跑对应 Step X.V 验证脚本，**不做到上一步绿就不开下一步**（经验 1246097 教训：出 2 次以上同类错误就停止收敛，不能堆着改）。

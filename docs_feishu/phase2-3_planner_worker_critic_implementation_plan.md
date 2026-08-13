# Phase 2.3 实施计划：PlannerWorkerCritic 多 Agent 模式包（产品级）

> grill-me 3 轮共享理解冻结（21 项决策）。2026-08-13 定稿。
>
> 实施原则（沿用 Phase 2.1 / 2.2 成功实践 + 用户规则）：
> 1. 每步独立：完成后**立即**有一行 `python -c` 验证，不做"半成品等下一步"
> 2. 严格单向依赖：Step 编号越大依赖越多，绝不反向依赖；patterns 包绝不被 atlas-core 导入（DR-0 红线）
> 3. Windows 纯 Python 验证命令（不 head/curl 类 Unix 工具）
> 4. 不考虑 fallback / 旧兼容，没有老用户：直接删 `atlas/core/orchestrator.py` + 删 `examples/multi_agent_demo.py`，不做 alias
> 5. 代码迁移先 copy 再改写：`_common.py` 的 `_make_agent()` 从 `agent.py` Agent.__init__ 参数表 copy 过来做适配，不自己重写一遍（防止默认值漏掉）

---

## 0. 决策速查表（21 项 grill-me 冻结）

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | patterns vs core 边界 + 迁移 | A：从 core/orchestrator.py copy 到 patterns/，重写为基于 Agent 内核 + spawn_subagents |
| Q2 | PWC 与 spawn_subagents 关系 | C：多层嵌套 Agent（Planner/Worker/Critic 都是内核 Agent 实例） |
| Q3 | PWC 输出形状 | B：`PWCResult(final_answer: str, trace: PWCTrace)`，专用扁平 dataclass |
| Q4 | Critic 打回重做上下文 | C：重做时给 Worker 传「Critic 评语 + 前一版结果」，全部写入 trace |
| Q5 | Phase 2.3 做几个模式 | B：只做 PlannerWorkerCritic，OrchestratorWorkers 后移 |
| Q6 | patterns 对外导出形态 | A：独立 subpackage `from atlas.patterns import Xxx`，不进 atlas 顶层导出 |
| Q7 | pwc_demo 断言数 | A：5 项断言（对齐 compact_demo 密度） |
| Q8 | patterns 内部代码共享 | A：允许 `_common.py` 辅助（模式间互不 import，只都 import `_common`） |
| Q9 | 旧 core/orchestrator 兼容策略 | C：不兼容，直接删 core/orchestrator.py + 删 examples/multi_agent_demo.py |
| Q10 | Planner Agent 的 tools 注入 | A：只有 spawn_subagents 工具，不注入任何业务工具 |
| Q11 | Critic 打回循环控制位置 | A：PWC 组合器层的 Python while（LLM 只做内容判断，不控制循环次数） |
| Q12 | 并行 vs 串行粒度 | A：**串行** PWC（每个 Worker 的输入含前面所有已 approved 结果 + Critic 重做评语，顺序有上下文依赖） |
| Q13 | FakeLLM 模拟 + 5 断言 | A：4 阶段（Plan / Worker 串行 / Critic / Summary），1 号 Worker 第 1 次 FAIL → 重做 → PASS |
| Q14 | PWCTrace 结构 | B：专用扁平 dataclass（不从嵌套 Trace 深 path 遍历，demo 断言扁平访问） |
| Q15 | `_common.py` 共享内容 | A：2 个辅助：`_make_agent()` + `_validate_artifact_schema()` |
| Q16 | 文件清单（新建/删除/修改） | C：删 core/orchestrator.py + 删 examples/multi_agent_demo.py；新建 patterns 三文件 + pwc_demo |
| Q17 | `patterns/__init__.py` re-export | A：`PlannerWorkerCritic` / `PWCResult` / `PWCTrace` 三项全 re-export |
| Q18 | PWC 构造函数字段 | A：必填 2（llm, worker_tools）+ 可选 10 = 12 项（≤ DR-2 15 限额） |
| Q19 | max_revisions 耗尽处理 | A：该子任务标 status="failed"（不抛异常 abort 全流程），继续下一子任务，最终汇总明确标注 |
| Q20 | 内置 system prompt 模板 | A：3 角色内置默认模板（写在 `planner_worker_critic.py` 顶部常量），3 个可选参数可覆写 |
| Q21 | README 两 demo 定位差异 | A：parallel_subagent_demo = 并行加速 vs pwc_demo = 质量闭环（串行 + Critic 打回），README 注释区分 |

---

## 0.5 依赖顺序与整体验证

```
Step 0：删除 core/orchestrator.py + 删除 examples/multi_agent_demo.py  ─── 无依赖（清理旧代码）
Step 1：新建 patterns 目录骨架（__init__.py + _common.py）              ─── 无依赖（可与 Step 0 并行）
        │
        ▼
Step 2：新建 atlas/patterns/planner_worker_critic.py 主体
    （PWCTrace / PWCResult dataclass + 3 角色内置 prompt 常量
     + PlannerWorkerCritic 构造函数 12 项 + run() 串行 while 流程
     + 私有辅助：_plan_parse / _critic_parse / _serialize_trace）
        │
        ▼
Step 3：examples/pwc_demo.py 新建（FakeLLM 4 阶段 + 5 项断言
    + --mock --plain / --mock --dashboard 双模式）
        │
        ▼
Step 4：atlas/__init__.py 检查（patterns 不进顶层）+ README 更新
    （新增 patterns 段落 + pwc_demo 示例 + PWC vs parallel 定位差异
     + 路线图 Phase 2.3 打勾 ✓ / 2.2 改 2.3 / 旧 2.3 顺延）
        │
        ▼
Step 5：demo 脚本 + FakeLLM 调试（可选但推荐先跑 V2 再 V3）
```

验证层（全部完成后）：
  V1：每步独立 `python -c` 导入验证
  V2：pwc_demo --mock --plain（5 项断言全通过）
  V3：10 个 mock demo 回归（parallel_subagent / tool_abi / journal_replay / terminator / plugin_cost / saga / compact / scope / dashboard / circuit_retry）

---

## Step 0：删除遗留 core/orchestrator.py + 旧 multi_agent_demo.py

**目的**：从 atlas-core 层移除"不属于内核原语的产品级 orchestrator 代码"（DR-0 红线：atlas-core 严禁产品级内容）。同时移除对旧 MultiAgentOrchestrator 有依赖的 demo，没有老用户，不留兼容。

**改动文件**（2 删除）：

| 操作 | 路径 | 确认点 |
|------|------|--------|
| Delete | `atlas/core/orchestrator.py` | 确认：`examples/parallel_subagent_demo.py` / `examples/dashboard_demo.py` / 其他 demo 都不 import 它（只 `multi_agent_demo.py` 用，已在删除清单里） |
| Delete | `examples/multi_agent_demo.py` | 一并删除 |

**前置验证（删之前运行，确认 import 范围）**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
# grep 替代：手动列出所有 import 的文件
import os, re
matches = []
for root, _, files in os.walk('atlas'):
    for f in files:
        if f.endswith('.py'):
            p = os.path.join(root, f)
            txt = open(p, encoding='utf-8').read()
            if 'orchestrator' in txt.lower() and 'multi_agent_orchestrator' in txt:
                matches.append(p)
for root, _, files in os.walk('examples'):
    for f in files:
        if f.endswith('.py'):
            p = os.path.join(root, f)
            txt = open(p, encoding='utf-8').read()
            if 'orchestrator' in txt.lower() and 'multi_agent_orchestrator' in txt:
                matches.append(p)
print('引用 orchestrator 的文件:', matches)
# 期望：只出现 ['examples\\\\multi_agent_demo.py']
"
```

**删后验证 Step 0.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
# atlas 包本身导入不受影响
import atlas
print('atlas import OK')
from atlas import Agent, LLM, Toolkit, Scope, Evaluator, ArtifactSchemaEvaluator
print('atlas 顶层导入 OK')
# core 层目录下不再有 orchestrator.py
import os, atlas.core
core_dir = os.path.dirname(atlas.core.__file__)
assert 'orchestrator.py' not in os.listdir(core_dir), 'core/orchestrator.py 未删除'
print('core/orchestrator.py 已删除 OK')
# examples 目录下不再有 multi_agent_demo.py
examples_dir = os.path.join(os.path.dirname(os.path.dirname(core_dir)), 'examples')
assert 'multi_agent_demo.py' not in os.listdir(examples_dir), 'examples/multi_agent_demo.py 未删除'
print('examples/multi_agent_demo.py 已删除 OK')
print('=== Step 0.V 通过 ===')
"
```

**回滚方式**：从 git 恢复两文件（`git checkout HEAD -- atlas/core/orchestrator.py examples/multi_agent_demo.py`）
**风险**：低（只要前置 grep 确认引用只在删除目标 demo 自己身上就没破坏）

---

## Step 1：patterns 目录骨架 + `_common.py` 2 个辅助

**目的**：`atlas/patterns/` 全新子包（新建 `__init__.py` + `_common.py`），放通用辅助。`_common.py` 提供 2 个共享函数：
1. `_make_agent()`：封装"基于内核 Agent 构造函数 + 正确参数默认值"快速造出 Planner / Worker / Critic 三种角色 Agent，避免每处 copy 18 个关键字参漏默认值
2. `_validate_artifact_schema(artifact, schema) -> (bool, str|None)`：从 `atlas/core/multi_agent.py` 里现有的 artifact_schema 校验逻辑 copy 过来（pattern 层不能 import core/multi_agent.py？不，DR-0 允许上层 import 下层，所以其实也可以直接 `from atlas.core.multi_agent import xxx`；但为了 patterns 层独立、降低耦合，我们直接 copy 校验逻辑到 `_common.py`——10 行代码，copy 优于依赖）

**改动文件**（2 新建）：

| 文件 | 改动点 | 行数估算 |
|------|--------|---------|
| `atlas/patterns/__init__.py`（**新建**） | 3 行 `from .planner_worker_critic import PlannerWorkerCritic, PWCResult, PWCTrace`（此时 planner_worker_critic 还没实现，先不 import，留占位空字符串 + 注释说明"等 Step 2 完成后再填入"——其实 Python 循环 import 不怕，只要 import 在函数内部。正确做法是：`__init__.py` 先空文件，Step 2 写完最后回来填 3 行 import。所以本步只写空 `__init__.py` + 模块 docstring） | +3 行 |
| `atlas/patterns/_common.py`（**新建**） | 模块 docstring + 2 个辅助：<br>① `_make_agent(role_name, system_prompt, llm, tools, *, max_steps=10, shared_renderer=None, shared_terminator=None, shared_plugin_manager=None, shared_circuit_breaker_registry=None, shared_journal=None, compact_threshold=None) -> Agent`<br>② `_validate_artifact_schema(artifact: str, schema: dict) -> tuple[bool, str \| None]` | +80 行 |

**`_make_agent()` 实现注意事项（先 copy Agent.__init__ 参数表，再做适配）**：

必须严格从 `atlas/core/agent.py` Agent.__init__ 函数签名 **copy 关键字参名 + 默认值**，然后再做适配（用户规则 6：copy 再改写，不自己重写容易漏默认值）：

```python
# atlas/patterns/_common.py _make_agent 关键骨架
# 注意：Agent.__init__ 参数（copy 自 agent.py L37-L60）：
#   llm, tools, system_prompt=None, max_steps=10, renderer=None,
#   terminator=None, plugin_manager=None, journal=None,
#   role_configs=None, parent_trace=None, auto_inject_spawn_tool=True,
#   tool_specs_pool=None, compact_threshold=8000, transcript=None,
#   circuit_breaker_registry=None, auto_default_registry=True
#
# _make_agent 的作用：把 Planner/Worker/Critic 三种角色的"共性"参数压缩到
#   shared_* 里（shared_*=None 时自动用 Agent 默认），只暴露 role 差异

import json
from typing import Any
import jsonschema  # P-0 已依赖 pyproject.toml

from atlas import Agent, LLM, Toolkit
from atlas.render import PlainRenderer, Renderer
from atlas.core.terminator import Terminator, make_default_terminator
from atlas.core.plugin import PluginManager
from atlas.core.circuit_breaker import CircuitBreakerRegistry
from atlas.journal.base import JournalProtocol


def _make_agent(
    role_name: str,
    system_prompt: str,
    llm: LLM,
    tools: Toolkit,
    *,
    max_steps: int = 10,
    shared_renderer: Renderer | None = None,
    shared_terminator: Terminator | None = None,
    shared_plugin_manager: PluginManager | None = None,
    shared_circuit_breaker_registry: CircuitBreakerRegistry | None = None,
    shared_journal: JournalProtocol | None = None,
    compact_threshold: int | None = 8000,
    # role_configs=None + parent_trace=None：PWC 都是顶层 Agent，无子嵌套
    # auto_inject_spawn_tool=False：仅 Planner 单独设 True（见 planner_worker_critic.py）
    auto_inject_spawn_tool: bool = False,
    role_configs: dict | None = None,
) -> Agent:
    """基于 atlas Agent 内核构造一个角色 Agent。
    所有 shared_* 为 None 时走 Agent.__init__ 默认值（不破坏内核行为）。
    """
    renderer = shared_renderer or PlainRenderer()
    terminator = shared_terminator or make_default_terminator(max_steps)
    return Agent(
        llm=llm,
        tools=tools,
        system_prompt=system_prompt,
        max_steps=max_steps,
        renderer=renderer,
        terminator=terminator,
        plugin_manager=shared_plugin_manager,  # None 时 Agent 自动造空
        journal=shared_journal,
        role_configs=role_configs,  # Worker/Critic = None，Planner = {role_name: RoleConfig}
        parent_trace=None,  # PWC 的 Planner/Worker/Critic 都是顶层 new Agent（无嵌套守卫）
        auto_inject_spawn_tool=auto_inject_spawn_tool,
        tool_specs_pool=None,
        compact_threshold=compact_threshold,
        transcript=None,  # Agent 默认自建 Transcript
        circuit_breaker_registry=shared_circuit_breaker_registry,  # None 时 Agent 自动造空 Registry
        auto_default_registry=(shared_circuit_breaker_registry is None),
    )


def _validate_artifact_schema(artifact: str, schema: dict) -> tuple[bool, str | None]:
    """artifact JSON Schema 校验。返回 (是否通过, 错误说明/None)。
    artifact 非合法 JSON 也视为不通过。不改写 artifact 原文。
    """
    if not artifact or not schema:
        return (True, None)
    try:
        parsed = json.loads(artifact) if isinstance(artifact, str) else artifact
        jsonschema.validate(parsed, schema)
        return (True, None)
    except json.JSONDecodeError as e:
        return (False, f"artifact 非合法 JSON: {e}")
    except jsonschema.ValidationError as e:
        return (False, f"artifact schema 校验失败: {e.message}")
```

**验证 Step 1.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
from atlas.patterns._common import _make_agent, _validate_artifact_schema
from atlas import LLM, Toolkit

# 1. _make_agent 不抛（llm 是 FakeLLM 也行，这里不调用 .run() 只验证构造）
llm = LLM()
tools = Toolkit()
a = _make_agent(
    role_name='worker',
    system_prompt='你是执行者',
    llm=llm,
    tools=tools,
    max_steps=3,
)
assert isinstance(a, Agent)
assert a.max_steps == 3
assert '你是执行者' in a.memory.messages[0].get('content', '')
print('_make_agent 构造 OK')

# 2. _validate_artifact_schema 四场景
ok, err = _validate_artifact_schema('', {})
assert ok and err is None
ok, err = _validate_artifact_schema('{\"a\":1}', {'type':'object', 'properties':{'a':{'type':'integer'}}})
assert ok and err is None
ok, err = _validate_artifact_schema('not_json', {'type':'object'})
assert not ok and '非合法 JSON' in err
ok, err = _validate_artifact_schema('{\"a\":\"string\"}', {'type':'object','properties':{'a':{'type':'integer'}}})
assert not ok and '校验失败' in err
print('_validate_artifact_schema 4 场景 OK')

print('=== Step 1.V 通过 ===')
"
```

**回滚方式**：删 `atlas/patterns/` 整个目录（只有 2 文件）
**风险**：0（纯新建，零影响现有）

---

## Step 2：atlas/patterns/planner_worker_critic.py 主实现

**目的**：Phase 2.3 核心。PlannerWorkerCritic 类 + 配套 dataclass（PWCResult / PWCTrace / PlanItem / WorkerRun / CriticReview）+ 3 角色内置 prompt 常量。

**改动文件**（1 新建）：

| 文件 | 内容分块 | 行数估算 |
|------|---------|---------|
| `atlas/patterns/planner_worker_critic.py`（**新建**） | 1. 模块文档字符串（PWC 整体说明：Planner→串行 Workers→每个 Worker 跑 Critic→Critic FAIL 打回重做 max_revisions 次→耗尽标 failed→Planner 汇总标注）<br>2. 3 条内置 prompt 常量（`_DEFAULT_PLANNER_PROMPT` / `_DEFAULT_WORKER_PROMPT` / `_DEFAULT_CRITIC_PROMPT`）<br>3. 5 个 dataclass：`PlanItem`、`WorkerRun`、`CriticReview`、`PWCTrace`、`PWCResult`<br>4. `@dataclass class PlannerWorkerCritic` 主类（Q18 12 项构造函数 + `run(task=None) -> PWCResult`）<br>5. 私有辅助：`_parse_plan(output) -> list[str]`、`_parse_critic(output) -> tuple[bool, str]`、`_extract_nested_trace(agent_instance) -> dict`（把 Agent.trace 扁平数据摘出塞进 PWCTrace） | +380 行 |
| `atlas/patterns/__init__.py`（回填） | 末尾追加 3 行 re-export：<br>`from .planner_worker_critic import PlannerWorkerCritic, PWCResult, PWCTrace`（Q17=A） | +3 行 |

### 2.1 5 个 dataclass 形状

```python
# atlas/patterns/planner_worker_critic.py dataclass 段
from __future__ import annotations
import json
import threading
from dataclasses import dataclass, field
from typing import Any

from atlas import Agent, LLM, Toolkit
from atlas.render import PlainRenderer, Renderer
from atlas.core.terminator import Terminator
from atlas.core.plugin import PluginManager
from atlas.core.circuit_breaker import CircuitBreakerRegistry
from atlas.journal.base import JournalProtocol
from atlas.context.trace import Trace as AgentTrace
from atlas.core.multi_agent import RoleConfig

from ._common import _make_agent, _validate_artifact_schema


# =============================================================
# 3 条内置 prompt 模板（Q20=A）。3 个可选参数可覆写
# =============================================================
_DEFAULT_PLANNER_PROMPT = """你是任务规划师（Planner）。
职责：
  1. 把用户任务拆解为 2~{max_plan_items} 个可独立执行的子任务
  2. 等待所有子任务执行完成后，基于各子任务结果汇总出最终答案
输出格式（规划阶段）：
  每行一个子任务，不要编号，不要其他文字。
输出格式（汇总阶段）：
  给出完整、连贯的最终答案，如果某些子任务被标记为 "Critic 审查未通过"，
  请在最终答案中**显式标注**"此项为 Critic 连续打回 {n} 次未过版本，仅供参考"字样。
"""

_DEFAULT_WORKER_PROMPT = """你是子任务执行者（Worker）。
职责：完成下面这个子任务，可调用指定工具。

子任务：
{subtask_description}

已完成的前置子任务结果（上下文）：
{context_block}

{critic_revision_block}

输出要求：
  - 直接给出子任务执行结果，不要多余解释
  - 如果任务要求 JSON，请返回纯 JSON 字符串（不要 Markdown 代码块包装）
"""

_DEFAULT_CRITIC_PROMPT = """你是质量审查员（Critic）。
职责：判断下面 Worker 的执行结果是否**准确、完整**地完成了子任务要求。

子任务：
{subtask_description}

Worker 执行结果：
{worker_result}

输出格式（严格遵守，一行都不能错）：
  第一行：PASS  或  FAIL（必须全大写）
  第二行起：评语（中文，说明为什么通过 / 哪里需要修改，最多 300 字）
"""


# =============================================================
# 5 个扁平 dataclass（Q14=B）
# =============================================================
@dataclass
class PlanItem:
    """Planner 拆解出的单个子任务条目。"""
    idx: int                       # 1-based 顺序
    description: str               # 子任务文字描述
    status: str = "pending"        # pending / running / done / failed（Critic 耗尽标 failed）
    artifact: str = ""             # 最终 approved 的 Worker 输出（或耗尽时的最后版本）
    revisions: int = 0             # 被 Critic 打回重做的次数（第一次 FAIL 后 revisions 从 0→1）


@dataclass
class WorkerRun:
    """每次 Worker 执行的记录（含重做）。供 demo 断言和 trace 可视化。"""
    plan_idx: int                  # 对应 PlanItem.idx
    attempt: int                   # 第几次执行（1=首次，>=2=重做）
    input_has_critic_comment: bool # Q13 断言⑤：重做时输入是否含 Critic 评语
    raw_artifact: str              # 本次执行的原始输出
    artifact_schema_pass: bool     # artifact_schema 校验结果（若有 schema）
    duration_ms: int = 0           # 本次执行耗时
    sub_agent_trace_snapshot: dict | None = None  # 从 Agent.trace 摘出的关键信息


@dataclass
class CriticReview:
    """每次 Critic 审查的记录。"""
    plan_idx: int                  # 对应 PlanItem.idx
    worker_attempt: int            # 对应 WorkerRun.attempt
    approved: bool                 # True=PASS / False=FAIL
    comment: str                   # 评语（给用户看 + 下次重做传给 Worker）
    duration_ms: int = 0


@dataclass
class PWCTrace:
    """PWC 全程扁平可观测存档。Q14=B 专用结构，不依赖嵌套 Agent Trace。"""
    task: str = ""
    # Plan 层
    plan_items: list[PlanItem] = field(default_factory=list)
    # Worker 层（按执行顺序 append，重做也 append 新条目，plan_idx 关联）
    worker_runs: list[WorkerRun] = field(default_factory=list)
    # Critic 层（每次审查一条，plan_idx + worker_attempt 关联）
    critic_reviews: list[CriticReview] = field(default_factory=list)
    # 时间戳
    start_ts: float = 0.0
    end_ts: float = 0.0
    total_duration_ms: int = 0


@dataclass
class PWCResult:
    """run(task) 的返回值（Q3=B）。"""
    final_answer: str              # Planner 汇总后的最终答案（可能带 Critic 未过告警）
    trace: PWCTrace                # 全程扁平 trace
```

### 2.2 PlannerWorkerCritic 主类（构造函数 + run）

```python
# 接上面文件继续

@dataclass
class PlannerWorkerCritic:
    """Planner-Worker-Critic 多 Agent 模式包（Q2=C 多层嵌套 Agent + Q12=A 串行 + Q11=A Python while Critic 循环）。

    构造函数字段：必填 2 + 可选 10 = 12 项（Q18=A，≤ DR-2 15 上限）
    """
    # ── 必填（2 项） ──
    llm: LLM
    worker_tools: Toolkit          # Worker 可用的业务工具（Calculator、读写文件等），Planner/Critic 不注入

    # ── 可选（10 项，全有默认值） ──
    task: str | None = None        # 可选，也可在 run(task=...) 传入时覆盖
    planner_system_prompt: str | None = None
    worker_system_prompt: str | None = None
    critic_system_prompt: str | None = None
    max_revisions: int = 2         # Critic 打回重做上限（达到仍 FAIL 标 failed，不 abort）
    max_plan_items: int = 4        # Planner 拆解上限（超上限时由 prompt 约束，Python 侧也截断）
    max_steps_per_worker: int = 10 # 单个 Worker Agent 的 max_steps 参数
    circuit_breaker_registry: CircuitBreakerRegistry | None = None  # 所有角色共享
    terminator: Terminator | None = None                            # 所有角色共享（None 用默认）
    plugin_manager: PluginManager | None = None                      # 所有角色共享

    # ── 内部状态（post_init 初始化） ──
    _planner_agent: Agent | None = field(default=None, init=False, repr=False)
    _worker_template_tools: Toolkit | None = field(default=None, init=False, repr=False)
    _renderer: Renderer = field(default_factory=PlainRenderer, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        # 默认 prompt（用户没传时用内置常量）
        self._final_planner_prompt = (
            self.planner_system_prompt
            or _DEFAULT_PLANNER_PROMPT.format(max_plan_items=self.max_plan_items)
        )
        self._final_worker_prompt = self.worker_system_prompt or _DEFAULT_WORKER_PROMPT
        self._final_critic_prompt = self.critic_system_prompt or _DEFAULT_CRITIC_PROMPT

        # 共享 renderer：所有 Planner/Worker/Critic Agent 共用同一个 Renderer 实例
        # （这样 RichRenderer 终端输出不会乱序，PlainRenderer 也安全）
        self._renderer = PlainRenderer()  # 先默认，用户若要 Rich 自己在外部传 renderer 参数
        # （注：上面 Q18 的构造函数字段没列 renderer，因为 PWC 对外只暴露 12 项限额，
        #  renderer 走内部默认 Plain。用户如果真要 Rich，可以手动 new 完 PWC 后
        #  p._renderer = RichRenderer()；不进 public 构造函数是为了卡 12 项限额）

    # ──────────────────────────────────────────────
    # 外部 API
    # ──────────────────────────────────────────────
    def run(self, task: str | None = None) -> PWCResult:
        """运行完整 PWC 流程。task 若传则覆盖构造函数 task 字段。
        返回 PWCResult(final_answer, trace)。

        流程：Step 1 拆解 → Step 2 串行子任务（Worker→Critic→重做）→ Step 3 汇总标注
        """
        import time
        run_task = task or self.task
        if not run_task:
            raise ValueError("PlannerWorkerCritic.run(task): 请传入 task 或构造时指定 task")

        trace = PWCTrace(task=run_task, start_ts=time.time())
        _trace = trace  # alias，避免和 Agent.trace 属性混名
        _start = time.time()

        # =============================================================
        # Phase A：Planner 拆解
        # =============================================================
        self._renderer.rule("PWC · Planner 拆解")
        planner_tools = Toolkit()  # Q10=A：Planner 只有 spawn_subagents？不对——Planner 拆解任务文本不需要工具！
        # （Q10=A 的"spawn_subagents 仅注入给 Planner"是指当 Planner Agent 需要启动 Worker 时。
        #  但我们在 Q2=C + Q12=A 下是"PWC Python while 控制流程"，Planner Agent 只负责
        #  "拆解" 和 "汇总" 两个文本任务，**不**调 spawn_subagents（Worker 由 PWC 直接 new Agent 执行）。
        #  所以这里 planner_tools 是空 Toolkit。这是 Q2=C + Q11=A + Q12=A 组合下的实际结果：
        #  Planner 只做纯文本推理，不走工具；Worker 是单独的 Agent 实例。）
        planner_agent = _make_agent(
            role_name="planner",
            system_prompt=self._final_planner_prompt,
            llm=self.llm,
            tools=planner_tools,  # 空 toolkit
            max_steps=2,          # 拆解只要 1~2 步 LLM 调用
            shared_renderer=self._renderer,
            shared_terminator=self.terminator,
            shared_plugin_manager=self.plugin_manager,
            shared_circuit_breaker_registry=self.circuit_breaker_registry,
            compact_threshold=4000,  # Planner 对话短，降低 compact 阈值
        )
        # 拆解：把用户任务 + "现在开始拆解"指令（通过 memory 加 user 消息）喂给 Planner
        planner_decompose_input = (
            f"请开始拆解。用户任务：{run_task}\n\n"
            "输出格式：每行一个子任务，不要编号。"
        )
        plan_output = planner_agent.run(planner_decompose_input)
        plan_lines = _parse_plan(plan_output)
        # max_plan_items 截断（Prompt 应该限制住，Python 侧兜底）
        plan_lines = plan_lines[: self.max_plan_items]
        if len(plan_lines) < 2:
            # Planner 拆不出 2 项以上时，退化为"整个任务当一项" + 加一个"汇总校验项"
            plan_lines = [run_task, "校验并整合上面的结果，输出最终结论"]

        # 写入 trace
        for i, desc in enumerate(plan_lines, 1):
            _trace.plan_items.append(PlanItem(idx=i, description=desc))
        self._renderer.print(f"拆解为 {len(_trace.plan_items)} 个子任务:")
        for item in _trace.plan_items:
            self._renderer.print(f"  {item.idx}. {item.description}")

        # =============================================================
        # Phase B：串行子任务（每个：Worker 执行 → Critic 审查 → while 重做）
        # =============================================================
        context_parts: list[str] = []  # Q4=A 串行上下文：已 approved 的 plan item 结果作为后续 Worker 的输入

        for plan_item in _trace.plan_items:
            plan_item.status = "running"
            plan_idx = plan_item.idx

            context_block = "\n".join(context_parts) if context_parts else ""

            # Worker 循环：最多 1 + max_revisions 次（首次 + max_revisions 次重做）
            worker_attempt = 0
            final_artifact = ""
            approved = False

            while worker_attempt <= self.max_revisions:
                worker_attempt += 1

                # ── 构造 Worker 输入 prompt ──
                subtask_desc = plan_item.description
                revision_block = ""
                input_has_comment = False
                if worker_attempt > 1:
                    # Q4=C：重做时追加 Critic 评语 + 前一版结果
                    last_review = _trace.critic_reviews[-1] if _trace.critic_reviews else None
                    prev_result = _trace.worker_runs[-1].raw_artifact if _trace.worker_runs else ""
                    if last_review:
                        revision_block = (
                            "前一版 Critic 打回，评语如下（请据此修改）：\n"
                            f"  Critic 评语：{last_review.comment}\n"
                            f"  前一版 Worker 输出：\n{prev_result}\n\n"
                            "请基于以上修正，重新输出正确结果。"
                        )
                        input_has_comment = True

                worker_prompt = self._final_worker_prompt.format(
                    subtask_description=subtask_desc,
                    context_block=context_block or "(无前置上下文)",
                    critic_revision_block=revision_block or "(首次执行，无 Critic 反馈)",
                )

                # ── Worker Agent 执行 ──
                t0 = time.time()
                worker_agent = _make_agent(
                    role_name=f"worker_{plan_idx}_v{worker_attempt}",
                    system_prompt=worker_prompt,  # 把完整 prompt 直接当 system prompt 给 Worker Agent
                    llm=self.llm,
                    tools=self.worker_tools,  # Q10=A：业务工具只给 Worker
                    max_steps=self.max_steps_per_worker,
                    shared_renderer=self._renderer,
                    shared_terminator=self.terminator,
                    shared_plugin_manager=self.plugin_manager,
                    shared_circuit_breaker_registry=self.circuit_breaker_registry,
                )
                # Worker 只 run 一个 user 消息（"请开始执行子任务"），然后拿最终答案当 artifact
                worker_result_raw = worker_agent.run("请开始执行上面的子任务。")
                worker_duration_ms = int((time.time() - t0) * 1000)

                # artifact_schema（本实现 schema 可选，默认 worker_tools 不带 schema 时 True）
                schema_ok, schema_err = True, None
                # 若未来用户要加 schema，可在 WorkerToolkit 上挂 artifact_schema 属性；
                # 当前 v1 不做，保持简单。

                # 记录 WorkerRun
                wr = WorkerRun(
                    plan_idx=plan_idx,
                    attempt=worker_attempt,
                    input_has_critic_comment=input_has_comment,
                    raw_artifact=worker_result_raw,
                    artifact_schema_pass=schema_ok,
                    duration_ms=worker_duration_ms,
                    sub_agent_trace_snapshot=_extract_nested_trace(worker_agent),
                )
                _trace.worker_runs.append(wr)

                # ── Critic 审查 ──
                t1 = time.time()
                critic_prompt = self._final_critic_prompt.format(
                    subtask_description=subtask_desc,
                    worker_result=worker_result_raw,
                )
                critic_tools = Toolkit()  # Critic 也纯文本，不用工具
                critic_agent = _make_agent(
                    role_name=f"critic_{plan_idx}_v{worker_attempt}",
                    system_prompt=critic_prompt,
                    llm=self.llm,
                    tools=critic_tools,
                    max_steps=1,
                    shared_renderer=self._renderer,
                    shared_terminator=self.terminator,
                    shared_plugin_manager=self.plugin_manager,
                    shared_circuit_breaker_registry=self.circuit_breaker_registry,
                )
                critic_output = critic_agent.run("请开始审查。")
                critic_duration_ms = int((time.time() - t1) * 1000)
                c_approved, c_comment = _parse_critic(critic_output)

                # 记录 CriticReview
                _trace.critic_reviews.append(CriticReview(
                    plan_idx=plan_idx,
                    worker_attempt=worker_attempt,
                    approved=c_approved,
                    comment=c_comment,
                    duration_ms=critic_duration_ms,
                ))

                if c_approved:
                    final_artifact = worker_result_raw
                    approved = True
                    break
                # 没通过：revisions +=1，while 循环继续（若没到 max_revisions）
                plan_item.revisions = worker_attempt  # 已尝试次数就是 revisions 当前值

            # ── Worker 循环结束 ──
            if approved:
                plan_item.status = "done"
                plan_item.artifact = final_artifact
                # 加入串行上下文
                context_parts.append(f"[子任务{plan_idx} 结果（已通过审查）]\n{final_artifact}")
                self._renderer.panel(final_artifact[:200],
                                     title=f"子任务 {plan_idx} · 通过",
                                     border_style="green")
            else:
                # Q19=A：max_revisions 耗尽仍 FAIL → 标 failed + 继续下一子任务（不 abort）
                plan_item.status = "failed"
                plan_item.artifact = _trace.worker_runs[-1].raw_artifact  # 用最后一版占位
                context_parts.append(
                    f"[子任务{plan_idx} 结果（Critic 连续打回 {self.max_revisions} 次未过，仅供参考）]\n"
                    f"{plan_item.artifact}"
                )
                self._renderer.panel(plan_item.artifact[:200],
                                     title=f"子任务 {plan_idx} · Critic 未通过（耗尽）",
                                     border_style="red",
                                     revisions=str(self.max_revisions))

        # =============================================================
        # Phase C：Planner 汇总
        # =============================================================
        self._renderer.rule("PWC · Planner 汇总")
        results_block = "\n".join(context_parts)
        summary_input = (
            f"所有子任务已完成。用户原始任务：{run_task}\n\n"
            f"各子任务执行结果（包含 Critic 状态标注）：\n{results_block}\n\n"
            "请基于以上信息给出最终答案。如果有子任务被标 '仅供参考'，请在答案中**显式标注**对应说明。"
        )

        summary_agent = _make_agent(
            role_name="planner_summary",
            system_prompt=self._final_planner_prompt,
            llm=self.llm,
            tools=Toolkit(),  # 汇总也不用工具
            max_steps=2,
            shared_renderer=self._renderer,
            shared_terminator=self.terminator,
            shared_plugin_manager=self.plugin_manager,
            shared_circuit_breaker_registry=self.circuit_breaker_registry,
            compact_threshold=4000,
        )
        final_answer = summary_agent.run(summary_input)

        # 收尾
        _trace.end_ts = time.time()
        _trace.total_duration_ms = int((_trace.end_ts - _start) * 1000)

        self._renderer.markdown(final_answer[:500], title="最终答案（摘要）")
        return PWCResult(final_answer=final_answer, trace=_trace)


# =============================================================
# 私有辅助：解析 + trace 摘出
# =============================================================
def _parse_plan(output: str) -> list[str]:
    """Planner 拆解输出 → 子任务字符串列表。
    规则：按 \n 切分，去掉空行、去掉前后空白、去掉行首数字编号（1. / 2) / 3））等。
    """
    lines = []
    for line in (output or "").split("\n"):
        s = line.strip()
        if not s:
            continue
        # 去行首编号
        import re
        s = re.sub(r"^(\d+[\.\)、\s]+)", "", s).strip()
        if s:
            lines.append(s)
    return lines


def _parse_critic(output: str) -> tuple[bool, str]:
    """Critic 审查输出 → (approved, comment)。
    规则：第一行全大写包含 PASS → approved；否则 FAIL（容错：只要首行不含 PASS 就 FAIL）。
    第二行起是评语。
    """
    lines = (output or "").split("\n")
    first_line = lines[0].strip().upper() if lines else ""
    approved = first_line.startswith("PASS")
    # 容错：如果没两行，把全文当评语
    comment = "\n".join(lines[1:]).strip() or (output or "").strip() or "(无评语)"
    return (approved, comment)


def _extract_nested_trace(agent: Agent) -> dict:
    """从 Agent.trace（嵌套 Trace 树）摘出 demo 断言需要的最小扁平字典。"""
    t: AgentTrace = agent.trace
    return {
        "step_count": len(t.steps) if hasattr(t, "steps") else 0,
        "tool_names": sorted({sr.name for sr in t.steps for sr in [sr] if hasattr(sr, "tool_calls")}) if hasattr(t, "steps") else [],
        "status": t.meta.get("status", "unknown") if hasattr(t, "meta") else "unknown",
    }
```

### 2.3 `atlas/patterns/__init__.py` 回填

```python
# atlas/patterns/__init__.py
"""Atlas 多 Agent 产品级模式包（独立于 atlas.core，仅组合内核原语）。

当前提供：
- PlannerWorkerCritic：Planner 拆解 → 串行 Worker 执行 → Critic 打回重做 → Planner 汇总（质量闭环）
"""
from .planner_worker_critic import PlannerWorkerCritic, PWCResult, PWCTrace

__all__ = ["PlannerWorkerCritic", "PWCResult", "PWCTrace"]
```

**验证 Step 2.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
# 1. 顶层 re-export 正确（Q17=A）
from atlas.patterns import PlannerWorkerCritic, PWCResult, PWCTrace
print('atlas.patterns re-export OK')

# 2. atlas 顶层不 patterns 不进顶层
import atlas
assert not hasattr(atlas, 'PlannerWorkerCritic'), 'PWC 不应进 atlas 顶层'
print('atlas 顶层不暴露 patterns OK')

# 3. 构造函数字段数（Q18=A：必填 2 + 可选 10 = 12 且顺序正确）
import dataclasses
fields = {f.name: f.default for f in dataclasses.fields(PlannerWorkerCritic)}
required = [n for n, d in fields.items() if d is dataclasses.MISSING]
optional = [n for n, d in fields.items() if d is not dataclasses.MISSING]
assert len(required) == 2, f'必填字段应为 2 实际 {len(required)}: {required}'
assert 'llm' in required and 'worker_tools' in required
assert len(optional) == 10, f'可选字段应为 10 实际 {len(optional)}: {optional}'
assert fields['max_revisions'] == 2
assert fields['max_plan_items'] == 4
assert fields['max_steps_per_worker'] == 10
print('PlannerWorkerCritic 构造函数 2+10=12 字段 OK')

# 4. PWCResult / PWCTrace 结构存在
r = PWCResult(final_answer='test', trace=PWCTrace())
assert isinstance(r.trace, PWCTrace)
assert hasattr(r.trace, 'plan_items') and hasattr(r.trace, 'worker_runs') and hasattr(r.trace, 'critic_reviews')
print('PWCResult + PWCTrace 形状 OK')

# 5. _parse_plan + _parse_critic 解析（import 内部模块）
from atlas.patterns.planner_worker_critic import _parse_plan, _parse_critic
assert _parse_plan('任务一\\n2) 任务二\\n\\n3. 任务三') == ['任务一', '任务二', '任务三']
ok, cmt = _parse_critic('PASS\\n完成得很好')
assert ok and '很好' in cmt
ok, cmt = _parse_critic('FAIL\\n缺少论据')
assert not ok and '缺少论据' in cmt
# 容错：没 PASS 关键字就 FAIL
ok, _ = _parse_critic('我觉得 OK\\n理由...')
assert not ok
print('_parse_plan + _parse_critic 4 场景 OK')

print('=== Step 2.V 通过 ===')
"
```

**回滚方式**：删 `atlas/patterns/planner_worker_critic.py`，清空 `atlas/patterns/__init__.py`（回到 Step 1 状态）
**风险**：低（纯新建，无现有代码依赖）

---

## Step 3：examples/pwc_demo.py 新建（5 项断言 FakeLLM 可复现）

**目的**：Q7=A 5 项断言 + Q13=A FakeLLM 4 阶段 + Q16=C 替代旧 multi_agent_demo.py。支持 `--mock --plain`（断言模式）与 `--mock --dashboard`（断言 + Dashboard 可视化）。

**改动文件**（1 新建）：

| 文件 | 内容分块 | 行数估算 |
|------|---------|---------|
| `examples/pwc_demo.py`（**新建**） | 1. 头部文档注释（预期行为 + 如何判断成功）<br>2. argparse：--mock / --plain / --dashboard / --port<br>3. SideEffectTracker 不需要（PWC 不涉及副作用）；用全局 Counter 控制 FakeLLM 阶段切换<br>4. **PWCFakeLLM** 类（继承 LLM，覆写 chat()）4 阶段行为精确控制：<br>   ① Planner 拆解：固定输出 3 行 plan<br>   ② Worker（串行 3 次，每次含 Critic 重做循环）：<br>     - plan1 v1：故意输出"缺证据版本"→ Critic FAIL→ plan1 v2：补证据版本→ Critic PASS<br>     - plan2 v1：一次 PASS<br>     - plan3 v1：一次 PASS<br>   ③ Critic（每个 Worker 1~2 次）：plan1 v1 输出"FAIL\\n缺少闰年判定依据"，其余"PASS\\n结果正确"<br>   ④ Planner 汇总：固定输出含"闰年"关键字<br>5. demo 主流程：构造 PWC → run(task) → 5 项 assert<br>6. --dashboard 分支：后台起 Dashboard + publish | +180 行 |

**关键 FakeLLM 骨架**：
```python
# examples/pwc_demo.py FakeLLM 段
from atlas.llm.base import LLM

class PWCFakeLLM(LLM):
    model: str = "mock-pwc"
    _call_count: int = 0

    def chat(self, messages, tools=None):
        self._call_count += 1
        ts = int(time.time() * 1000)
        content = messages[-1].get("content") or ""

        # Phase A：Planner 拆解（第 1 次 LLM 调用）
        if self._call_count == 1:
            return {"message": {"role": "assistant", "content": (
                "计算 2024 是否是闰年并算总小时数\n"
                "获取当前时间\n"
                "总结两者关联"
            )}, "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "duration_ms": 10}

        # Phase B1：Worker 1 v1（缺证据版本）
        if self._call_count == 2:
            return {"message": {"role": "assistant", "content": "2024 年共 366*24 = 8784 小时"}, "usage": {"prompt_tokens": 100, "completion_tokens": 50}, "duration_ms": 10}
        # Critic 1 v1：FAIL
        if self._call_count == 3:
            return {"message": {"role": "assistant", "content": "FAIL\n缺少闰年判定依据。你只算了 366*24 的小时数，但没先论证 2024 为什么是闰年。请补充'2024 能被 4 整除且不能被 100 整除，或能被 400 整除'的判定依据。"}, "usage": {"prompt_tokens": 120, "completion_tokens": 100}, "duration_ms": 10}
        # Worker 1 v2（修正版本，含闰年判定）
        if self._call_count == 4:
            return {"message": {"role": "assistant", "content": "2024 能被 4 整除（2024/4=506）且不能被 100 整除，符合闰年判定规则，因此 2024 是闰年，共 366 天，即 366×24=8784 小时"}, "usage": {"prompt_tokens": 200, "completion_tokens": 80}, "duration_ms": 10}
        # Critic 1 v2：PASS
        if self._call_count == 5:
            return {"message": {"role": "assistant", "content": "PASS\n已补充闰年判定依据，计算正确"}, "usage": {"prompt_tokens": 200, "completion_tokens": 50}, "duration_ms": 10}

        # Worker 2（一次性 PASS）+ Critic + Worker 3 + Critic + Summary
        # （后续按 count 顺序平铺，逻辑同上；实际实现时一一对应）
        # 这里只列骨架，完整实现中精确映射所有 LLM 调用轮次

        # 兜底：超出预期 call 数返回空字符串
        return {"message": {"role": "assistant", "content": "（mock 默认响应：完成）"}, "usage": {"prompt_tokens": 50, "completion_tokens": 50}, "duration_ms": 10}
```

**5 项断言清单（Q7=A + Q13=A 精确对齐）**：
```python
# examples/pwc_demo.py 断言段
from atlas.patterns import PlannerWorkerCritic, PWCResult, PWCTrace

# ... 构造 PWC + FakeLLM + run(task) 后
result: PWCResult = pwc.run(task)
t = result.trace

# ① plan 拆解得到 3 个子任务
assert len(t.plan_items) == 3, f"plan 数量: 预期 3 实际 {len(t.plan_items)}"
assert t.plan_items[0].description == "计算 2024 是否是闰年并算总小时数"
print("[1] plan 拆解 3 项 OK")

# ② Worker artifact_schema_pass == True（本 demo 无 schema，_common._validate 空 schema 返回 True）
plan1_runs = [w for w in t.worker_runs if w.plan_idx == 1]
assert all(w.artifact_schema_pass for w in plan1_runs)
print("[2] Worker schema 校验 OK")

# ③ Critic 打回 1 次 + revisions 计数器 = 1 + 最终 approved
c1_reviews = [c for c in t.critic_reviews if c.plan_idx == 1]
assert len(c1_reviews) == 2, f"子任务 1 Critic 审查次数: 预期 2 实际 {len(c1_reviews)}"
assert c1_reviews[0].approved is False and c1_reviews[1].approved is True
assert t.plan_items[0].revisions == 1  # Q13 断言③
assert t.plan_items[0].status == "done"
print("[3] Critic 打回 1 次后通过 OK（revisions=1）")

# ④ final_answer 含关键字
assert "闰年" in result.final_answer, f"最终答案缺关键字'闰年': {result.final_answer[:100]}"
print("[4] 最终答案含'闰年'关键字 OK")

# ⑤ 重做 Worker 时输入包含了 Critic FAIL 评语
plan1_v2 = [w for w in t.worker_runs if w.plan_idx == 1 and w.attempt == 2][0]
assert plan1_v2.input_has_critic_comment is True, "重做 Worker 未检测到 Critic 评语注入"
print("[5] Worker 重做输入含 Critic 评语 OK")

print("\n=== pwc_demo.py --mock 5 项断言全部通过 ===")
```

**验证 Step 3.V（单独 pwc_demo）**：
```powershell
cd e:\Learn_Project\Atlas
python examples/pwc_demo.py --mock --plain
# 期望：5 项断言全部打印 OK
```

---

## Step 4：README 更新 + atlas/__init__.py 检查

**改动文件**（1 修改 + 1 检查）：

| 文件 | 改动点 |
|------|--------|
| `README.md`（修改） | 6 处更新：<br>① "核心特性（已实现）"段追加 PWC 模式包说明<br>② "快速开始示例"bash 代码块追加 `python examples/pwc_demo.py --mock --plain` 示例，并写清楚 Q21=A **并行 vs 质量闭环 定位差异**注释<br>③ "项目结构"段：`atlas/` 下新增 `patterns/` 子树条目（`planner_worker_critic.py` / `_common.py` / `__init__.py`）；`examples/` 下把 `multi_agent_demo.py` 条目替换为 `pwc_demo.py`<br>④ "9 个核心原语"：无变化<br>⑤ "路线图"：Phase 2.2 标题下原内容不动；**原 2.3 顺延为 2.4**；在 Phase 2.2 与顺延 2.4 之间插入新 "Phase 2.3 产品级模式包（atlas.patterns）" 小节，5 个勾选项全打勾 ✓<br>⑥ "开发规则"：无变化 |
| `atlas/__init__.py`（检查，**不修改**） | 运行时确认 `hasattr(atlas, 'PlannerWorkerCritic') == False`（Q6=A patterns 不进顶层） |

**验证 Step 4.V**：
```powershell
cd e:\Learn_Project\Atlas
python -c "
# patterns 不进顶层（Q6=A）
import atlas
assert not hasattr(atlas, 'PlannerWorkerCritic'), 'PWC 不应进 atlas 顶层'
# patterns 子包独立可用
from atlas.patterns import PlannerWorkerCritic
print('README + import 路径约束 OK')
"
```

---

## Step 5：整体验证矩阵（V1/V2/V3）

### V1：分步 python -c 导入验证（每步独立，已在各步 V 节列出）
跑完 V1 后无 ImportError / AttributeError / AssertionError 即过。

### V2：pwc_demo --mock --plain 5 项断言
```powershell
cd e:\Learn_Project\Atlas
python examples/pwc_demo.py --mock --plain
# 期望输出：
#   [1] plan 拆解 3 项 OK
#   [2] Worker schema 校验 OK
#   [3] Critic 打回 1 次后通过 OK（revisions=1）
#   [4] 最终答案含'闰年'关键字 OK
#   [5] Worker 重做输入含 Critic 评语 OK
#   === pwc_demo.py --mock 5 项断言全部通过 ===
```

### V3：10 个已有 mock demo 回归（不能破坏现有行为）
```powershell
cd e:\Learn_Project\Atlas
# 支持 --mock 的 8 个串联
python examples/parallel_subagent_demo.py --mock --plain ;
python examples/tool_abi_demo.py --mock ;
python examples/journal_replay_demo.py --mock --plain ;
python examples/terminator_demo.py --mock --plain ;
python examples/plugin_cost_demo.py --mock --plain ;
python examples/saga_demo.py --mock --plain ;
python examples/compact_demo.py --mock --plain ;
python examples/circuit_retry_demo.py --mock --plain ;
# 无 mock 的 2 个
python examples/scope_demo.py ;
python examples/dashboard_demo.py ;
```
期望：所有 assert 不触发、进程 exit_code=0。

### 交付依赖
- Steps 0 & 1：完全并行（无依赖）
- Step 2：依赖 Step 1（_common.py 的 2 个辅助）
- Step 3：依赖 Steps 0/1/2（PWC 主类 + demo 5 断言）
- Step 4：依赖 Steps 0/1/2/3（README 引用 demo 名称必须存在）
- V2：依赖 Steps 0/1/2/3
- V3：依赖所有 Steps 0~4

---

## 风险矩阵（4 项）

| 风险 | 概率 | 影响 | 缓解 |
|------|------|------|------|
| 删除 core/orchestrator.py 后有隐式 import 没被 grep 出来 | 低 | 中：V3 某个 demo ImportError | grep 前置 + V3 回归立刻发现；git checkout 恢复 10 秒 |
| FakeLLM call_count 精确计数和实际 PWC LLM 调用轮次对不上 | 中 | 高：V2 断言跑不通 | 实现时每轮 FakeLLM 的 call_count 在 `print()` 里打出来，手动比对一次精确值后写死 |
| Agent.__init__ 参数未来增加，_make_agent() copy 漏更新导致默认值不一致 | 低 | 中：PWC 行为与 Agent 默认行为 drift | 用户规则 6：**copy 再改写**，本计划中 `_make_agent()` 注释明确要求"从 agent.py L37-L60 copy 签名"——每次改动 Agent 参数后必须同步改；出问题 V3 中 saga_demo/compact_demo 等会先暴露 |
| Critic 解析 `_parse_critic("我觉得 OK...")` 返回 FAIL 过度保守导致 max_revisions 耗尽标记 failed | 中 | 中：真实 LLM 下可能 PASS 被标 FAIL | FakeLLM 不影响；真实场景只影响 trace 标记不抛异常 abort（Q19=A 标 failed 继续），属优雅降级；后续若需要可放宽解析（"含 PASS 字串不强制首行"）但不进 Phase 2.3 |

---

## 交付清单最终确认

**文件数总计：11（3 删除 + 5 新建 + 3 修改）**

| 类型 | 路径 | 类型 | 归属 Step |
|---|---|---|---|
| D | `atlas/core/orchestrator.py` | 删除 | 0 |
| D | `examples/multi_agent_demo.py` | 删除 | 0 |
| N | `atlas/patterns/__init__.py` | 新建（空→Step 2 末回填） | 1 + 2 |
| N | `atlas/patterns/_common.py` | 新建（2 个辅助） | 1 |
| N | `atlas/patterns/planner_worker_critic.py` | 新建（主实现 + 5 dataclass + prompt 常量） | 2 |
| N | `examples/pwc_demo.py` | 新建（FakeLLM + 5 断言 + dashboard 分支） | 3 |
| N | `docs_feishu/phase2-3_planner_worker_critic_implementation_plan.md` | 新建（本文件） | 实施前交付 |
| M | `atlas/patterns/__init__.py`（回填 3 行 import） | 修改 | 2 末 |
| M | `README.md`（6 处更新） | 修改 | 4 |
| M | `atlas/__init__.py` | **检查（不修改）** | 4 |
| M | `pyproject.toml` | 不需要改（patterns 是纯 Python 子包） | — |

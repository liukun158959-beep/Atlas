# Phase 2.1：并行子 Agent 内核原语实施计划

> 设计决策来源：grill-me 盘问 4 轮共 22 个决策点，已达共享理解。
> 实施策略：自底向上，每步有最小可验证边界。
> 涉及文件：6 个内核文件 + 1 个 demo 文件 + 1 个导出文件。

---

## 前置准备（实施前必做，可行性审查后补充）

本章节为可行性审查确认的前置动作，在 Step 1 之前必须完成。

### P-0 新增 jsonschema 依赖（DR-3 流程已通过）

**操作**：在 `pyproject.toml` 的 `[project].dependencies` 中追加 `"jsonschema>=4.0"`。

**DR-3 审批说明**：jsonschema 含 attrs / pyrsistent / referencing / rpds-py 四项传递依赖，
体积超出"<100KB、无 transitive 膨胀"字面条款，但为事实标准（LLM 可直接生成 JSON Schema
描述输出格式），手写极简校验器的工程复杂度与维护成本显著高于引入依赖，故正式批准。
rpds-py 虽提供二进制 wheel，但含 pure Python fallback，不破坏 Windows 安装体验。

**验证**：
```bash
cd Atlas
pip install -e .   # 或在 .venv 激活后执行
python -c "import jsonschema; print(jsonschema.__version__)"
```

### P-1 已知限制声明：ThreadPool 超时与子线程

Q18 的 wall-clock 超时使用 `ThreadPoolExecutor(max_workers=1).submit(...).result(timeout=X)`。
TimeoutError 触发后**子线程不会被强制终止**（Python 线程无安全 kill API），子 Agent 内部的
LLM HTTP 请求通常在 SDK 超时（30-60s）后自行结束，一般不会造成常驻泄漏。
极端场景（max_steps 极大 + 工具全部瞬时返回 + 无 HTTP）下存在小概率线程累积，
可接受，未来需要强保障时改用"协作式 deadline 检查 + ThreadPool 兜底"双保险。

---

## 前置检查

实施前需确认的现状（已核实）：

- `atlas/core/multi_agent.py` 存在，但当前内容是 **Planner-Worker-Critic 编排器**（`MultiAgentOrchestrator`），与并行子 Agent 无关。处理方式：**重命名该文件为 `atlas/core/orchestrator.py`**，空出 `multi_agent.py` 给并行原语。
- `atlas/core/agent.py`：`Agent.__init__` 参数为 `(llm, tools, system_prompt, max_steps, renderer)`；实例有 `trace: Trace`、`memory: Memory`、`renderer`。
- `atlas/context/trace.py`：`Trace` 只有 `steps: list[StepRecord]`，`render()` 方法不存在（demo 的 `ParallelTrace` 自己实现了 render）。
- `atlas/tools/base.py`：`Toolkit.register(func, name, description, parameters)` 返回 Tool；`Tool.parameters` 是静态 dict，没有动态生成能力。
- `atlas/__init__.py`：只导出 `Agent/LLM/Tool/Toolkit/Memory/Trace/StepRecord/Renderer/PlainRenderer`。
- `examples/parallel_subagent_demo.py`：当前约 400 行，包含全部 demo 实现（SubagentSpec/Result/RoleConfig/_run_subagent_sync/durable_gather/make_spawn_subagents_tool/ParallelTrace），下沉后将重写为≈80行的最小调用示例。
- `examples/multi_agent_demo.py`：当前 `from atlas.core.multi_agent import MultiAgentOrchestrator`，重命名文件后需同步改为 `from atlas.core.orchestrator import MultiAgentOrchestrator`。

---

## Step 1：创建 multi_agent 模块，定义数据类 + 闭包工具骨架

### 目标
把 demo 中的 4 个数据类 + 闭包工具工厂移到内核模块，但先不接任何业务逻辑，只有结构和占位实现。最小可编译通过。

### 变更文件

#### 1.1 重命名：`atlas/core/multi_agent.py` → `atlas/core/orchestrator.py`
- **操作 1**：文件级 rename（按用户规则 6，先 copy 再删原文件，避免 rename 原子失败导致丢失）
  ```
  copy atlas\core\multi_agent.py atlas\core\orchestrator.py
  del  atlas\core\multi_agent.py
  ```
- **操作 2**：同步修改 `examples/multi_agent_demo.py` 导入
  ```python
  # 修改前
  from atlas.core.multi_agent import MultiAgentOrchestrator
  # 修改后
  from atlas.core.orchestrator import MultiAgentOrchestrator
  ```
- **原因**：当前 `multi_agent.py` 存的是 `MultiAgentOrchestrator`（Planner-Worker-Critic 串编排器），名实不符，需让 `multi_agent.py` 回归"并行子 Agent 原语"语义。不改导入将直接 `ImportError`。

#### 1.2 新建：`atlas/core/multi_agent.py`（完整内容≈220行）
在空文件中按顺序定义以下内容：

**A. 4 个数据类（从 demo 拷贝改写，去 LLM/Toolkit 强引用）：**

```python
@dataclass
class RoleConfig:
    name: str
    system_prompt: str
    tool_names: list[str]
    temperature: float = 0.3
    max_steps: int = 5
    timeout_ms: int = 60_000   # Q18：新增，RoleConfig 带 wall-clock 超时

@dataclass
class SubagentSpec:
    subagent_id: str
    role: str
    task: str
    # 注入后填充：_role_config、_llm、_toolkit、_renderer_cls
    extra_system_prompt: str | None = None
    artifact_schema: dict | None = None

@dataclass
class SubagentResult:
    subagent_id: str
    role: str
    task: str
    artifact: str
    duration_ms: int
    error: str | None = None
    # artifact_schema 校验结果：通过=True/缺失=None/不通过=False
    schema_passed: bool | None = None
    # 不通过时的校验错误消息
    schema_error: str | None = None
```

**B. `_run_subagent(spec, agent_ref)` → SubagentResult**
- 先用 `def _run_subagent(*args, **kwargs): raise NotImplementedError("Step 2 实现")` 占位
- 签名里 `agent_ref` 用于后面 Step 2 访问 `agent_ref.role_configs`、`agent_ref.llm`、`agent_ref.tools` 全局池、`agent_ref.renderer`（用于拿 renderer 类型但不直接打印）

**C. `parallel_gather(specs, agent_ref) -> list[SubagentResult]`**
- 先用占位实现：`return [SubagentResult(..., error="Step 3 实现")]`

**D. 闭包工具工厂 `make_spawn_subagents_tool(agent) -> tuple[str, str, dict, Callable[[dict], str]]`**
- 工厂返回 `(name, description, parameters_schema_factory, func)` 四元组（注意 parameters 不是静态 dict，是**可调用的 factory**——这是 Q19 schema 懒加载的关键）
- `parameters_schema_factory()` 每次调用时读 `agent.role_configs.keys()` 生成 enum，返回完整 JSON Schema
- `func(args)` 先占位，返回 `"Step 4 实现"`，但里面已经写好嵌套守卫：**用 getattr 做防御性读取**（Step 1 时 Trace 还没 `_parent_trace` 字段，直接访问会 AttributeError），非空则抛 `RuntimeError("spawn_subagents 仅顶层父 Agent 可用，禁止嵌套")`
  ```python
  # 占位 func 内部守卫的推荐写法
  if getattr(agent.trace, '_parent_trace', None) is not None:
      raise RuntimeError("spawn_subagents 仅顶层父 Agent 可用，禁止嵌套")
  ```

### 验证步骤
```bash
cd Atlas
python -c "from atlas.core.multi_agent import RoleConfig, SubagentSpec, SubagentResult, make_spawn_subagents_tool; print('Step 1 OK')"
```

---

## Step 2：实现 `_run_subagent` 单子 Agent 跑通

### 目标
让单个子 Agent 能实例化 + 跑 ReAct 循环 + 返回 SubagentResult。不涉及并行。

### 变更文件

#### 2.1 修改：`atlas/context/trace.py`
- 在 `Trace.__init__` 中新增：
  ```python
  self.subagent_traces: list["Trace"] = []   # Q5：父子嵌套
  self._parent_trace: "Trace | None" = None   # Q17：嵌套守卫用
  ```
- 新增方法 `Trace.render(renderer: Renderer) -> None`（Q9：父 Trace 统一递归渲染）：
  - 先渲染自己的 steps（调用 renderer.rule + renderer.table 或按 demo 的 ParallelTrace.render 格式）
  - 再递归遍历 `self.subagent_traces`，每个子 Trace 调 `.render(renderer)`（开头加缩进或分隔线区分层级）
- 把原来的 `__str__` 保留不变（纯文本 fallback），新的 `render()` 用 renderer

#### 2.2 修改：`atlas/core/agent.py`
- `Agent.__init__` 新增 4 个参数（均放末尾、有默认值，不破坏现有调用）：
  ```python
  role_configs: dict[str, RoleConfig] | None = None,   # Q11：初始集合
  parent_trace: "Trace | None" = None,                   # Q17：嵌套守卫
  auto_inject_spawn_tool: bool = True,                   # Q15：默认自动注入
  tool_specs_pool: list[dict] | None = None,             # 🔴可行性审查补充：全局工具规格池（如 BUILTIN_TOOLS），供 _run_subagent 按角色过滤。内核层不 hardcode 具体工具实现，遵循 EP-2 解耦。
  ```
- `__init__` 末尾赋值：
  ```python
  self.role_configs = role_configs or {}
  self.tool_specs_pool = tool_specs_pool or []
  self.trace._parent_trace = parent_trace   # 告诉 Trace 自己是不是子 Agent
  if self.role_configs and self.auto_inject_spawn_tool:
      self._inject_spawn_tool()   # 有初始 role 时，构造即注入 spawn 工具
  ```
- 新增私有方法 `_inject_spawn_tool(self) -> None`（首次调用才生效，防重复注入 + 🔴子 Agent 快速返回）：
  ```python
  def _inject_spawn_tool(self) -> None:
      # 🔴可行性审查补充优化：子 Agent（有父 Trace）根本不注入 spawn 工具，
      # LLM 连该工具选项都看不到 → 从源头消除误调用，比调用时再抛错更好
      if self.trace._parent_trace is not None:
          return
      if "spawn_subagents" in self.tools._tools:   # 已注入过就跳过
          return
      from atlas.core.multi_agent import make_spawn_subagents_tool
      name, desc, params_factory, func = make_spawn_subagents_tool(self)
      tool = self.tools.register(
          func=func, name=name, description=desc,
          parameters=params_factory(),   # 首次注册用静态 dict 占位
      )
      # Q19 懒加载：注册后把 factory 挂上 Tool 实例（配合 Toolkit.to_openai_tools 改逻辑）
      tool.parameters_factory = params_factory
  ```
- 新增 `Agent.register_role(config: RoleConfig) -> None` 方法：
  ```python
  def register_role(self, config: RoleConfig) -> None:
      was_empty = not self.role_configs
      self.role_configs[config.name] = config
      # 第一个 role 注册时触发注入（之后新增 role 由 parameters_factory 自动感知）
      if was_empty and self.role_configs and self.auto_inject_spawn_tool:
          self._inject_spawn_tool()
  ```
- 重要：由于 `Tool.parameters` 是静态 dict，但我们要支持 schema 懒加载（Q19），所以这里还要改 `Toolkit`——见 Step 2.3。

#### 2.3 修改：`atlas/tools/base.py`
- `Tool` 新增可选字段：
  ```python
  parameters_factory: Callable[[], dict] | None = None
  ```
- `Toolkit.to_openai_tools()` 改逻辑：
  ```python
  for t in self._tools.values():
      params = t.parameters_factory() if t.parameters_factory else t.parameters
      # 后面用 params 替代原来的 t.parameters
  ```
- `Toolkit.register()` 的签名保留不变，但返回的 `Tool` 对象上可以直接赋值 `tool.parameters_factory = xxx`（便于 Step 4 注入懒加载）

#### 2.4 修改：`atlas/core/multi_agent.py` — 填充 `_run_subagent`
```python
def _run_subagent(spec: SubagentSpec, agent_ref: "Agent") -> SubagentResult:
    t0 = time.perf_counter()
    # 1. 拿到 RoleConfig（来自 agent_ref.role_configs[spec.role]）
    role_cfg = agent_ref.role_configs[spec.role]

    # 2. 按 RoleConfig.tool_names 从 agent_ref.tool_specs_pool 过滤，构建子 Toolkit
    #    🔴可行性审查修正：内核不 hardcode BUILTIN_TOOLS，遵守 EP-2 解耦；
    #    规格池由上层（如 demo）在构造 Agent 时通过 tool_specs_pool 参数传入。
    if role_cfg.tool_names and not agent_ref.tool_specs_pool:
        raise ValueError(
            f"角色 {role_cfg.name} 需要工具 {role_cfg.tool_names}，"
            "但 Agent.tool_specs_pool 为空。构造父 Agent 时请传入 "
            "tool_specs_pool=BUILTIN_TOOLS（或其他全局工具规格列表）。"
        )
    sub_toolkit = Toolkit()
    name_to_specs = {s["name"]: s for s in agent_ref.tool_specs_pool}
    for tool_name in role_cfg.tool_names:
        if tool_name not in name_to_specs:
            raise ValueError(
                f"角色 {role_cfg.name} 要求的工具 {tool_name} 不在工具规格池中，"
                "请检查 tool_specs_pool 是否包含该工具。"
            )
        sub_toolkit.register(**name_to_specs[tool_name])
    # 子 Agent 永不注入 spawn_subagents（即使有 role_configs），因为 _inject_spawn_tool
    # 内部已按 _parent_trace 快速返回；再显式置 auto_inject_spawn_tool=False 双保险。

    # 3. 子 Agent 实例化
    sub_agent = Agent(
        llm=agent_ref.llm,                           # 共享 LLM
        tools=sub_toolkit,
        system_prompt=(role_cfg.system_prompt
                       + (spec.extra_system_prompt or "")),
        max_steps=role_cfg.max_steps,
        renderer=type(agent_ref.renderer)(),          # Q20：同类型新实例
        parent_trace=agent_ref.trace,                  # 告诉 Trace 有父 => 嵌套守卫生效
        auto_inject_spawn_tool=False,                  # 双保险：子 Agent 绝不注入 spawn
        tool_specs_pool=agent_ref.tool_specs_pool,     # 池子透传（未来若允许嵌套，可直接复用）
    )

    # 4. wall-clock 超时（Q18）：ThreadPoolExecutor + future.result(timeout)
    #    已知限制（前置准备 P-1）：超时子线程不会被 kill，通常 SDK HTTP 超时会自结束
    artifact: str = ""
    error: str | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(sub_agent.run, spec.task)
            artifact = future.result(timeout=role_cfg.timeout_ms / 1000)
    except TimeoutError:
        error = f"子 Agent 超时（{role_cfg.timeout_ms}ms），已返回部分结果"
        artifact = artifact or ""
    except Exception as e:
        error = f"子 Agent 异常: {type(e).__name__}: {e}"

    # 5. artifact_schema 校验（Q4 + Q8）：不通过→标记，artifact 原文保留，error 不改
    schema_passed: bool | None = None
    schema_error: str | None = None
    if spec.artifact_schema and artifact and error is None:
        import jsonschema  # 依赖已在前置 P-0 加入
        try:
            parsed = json.loads(artifact) if isinstance(artifact, str) else artifact
            jsonschema.validate(parsed, spec.artifact_schema)
            schema_passed = True
        except json.JSONDecodeError as e:
            schema_passed = False
            schema_error = f"artifact 非合法 JSON: {e}"
        except jsonschema.ValidationError as e:
            schema_passed = False
            schema_error = e.message
        # Q8：无论校验是否通过，artifact 保留原始文本，绝不改写

    # 6. 挂父子关联（用于后续 Trace.render 递归渲染）
    agent_ref.trace.subagent_traces.append(sub_agent.trace)
    # 7. 子 Trace.meta 记录 role/task/耗时（用于父汇总表渲染）
    duration_ms = int((time.perf_counter() - t0) * 1000)
    sub_agent.trace.meta.update({
        "subagent_id": spec.subagent_id,
        "role": spec.role,
        "task": spec.task,
        "duration_ms": duration_ms,
        "schema_passed": schema_passed,
        "error": error,
    })
    return SubagentResult(
        subagent_id=spec.subagent_id, role=spec.role, task=spec.task,
        artifact=artifact, duration_ms=duration_ms, error=error,
        schema_passed=schema_passed, schema_error=schema_error,
    )
```

### 验证步骤
```bash
cd Atlas
python -c "
from atlas import LLM, Toolkit, Agent
from atlas.core.multi_agent import RoleConfig, SubagentSpec, _run_subagent
from atlas.tools.builtin import BUILTIN_TOOLS
# 🟡可行性审查修正：不存在 build_llm API，直接用 LLM()（三级配置优先级已够用）
llm = LLM()
# 构造一个父 Toolkit + 注册 RoleConfig
parent_tk = Toolkit()
for s in BUILTIN_TOOLS:
    parent_tk.register(**s)
cfg = RoleConfig(
    name='assistant',
    system_prompt='你是助手',
    tool_names=['calculator'],
    max_steps=3,
    timeout_ms=30_000,
)
agent = Agent(
    llm=llm, tools=parent_tk,
    role_configs={'assistant': cfg},
    tool_specs_pool=BUILTIN_TOOLS,   # 🔴可行性审查补充：工具规格池由上层传入
)
spec = SubagentSpec(subagent_id='s1', role='assistant', task='1+1等于几')
result = _run_subagent(spec, agent)
assert result.artifact is not None and result.error is None, f'异常: {result.error}'
print('Step 2 OK')
"
```

---

## Step 3：实现 `parallel_gather` 并行（ThreadPoolExecutor + partial success + 超时）

### 目标
多个子 Agent 同时启动，等全部结束返回结果，失败不拖垮整体（partial success）。`_run_subagent` 已在 Step 2 验证。

### 变更文件

#### 3.1 修改：`atlas/core/multi_agent.py` — 填充 `parallel_gather`
```python
def parallel_gather(
    specs: list[SubagentSpec],
    agent_ref: "Agent",
) -> list[SubagentResult]:
    t0 = time.perf_counter()
    # Q7：去掉 asyncio，直接用 ThreadPoolExecutor + future.result
    # Q13：max_workers = len(specs)（子 Agent 是 IO 密集，线程数 = 任务数完全 OK）
    # Q10：partial success（一个崩溃不影响其他）
    #
    # 🟢可行性审查优化：用 enumerate 建 (spec, idx) 映射，避免 specs.index() O(n) 查找
    results: list[SubagentResult | None] = [None] * len(specs)
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        future_map: dict[Future, tuple[SubagentSpec, int]] = {}
        for idx, spec in enumerate(specs):
            fut = pool.submit(_run_subagent, spec, agent_ref)
            future_map[fut] = (spec, idx)

        for future in as_completed(future_map):
            spec, idx = future_map[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                # future 层异常只在崩溃级未预期错误时出现；预期内的超时/工具失败等
                # 已在 _run_subagent 内部捕获为 SubagentResult.error=True。
                results[idx] = SubagentResult(
                    subagent_id=spec.subagent_id, role=spec.role,
                    task=spec.task, artifact="", duration_ms=0,
                    error=f"子 Agent 未预期异常: {type(e).__name__}: {e}",
                )

    total_ms = int((time.perf_counter() - t0) * 1000)
    # 并行效率指标写进父 Trace.meta，供 Step 5 的 Trace.render 展示
    sum_child_dur = sum(r.duration_ms for r in results if r is not None)
    parallel_efficiency = sum_child_dur / max(total_ms, 1)
    agent_ref.trace.meta.update({
        "parallel_total_ms": total_ms,
        "parallel_sum_child_ms": sum_child_dur,
        "parallel_efficiency": parallel_efficiency,
    })
    # 防御：results 中的 None 不应该出现（as_completed 遍历全部 future），
    # 若真出现填 error 占位，不返回 None 列表项
    for i, r in enumerate(results):
        if r is None:
            spec = specs[i]
            results[i] = SubagentResult(
                subagent_id=spec.subagent_id, role=spec.role, task=spec.task,
                artifact="", duration_ms=0, error="子 Agent 未返回结果",
            )
    return results   # type: ignore[return-value]
```

#### 3.2 依赖说明
jsonschema>=4.0 依赖已在**前置准备 P-0** 加入 `pyproject.toml`，无需在本 Step 重复修改。
（可行性审查修正：jsonschema 在 Step 2.4 就会用到，必须前置到 P-0，避免顺序问题。）

### 验证步骤
```bash
cd Atlas
python -c "
# 构造 3 个 SubagentSpec（calculator 类任务，耗时短）
# 计时调用 parallel_gather
# 断言实际总耗时 ≈ 单个最长子 Agent，远小于三者之和
# 断言 3 个 SubagentResult 顺序与 specs 顺序一致
# 故意让其中一个 spec 的 task 写成明显会抛异常的字符串，
#   验证 partial success：另外两个正常返回，坏的那个 error 字段有内容
print('Step 3 OK')
"
```

---

## Step 4：接入 Agent — 自动注入 + 闭包 schema 懒加载

### 目标
用户只要在 Agent 构造时传入 `role_configs={...}`（或后续 `register_role()`），工具立即能用，且 schema 动态随 role_configs 更新而更新。spawn_subagents 工具内部把 LLM 发来的 specs 列表走 parallel_gather，返回结构化 JSON。

### 变更文件

#### 4.1 修改：`atlas/core/multi_agent.py` — 填充 `make_spawn_subagents_tool` 的 `func`
```python
def make_spawn_subagents_tool(agent: "Agent"):
    def _func(args: dict) -> str:
        # a) 嵌套守卫（Q17）：agent.trace._parent_trace 非空则抛 RuntimeError
        # b) 遍历 args["subagents"] → 组装 SubagentSpec 列表
        #    role 必须在 agent.role_configs 中（不存在抛 ValueError）
        #    subagent_id: 自动编号 f"sub-{i+1}"
        #    artifact_schema: 如果 args["subagents"][i] 有 artifact_schema 字段就带上
        # c) 调用 parallel_gather(specs, agent)
        # d) 组装返回 JSON：
        #    {
        #      total_duration_ms, subagent_count, parallel_efficiency,
        #      results: [{subagent_id,role,task,artifact,duration_ms,
        #                 error,schema_passed,schema_error}]
        #    }
        #    parallel_efficiency = sum(r.duration_ms) / max(total_ms, 1)
        # e) return json.dumps(..., ensure_ascii=False)
        pass

    def _params_factory() -> dict:
        # Q19：每次 bind_tools 时重新拿 agent.role_configs.keys()
        known_roles = list(agent.role_configs.keys())
        return {
            "type": "object",
            "properties": {
                "subagents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {"type": "string", "enum": known_roles,
                                     "description": f"从枚举中选: {known_roles}"},
                            "task": {"type": "string"},
                            "artifact_schema": {"type": "object",
                                                "description": "可选。JSON Schema，用于校验返回 artifact"},
                        },
                        "required": ["role", "task"],
                    }
                }
            },
            "required": ["subagents"]
        }

    name = "spawn_subagents"
    description = (
        "并行启动多个子 Agent 执行独立子任务。"
        "每个子 Agent 拥有独立上下文，互不干扰。"
        "适用于可并行的独立子任务（多主题调研、多方案对比等）。"
        "可选角色必须从 role enum 中选择，不可自创。"
        "请在单次调用的 subagents 数组中列出所有要并行执行的子任务，"
        "不要分多次调用（并行发生在本工具内部）。"
        "返回结构化结果：含每个子 Agent 的 artifact 和并行效率指标。"
    )
    return name, description, _params_factory, _func
```

#### 4.2 修改：`atlas/core/agent.py` — 说明注入完善
> 🟡可行性审查说明：`_inject_spawn_tool` 的完整实现、`__init__` 末尾注入、`register_role` 的 was_empty 判断，**已在 Step 2.2 一次性写完**（包含子 Agent `_parent_trace` 快速返回、防重复注入、懒加载 factory 挂载三步）。本 Step 2.2 已覆盖，无需二次改动。

#### 4.3 修改：`atlas/__init__.py` — 新增导出
```python
from .core.multi_agent import RoleConfig, SubagentSpec, SubagentResult, parallel_gather
__all__ += ["RoleConfig", "SubagentSpec", "SubagentResult", "parallel_gather"]
```

### 验证步骤
```bash
cd Atlas
python -c "
from atlas import Agent, RoleConfig
# 构造 Agent 时传 role_configs={'x': RoleConfig(...)}
# 检查：agent.tools._tools 中是否有 'spawn_subagents' 键
# 检查：agent.tools.to_openai_tools() 返回的 function schema 中
#       parameters.properties.subagents.items.properties.role.enum
#       等于 role_configs.keys()
# 然后调用 agent.register_role(RoleConfig(name='y', ...))
# 再次检查 enum：应更新为 ['x', 'y']（Q19 schema 懒加载验证）
# 最后构造一个完全独立的子 Agent，传 parent_trace=parent_agent.trace，
#   尝试在子 Agent 里调用 spawn_subagents（或构造时给子 Agent 传 role_configs）
#   预期抛出 RuntimeError（Q17 嵌套守卫验证）
print('Step 4 OK')
"
```

---

## Step 5：Trace 扩展 + 渲染完整链路

### 目标
父 Agent 结束后，`agent.trace.render(renderer)` 能完整输出：父步聚→子步聚表格→并行效率→全部子 Agent artifact。当前 demo 的 ParallelTrace.render 的逻辑要合并进 Trace.render。

### 变更文件

#### 5.1 修改：`atlas/context/trace.py` — 完善 `Trace.render`
- 在 `Trace.__init__` / `Trace` dataclass 里新增：
  ```python
  meta: dict = field(default_factory=dict)   # Q5 + Q9：存 role/task/duration_ms/error/schema_passed/parallel_efficiency
  ```
  注意：Step 2.1 已加了 subagent_traces / _parent_trace，meta 字段在本 Step 追加。
- 实现 `Trace.render(renderer: Renderer, depth: int = 0) -> None`：
  - 🟢可行性审查优化：加 `depth >= 3` 递归上限，防理论多层嵌套导致栈溢出
  - 分 3 段渲染：
    1. **父 Agent 步骤汇总**（`depth == 0` 才渲染这个大号表格标题，深度>0 渲染小一号标题）：用 `renderer.table(rows=[[step_no, thought, actions], ...], headers=['Step', 'Thought', '工具调用'], title='父 Agent 步骤' if depth==0 else f'子 Agent {depth} 步骤')`
    2. **子 Agent 汇总表**（仅 `depth == 0` 展示）：若 `self.subagent_traces` 非空，table 展示 `[subagent_id, role, task_summary, status, duration_ms, schema_passed]`，行数据从 `sub_trace.meta` 读取
    3. **并行效率 Panel**（仅 `depth == 0` 且 `parallel_efficiency` 存在于 meta）：`renderer.panel(content=f"并行效率 x{parallel_efficiency:.2f}  |  串行估算 {sum_ms}ms  → 实际 {total_ms}ms", title="并行效率")`
    4. **递归渲染子 Agent**：每个 `sub_trace.render(renderer, depth=depth+1)`，**递归前调 `renderer.rule(title=f'[{sub_trace.meta.get("role", "?")}] {sub_trace.meta.get("task","?")[:60]}')` 作为分隔线**

#### 5.2 修改：`atlas/core/agent.py` — `Agent.run` 末尾自动渲染 + quickstart 同步清理
- **Agent.run 两个 return 前**都加（正常结束 + max_steps 超限两个路径）：
  ```python
  if self.trace._parent_trace is None:   # 仅顶层父 Agent 自动渲染
      self.trace.render(self.renderer)
  ```
- 🟡可行性审查决策：按项目规则 7"不考虑旧兼容、没有老用户"，**不引入 `auto_render_trace` 开关**。自动渲染统一开启，原来 examples/quickstart.py 里的外部手动渲染在本 Step 同步删除。
  - 改 examples/quickstart.py：删除 `agent.trace.steps` 手动遍历的 10+ 行代码，只保留 `answer = agent.run(q)` 和最终答案断言。
  - 作用：Trace.render 统一渲染效果（富文本 table/panel），比旧版纯文字格式化更友好，且不重复输出。

#### 5.3 兼容性：Trace.__str__ 保留不变
旧代码 `print(str(trace))` 行为不改，继续用纯文字版 steps 汇总。

### 验证步骤
```bash
cd Atlas
# 用最小 LLM 模拟，构造一个 2 子 Agent 的并行调用，跑完后
# 观察 stdout：父步骤表→子汇总表→子 Agent 详细步骤
# 用 RichRenderer：panel、table、rule 分隔线都正确渲染
# 用 PlainRenderer：不抛异常，纯文本输出
print('Step 5 OK')
```

---

## Step 6：demo 改写为纯内核 API 调用

### 目标
`examples/parallel_subagent_demo.py` 从≈400行（全自实现）降到≈80行（纯声明式配置 + 调用 Agent 新 API）。不再有 SubagentSpec/RoleConfig/durable_gather 等定义——全部从 `atlas` 包 import。

### 变更文件

#### 6.1 重写：`examples/parallel_subagent_demo.py`（全量替换）
新结构：
```python
"""并行子 Agent demo：纯内核 API 调用。

声明式配置（parallel_subagent_config.yaml）→ build_role_configs() → Agent(role_configs=...) → run()

预期输出：
  [任务] 三个子主题并行调研（并行效率通常在 1.8x ~ 2.8x）
  → [父 Agent 步骤表格]（父 ReAct 步骤 + spawn_subagents 工具调用 + 最终总结）
  → [子 Agent 汇总表格]（3-4 行：researcher / critic / researcher，各自 status / duration_ms / schema_ok）
  → [并行效率 Panel] 并行效率 x2.3 | 串行估算 15200ms → 实际 6600ms
  → [3 段子 Agent 详细步骤]（各自 step 表）
  → [最终答案 Markdown] 三主题结论汇总
"""
from __future__ import annotations
import sys, yaml
from pathlib import Path
from dotenv import load_dotenv
# 🟡可行性审查修正：不存在 build_llm，直接用 LLM 类（三级配置优先级已够用）
from atlas import Agent, LLM, RoleConfig, Toolkit
from atlas.tools.builtin import BUILTIN_TOOLS
from atlas.render import PlainRenderer, RichRenderer

def build_role_configs(config: dict) -> dict[str, RoleConfig]:
    """从 YAML roles 段加载为 RoleConfig 映射（~25 行）。"""
    roles = config.get("roles", {})
    mapping: dict[str, RoleConfig] = {}
    for name, raw in roles.items():
        mapping[name] = RoleConfig(
            name=name,
            system_prompt=raw["system_prompt"],
            tool_names=list(raw.get("tools", [])),
            temperature=float(raw.get("temperature", 0.3)),
            max_steps=int(raw.get("max_steps", 5)),
            timeout_ms=int(raw.get("timeout_ms", 60_000)),
        )
    return mapping

def main(config_path: str, plain: bool = False) -> None:
    load_dotenv()
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    role_configs = build_role_configs(cfg)
    # LLM 三级配置优先级生效：显式参数 > env(ATLAS_LLM_*) > provider 预设
    llm = LLM()
    # 父 Agent Toolkit：BUILTIN_TOOLS 全部注册，spawn_subagents 由内核自动注入
    parent_tk = Toolkit()
    for spec in BUILTIN_TOOLS:
        parent_tk.register(**spec)
    renderer = PlainRenderer() if plain else RichRenderer()
    parent_cfg = cfg.get("parent", {})
    known_roles = list(role_configs.keys())
    agent = Agent(
        llm=llm, tools=parent_tk,
        system_prompt=parent_cfg.get("system_prompt_template", "")
            .format(known_roles=known_roles),
        max_steps=parent_cfg.get("max_steps", 8),
        role_configs=role_configs,
        renderer=renderer,
        # 🔴可行性审查补充：工具规格池透传，供 _run_subagent 按名过滤
        tool_specs_pool=BUILTIN_TOOLS,
    )
    task = cfg["task"]
    print(f"[任务] {task[:80]}")
    answer = agent.run(task)
    # Trace 已在 Agent.run 顶层自动渲染
    print("\n[最终答案已在上方渲染完成]")
    # 可选：答案简单存在性断言
    assert isinstance(answer, str) and len(answer) > 0

if __name__ == "__main__":
    plain = "--plain" in sys.argv
    path = (sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("-")
            else "examples/parallel_subagent_config.yaml")
    main(path, plain=plain)
```

### 验证步骤
```bash
cd Atlas
python examples/parallel_subagent_demo.py    # RichRenderer Markdown 渲染
python examples/parallel_subagent_demo.py --plain > /tmp/out.log && wc -l /tmp/out.log
# 断言：无 import 错误；代码行数 < 100 行（wc -L < 100）
```

---

## Step 7：回归测试

### 目标
用最小真实 LLM 调用（5-15元钱范围）覆盖关键路径，把验证点全部跑一遍。无需写测试脚本（用户规则 4），在终端直接执行。

### 验证清单

| 编号 | 场景 | 操作 | 预期 |
|---|---|---|---|
| 7.1 | 工具自动注入 | `Agent(role_configs={1个})` 构造后检查 toolkit | `spawn_subagents` 工具存在 |
| 7.2 | register_role 动态注入 | 构造时空 role_configs，再 `register_role` | 工具注入，LLM 下次调用时 schema enum 含新 role（Q19） |
| 7.3 | schema 懒加载 | `register_role('z')` 后 `to_openai_tools()` 的 enum | 包含 'z' |
| 7.4 | 嵌套守卫 | 方案 A（更严谨）：构造子 Agent 时传 `parent_trace=parent_agent.trace, role_configs={...}` → 子 Agent 的 Toolkit 应 **根本不包含** spawn_subagents（_inject_spawn_tool 已按 _parent_trace 快速返回）；方案 B（兜底）：强行 hack 往子 Toolkit 注入 spawn_subagents 再调用 → 抛 `RuntimeError: 仅顶层父 Agent 可用` | A 方案：spawn_subagents 不在 tool 列表；B 方案：抛异常 |
| 7.5 | 并行 3 子 Agent | demo 默认 config | 总耗时 ≈ 单个最长子 Agent；加速比 >1.5x |
| 7.6 | partial success | 1 个子 Agent 指定不存在的 tool_names（在 role_config 中写）触发 `_run_subagent` 启动前 ValueError → parallel_gather 捕获 → error 字段写回 | 另外 2 个正常返回，异常的 SubagentResult.error 有明确报错，顺序不变 |
| 7.7 | 超时 | 构造 RoleConfig.timeout_ms=100 + 子任务用 max_steps 极大 + system_prompt 提示"重复调用 calculator 算 100 个求和" → 子 Agent 跑几步后被超时 | 该子 Agent `error="子 Agent 超时（100ms）..."`，schema_passed=None，其他子 Agent 正常。**已知限制**（P-1）：超时后子线程通常在 SDK HTTP 超时时自结束，无强制 kill。 |
| 7.8 | artifact_schema 校验通过 + 不通过 | 2 个子 Agent，一个给合法 schema，一个给 artifact 填错 | 通过的 schema_passed=True；不通过的 schema_passed=False,schema_error 有消息,artifact 保留原始文本，error=None（Q8） |
| 7.9 | Trace 渲染完整性 | 跑 demo，RichRenderer | 父步骤表 / 子汇总表 / 递归子步骤 / 并行效率 panel，四项齐全 |
| 7.10 | 纯文本渲染 | demo --plain 重定向到文件 | 文件可读，无 rich 控制字符 |

---

## 文件变更汇总

| 步骤 | 操作 | 文件 | 说明 |
|---|---|---|---|
| P-0 | Edit | `pyproject.toml` | **前置**：新增 `jsonschema>=4.0` 依赖（DR-3 已审批） |
| 1 | Rename | `atlas/core/multi_agent.py` → `atlas/core/orchestrator.py` | 先 copy 再 delete（规则 6），空出 multi_agent.py 给并行原语 |
| 1 | Edit | `examples/multi_agent_demo.py` | **可行性审查遗漏修正**：同步改导入路径 `orchestrator` |
| 1 | New | `atlas/core/multi_agent.py` | Step 1 先写数据类 + 占位 + 闭包骨架 |
| 2 | Edit | `atlas/context/trace.py` | subagent_traces、_parent_trace（Step 2.1） |
| 2 | Edit | `atlas/core/agent.py` | 🔴新增 `tool_specs_pool`；role_configs、parent_trace、_inject_spawn_tool（子 Agent 快速返回）、register_role |
| 2 | Edit | `atlas/tools/base.py` | Tool.parameters_factory 可选字段 + Toolkit.to_openai_tools 懒加载 |
| 2 | Edit | `atlas/core/multi_agent.py` | 填充 _run_subagent（从 tool_specs_pool 取工具、挂子 Trace + meta、超时 + schema 校验） |
| 3 | Edit | `atlas/core/multi_agent.py` | 填充 parallel_gather（enumerate 建索引 + partial success + 并行效率写入 meta） |
| 4 | Edit | `atlas/core/multi_agent.py` | 填充 make_spawn_subagents_tool（嵌套守卫 getattr + func 返回完整 JSON） |
| 4 | Edit | `atlas/__init__.py` | 导出 RoleConfig / SubagentSpec / SubagentResult / parallel_gather |
| 5 | Edit | `atlas/context/trace.py` | Trace.meta 字段 + Trace.render（depth 参数 + 递归上限 + 三段式渲染） |
| 5 | Edit | `atlas/core/agent.py` | Agent.run 两个 return 前自动渲染 Trace（仅顶层父 Agent） |
| 5 | Edit | `examples/quickstart.py` | 按规则 7 删除外部手动 trace 渲染，统一由 Agent.run 自动渲染 |
| 6 | Rewrite | `examples/parallel_subagent_demo.py` | 从≈400 行降到 < 100 行；改 `LLM()` 替代不存在的 build_llm；补 `tool_specs_pool=BUILTIN_TOOLS` |
| 7 | N/A | N/A | 10 条回归场景人工验证 |

### 总计数
- **前置 Edit**: 1 个文件（pyproject.toml）
- **Rename + 配套 Edit**: 1 对文件（multi_agent.py → orchestrator.py + multi_agent_demo.py 导入）
- **New**: 1 个文件
- **Edit**: 9 个文件（trace.py ×2、agent.py ×2、tools/base.py ×1、multi_agent.py ×3、__init__.py ×1、quickstart.py ×1——实施时按文件合并，每文件 1 次 Edit）
- **Rewrite**: 1 个文件
- **手动回归**: 10 条场景

---

## 依赖变更清单

| 包 | 版本下限 | 用途 | 影响步骤 |
|---|---|---|---|
| `jsonschema` | >= 4.0 | artifact_schema 校验（Q4） | **前置 P-0**，Step 2.4 起使用 |

已在项目中但本实施新增直接使用的包：`concurrent.futures`（标准库）、`json`（标准库）、`jsonschema`（新增三方）。

---

## 风险与回退

- **风险 1（已消除）**：`Toolkit.to_openai_tools` 的懒加载改动影响旧工具。
  → 消除方式：`Tool.parameters_factory` 是 dataclass 新字段，默认值 `None`，`if t.parameters_factory` 为假时走 `t.parameters`（原逻辑）。旧工具不改。无需 hasattr。
- **风险 2（已消除）**：Agent.run 自动渲染与 quickstart.py 手动渲染双重输出。
  → 消除方式（按规则 7 不兼容、不折中）：不引入 `auto_render_trace` 开关；Step 5 同步删除 quickstart.py 中手动遍历 trace 的代码，统一使用 Trace.render。不重复输出。
- **风险 3（已确认安全）**：Windows 下并发差异。本实施用 `ThreadPoolExecutor`（线程，非进程），与 demo 已验证的并行方式一致，Windows 完全安全。ProcessPool 才会有 fork 差异，与本方案无关。
- **风险 4（已知限制 P-1）**：超时后子线程不强制 kill。
  → 处理方式：LLM SDK 本身有 HTTP 超时（30-60s），子 Agent 通常会自结束。不做强制线程终止（Python 不支持安全 kill），若未来出现常驻泄漏再补"协作式 deadline + ThreadPool 兜底"。
- **风险 5（Q17 嵌套守卫双保险）**：子 Agent 的 LLM 可能"看到并选择" spawn_subagents 工具。
  → 消除方式（Step 2.2 已落实）：`_inject_spawn_tool` 在 `_parent_trace is not None` 时直接 return，子 Agent Toolkit 中根本不存在 spawn_subagents → LLM 看不到该选项 → 从源头消除误调用。另有 func 内部的 RuntimeError 作为第二道防线。

**回退策略**：每步完成后本地 `git commit`（非 push），任何一步有问题 `git reset --hard HEAD^1` 回退到上一步。依赖变更在 P-0 一次性完成，不会在中途引起环境不一致。

---

## 可行性审查修正点汇总（本文件 🔴🟠🟡🟢 级改动一览）

> 本章节由 2026-08-12 可行性审查后追加，便于快速定位改动点。

| 级别 | 条目 | 所在章节 | 前后对比 |
|---|---|---|---|
| 🔴 设计缺陷 | 内核 hardcode `BUILTIN_TOOLS` 违反 EP-2 | Step 2.4 + Step 2.2 `Agent.__init__` | 前：`_run_subagent` 直接从 `atlas.tools.builtin` import → 后：上层构造 Agent 时传 `tool_specs_pool=BUILTIN_TOOLS`，_run_subagent 从 `agent_ref.tool_specs_pool` 按名过滤，缺工具直接抛 ValueError |
| 🟠 依赖审批 | jsonschema 未走 DR-3 流程、放置位置错误 | 新增 "前置准备 P-0" + 原 Step 3.2 改为说明 | 前：Step 3.2 才改 pyproject.toml（Step 2.4 就用到了，顺序错）→ 后：P-0 前置一次性加入，并附审批理由（JS 是事实标准，手写维护成本更高） |
| 🟠 已知限制 | ThreadPool 超时子线程泄漏（Q18 wall-clock） | 新增 "前置准备 P-1" + Step 7.7 回写说明 | 前：文档完全未提及 Python 线程无 kill API → 后：明确写出影响、通常无害的原因、未来补协作式超时的备选路线 |
| 🟡 遗漏项 | 重命名 multi_agent.py 后未改 multi_agent_demo.py | 前置检查 + Step 1.1 | 前：Step 1.1 只 rename → 后：新增"操作 2"，同步改 `examples/multi_agent_demo.py` 导入为 orchestrator。在文件汇总表新增一行。 |
| 🟡 顺序错误 | jsonschema 在 Step 3.2 才加入（Step 2.4 已经用了） | Step 3.2 改写 | 前：Step 3.2 = Edit pyproject.toml → 后：Step 3.2 = 说明"已在 P-0 完成"，消除顺序依赖 |
| 🟡 不存在 API | `from atlas.providers import build_llm`（providers.py 没有此函数） | Step 2 验证脚本 + Step 6 demo 代码 | 前：两处引用不存在的 `build_llm()` → 后：统一改 `llm = LLM()`（三级配置优先级已够用），并附注释说明 |
| 🟡 重复开发 | Step 2.2 和 Step 4.2 各写一遍 `_inject_spawn_tool` | Step 4.2 改写为说明 | 前：两处都写实现细节 → 后：Step 2.2 一次性完整实现（含子 Agent 快速返回、防重复注入、懒加载 factory 挂载），Step 4.2 改为"已在 Step 2.2 覆盖，无需二次改动" |
| 🟢 小优化 1 | Step 1 占位 func 守卫直接访问 `_parent_trace`（Step 1 时字段还不存在） | Step 1.D | 前：`if agent.trace._parent_trace ...` → 后：`if getattr(agent.trace, '_parent_trace', None)`，防 AttributeError |
| 🟢 小优化 2 | parallel_gather 回填索引用 `specs.index(spec)`（O(n) 小瑕疵） | Step 3.1 | 前：`futures = {fut: spec}` 再 `idx = specs.index(spec)` → 后：`enumerate` 建 `{fut: (spec, idx)}`，O(1) 回填 |
| 🟢 小优化 3 | 子 Agent 虽然 spawn 抛错，但 LLM 仍能看到工具选项 → 不如从源头消除 | Step 2.2 `_inject_spawn_tool` 首行 + Step 7.4 双场景验证 | 前：仅在 func 内部抛 RuntimeError → 后：注入前 `if _parent_trace: return`，子 Toolkit 根本不含该工具；回归测试补 A/B 两个场景 |
| 🟢 小优化 4 | Trace.render 递归无栈上限 | Step 5.1 签名 + 实现 | 前：`def render(renderer)` 无参数 → 后：`def render(renderer, depth=0)`，`depth >= 3` 停止，防御性编程 |
| 🟢 小优化 5 | quickstart.py 自动+手动双重输出，文档给折中开关 | Step 5.2 + 文件变更汇总 | 前：回退方案是加 `auto_render_trace` 开关 → 后：按规则 7 不折中、不兼容，直接删 quickstart 外部遍历，不加开关。在风险 2 中写明"已消除" |
| 🟢 小优化 6 | parallel_gather 仅返回结果，Trace.render 拿不到并行效率指标 | Step 3.1 末尾 + Step 5.1 第 3 段 | 前：无写 meta 动作 → 后：`parallel_gather` 返回前把 `parallel_total_ms / parallel_sum_child_ms / parallel_efficiency` 写进 `agent_ref.trace.meta`；Step 5.1 从 meta 取来做 Panel 渲染 |

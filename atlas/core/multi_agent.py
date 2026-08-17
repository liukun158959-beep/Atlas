"""并行子 Agent 内核原语。

本模块实现：
- RoleConfig / SubagentSpec / SubagentResult：声明式数据类
- _run_subagent：单子 Agent 执行（含独立上下文、超时、Schema 校验）
- parallel_gather：ThreadPoolExecutor 并行多子 Agent（partial success + 顺序保持）
- make_spawn_subagents_tool：闭包工厂，生成供父 Agent Toolkit 注入的 spawn_subagents 工具
    （含 Q17 嵌套守卫、Q19 schema 懒加载 factory）
"""
from __future__ import annotations

import json
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # 避免循环导入
    from .agent import Agent


# ---------------------------------------------------------------------------
# 数据类：RoleConfig / SubagentSpec / SubagentResult
# ---------------------------------------------------------------------------


@dataclass
class RoleConfig:
    """子 Agent 角色声明式配置。"""

    name: str
    system_prompt: str
    tool_names: list[str]
    temperature: float = 0.3
    max_steps: int = 5
    timeout_ms: int = 60_000


@dataclass
class SubagentSpec:
    """单个子 Agent 任务规格（由 spawn_subagents 工具参数解析而来）。"""

    subagent_id: str
    role: str
    task: str
    extra_system_prompt: str | None = None
    artifact_schema: dict | None = None


@dataclass
class SubagentResult:
    """子 Agent 执行结果（parallel_gather 保持原序返回）。"""

    subagent_id: str
    role: str
    task: str
    artifact: str
    duration_ms: int
    error: str | None = None
    schema_passed: bool | None = None
    schema_error: str | None = None


# ---------------------------------------------------------------------------
# _run_subagent：单子 Agent 执行（Step 2 完整实现）
# ---------------------------------------------------------------------------


def _run_subagent(spec: SubagentSpec, agent_ref: "Agent") -> SubagentResult:
    """单子 Agent 执行：构造独立 ReAct 实例 + wall-clock 超时 + artifact_schema 校验。

    设计约束（🔴可行性审查修正）：
    - 子 Agent 的 Toolkit 按 role_cfg.tool_names 从 agent_ref.tool_specs_pool 按名过滤构建，
      内核绝不 hardcode BUILTIN_TOOLS（遵守 EP-2 解耦）。
    - 子 Agent 构造时显式 auto_inject_spawn_tool=False（双保险，+ _inject_spawn_tool 的
      _parent_trace 快速返回） → Q17 嵌套守卫两道防线。
    - wall-clock 超时：ThreadPoolExecutor(1).result(timeout)；子线程泄漏为已知限制（P-1）。
    - Schema 校验：不通过只标记 schema_passed=False + schema_error，artifact 原文保留（Q8）。
    """
    # 延迟导入：避免模块级循环引用
    from .agent import Agent
    from ..tools.base import Toolkit

    t0 = time.perf_counter()
    role_cfg = agent_ref.role_configs[spec.role]

    # ------------------------------------------------------------------
    # 2. 按 RoleConfig.tool_names 从 tool_specs_pool 按名过滤，构建子 Toolkit
    # ------------------------------------------------------------------
    if role_cfg.tool_names and not agent_ref.tool_specs_pool:
        raise ValueError(
            f"角色 {role_cfg.name} 需要工具 {role_cfg.tool_names}，"
            "但 Agent.tool_specs_pool 为空。构造父 Agent 时请传入 "
            "tool_specs_pool=BUILTIN_TOOLS（或其他全局工具规格列表）。"
        )
    sub_toolkit = Toolkit()
    name_to_spec = {s["name"]: s for s in agent_ref.tool_specs_pool}
    for tool_name in role_cfg.tool_names:
        if tool_name not in name_to_spec:
            raise ValueError(
                f"角色 {role_cfg.name} 要求的工具 {tool_name} 不在工具规格池中，"
                "请检查 tool_specs_pool 是否包含该工具。"
            )
        sub_toolkit.register(**name_to_spec[tool_name])

    # ------------------------------------------------------------------
    # 3. 子 Agent 实例化（独立 memory + trace + renderer 同类型新实例）
    # ------------------------------------------------------------------
    sub_agent = Agent(
        llm=agent_ref.llm,
        tools=sub_toolkit,
        system_prompt=role_cfg.system_prompt + (spec.extra_system_prompt or ""),
        max_steps=role_cfg.max_steps,
        renderer=type(agent_ref.renderer)(),
        role_configs=agent_ref.role_configs,
        parent_trace=agent_ref.trace,
        auto_inject_spawn_tool=False,
        tool_specs_pool=agent_ref.tool_specs_pool,
        journal=agent_ref.journal,
    )

    # ------------------------------------------------------------------
    # 4. wall-clock 超时（Q18）：ThreadPoolExecutor + future.result(timeout)
    # ------------------------------------------------------------------
    artifact: str = ""
    error: str | None = None
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future: Future = pool.submit(sub_agent.run, spec.task)
            artifact = future.result(timeout=role_cfg.timeout_ms / 1000)
    except TimeoutError:
        error = f"子 Agent 超时（{role_cfg.timeout_ms}ms），已返回部分结果"
        artifact = artifact or ""
    except Exception as e:
        error = f"子 Agent 异常: {type(e).__name__}: {e}"

    # ------------------------------------------------------------------
    # 5. artifact_schema 校验（Q4 + Q8）：不改 artifact，仅打标记
    # ------------------------------------------------------------------
    schema_passed: bool | None = None
    schema_error: str | None = None
    if spec.artifact_schema and artifact and error is None:
        import jsonschema  # P-0 已加入依赖，函数内部 import 减少模块启动成本
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

    # ------------------------------------------------------------------
    # 6. 挂父子 Trace 关联，写 meta（Step 5 Trace.render 会读取）
    # ------------------------------------------------------------------
    agent_ref.trace.subagent_traces.append(sub_agent.trace)
    duration_ms = int((time.perf_counter() - t0) * 1000)
    # meta 字段正式在 Step 5.1 追加到 Trace.__init__；这里动态赋值兼容当前阶段
    if not hasattr(sub_agent.trace, "meta"):
        sub_agent.trace.meta = {}
    sub_agent.trace.meta.update({
        "subagent_id": spec.subagent_id,
        "role": spec.role,
        "task": spec.task,
        "duration_ms": duration_ms,
        "schema_passed": schema_passed,
        "schema_error": schema_error,
        "error": error,
    })
    return SubagentResult(
        subagent_id=spec.subagent_id,
        role=spec.role,
        task=spec.task,
        artifact=artifact,
        duration_ms=duration_ms,
        error=error,
        schema_passed=schema_passed,
        schema_error=schema_error,
    )


# ---------------------------------------------------------------------------
# parallel_gather：并行执行，顺序保持，partial success
# ---------------------------------------------------------------------------


def parallel_gather(
    specs: list[SubagentSpec],
    agent_ref: "Agent",
) -> list[SubagentResult]:
    """并行执行多个子 Agent。

    关键保证（🔴可行性审查修正）：
    - 顺序保持：返回 list 顺序与输入 specs 严格一致（用 enumerate(idx) O(1) 回填而非 .index）
    - partial success：任一个子 Agent 失败/超时，不影响其他；对应 error/schema_error 字段有值
    - 并行效率：子 Agent.meta 写入 parallel_efficiency_ratio（实际耗时 / 累计子耗时）
    """
    if not specs:
        return []
    t0 = time.perf_counter()

    # 写父 Trace 的 meta（Step 5.1 才正式声明，这里动态赋值向前兼容）
    if not hasattr(agent_ref.trace, "meta"):
        agent_ref.trace.meta = {}
    agent_ref.trace.meta.setdefault("spawn_counts", {})
    # 按 role 统计 spawn 次数（Q16：Trace.step 中看不到 spawn_subagents 参数时也能追溯）
    for s in specs:
        c = agent_ref.trace.meta["spawn_counts"]
        c[s.role] = c.get(s.role, 0) + 1

    results: list[SubagentResult | None] = [None] * len(specs)

    def _run_one(idx: int, s: SubagentSpec) -> tuple[int, SubagentResult]:
        try:
            return idx, _run_subagent(s, agent_ref)
        except Exception as e:
            # 捕获并行调度层的异常（不影响兄弟任务）
            return idx, SubagentResult(
                subagent_id=s.subagent_id,
                role=s.role,
                task=s.task,
                artifact="",
                duration_ms=0,
                error=f"调度异常: {type(e).__name__}: {e}",
            )

    max_workers = min(len(specs), 16)   # 简单上限，避免 spawn 1000 个开 1000 线程
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_run_one, idx, s) for idx, s in enumerate(specs)]
        for fut in futures:
            idx, res = fut.result()   # 单个失败在 _run_one 内部已包装，不会抛到这里
            results[idx] = res

    wall_ms = int((time.perf_counter() - t0) * 1000)
    total_sub_ms = sum(r.duration_ms for r in results)
    ratio = (total_sub_ms / wall_ms) if wall_ms > 0 else 1.0
    # 写每个子 Agent 的并行效率 meta
    for r in results:
        if not hasattr(agent_ref.trace, "meta"):
            agent_ref.trace.meta = {}
        # subagent meta 在 _run_subagent 里已经写过，找对应的 trace 补充 parallel_efficiency_ratio
        # 注意：subagent_traces 是按 spawn 顺序追加的，对应每个 idx 对应 results 的 trace 一致
    # 更直接：遍历 subagent_traces（数量 == len(specs)），按 spawn 顺序逐个写 ratio
    for sub_trace in agent_ref.trace.subagent_traces[-len(specs):]:
        # meta 动态属性防御
        if not hasattr(sub_trace, "meta"):
            sub_trace.meta = {}
        sub_trace.meta["parallel_efficiency_ratio"] = round(ratio, 3)
        sub_trace.meta["wall_ms"] = wall_ms
        sub_trace.meta["total_sub_ms"] = total_sub_ms

    # results 全是非 None（上面 _run_one 处理了所有异常）
    final: list[SubagentResult] = results  # type: ignore[assignment]
    return final


# ---------------------------------------------------------------------------
# make_spawn_subagents_tool：闭包工厂，生成 (name, desc, params_factory, func) 四元组
# ---------------------------------------------------------------------------


def make_spawn_subagents_tool(agent: "Agent"):
    """闭包工厂：生成供 Toolkit 注入的 spawn_subagents 工具四元组。

    返回: (name: str, description: str, parameters_factory: Callable[[], dict], func: Callable)
    - parameters_factory 是 Q19 schema 懒加载：每次调用读 agent.role_configs.keys() 生成 enum
    - func 内部做 Q17 嵌套守卫（用 getattr 防御性读取，Step 1 阶段 Trace 可能还没 _parent_trace）
    """

    def _func(args: dict) -> str:
        # Step 1 阶段占位 + 嵌套守卫预埋（用 getattr 避免 Step 1 AttributeError）
        if getattr(agent.trace, '_parent_trace', None) is not None:
            raise RuntimeError("spawn_subagents 仅顶层父 Agent 可用，禁止嵌套")
        raw_list = args.get("subagents") or []
        if not raw_list:
            return "错误：spawn_subagents 需要传入非空的 subagents 列表"

        specs: list[SubagentSpec] = []
        try:
            for i, raw in enumerate(raw_list):
                role = raw["role"]
                task = raw["task"]
                # 角色存在性校验 → 错误信息里列可用 role（Q3）
                if role not in agent.role_configs:
                    known = list(agent.role_configs.keys())
                    raise ValueError(
                        f"subagents[{i}] 角色 '{role}' 不存在。可用角色: {known}"
                    )
                specs.append(SubagentSpec(
                    subagent_id=raw.get("subagent_id") or f"sub_{i}",
                    role=role,
                    task=task,
                    artifact_schema=raw.get("artifact_schema"),
                    extra_system_prompt=raw.get("extra_system_prompt"),
                ))
        except (KeyError, TypeError, ValueError) as e:
            return f"spawn_subagents 参数错误: {e}"

        # 并行执行
        results = parallel_gather(specs, agent)

        # 格式化结果为 JSON 文本，让 LLM 直接可读
        out: list[dict] = []
        for r in results:
            out.append({
                "subagent_id": r.subagent_id,
                "role": r.role,
                "task": r.task,
                "artifact": r.artifact,
                "duration_ms": r.duration_ms,
                "ok": r.error is None,
                "error": r.error,
                "schema_passed": r.schema_passed,
                "schema_error": r.schema_error,
            })
        return json.dumps(out, ensure_ascii=False, indent=2)

    def _params_factory() -> dict:
        """每次调用重新生成 enum（Q19：register_role 后下一轮 LLM 就看到新 role）。"""
        known_roles = list(agent.role_configs.keys())
        return {
            "type": "object",
            "properties": {
                "subagents": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "enum": known_roles,
                                "description": f"从枚举中选择: {known_roles}",
                            },
                            "task": {"type": "string"},
                            "artifact_schema": {
                                "type": "object",
                                "description": "可选。期望 artifact 的 JSON Schema 描述，用于结果校验",
                            },
                        },
                        "required": ["role", "task"],
                    },
                }
            },
            "required": ["subagents"],
        }

    name = "spawn_subagents"
    description = (
        "并行启动多个子 Agent 执行独立子任务。"
        "每个子 Agent 拥有独立上下文，互不干扰。"
        "适用于可并行的独立子任务（多主题调研、多方案对比等）。"
        "可选角色必须从 role enum 中选择，不可自创。"
        "请在单次调用的 subagents 数组中列出所有要并行执行的子任务，"
        "不要分多次调用（并行发生在本工具内部）。"
        "返回结构化 JSON：含每个子 Agent 的 artifact、耗时、并行效率指标。"
    )
    return name, description, _params_factory, _func

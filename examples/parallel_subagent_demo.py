"""基于 spawn_subagent 的并行任务 demo。

验证 Phase 2 规划中的两个核心原语：
    1. spawn_subagent —— 父 Agent 通过工具调用启动子 Agent，子 Agent 拥有独立 context（不复制父对话）
    2. durable_gather  —— 并行 fan-out 多个子 Agent，gather 汇总结果

本 demo 不引入内核改动，仅用现有 LLM + Toolkit + Memory 在 examples 层组装出最小可用版本，
用于验证多 Agent 并行协作的实际效果，为 Phase 2 内核原语落地提供参考实现。

声明式配置：角色定义（Role 三元组：system_prompt + tools + llm_config）+ 父 Agent 配置 + 任务
全部从 parallel_subagent_config.yaml 加载，修改 YAML 即可调整行为，无需改代码。

用法：
    python examples/parallel_subagent_demo.py                              # 用默认配置
    python examples/parallel_subagent_demo.py path/to/your_config.yaml     # 指定配置

预期输出：
    - 父 Agent 收到"并行调研 3 个主题"的复合任务
    - 父 Agent 一次性调用 spawn_subagents 工具，并行启动 3 个子 Agent
    - 3 个子 Agent 在独立 context 中各自执行，互不干扰
    - 父 Agent 收到结构化 artifact 列表，汇总生成最终答案
    - 日志中能看到并行耗时 ≈ 单个子 Agent 耗时（而非三者之和）

运行前确保已配置 .env（python -m atlas init）。
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml
from dotenv import load_dotenv

from atlas import LLM, Toolkit
from atlas.memory.base import Memory
from atlas.render import PlainRenderer, Renderer, RichRenderer
from atlas.tools.builtin import BUILTIN_TOOLS


# ----------------- 最小版 spawn_subagent + durable_gather -----------------


@dataclass
class SubagentResult:
    """子 Agent 执行结果（artifact）。"""

    subagent_id: str
    role: str
    task: str
    artifact: str
    duration_ms: int
    error: str | None = None


@dataclass
class SubagentSpec:
    """子 Agent 规格 = Role 三元组（system_prompt + tools + llm_config）+ task。

    对应 Phase 2 规划的 Role 抽象：system_prompt + tools + llm_config。
    """

    subagent_id: str
    role: str
    system_prompt: str
    task: str
    tools: Toolkit
    llm: LLM
    max_steps: int = 5


@dataclass
class RoleConfig:
    """角色配置（Role 抽象三元组）：从 YAML 加载，运行时注入子 Agent。

    对应 Phase 2 规划的 Role 抽象：
        - system_prompt: 角色身份与输出格式
        - tools:         该角色可用的工具子集（按名过滤自全局工具池）
        - llm_config:    模型参数（model/temperature/max_steps 等）
    """

    name: str
    system_prompt: str
    tool_names: list[str]
    llm_config: dict
    max_steps: int = 5

    def build_toolkit(self, all_tool_specs: list[dict]) -> Toolkit:
        """按 tool_names 从全局工具池中过滤，构建该角色专属的 Toolkit。

        未声明的工具不会暴露给子 Agent，从源头限制能力边界。
        """
        toolkit = Toolkit()
        for spec in all_tool_specs:
            if spec["name"] in self.tool_names:
                toolkit.register(**spec)
        return toolkit


def _run_subagent_sync(spec: SubagentSpec) -> SubagentResult:
    """同步执行单个子 Agent：独立 Memory（context 隔离）+ 简化 ReAct 循环。

    关键特性：
        - context 隔离：子 Agent 用全新 Memory，不继承父 Agent 对话
        - artifact 返回：只返回最终结论，不返回内部对话
        - 失败显式抛出：不静默兜底（遵循"消除 fallback 触发场景"原则）
    """
    start = time.time()
    memory = Memory(system_prompt=spec.system_prompt)
    memory.add_user(spec.task)
    openai_tools = spec.tools.to_openai_tools() or None

    final_content = ""
    for step_no in range(1, spec.max_steps + 1):
        assistant = spec.llm.chat(memory.messages, tools=openai_tools)
        memory.add_assistant(assistant)
        tool_calls = assistant.get("tool_calls") or []

        if not tool_calls:
            final_content = assistant.get("content") or ""
            break

        for call in tool_calls:
            fn = call["function"]
            try:
                result = spec.tools.execute(fn["name"], fn["arguments"])
            except Exception as e:
                # 工具失败是有效信息，回填给子 Agent 让其调整
                result = f"工具执行失败: {e}"
            memory.add_tool(result, call["id"])
    else:
        # 循环正常结束说明达到 max_steps 仍未完成，显式标记
        return SubagentResult(
            subagent_id=spec.subagent_id,
            role=spec.role,
            task=spec.task,
            artifact="",
            duration_ms=int((time.time() - start) * 1000),
            error=f"子 Agent 在 {spec.max_steps} 步内未完成",
        )

    return SubagentResult(
        subagent_id=spec.subagent_id,
        role=spec.role,
        task=spec.task,
        artifact=final_content,
        duration_ms=int((time.time() - start) * 1000),
    )


async def durable_gather(specs: list[SubagentSpec]) -> list[SubagentResult]:
    """持久化并行 fan-out/gather 雏形。

    Phase 2 规划中 durable_gather 应基于 Journal 持久化 + 幂等防重，
    本 demo 仅做并行执行的最小验证，不引入持久化（持久化是 Journal 原语的职责）。

    实现要点：
        - 用 ThreadPoolExecutor 把同步 LLM 调用包装成并行任务
        - asyncio.gather 等待全部完成（fan-out + gather）
        - 任一子 Agent 异常不影响其他子 Agent（错误隔离在单个子 Agent 内）
    """
    loop = asyncio.get_event_loop()
    # LLM 调用是 IO 密集型，线程池大小等于子 Agent 数即可
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        tasks = [loop.run_in_executor(pool, _run_subagent_sync, spec) for spec in specs]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    return list(results)


# ----------------- 把 spawn_subagents 暴露为父 Agent 的工具 -----------------


def make_spawn_subagents_tool(
    llm: LLM,
    all_tool_specs: list[dict],
    role_configs: dict[str, RoleConfig],
) -> tuple[dict, Callable[[dict], str]]:
    """构造 spawn_subagents 工具规格与实现。

    父 Agent 通过该工具一次性发起多个子 Agent（并行），
    工具内部调用 durable_gather，返回结构化 artifact 列表。

    参数：
        llm           - 子 Agent 共用的 LLM 实例（llm_config 中的 model 暂不生效，待 Phase 2 LLM 层增强）
        all_tool_specs - 全局工具规格池（BUILTIN_TOOLS），按角色 tool_names 过滤
        role_configs  - 角色 → RoleConfig 映射（从 YAML 加载的 Role 抽象三元组）
    """
    def _func(args: dict) -> str:
        subagents_input = args["subagents"]
        specs: list[SubagentSpec] = []
        for idx, item in enumerate(subagents_input):
            role = item["role"]
            task = item["task"]
            role_cfg = role_configs.get(role)
            if role_cfg is None:
                # 未知角色直接报错，不静默降级
                raise ValueError(f"未知角色: {role}，已知角色: {list(role_configs.keys())}")
            specs.append(
                SubagentSpec(
                    subagent_id=f"sub-{idx + 1}",
                    role=role,
                    system_prompt=role_cfg.system_prompt,
                    task=task,
                    tools=role_cfg.build_toolkit(all_tool_specs),
                    llm=llm,
                    max_steps=role_cfg.max_steps,
                )
            )

        start = time.time()
        results = asyncio.run(durable_gather(specs))
        total_ms = int((time.time() - start) * 1000)

        # 构造结构化 artifact 返回给父 Agent
        artifact = {
            "total_duration_ms": total_ms,
            "subagent_count": len(results),
            "parallel_efficiency": (
                # 并行效率 = 各子 Agent 耗时之和 / 实际总耗时（>1 说明并行生效）
                round(sum(r.duration_ms for r in results) / max(total_ms, 1), 2)
            ),
            "results": [
                {
                    "subagent_id": r.subagent_id,
                    "role": r.role,
                    "task": r.task,
                    "artifact": r.artifact,
                    "duration_ms": r.duration_ms,
                    "error": r.error,
                }
                for r in results
            ],
        }
        return json.dumps(artifact, ensure_ascii=False)

    # 已知角色列表，用于 enum 约束 + description 提示，从源头消除 LLM 填错角色的可能
    known_roles = list(role_configs.keys())

    tool_spec = {
        "func": _func,
        "name": "spawn_subagents",
        "description": (
            "并行启动多个子 Agent 执行独立子任务。"
            "每个子 Agent 拥有独立上下文，互不干扰。"
            "适用于可并行的独立子任务（如多主题调研、多方案对比）。"
            f"可选角色（必须从中选择，不可自创）: {known_roles}。"
            "返回结构化结果，包含每个子 Agent 的 artifact 和并行效率指标。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "subagents": {
                    "type": "array",
                    "description": "要并行启动的子 Agent 列表",
                    "items": {
                        "type": "object",
                        "properties": {
                            "role": {
                                "type": "string",
                                "description": f"子 Agent 角色，必须从以下枚举中选择: {known_roles}",
                                "enum": known_roles,
                            },
                            "task": {
                                "type": "string",
                                "description": "该子 Agent 要执行的具体任务",
                            },
                        },
                        "required": ["role", "task"],
                    },
                }
            },
            "required": ["subagents"],
        },
    }
    return tool_spec, _func


# ----------------- 父 Agent 循环（带 spawn_subagents 工具） -----------------


@dataclass
class ParallelTrace:
    """并行多 Agent 执行轨迹。"""

    parent_steps: list[dict] = field(default_factory=list)
    subagent_results: list[SubagentResult] = field(default_factory=list)
    total_duration_ms: int = 0
    final_answer: str = ""

    def __str__(self) -> str:
        """纯文本格式（重定向到文件时使用，不渲染 Markdown）。"""
        lines = ["=== Parallel Subagent Trace ==="]
        lines.append(f"\n[Parent] 共 {len(self.parent_steps)} 步:")
        for step in self.parent_steps:
            lines.append(f"  - {step['desc']}")
        lines.append(f"\n[Subagents] 共 {len(self.subagent_results)} 个子 Agent:")
        for r in self.subagent_results:
            status = "OK" if not r.error else f"FAIL: {r.error}"
            lines.append(f"  - [{r.role}] {r.task}")
            lines.append(f"    耗时: {r.duration_ms}ms  状态: {status}")
            if r.artifact:
                for artifact_line in r.artifact.splitlines():
                    lines.append(f"      {artifact_line}")
        lines.append(f"\n[Total] 并行总耗时: {self.total_duration_ms}ms")
        return "\n".join(lines)

    def render(self, renderer: Renderer) -> None:
        """用注入的 Renderer 渲染执行轨迹。

        renderer 为 PlainRenderer 时退化为纯文本，为 RichRenderer 时渲染 Markdown + Panel。
        上层不直接接触 rich，只传 renderer。
        """
        renderer.rule("执行轨迹")

        # Parent Steps 用表格
        renderer.table(
            rows=self.parent_steps,
            headers=["step", "desc"],
            title="Parent Steps",
        )

        # 每个子 Agent artifact 用面板
        for idx, r in enumerate(self.subagent_results, 1):
            status = "OK" if not r.error else f"FAIL: {r.error}"
            renderer.panel(
                content=r.artifact or "(empty)",
                title=f"{idx}. [{r.role}] {r.task}",
                border_style="green" if not r.error else "red",
                **{"耗时": f"{r.duration_ms}ms", "状态": status},
            )

        renderer.print(f"并行总耗时: {self.total_duration_ms}ms")


class ParallelParentAgent:
    """父 Agent：通过 spawn_subagents 工具编排并行子 Agent。"""

    def __init__(
        self,
        llm: LLM,
        all_tool_specs: list[dict],
        role_configs: dict[str, RoleConfig],
        parent_config: dict,
        renderer: Renderer | None = None,
    ) -> None:
        """
        参数：
            llm             - 父 Agent 与子 Agent 共用的 LLM 实例
            all_tool_specs  - 全局工具规格池（用于子 Agent 按角色过滤）
            role_configs    - 角色 → RoleConfig 映射（从 YAML 加载）
            parent_config   - 父 Agent 配置 dict，含 system_prompt_template / max_steps / llm_config
            renderer        - 渲染器，默认 PlainRenderer，注入 RichRenderer 后支持 Markdown
        """
        self.llm = llm
        self.all_tool_specs = all_tool_specs
        self.role_configs = role_configs
        self.parent_config = parent_config
        self.max_steps = parent_config.get("max_steps", 8)
        self.renderer: Renderer = renderer or PlainRenderer()
        self.trace = ParallelTrace()

        # 注册 spawn_subagents 工具到独立 Toolkit（不与子 Agent 工具混用）
        self.parent_tools = Toolkit()
        spec, _ = make_spawn_subagents_tool(llm, all_tool_specs, role_configs)
        self.parent_tools.register(**spec)

    def run(self, task: str) -> str:
        """运行父 Agent，自动决定是否调用 spawn_subagents 并行子 Agent。"""
        known_roles = list(self.role_configs.keys())
        # system_prompt_template 中的 {known_roles} 占位符替换为实际角色列表
        template = self.parent_config.get(
            "system_prompt_template",
            "你是任务编排 Agent。主动调用 spawn_subagents 工具并行启动子 Agent。"
            f"子 Agent 角色必须从以下枚举中选择，不可自创: {known_roles}。",
        )
        system_prompt = template.format(known_roles=known_roles)
        memory = Memory(system_prompt=system_prompt)
        memory.add_user(task)
        openai_tools = self.parent_tools.to_openai_tools()

        final_content = ""
        for step_no in range(1, self.max_steps + 1):
            assistant = self.llm.chat(memory.messages, tools=openai_tools)
            memory.add_assistant(assistant)
            tool_calls = assistant.get("tool_calls") or []

            if not tool_calls:
                final_content = assistant.get("content") or ""
                self.trace.parent_steps.append(
                    {"step": step_no, "desc": "给出最终答案"}
                )
                break

            for call in tool_calls:
                fn = call["function"]
                name = fn["name"]
                self.trace.parent_steps.append(
                    {"step": step_no, "desc": f"调用工具 {name}"}
                )
                try:
                    result_str = self.parent_tools.execute(name, fn["arguments"])
                except Exception as e:
                    # 工具失败显式抛出，不静默兜底
                    raise RuntimeError(f"父 Agent 工具执行失败: {name} -> {e}") from e

                memory.add_tool(result_str, call["id"])

                # 解析 spawn_subagents 的结构化返回，记录到 trace
                if name == "spawn_subagents":
                    data = json.loads(result_str)
                    self.trace.total_duration_ms = data["total_duration_ms"]
                    self.trace.subagent_results = [
                        SubagentResult(
                            subagent_id=item["subagent_id"],
                            role=item["role"],
                            task=item["task"],
                            artifact=item["artifact"],
                            duration_ms=item["duration_ms"],
                            error=item["error"],
                        )
                        for item in data["results"]
                    ]
                    efficiency = data["parallel_efficiency"]
                    self.renderer.print(
                        f"  [gather] 并行 {data['subagent_count']} 个子 Agent，"
                        f"总耗时 {data['total_duration_ms']}ms，并行效率 {efficiency}x"
                    )
        else:
            final_content = f"父 Agent 在 {self.max_steps} 步内未完成"

        self.trace.final_answer = final_content
        return final_content


# ----------------- YAML 声明式配置加载 -----------------


def load_config(yaml_path: Path) -> dict:
    """从 YAML 加载完整配置（roles + parent + task）。

    配置结构见 examples/parallel_subagent_config.yaml。
    """
    with open(yaml_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_role_configs(config: dict) -> dict[str, RoleConfig]:
    """把 YAML 中的 roles 段落转成 RoleConfig 字典。

    每个 RoleConfig 完整承载 Role 三元组：system_prompt + tool_names + llm_config。
    max_steps 从 llm_config.max_steps 提取，缺省值为 5。
    """
    roles_raw = config.get("roles", {})
    role_configs: dict[str, RoleConfig] = {}
    for name, cfg in roles_raw.items():
        llm_cfg = cfg.get("llm_config", {})
        role_configs[name] = RoleConfig(
            name=name,
            system_prompt=cfg["system_prompt"].strip(),
            tool_names=cfg.get("tools", []),
            llm_config=llm_cfg,
            max_steps=llm_cfg.get("max_steps", 5),
        )
    return role_configs


# ----------------- Demo 主流程 -----------------


def main() -> None:
    load_dotenv()

    # 渲染器：默认 RichRenderer（终端 Markdown），--plain 参数切换为纯文本
    renderer: Renderer = PlainRenderer() if "--plain" in sys.argv else RichRenderer()

    # 配置文件路径：第一个非 --plain 参数，缺省为同目录下的 parallel_subagent_config.yaml
    args = [a for a in sys.argv[1:] if a != "--plain"]
    default_cfg = Path(__file__).parent / "parallel_subagent_config.yaml"
    yaml_path = Path(args[0]) if args else default_cfg

    config = load_config(yaml_path)
    role_configs = build_role_configs(config)
    parent_config = config.get("parent", {})
    task = config.get("task", "").strip()

    if not role_configs:
        raise ValueError(f"配置文件 {yaml_path} 中未定义任何角色（roles 段为空）")
    if not task:
        raise ValueError(f"配置文件 {yaml_path} 中未定义任务（task 段为空）")

    llm = LLM()

    renderer.rule("任务启动")
    renderer.print(f"配置文件: {yaml_path}")
    renderer.print(f"角色列表: {list(role_configs.keys())}")
    renderer.print(f"任务:\n{task}")
    renderer.print()
    renderer.print("[Parent] 父 Agent 开始编排...")
    renderer.print()

    agent = ParallelParentAgent(
        llm=llm,
        all_tool_specs=BUILTIN_TOOLS,
        role_configs=role_configs,
        parent_config=parent_config,
        renderer=renderer,
    )

    overall_start = time.time()
    result = agent.run(task)
    overall_ms = int((time.time() - overall_start) * 1000)

    # 最终答案走 Markdown 渲染
    renderer.markdown(result, title="最终答案")

    # 执行轨迹走 Panel/Table 渲染
    agent.trace.render(renderer)

    # 并行效果指标
    renderer.rule("并行效果指标")
    if agent.trace.subagent_results:
        serial_sum = sum(r.duration_ms for r in agent.trace.subagent_results)
        parallel_actual = agent.trace.total_duration_ms
        speedup = round(serial_sum / max(parallel_actual, 1), 2)
        renderer.table(
            rows=[
                {"指标": "串行理论耗时（各子 Agent 耗时之和）", "值": f"{serial_sum}ms"},
                {"指标": "并行实际耗时", "值": f"{parallel_actual}ms"},
                {"指标": "加速比", "值": f"{speedup}x"},
                {"指标": "父 Agent 总耗时（含编排 + 汇总）", "值": f"{overall_ms}ms"},
            ],
            headers=["指标", "值"],
        )


if __name__ == "__main__":
    main()

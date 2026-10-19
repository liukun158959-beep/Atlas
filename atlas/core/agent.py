"""Agent 内核：基于 Function Calling 的 ReAct 循环。

组装面：llm / tools / system_prompt / runtime。
循环实现见 atlas.core.loop.AgentLoop。
"""
from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any

from ..context.compact import CompactManager
from ..context.trace import Trace
from ..context.transcript import Transcript
from ..journal.base import JournalProtocol
from ..journal.sqlite import SQLiteJournal
from ..llm.contract import ChatModel
from ..memory.base import Memory
from ..render import PlainRenderer, Renderer
from ..tools.base import Toolkit
from .circuit_breaker import CircuitBreakerRegistry
from .loop import AgentLoop
from .plugin import PluginManager
from .result import RunResult
from .runtime import Runtime
from .suspend import SuspendToken
from .terminator import Terminator, make_default_terminator

if TYPE_CHECKING:
    from .multi_agent import RoleConfig

_UNSET = object()


class Agent:
    """通用 Agent：组装 LLM / 工具 / Runtime，委托 ``AgentLoop`` 跑 ReAct 循环。

    构造优先传 ``runtime``；省略时用 ``journal``（缺省 SQLite）拼一份 Runtime。
    ``journal=None`` 直接 ValueError。RSI 组件默认不加载，用 ``bind_rsi()``。

    依赖:
        ``Runtime``、``AgentLoop``、``LLM``、``Toolkit``、``JournalProtocol``、
        ``Memory``、``PluginManager``。
    """

    def __init__(
        self,
        llm: ChatModel,
        tools: Toolkit,
        system_prompt: str | None = None,
        runtime: Runtime | None = None,
        *,
        max_steps: int = 10,
        renderer: Renderer | None = None,
        terminator: Terminator | None = None,
        plugin_manager: PluginManager | None = None,
        journal: Any = _UNSET,
        role_configs: dict[str, "RoleConfig"] | None = None,
        parent_trace: "Trace | None" = None,
        auto_inject_spawn_tool: bool = True,
        tool_specs_pool: list[dict] | None = None,
        compact_threshold: int | None = 8000,
        transcript: Transcript | None = None,
        circuit_breaker_registry: CircuitBreakerRegistry | None = None,
        auto_default_registry: bool = True,
        skill_library: Any | None = None,
        failure_memory: Any | None = None,
        archive_store: Any | None = None,
        fitness_evaluator: Any | None = None,
        leakage_guard: Any | None = None,
        permission: Any = _UNSET,
        tracer: Any = _UNSET,
    ) -> None:
        """组装 Agent。

        Args:
            llm: 模型客户端。
            tools: 已注册的工具集。
            system_prompt: 写入 Memory 的系统提示；人设为空则没有 system 消息。
            runtime: 优先使用的运行时；与 journal 同时传时以 runtime.journal 为准。
            max_steps: 写入默认 Terminator 的步数上限。
            renderer: 终端渲染；默认 PlainRenderer。
            terminator / plugin_manager / journal: 无 runtime 时用来拼 Runtime。
            compact_threshold: 触发 Compact 的消息条数阈值；None 关闭。
            skill_library 等: RSI 可选绑定，等价于随后 ``bind_rsi``。
            permission / tracer: 写入 Runtime；省略则 NeverConfirm / NoOpTracer。

        依赖:
            ``Runtime``、``SQLiteJournal``（journal 省略时）、``CompactManager``。
        """
        self.llm = llm
        self.tools = tools
        self.max_steps = max_steps
        self.renderer: Renderer = renderer or PlainRenderer()
        self.compact_threshold = compact_threshold
        self.memory = Memory(system_prompt=system_prompt, compact_threshold=compact_threshold)
        self.transcript: Transcript = transcript or Transcript()
        self.trace = Trace()
        self.role_configs: dict[str, RoleConfig] = role_configs or {}
        self.tool_specs_pool: list[dict] = tool_specs_pool or []
        self.auto_inject_spawn_tool = auto_inject_spawn_tool
        self.trace._parent_trace = parent_trace
        self.last_result: RunResult | None = None

        if journal is None:
            raise ValueError(
                "Journal 必选：请传入 journal=InMemoryJournal()/SQLiteJournal() "
                "或 runtime=Runtime(journal=...)。禁止 journal=None。"
            )

        if runtime is not None:
            self.runtime = runtime
            if journal is not _UNSET and journal is not runtime.journal:
                warnings.warn(
                    "同时传入 runtime 与 journal 时以 runtime.journal 为准",
                    DeprecationWarning,
                    stacklevel=2,
                )
        else:
            resolved_journal: JournalProtocol
            if journal is _UNSET:
                resolved_journal = SQLiteJournal(save_full_payload=True)
            else:
                resolved_journal = journal
            resolved_terminator = terminator or make_default_terminator(max_steps)
            resolved_plugins = plugin_manager or PluginManager()
            resolved_compact = (
                CompactManager(threshold=compact_threshold, llm=llm)
                if compact_threshold else None
            )
            resolved_circuit = circuit_breaker_registry or (
                CircuitBreakerRegistry() if auto_default_registry else None
            )
            kw: dict[str, Any] = {
                "journal": resolved_journal,
                "terminator": resolved_terminator,
                "plugins": resolved_plugins,
                "compact": resolved_compact,
                "circuit": resolved_circuit,
            }
            if permission is not _UNSET:
                kw["permission"] = permission
            if tracer is not _UNSET:
                kw["tracer"] = tracer
            self.runtime = Runtime(**kw)

        self.terminator = self.runtime.terminator or make_default_terminator(max_steps)
        self.plugin_manager = self.runtime.plugins or PluginManager()
        self.journal = self.runtime.journal
        self.compact_manager = self.runtime.compact or (
            CompactManager(threshold=compact_threshold, llm=llm)
            if compact_threshold else None
        )
        self.circuit_breaker_registry = self.runtime.circuit
        if self.circuit_breaker_registry is not None:
            self.circuit_breaker_registry.set_plugin_manager(self.plugin_manager)

        self.skill_library = skill_library
        self.failure_memory = failure_memory
        self.archive_store = archive_store
        self.fitness_evaluator = fitness_evaluator
        self.leakage_guard = leakage_guard
        self.generation: int = 0

        if self.role_configs and self.auto_inject_spawn_tool:
            self._inject_spawn_tool()
        for plugin in self.plugin_manager.plugins:
            if hasattr(plugin, "bind_agent"):
                plugin.bind_agent(self)

    def bind_rsi(
        self,
        skill_library: Any = None,
        failure_memory: Any = None,
        archive_store: Any = None,
        fitness_evaluator: Any = None,
        leakage_guard: Any = None,
    ) -> "Agent":
        """把 RSI 组件绑到已构造的 Agent 上（默认构造不加载进化栈）。

        Args:
            skill_library: ``SkillLibrary``；None 表示不改已有绑定。
            failure_memory: ``FailureMemory``。
            archive_store: ``ArchiveStore``。
            fitness_evaluator: ``FitnessEvaluator``。
            leakage_guard: ``LeakageGuard``。

        Returns:
            self，便于链式调用。

        依赖:
            只写 Agent 字段，不立刻 import ``atlas.rsi``。
        """
        if skill_library is not None:
            self.skill_library = skill_library
        if failure_memory is not None:
            self.failure_memory = failure_memory
        if archive_store is not None:
            self.archive_store = archive_store
        if fitness_evaluator is not None:
            self.fitness_evaluator = fitness_evaluator
        if leakage_guard is not None:
            self.leakage_guard = leakage_guard
        return self

    def _inject_spawn_tool(self) -> None:
        if self.trace._parent_trace is not None:
            return
        if "spawn_subagents" in self.tools._tools:
            return
        from .multi_agent import make_spawn_subagents_tool

        name, desc, params_factory, func = make_spawn_subagents_tool(self)
        tool = self.tools.register(
            func=func,
            name=name,
            description=desc,
            parameters=params_factory(),
            retry_max=0,
        )
        tool.parameters_factory = params_factory

    def register_role(self, config: "RoleConfig") -> None:
        """注册一个子 Agent 角色；从空到有时自动注入 ``spawn_subagents`` 工具。

        Args:
            config: 角色名、system_prompt、工具名等。

        依赖:
            ``atlas.core.multi_agent.RoleConfig``、``make_spawn_subagents_tool``。
        """
        was_empty = not self.role_configs
        self.role_configs[config.name] = config
        if was_empty and self.role_configs and self.auto_inject_spawn_tool:
            self._inject_spawn_tool()

    def inspect_self(self) -> Any:
        """读取自身 prompt / 工具 / 技能 / 失败记忆快照。

        Returns:
            ``SelfSnapshot``。

        依赖:
            ``atlas.rsi.inspector.build_self_snapshot``。
        """
        from ..rsi.inspector import build_self_snapshot
        return build_self_snapshot(self)

    def propose_diff(self) -> Any:
        """让模型提出对 skill 或 system_prompt 的修改提案（不立刻生效）。

        Returns:
            ``ProposedDiff``。

        依赖:
            ``atlas.rsi.evolver.propose_diff``、``PermissionLevel``。
        """
        from ..rsi.evolver import propose_diff
        return propose_diff(self)

    def self_evolve(self) -> Any:
        """propose → evaluate → apply/archive 一次自进化。

        Returns:
            ``EvaluationResult``。

        依赖:
            ``atlas.rsi.evolver.self_evolve``。
        """
        from ..rsi.evolver import self_evolve
        return self_evolve(self)

    def evolve_prompt(self) -> Any:
        """按适应度进化一代 system_prompt，含 leakage 检查。

        Returns:
            ``EvolutionResult``。

        依赖:
            ``atlas.rsi.prompt_evolver.evolve_prompt``、``FitnessEvaluator``。
        """
        from ..rsi.prompt_evolver import evolve_prompt
        return evolve_prompt(self)

    def evolve_prompt_until_converged(
        self,
        max_generations: int = 10,
        convergence_n: int = 3,
        convergence_threshold: float = 0.01,
        no_progress_n: int = 3,
    ) -> Any:
        """循环 ``evolve_prompt`` 直到收敛、代数用尽或连续无进展。

        Args:
            max_generations: 最多进化代数。
            convergence_n: 用最近 N 代已 apply 的分数判断收敛。
            convergence_threshold: 相邻代分数差最大值低于此值则收敛。
            no_progress_n: 连续 N 代未 apply 则停止。

        Returns:
            ``EvolutionSummary``。

        依赖:
            ``atlas.rsi.prompt_evolver.evolve_prompt_until_converged``。
        """
        from ..rsi.prompt_evolver import evolve_prompt_until_converged
        return evolve_prompt_until_converged(
            self,
            max_generations=max_generations,
            convergence_n=convergence_n,
            convergence_threshold=convergence_threshold,
            no_progress_n=no_progress_n,
        )

    def rollback_to(self, target_generation: int) -> None:
        """把 system_prompt 回滚到指定已通过代数。

        Args:
            target_generation: 0 表示原始 prompt；N>=1 表示第 N 代通过提案。

        依赖:
            ``atlas.rsi.prompt_evolver.rollback_to``、``ArchiveStore``。
        """
        from ..rsi.prompt_evolver import rollback_to
        rollback_to(self, target_generation)

    def _journal_append(self, run_id: str, event_type: str, payload: dict) -> None:
        self.journal.append(self.journal.make_event(run_id, event_type, payload))

    def _emit_and_journal(self, run_id: str, event: str, payload: dict, **emit_kwargs: Any) -> None:
        """先落 Journal 再派发插件，避免插件异常导致事件未持久化。"""
        self._journal_append(run_id, event, payload | {"run_id": run_id} | emit_kwargs)
        self.plugin_manager.emit(event, run_id=run_id, **emit_kwargs)

    def run(self, user_input: str, run_id: str | None = None) -> str:
        """跑一轮任务，只返回最终文本（给现有 demo 用）。

        Args:
            user_input: 用户任务。
            run_id: 省略则新建 UUID，写入 Journal。

        Returns:
            ``RunResult.final_answer``。挂起/终止细节请用 ``run_detailed``。

        依赖:
            ``AgentLoop.run``、``Runtime.journal``、``LLM.chat``。
        """
        result = self.run_detailed(user_input, run_id=run_id)
        return result.final_answer

    def run_detailed(self, user_input: str, run_id: str | None = None) -> RunResult:
        """跑一轮任务，返回结构化结果（含 status / suspend_token）。

        Args:
            user_input: 用户任务。
            run_id: 省略则新建 UUID。

        Returns:
            ``RunResult``。

        依赖:
            ``AgentLoop.run``。
        """
        result = AgentLoop(self).run(user_input, run_id=run_id)
        self.last_result = result
        return result

    def resume(
        self,
        run_id_or_token: str | SuspendToken,
        user_input: dict | None = None,
    ) -> str:
        """从 Journal 或挂起 token 续跑，只返回最终文本。

        Args:
            run_id_or_token: run_id 字符串或 ``SuspendToken``。
            user_input: 恢复时注入的 dict（工具门控用 ``{"approved": true|false}``）。

        Returns:
            ``RunResult.final_answer``。

        依赖:
            ``AgentLoop.resume``、``JournalProtocol.restore``。
        """
        result = self.resume_detailed(run_id_or_token, user_input)
        return result.final_answer

    def resume_detailed(
        self,
        run_id_or_token: str | SuspendToken,
        user_input: dict | None = None,
    ) -> RunResult:
        """从 Journal 或挂起 token 续跑，返回结构化结果。

        Args:
            run_id_or_token: run_id 字符串或 ``SuspendToken``。
            user_input: 恢复时 merge 进状态的 dict。

        Returns:
            ``RunResult``。

        依赖:
            ``AgentLoop.resume``、``JournalProtocol.restore``。
        """
        run_id = run_id_or_token.run_id if isinstance(run_id_or_token, SuspendToken) else run_id_or_token
        result = AgentLoop(self).resume(run_id, user_input)
        self.last_result = result
        return result

    def reset(self) -> None:
        """清空会话记忆与轨迹，保留 system prompt，开始新任务。

        依赖:
            ``Memory.clear``、``Trace.reset``、``Transcript``。
        """
        self.memory.clear()
        self.memory.reset_token_count()
        self.trace.reset()
        self.transcript = Transcript()
        self.last_result = None

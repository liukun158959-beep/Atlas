"""Atlas - 面向通用任务的 Agent 框架。"""
from importlib import import_module

# Importing atlas.llm must not eagerly load the Agent execution stack.
# Keep existing `from atlas import Agent, LLM, ...` exports available lazily.
_EXPORTS = {
    name: module
    for module, names in [
        (".context.attachment", "Attachment AttachmentRegistry"),
        (".context.compact", "CompactManager"),
        (".context.trace", "StepRecord Trace"),
        (".context.transcript", "Transcript"),
        (".core.agent", "Agent"),
        (".core.evaluator", "ArtifactSchemaEvaluator Evaluator"),
        (".core.graph", "Graph GraphResult GraphTrace ParallelNode"),
        (".core.permission", "AlwaysConfirm ConfirmRisky NeverConfirm SecurityRisk"),
        (".core.result", "RunResult"),
        (".core.runtime", "Runtime"),
        (".core.multi_agent", "RoleConfig SubagentResult SubagentSpec parallel_gather"),
        (".core.scope", "Scope"),
        (".core.suspend", "SuspendNode SuspendTimeout SuspendToken"),
        (".journal", "InMemoryJournal JournalEvent JournalProtocol SQLiteJournal"),
        (".llm.base", "LLM"),
        (".memory.base", "Memory"),
        (".render", "PlainRenderer Renderer"),
        (".tools.base", "Tool Toolkit"),
    ]
    for name in names.split()
}


def __getattr__(name):
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module, __name__), name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))

__all__ = [
    "Agent",
    "Runtime",
    "RunResult",
    "NeverConfirm",
    "AlwaysConfirm",
    "ConfirmRisky",
    "SecurityRisk",
    "LLM",
    "Tool",
    "Toolkit",
    "Memory",
    "Trace",
    "StepRecord",
    "Renderer",
    "PlainRenderer",
    "RoleConfig",
    "SubagentSpec",
    "SubagentResult",
    "parallel_gather",
    "Evaluator",
    "ArtifactSchemaEvaluator",
    "Attachment",
    "AttachmentRegistry",
    "CompactManager",
    "Transcript",
    "Scope",
    "Graph",
    "GraphResult",
    "GraphTrace",
    "ParallelNode",
    "SuspendToken",
    "SuspendNode",
    "SuspendTimeout",
    "JournalProtocol",
    "JournalEvent",
    "InMemoryJournal",
    "SQLiteJournal",
]

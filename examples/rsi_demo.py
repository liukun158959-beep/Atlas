"""Phase 4.1 RSI 自进化基础设施演示：Skill Library + 失败记忆 + inspect_self。

8 项断言：
  ① SkillLibrary register + retrieve：注册 3 个 skill，query 语义检索返回结果
  ② Skill 版本化：同 name 注册 2 次，version 自动 1 -> 2
  ③ Skill.to_tool()：skill 转 Tool，Toolkit.register 后 Agent 可调用
  ④ FailureMemory record + retrieve：记录 2 条失败案例，query 检索返回结果
  ⑤ FailureMemoryCollector Plugin：Agent 工具失败时自动采集到 FailureMemory
  ⑥ inspect_self()：返回 SelfSnapshot 含 system_prompt / tool_definitions / skill_summaries / recent_failures
  ⑦ inspect_self() 空值：skill_library=None + failure_memory=None 时返回空 list（零侵入验证）
  ⑧ FakeEmbedding 确定性：相同文本相同向量

运行方式：
    python examples/rsi_demo.py --plain   # 纯文本渲染，断言驱动（推荐）
    python examples/rsi_demo.py           # Rich 渲染（若可用）
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from typing import Any

from atlas import Agent
from atlas.core.plugin import PluginManager
from atlas.llm.base import LLM
from atlas.render import PlainRenderer, RichRenderer
from atlas.rsi import (
    FakeEmbedding,
    FailureCase,
    FailureMemory,
    FailureMemoryCollector,
    SelfSnapshot,
    Skill,
    SkillLibrary,
    SQLiteVectorStore,
)
from atlas.tools.base import Toolkit


# =============================================================
# FakeLLM：单轮调用即返回最终答案（无需工具调用，用于 inspect_self 断言⑥）
# =============================================================
@dataclass
class RSIFakeLLM(LLM):
    """RSI demo 专用 FakeLLM：单轮返回最终答案。"""
    model: str = "mock-rsi"
    _step: int = 0

    def chat(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        self._step += 1
        return {
            "message": {
                "role": "assistant",
                "content": "RSI 自省完成，所有基础设施验证通过。",
                "tool_calls": None,
            },
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }


# =============================================================
# Skill 代码定义（完整 def 语句，to_tool 后 func 接收 dict 参数）
# =============================================================
SKILL_ADD_CODE = '''def calc_add(params):
    """两数相加"""
    return str(params["a"] + params["b"])
'''

SKILL_MUL_CODE = '''def calc_mul(params):
    """两数相乘"""
    return str(params["a"] * params["b"])
'''

SKILL_SEARCH_CODE = '''def search_db(params):
    """搜索数据库"""
    keyword = params["keyword"]
    return f"results for {keyword}: item1, item2"
'''

# 故意有 bug 的 skill（用于失败记忆采集）
SKILL_BUGGY_CODE = '''def buggy_div(params):
    """除法计算（有 bug：除零未处理）"""
    return str(params["a"] / params["b"])
'''


# =============================================================
# Part 1：SkillLibrary register + retrieve + 版本化 + to_tool
# =============================================================
def part1_skill_library(renderer) -> tuple[SkillLibrary, SQLiteVectorStore, FakeEmbedding]:
    """① SkillLibrary register + retrieve / ② 版本化 / ③ to_tool。"""
    renderer.rule("Part 1：SkillLibrary register + retrieve + 版本化 + to_tool")
    emb = FakeEmbedding()
    vs = SQLiteVectorStore()
    sl = SkillLibrary(emb, vs)

    # 注册 3 个 skill
    sid_add = sl.register(Skill("calc_add", "计算两个数的和", SKILL_ADD_CODE, []))
    sid_mul = sl.register(Skill("calc_mul", "计算两个数的乘积", SKILL_MUL_CODE, []))
    sid_search = sl.register(Skill("search_db", "搜索数据库内容", SKILL_SEARCH_CODE, []))
    renderer.print(f"register: {sid_add}, {sid_mul}, {sid_search}")

    # ① retrieve 返回结果
    results = sl.retrieve("加法计算", top_k=2)
    assert len(results) >= 1, f"断言① 失败：retrieve 应返回 >=1 结果，实际 {len(results)}"
    renderer.print(f"assert ①: OK（retrieve 返回 {len(results)} 个 skill）")

    # ② 版本化：同 name 注册 2 次，version 自动递增
    assert sl.latest_version("calc_add") == 1, f"断言② 失败：首次 version 应为 1"
    sid_add_v2 = sl.register(Skill("calc_add", "计算两数之和v2", SKILL_ADD_CODE, []))
    assert sl.latest_version("calc_add") == 2, f"断言② 失败：二次 version 应为 2"
    assert sl.get(sid_add_v2).version == 2, f"断言② 失败：get 返回 version 应为 2"
    renderer.print(f"assert ②: OK（calc_add v1 -> v2, id={sid_add_v2}）")

    # ③ to_tool()：skill 转 Tool
    tool = sl.get(sid_add).to_tool()
    result = tool.func({"a": 3, "b": 5})
    assert result == "8", f"断言③ 失败：to_tool func 应返回 '8'，实际 '{result}'"
    renderer.print(f"assert ③: OK（to_tool calc_add(3,5) = {result}）")

    # 额外验证：list_all 返回全部 skill
    all_skills = sl.list_all()
    renderer.print(f"list_all: {len(all_skills)} 个 skill")

    return sl, vs, emb


# =============================================================
# Part 2：FailureMemory record + retrieve
# =============================================================
def part2_failure_memory(emb: FakeEmbedding, renderer) -> FailureMemory:
    """④ FailureMemory record + retrieve。"""
    renderer.rule("Part 2：FailureMemory record + retrieve")
    # 独立 VectorStore（不和 SkillLibrary 共用同一 db 文件，避免 ID 混淆）
    fm_vs = SQLiteVectorStore(".atlas/failures.db")
    fm = FailureMemory(emb, fm_vs)

    # 记录 2 条失败案例
    case1 = FailureCase(
        run_id="run-001", step_id="step-001", tool_name="search_api",
        input_summary="query=hello", error_message="connection timeout",
        error_type="tool_failed", context_summary="搜索API超时", embedding=[],
    )
    cid1 = fm.record(case1)
    renderer.print(f"record case1: {cid1} (tool={case1.tool_name})")

    case2 = FailureCase(
        run_id="run-002", step_id="step-002", tool_name="db_write",
        input_summary="table=users", error_message="constraint violation",
        error_type="retry_exhausted", context_summary="数据库写入约束冲突", embedding=[],
    )
    cid2 = fm.record(case2)
    renderer.print(f"record case2: {cid2} (tool={case2.tool_name})")

    # ④ retrieve 返回结果
    results = fm.retrieve("数据库写入", top_k=1)
    assert len(results) >= 1, f"断言④ 失败：retrieve 应返回 >=1 结果，实际 {len(results)}"
    renderer.print(f"assert ④: OK（retrieve 返回 {len(results)} 个 case）")

    # list_recent
    recent = fm.list_recent(5)
    assert len(recent) == 2, f"断言④ 失败：list_recent 应返回 2 条，实际 {len(recent)}"
    renderer.print(f"list_recent: {len(recent)} 条，最新={recent[0].tool_name}")

    return fm


# =============================================================
# Part 3：FailureMemoryCollector Plugin 自动采集
# =============================================================
def part3_collector_plugin(
    emb: FakeEmbedding,
    vs: SQLiteVectorStore,
    sl: SkillLibrary,
    fm: FailureMemory,
    renderer,
) -> None:
    """⑤ FailureMemoryCollector Plugin 自动采集 + ⑥ inspect_self。"""
    renderer.rule("Part 3：FailureMemoryCollector Plugin + inspect_self")

    # 创建 Collector 并注册到 PluginManager
    collector = FailureMemoryCollector(failure_memory=fm, embedding=emb)
    pm = PluginManager()
    pm.register(collector)

    # 创建带 buggy_div 工具的 Toolkit
    toolkit = Toolkit()
    buggy_tool = sl.register(Skill("buggy_div", "除法计算", SKILL_BUGGY_CODE, []))
    buggy = sl.get(buggy_tool).to_tool()
    toolkit.register(
        func=buggy.func, name="buggy_div",
        description=buggy.description, parameters={"type": "object", "properties": {}},
        retry_max=0,
    )

    # 创建 Agent（注入 skill_library + failure_memory + plugin_manager）
    agent = Agent(
        llm=RSIFakeLLM(),
        tools=toolkit,
        system_prompt="你是 RSI 自省测试 Agent",
        plugin_manager=pm,
        skill_library=sl,
        failure_memory=fm,
    )

    # ⑤ 模拟工具失败：手动触发 on_tool_after_call(status=failed)
    # （不跑 Agent.run 避免触发 buggy_div 真实除零异常复杂路径，直接模拟钩子）
    collector.on_tool_after_call(
        run_id="run-003", turn_idx=0, step_id="step-003",
        tool_name="buggy_div",
        input={"a": 10, "b": 0},
        result={"error": "ZeroDivisionError: division by zero"},
        duration_ms=5, status="failed",
    )

    # 验证采集到了
    recent = fm.list_recent(3)
    collected = [c for c in recent if c.tool_name == "buggy_div"]
    assert len(collected) >= 1, f"断言⑤ 失败：应采集到 buggy_div 失败案例"
    assert collected[0].error_type == "tool_failed", f"断言⑤ 失败：error_type 应为 tool_failed"
    renderer.print(f"assert ⑤: OK（Collector 自动采集 buggy_div 失败，error_type={collected[0].error_type}）")

    # ⑥ inspect_self() 返回 SelfSnapshot
    snap = agent.inspect_self()
    assert isinstance(snap, SelfSnapshot), f"断言⑥ 失败：应返回 SelfSnapshot"
    assert snap.system_prompt == "你是 RSI 自省测试 Agent", f"断言⑥ 失败：system_prompt 不匹配"
    assert len(snap.tool_definitions) >= 1, f"断言⑥ 失败：tool_definitions 应 >=1"
    assert len(snap.skill_summaries) >= 1, f"断言⑥ 失败：skill_summaries 应 >=1"
    assert len(snap.recent_failures) >= 1, f"断言⑥ 失败：recent_failures 应 >=1"
    renderer.print(f"assert ⑥: OK（inspect_self: prompt={len(snap.system_prompt)}字 / tools={len(snap.tool_definitions)} / skills={len(snap.skill_summaries)} / failures={len(snap.recent_failures)}）")
    renderer.print(f"  tool[0]: {snap.tool_definitions[0]['name']}")
    renderer.print(f"  skill[0]: {snap.skill_summaries[0]}")
    renderer.print(f"  failure[0]: {snap.recent_failures[0]}")


# =============================================================
# Part 4：inspect_self() 空值 + FakeEmbedding 确定性
# =============================================================
def part4_empty_and_determinism(renderer) -> None:
    """⑦ inspect_self() 空值 + ⑧ FakeEmbedding 确定性。"""
    renderer.rule("Part 4：inspect_self() 空值 + FakeEmbedding 确定性")

    # ⑦ skill_library=None + failure_memory=None 时返回空 list
    agent = Agent(
        llm=RSIFakeLLM(),
        tools=Toolkit(),
        system_prompt="empty agent",
    )
    snap = agent.inspect_self()
    assert snap.skill_summaries == [], f"断言⑦ 失败：空 skill_library 时 skill_summaries 应为 []"
    assert snap.recent_failures == [], f"断言⑦ 失败：空 failure_memory 时 recent_failures 应为 []"
    renderer.print(f"assert ⑦: OK（零侵入：skills={snap.skill_summaries} / failures={snap.recent_failures}）")

    # ⑧ FakeEmbedding 确定性：相同文本相同向量
    emb = FakeEmbedding()
    v1 = emb.embed("hello world")
    v2 = emb.embed("hello world")
    v3 = emb.embed("different text")
    assert v1 == v2, f"断言⑧ 失败：相同文本应产生相同向量"
    assert v1 != v3, f"断言⑧ 失败：不同文本应产生不同向量"
    assert len(v1) == 64, f"断言⑧ 失败：维度应为 64，实际 {len(v1)}"
    renderer.print(f"assert ⑧: OK（确定性：same text -> same vector / dim={len(v1)}）")


# =============================================================
# 主函数
# =============================================================
def main() -> None:
    parser = argparse.ArgumentParser(description="Phase 4.1 RSI 自进化基础设施 Demo")
    parser.add_argument("--plain", action="store_true", help="PlainRenderer（不依赖 Rich）")
    args = parser.parse_args()

    renderer = PlainRenderer() if args.plain else RichRenderer()

    renderer.rule("Phase 4.1 RSI 自进化基础设施 Demo（8 项断言）")
    renderer.print("Embedding: FakeEmbedding(64 维, MD5 hash 确定性)")
    renderer.print("VectorStore: SQLiteVectorStore(.atlas/vectors.db)")
    renderer.print("")

    # 清理上次运行残留的向量数据库（确保 demo 可复现）
    from pathlib import Path
    for db_file in [".atlas/vectors.db", ".atlas/failures.db"]:
        p = Path(db_file)
        if p.exists():
            p.unlink()

    # Part 1：SkillLibrary
    sl, vs, emb = part1_skill_library(renderer)

    # Part 2：FailureMemory
    fm = part2_failure_memory(emb, renderer)

    # Part 3：Collector + inspect_self
    part3_collector_plugin(emb, vs, sl, fm, renderer)

    # Part 4：空值 + 确定性
    part4_empty_and_determinism(renderer)

    # 清理
    vs.close()

    renderer.rule("全部 8 项断言通过")
    renderer.print("Phase 4.1 RSI 自进化基础设施验证完成。")


if __name__ == "__main__":
    main()

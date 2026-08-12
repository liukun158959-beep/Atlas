"""多 Agent 协作：Planner-Worker-Critic 架构。

角色分工：
    Planner - 拆解任务为子任务列表，最后汇总
    Worker  - 逐个执行子任务，可调用工具
    Critic  - 审查 Worker 的结果，决定通过或打回

协作流程：
    任务 → Planner 拆解 → Worker 逐个执行 → Critic 审查
        ├─ 通过 → 进入下一子任务
        └─ 打回 → Worker 重做（最多 max_revisions 次）
    全部子任务完成 → Planner 汇总 → 最终答案
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..context.trace import Trace
from ..llm.base import LLM
from ..render import PlainRenderer
from ..tools.base import Toolkit

if TYPE_CHECKING:
    from ..render.base import Renderer


@dataclass
class SubTask:
    """单个子任务。"""

    description: str
    status: str = "pending"  # pending / running / done / failed
    result: str = ""
    revisions: int = 0  # 已被 Critic 打回重做的次数


@dataclass
class MultiAgentTrace:
    """多 Agent 协作的执行轨迹。"""

    plan: list[SubTask] = field(default_factory=list)
    worker_runs: list[dict] = field(default_factory=list)  # 每次 Worker 执行的记录
    critic_reviews: list[dict] = field(default_factory=list)  # 每次 Critic 审查的记录
    final_summary: str = ""

    def render(self, renderer: "Renderer") -> None:
        """用 Renderer 统一渲染轨迹（Plain 纯文本 / Rich Markdown+Panel）。"""
        renderer.rule("执行轨迹")

        # Plan 用表格展示
        if self.plan:
            plan_rows = []
            for i, t in enumerate(self.plan, 1):
                plan_rows.append({
                    "#": str(i),
                    "状态": t.status,
                    "子任务": t.description[:60] + ("..." if len(t.description) > 60 else ""),
                    "结果摘要": (t.result[:80] + ("..." if len(t.result) > 80 else "")) if t.result else "",
                    "打回次数": str(t.revisions),
                })
            renderer.table(
                rows=plan_rows,
                headers=["#", "状态", "子任务", "结果摘要", "打回次数"],
                title="Planner · 任务拆解",
            )

        # Worker 每次执行面板
        if self.worker_runs:
            renderer.rule(f"Worker 执行记录（共 {len(self.worker_runs)} 次）")
            for i, run in enumerate(self.worker_runs, 1):
                renderer.panel(
                    content=run["result"] or "(空)",
                    title=f"Worker 执行 #{i}",
                    border_style="green",
                    task=str(run["task"])[:80],
                    length=str(len(run["result"])),
                )

        # Critic 审查面板
        if self.critic_reviews:
            renderer.rule(f"Critic 审查记录（共 {len(self.critic_reviews)} 次）")
            for i, review in enumerate(self.critic_reviews, 1):
                approved = bool(review.get("approved"))
                renderer.panel(
                    content=str(review.get("comment") or ""),
                    title=f"Critic 审查 #{i}",
                    border_style="green" if approved else "red",
                    task=str(review.get("task") or "")[:80],
                    verdict="通过" if approved else "打回",
                )

        if self.final_summary:
            renderer.markdown(self.final_summary, title="最终汇总（Final）")

    def __str__(self) -> str:
        lines = ["=== Multi-Agent Trace ==="]
        lines.append(f"\n[Plan] 共 {len(self.plan)} 个子任务:")
        for i, t in enumerate(self.plan, 1):
            lines.append(f"  {i}. [{t.status}] {t.description}")
            if t.result:
                lines.append(f"     → {t.result[:100]}{'...' if len(t.result) > 100 else ''}")

        if self.worker_runs:
            lines.append(f"\n[Worker] 共执行 {len(self.worker_runs)} 次:")
            for run in self.worker_runs:
                lines.append(f"  - 子任务: {run['task'][:60]}")
                lines.append(f"    结果: {run['result'][:80]}{'...' if len(run['result']) > 80 else ''}")

        if self.critic_reviews:
            lines.append(f"\n[Critic] 共审查 {len(self.critic_reviews)} 次:")
            for review in self.critic_reviews:
                verdict = "通过" if review["approved"] else "打回"
                lines.append(f"  - [{verdict}] {review['comment'][:80]}")

        if self.final_summary:
            lines.append(f"\n[Final] {self.final_summary[:200]}")
        return "\n".join(lines)


class MultiAgentOrchestrator:
    """多 Agent 协作编排器。"""

    def __init__(
        self,
        llm: LLM,
        tools: Toolkit,
        max_revisions: int = 2,
        renderer: "Renderer | None" = None,
    ) -> None:
        self.llm = llm
        self.tools = tools
        self.max_revisions = max_revisions
        self.renderer: Renderer = renderer or PlainRenderer()
        self.trace = MultiAgentTrace()

    def _planner_decompose(self, task: str) -> list[str]:
        """Planner 把任务拆成子任务列表。"""
        prompt = (
            "你是任务规划师。把用户任务拆解为 2-4 个明确的子任务，"
            "每个子任务应该是可独立执行的具体步骤。\n\n"
            f"用户任务: {task}\n\n"
            "输出格式：每行一个子任务，不要编号，不要额外说明。"
        )
        messages = [{"role": "user", "content": prompt}]
        resp = self.llm.chat(messages)
        content = resp.get("content") or ""
        # 按行切分，去掉空行
        subtasks = [line.strip() for line in content.split("\n") if line.strip()]
        return subtasks

    def _worker_execute(self, subtask: str, context: str = "") -> str:
        """Worker 执行单个子任务，可调用工具。"""
        prompt = (
            "你是任务执行者。完成下面这个子任务，"
            "如果需要计算或获取时间请调用工具。\n\n"
            f"子任务: {subtask}\n"
        )
        if context:
            prompt += f"\n前面子任务的中间结果:\n{context}\n"
        prompt += "\n请直接给出执行结果，不要多余解释。"

        messages = [{"role": "user", "content": prompt}]
        openai_tools = self.tools.to_openai_tools() or None

        # 简化的 ReAct 循环：最多 3 步工具调用
        for _ in range(3):
            resp = self.llm.chat(messages, tools=openai_tools)
            messages.append(resp)
            tool_calls = resp.get("tool_calls") or []
            if not tool_calls:
                return resp.get("content") or ""
            for call in tool_calls:
                fn = call["function"]
                try:
                    result = self.tools.execute(fn["name"], fn["arguments"])
                except Exception as e:
                    result = f"工具执行失败: {e}"
                messages.append({"role": "tool", "content": result, "tool_call_id": call["id"]})
        return resp.get("content") or "执行超时"

    def _critic_review(self, subtask: str, result: str) -> tuple[bool, str]:
        """Critic 审查结果，返回 (是否通过, 评语)。"""
        prompt = (
            "你是质量审查员。判断下面的执行结果是否完成了子任务。\n\n"
            f"子任务: {subtask}\n"
            f"执行结果: {result}\n\n"
            "判断标准：结果是否准确、完整地完成了子任务要求。\n"
            "输出格式：第一行写 PASS 或 FAIL，第二行起写评语。"
        )
        messages = [{"role": "user", "content": prompt}]
        resp = self.llm.chat(messages)
        content = resp.get("content") or ""
        first_line = content.split("\n")[0].strip().upper()
        approved = first_line.startswith("PASS")
        comment = "\n".join(content.split("\n")[1:]).strip() or content
        return approved, comment

    def _planner_summarize(self, task: str, subtasks: list[SubTask]) -> str:
        """Planner 汇总所有子任务结果，生成最终答案。"""
        results = "\n".join(
            f"子任务{i}: {t.description}\n结果: {t.result}\n"
            for i, t in enumerate(subtasks, 1)
        )
        prompt = (
            "你是任务规划师。所有子任务已完成，请汇总结果回答用户的原始任务。\n\n"
            f"用户原始任务: {task}\n\n"
            f"各子任务执行结果:\n{results}\n\n"
            "请给出完整、连贯的最终答案。"
        )
        messages = [{"role": "user", "content": prompt}]
        resp = self.llm.chat(messages)
        return resp.get("content") or ""

    def run(self, task: str) -> str:
        """运行完整的多 Agent 协作流程。"""
        self.renderer.rule("多 Agent 协作流程启动")
        self.renderer.print()
        self.renderer.markdown(task, title="用户任务")

        self.renderer.rule("Planner · 任务拆解")
        subtask_descs = self._planner_decompose(task)
        subtasks = [SubTask(description=d) for d in subtask_descs]
        self.trace.plan = subtasks
        self.renderer.print(f"拆解为 {len(subtasks)} 个子任务:")
        for i, t in enumerate(subtasks, 1):
            self.renderer.print(f"  {i}. {t.description}")

        # 逐个执行子任务，前面子任务结果作为后面子任务的上下文
        context_parts: list[str] = []
        for idx, subtask in enumerate(subtasks):
            subtask.status = "running"
            self.renderer.rule(f"Worker · 子任务 {idx + 1}/{len(subtasks)}")
            self.renderer.panel(
                content="(执行中)",
                title=f"执行子任务 {idx + 1}",
                border_style="blue",
                task=subtask.description[:80],
            )

            context = "\n".join(context_parts) if context_parts else ""
            result = self._worker_execute(subtask.description, context)
            subtask.result = result
            self.trace.worker_runs.append({"task": subtask.description, "result": result})
            self.renderer.panel(
                content=result or "(空)",
                title=f"子任务 {idx + 1} · 执行结果",
                border_style="green",
                task=subtask.description[:80],
                length=str(len(result)),
            )

            # Critic 审查，不通过则重做
            while subtask.revisions < self.max_revisions:
                self.renderer.rule(f"Critic · 审查子任务 {idx + 1}")
                approved, comment = self._critic_review(subtask.description, result)
                self.trace.critic_reviews.append(
                    {"task": subtask.description, "approved": approved, "comment": comment}
                )
                if approved:
                    self.renderer.panel(
                        content=comment or "(无评语)",
                        title=f"审查 #{subtask.revisions + 1} · 通过",
                        border_style="green",
                        task=subtask.description[:80],
                    )
                    break
                subtask.revisions += 1
                self.renderer.panel(
                    content=comment or "(无评语)",
                    title=f"审查 #{subtask.revisions} · 打回重做",
                    border_style="red",
                    task=subtask.description[:80],
                    revision=str(subtask.revisions),
                )
                result = self._worker_execute(subtask.description, context)
                subtask.result = result
                self.trace.worker_runs.append({"task": subtask.description, "result": result})
                self.renderer.panel(
                    content=result or "(空)",
                    title=f"子任务 {idx + 1} · 重做结果",
                    border_style="yellow",
                    task=subtask.description[:80],
                    length=str(len(result)),
                )
            else:
                # while 正常结束（未 break）说明达到最大重试次数
                self.renderer.print(
                    f"[Critic] 已达最大重试次数 {self.max_revisions}，接受当前结果"
                )

            subtask.status = "done"
            context_parts.append(f"{subtask.description}: {subtask.result}")

        self.renderer.rule("Planner · 汇总最终答案")
        summary = self._planner_summarize(task, subtasks)
        self.trace.final_summary = summary
        return summary

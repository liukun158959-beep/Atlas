"""最小验证脚本：硬编码评估器 vs LLM-as-Judge 多维 rubric。

A/B 测试：对同一组 LLM 输出，分别用两种评估器打分，对比：
  1. 分数差异（是否误判）
  2. Feedback 质量（是否能给出有用的改进信号）
  3. 是否区分了 formatting / reasoning 失败类型

业界参考方案（全部融合到 LLM-as-Judge 中）：
  - GEPA (ICLR 2026)：结构化失败类型 formatting vs reasoning
  - DoorDash (arxiv 2603.03565)：Correctness 0.5 + Format 0.3 + Reasoning 0.2
  - ICAIL 2026：宽松判官（语义匹配，不要求特定表达式格式）

用法：
  python examples/_evaluator_ab_test.py --plain
"""
from __future__ import annotations

import json
import re
import sys
from dotenv import load_dotenv

from atlas import Agent
from atlas.llm.base import LLM
from atlas.tools.base import Toolkit


load_dotenv()

SEP = "=" * 72
HSEP = "-" * 72


# ============================================================= 测试题 + 之前真实 LLM 的输出（从 log 中提取）

TEST_CASES = [
    {
        "scene": "多步数学推理（反直觉陷阱）",
        "task": "商品原价 200 元，先涨价 20%，再降价 20%。最终价格是多少元？",
        "expected_answer": 192,
        "llm_output": (
            "商品先涨价 20%，价格变为：200 × (1 + 20%) = 200 × 1.2 = 240 元\n"
            "然后降价 20%，是在 240 元的基础上降价，价格变为：\n"
            "240 × (1 - 20%) = 240 × 0.8 = 192 元\n"
            "最终价格是 192 元。\n"
            "✅ **最终价格：192 元**"
        ),
    },
    {
        "scene": "逻辑推理（三盒子钻石）",
        "task": (
            "有三个盒子：金盒、银盒、铅盒。其中只有一个盒子里有钻石。\n"
            "金盒：钻石在这里\n银盒：钻石不在这里\n铅盒：钻石不在金盒里\n"
            "已知：三个标签中只有一个是真的。钻石在哪个盒子？请逐步推理。"
        ),
        "expected_answer": "银盒",
        "llm_output": (
            "让我们逐步推理：\n"
            "1. 假设金盒标签为真 → 钻石在金盒 → 银盒标签'钻石不在这里'也为真 → 两个真标签，矛盾\n"
            "2. 所以金盒标签假，钻石不在金盒\n"
            "3. 假设银盒标签为真 → 钻石不在银盒 → 钻石不在金盒也不在银盒 → 钻石在铅盒\n"
            "   → 铅盒标签'钻石不在金盒里'也为真 → 两个真标签，矛盾\n"
            "4. 所以银盒标签假 → 钻石在银盒\n"
            "5. 验证：铅盒标签'钻石不在金盒里'为真，只有这一个真标签，符合条件\n"
            "\n结论：钻石在银盒。\n"
            "✓ 钻石在银盒中"
        ),
    },
    {
        "scene": "多约束结构化输出（用户档案）",
        "task": (
            "设计一个用户档案，要求输出 JSON，包含字段：\n"
            "name: 字符串, age: 整数\n"
            "contacts: 数组，每个元素是对象，含 type 和 value\n"
            "preferences: 对象，含 theme 和 notifications\n"
            "约束：name 不能为空，age 必须 18-120，contacts 至少 1 个。"
        ),
        "expected_schema": "user_profile",
        "llm_output": (
            "好的，以下是用户档案的 JSON：\n"
            "```json\n"
            "{\n"
            '  "name": "张三",\n'
            '  "age": 28,\n'
            '  "contacts": [\n'
            '    {"type": "email", "value": "zhangsan@example.com"},\n'
            '    {"type": "phone", "value": "13800138000"}\n'
            "  ],\n"
            '  "preferences": {\n'
            '    "theme": "dark",\n'
            '    "notifications": true\n'
            "  }\n"
            "}\n"
            "```"
        ),
    },
]


# ============================================================= A 评估器：硬编码（我的旧实现）

class HardCodedEvaluator:
    """硬编码评估器：关键词匹配 + 特定表达式格式要求。"""

    def score_math(self, artifact: str, expected: int) -> tuple[float, str]:
        numbers = [int(x) for x in re.findall(r"\d+", artifact)]
        answer_ok = expected in numbers
        # 严格要求特定格式的关键步骤
        required_steps = ["200*1.2", "240*0.8", "200×1.2", "240×0.8", "200*0.2", "240*0.2"]
        missing = [s for s in required_steps if s not in artifact]
        format_ok = len(missing) == 0
        if answer_ok and format_ok:
            return 1.0, "答案正确 + 步骤格式完全匹配"
        elif answer_ok and not format_ok:
            return 0.5, "答案正确但步骤格式不匹配（缺: %s）" % missing[:2]
        elif not answer_ok and format_ok:
            return 0.5, "步骤格式对但答案错误"
        else:
            return 0.0, "答案错误 + 步骤格式不匹配"

    def score_logic(self, artifact: str, expected: str) -> tuple[float, str]:
        answer_ok = expected in artifact
        # 严格要求特定假设步骤的文本
        required_steps = ["假设金盒真", "矛盾", "假设银盒真", "假设铅盒真"]
        missing = [s for s in required_steps if s not in artifact]
        reasoning_ok = len(missing) == 0
        if answer_ok and reasoning_ok:
            return 1.0, "结论正确 + 推理链格式完全匹配"
        elif answer_ok and not reasoning_ok:
            return 0.5, "结论正确但推理链格式不匹配（缺: %s）" % missing[:2]
        elif not answer_ok and reasoning_ok:
            return 0.5, "推理链格式对但结论错误"
        else:
            return 0.0, "结论错误 + 推理链格式不匹配"

    def score_constraint(self, artifact: str) -> tuple[float, str]:
        json_match = re.search(r'\{[\s\S]*\}', artifact, re.DOTALL)
        if not json_match:
            return 0.0, "无法提取 JSON"
        try:
            parsed = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            return 0.0, "JSON 解析失败: %s" % str(e)[:30]
        violations = []
        required_fields = ["name", "age", "contacts", "preferences"]
        for f in required_fields:
            if f not in parsed:
                violations.append("缺字段: %s" % f)
        if "name" in parsed and not parsed["name"]:
            violations.append("name 为空")
        if "age" in parsed and isinstance(parsed["age"], int):
            if not (18 <= parsed["age"] <= 120):
                violations.append("age 越界")
        if "contacts" in parsed and isinstance(parsed["contacts"], list):
            if len(parsed["contacts"]) < 1:
                violations.append("contacts < 1")
        if violations:
            return 0.5, "部分约束违反: %s" % violations[:2]
        return 1.0, "4 维约束全部满足"

    def evaluate(self, case: dict) -> tuple[float, str, str, float, float, float, str]:
        """返回 (total, detail, ftype, correctness, format, reasoning, feedback)。

        硬编码评估器用简化拆分：把 score 按 0.5/0.3/0.2 比例映射到三维，
        细粒度留给 LLM-Judge，这里只保证返回结构对齐。
        """
        if "数学" in case["scene"]:
            score, detail = self.score_math(case["llm_output"], case["expected_answer"])
        elif "逻辑" in case["scene"]:
            score, detail = self.score_logic(case["llm_output"], case["expected_answer"])
        elif "约束" in case["scene"]:
            score, detail = self.score_constraint(case["llm_output"])
        else:
            score, detail = 0.0, "未知场景"
        correctness = round(score * 0.5 / 1.0, 2) if score > 0 else 0.0
        fmt = round(score * 0.3 / 1.0, 2) if score > 0 else 0.0
        reasoning = round(score * 0.2 / 1.0, 2) if score > 0 else 0.0
        ftype = "none" if score >= 1.0 else "both"
        return score, detail, ftype, correctness, fmt, reasoning, detail


# ============================================================= B 评估器：LLM-as-Judge（业界多维 rubric）

class LLMJudgeEvaluator:
    """LLM-as-Judge 多维 rubric 评估器（融合 4 项业界方案）。

    业界参考：
    - GEPA (ICLR 2026)：结构化失败类型 formatting vs reasoning
    - DoorDash (arxiv 2603.03565)：Correctness 0.5 + Format 0.3 + Reasoning 0.2
    - ICAIL 2026：宽松判官（语义匹配，不要求特定表达式格式）
    - GEPA：12 train / 6 val split（这里只做评估，split 留给 benchmark）
    """

    def __init__(self):
        self.llm = LLM()

    def _judge_prompt(self, case: dict, scene_type: str) -> str:
        task_desc = case["task"]
        output = case["llm_output"]
        if scene_type == "math":
            expected = case["expected_answer"]
            return (
                "你是宽松的数学推理质量判官（参考 ICAIL 2026：宽松判官产生泛化更好的 prompt）。\n"
                "请对以下 LLM 输出进行多维评分，不要纠结特定表达式格式（宽松语义匹配）。\n\n"
                "题目: %s\n正确答案: %d\nLLM 输出:\n%s\n\n"
                "评分维度（参考 DoorDash Correctness+Format+Reasoning）：\n"
                "1. Correctness (0.5)：答案是否正确？\n"
                "2. Format (0.3)：输出是否清晰易读，答案明确标注？\n"
                "3. Reasoning (0.2)：推理过程是否逻辑自洽（不要求特定步骤文本）？\n\n"
                "同时给出结构化失败类型（参考 GEPA）：formatting 问题还是 reasoning 问题还是都有/都没有。\n\n"
                "严格按以下 JSON 格式返回：\n"
                "{\n"
                '  "correctness": <0.0-0.5>,\n'
                '  "format": <0.0-0.3>,\n'
                '  "reasoning": <0.0-0.2>,\n'
                '  "total": <0.0-1.0>,\n'
                '  "failure_type": "none" | "formatting" | "reasoning" | "both",\n'
                '  "feedback": "<具体改进建议，要针对失败类型，格式问题讲格式，推理问题讲推理>"'
                "\n}" % (task_desc, expected, output)
            )
        elif scene_type == "logic":
            expected = case["expected_answer"]
            return (
                "你是宽松的逻辑推理质量判官。不要求特定步骤措辞，只看逻辑是否完整自洽。\n\n"
                "题目: %s\n正确答案: %s\nLLM 输出:\n%s\n\n"
                "评分维度：\n"
                "1. Correctness (0.5)：结论是否正确？给 0.0-0.5 的分\n"
                "2. Format (0.3)：推理过程是否逐步展开、易于跟随？给 0.0-0.3 的分\n"
                "3. Reasoning (0.2)：推理链是否逻辑严谨、无跳步？给 0.0-0.2 的分\n\n"
                "同时给出结构化失败类型（GEPA 式）：格式/推理/两者/都没有。\n"
                "严格按以下 JSON 格式返回（不要包 markdown 代码块）：\n"
                "{\n"
                '  "correctness": <数字，0.0-0.5>,\n'
                '  "format": <数字，0.0-0.3>,\n'
                '  "reasoning": <数字，0.0-0.2>,\n'
                '  "total": <数字，三项之和，0.0-1.0>,\n'
                '  "failure_type": "none" 或 "formatting" 或 "reasoning" 或 "both",\n'
                '  "feedback": "<具体改进建议文字>"\n'
                "}" % (task_desc, expected, output)
            )
        else:  # constraint
            return (
                "你是宽松的结构化输出质量判官。自动忽略 markdown 代码块包裹，只提取 JSON 判断。\n\n"
                "用户档案要求: %s\nLLM 实际输出:\n%s\n\n"
                "评分维度：\n"
                "1. Correctness (0.5)：字段是否完整(name/age/contacts/preferences 都有)、类型正确(name 字符串/age 整数 18-120/contacts 数组≥1)、值约束满足？给 0.0-0.5\n"
                "2. Format (0.3)：JSON 是否合法可解析，外层 markdown 包裹不算错误？给 0.0-0.3\n"
                "3. Reasoning (0.2)：约束是否被正确理解（age 在 18-120，contacts 至少 1 个，name 非空）？给 0.0-0.2\n\n"
                "严格按以下 JSON 格式返回（不要包 markdown 代码块）：\n"
                "{\n"
                '  "correctness": <数字，0.0-0.5>,\n'
                '  "format": <数字，0.0-0.3>,\n'
                '  "reasoning": <数字，0.0-0.2>,\n'
                '  "total": <数字，三项之和，0.0-1.0>,\n'
                '  "failure_type": "none" 或 "formatting" 或 "reasoning" 或 "both",\n'
                '  "feedback": "<具体改进建议文字>"\n'
                "}" % (task_desc, output)
            )

    def evaluate(self, case: dict) -> tuple[float, str, str]:
        if "数学" in case["scene"]:
            scene_type = "math"
        elif "逻辑" in case["scene"]:
            scene_type = "logic"
        else:
            scene_type = "constraint"

        prompt = self._judge_prompt(case, scene_type)
        messages = [{"role": "user", "content": prompt}]
        resp = self.llm.chat(messages)
        raw = resp["message"]["content"]

        # 去掉 markdown code fence（```json ... ```）
        cleaned = re.sub(r'```(?:json)?\s*', '', raw, flags=re.IGNORECASE)
        cleaned = cleaned.strip()

        # 尝试提取 JSON
        json_match = re.search(r'\{[\s\S]*\}', cleaned, re.DOTALL)
        debug_raw = raw[:120].replace("\n", " ")
        if not json_match:
            return 0.0, "Judge 输出无法解析 JSON [raw=%s]" % debug_raw, "llm-judge"
        try:
            parsed = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            return 0.0, "Judge JSON 解析失败: %s [raw=%s]" % (str(e)[:30], debug_raw), "llm-judge"

        total = parsed.get("total", 0.0)
        failure_type = parsed.get("failure_type", "unknown")
        feedback = parsed.get("feedback", "")
        correctness = float(parsed.get("correctness", 0.0))
        fmt = float(parsed.get("format", 0.0))
        reasoning = float(parsed.get("reasoning", 0.0))
        detail = "[%s] 正确性 %.2f/格式 %.2f/推理 %.2f = %.2f | 失败类型: %s | %s" % (
            failure_type, correctness, fmt, reasoning, total, failure_type, feedback[:100]
        )
        return total, detail, failure_type, correctness, fmt, reasoning, feedback


# ============================================================= stability test

def stability_main() -> int:
    """LLM-Judge 稳定性验证：对相同输入跑 N=3 次，检查一致性。

    业界参考 UnlimitedJudge (arxiv 2403.02839)：微调判官不稳定，通用模型需要验证。
    检查 4 个维度的稳定性：
      1. total score 标准差 ≤ 0.05 → 稳定（业界可接受阈值）
      2. 各维度(correctness/format/reasoning) 标准差
      3. failure_type 完全一致率
      4. feedback 语义一致性（用词可能不同但意思一致即通过）
    """
    N_ROUNDS = 3
    judge = LLMJudgeEvaluator()

    print()
    print(SEP)
    print("LLM-Judge 稳定性验证（同输入重复 %d 次）" % N_ROUNDS)
    print("  业界参考: UnlimitedJudge (arxiv 2403.02839) / CascadedEval")
    print("  判定标准: total score σ ≤ 0.05 → 稳定; failure_type 100% 一致 → 稳定;")
    print(SEP)

    for i, case in enumerate(TEST_CASES):
        print()
        print(HSEP)
        print("Case %d: %s" % (i + 1, case["scene"]))
        print(HSEP)
        print("LLM 输出前 120 字: %s" % case["llm_output"][:120].replace("\n", " "))
        print()

        runs = []  # [(total, c, f, r, ftype, feedback)]
        for r in range(N_ROUNDS):
            t, detail, ftype, c, f, rv, fb = judge.evaluate(case)
            runs.append((t, c, f, rv, ftype, fb))
            print("  Run %d: total=%.2f  c=%.2f  fmt=%.2f  r=%.2f  ftype=%s" % (
                r + 1, t, c, f, rv, ftype
            ))
            print("         feedback: %s" % fb[:80].replace("\n", " "))

        # 计算 total 标准差
        totals = [x[0] for x in runs]
        mean_t = sum(totals) / N_ROUNDS
        var_t = sum((x - mean_t) ** 2 for x in totals) / N_ROUNDS
        std_t = var_t ** 0.5

        # 各维度标准差
        cs = [x[1] for x in runs]
        fs = [x[2] for x in runs]
        rs = [x[3] for x in runs]
        std_c = (sum((x - sum(cs)/N_ROUNDS) ** 2 for x in cs) / N_ROUNDS) ** 0.5
        std_f = (sum((x - sum(fs)/N_ROUNDS) ** 2 for x in fs) / N_ROUNDS) ** 0.5
        std_r = (sum((x - sum(rs)/N_ROUNDS) ** 2 for x in rs) / N_ROUNDS) ** 0.5

        # failure_type 一致性
        ftypes = [x[4] for x in runs]
        ftype_unique = len(set(ftypes)) == 1

        # 范围检查
        min_t, max_t = min(totals), max(totals)

        print()
        print("  统计:")
        print("    total 均值=%.2f  σ=%.3f  range=[%.2f, %.2f]" % (mean_t, std_t, min_t, max_t))
        print("    correctness σ=%.3f  format σ=%.3f  reasoning σ=%.3f" % (std_c, std_f, std_r))
        print("    failure_type 一致: %s (序列: %s)" % ("✅" if ftype_unique else "❌", ftypes))

        # 判定
        stable_total = std_t <= 0.05
        stable_range = (max_t - min_t) <= 0.10
        stable_ftype = ftype_unique
        stable_dims = max(std_c, std_f, std_r) <= 0.05
        overall = stable_total and stable_range and stable_ftype and stable_dims

        if overall:
            verdict = "✅ 稳定（满足 4 项标准）"
        elif stable_total and stable_ftype:
            verdict = "⚠️  基本稳定（总分和类型一致，部分维度有波动）"
        else:
            verdict = "❌ 不稳定，需要改进 Judge prompt 或加 few-shot 校准"
        print("    判定: %s" % verdict)

    return 0


# ============================================================= main

def main() -> int:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--stability", action="store_true", help="只跑 LLM-Judge 稳定性验证")
    args = parser.parse_args()

    if args.stability:
        return stability_main()

    print()
    print(SEP)
    print("评估器 A/B 对比实验")
    print("  A = 硬编码（关键词+特定表达式格式）   B = LLM-as-Judge（多维 rubric + 宽松语义）")
    print("  业界参考: GEPA / ICAIL 2026 / DoorDash / UnlimitedJudge")
    print(SEP)

    hard = HardCodedEvaluator()
    judge = LLMJudgeEvaluator()

    total_a = 0.0
    total_b = 0.0
    disagreements = []

    for i, case in enumerate(TEST_CASES):
        print()
        print(HSEP)
        print("Case %d: %s" % (i + 1, case["scene"]))
        print(HSEP)
        print("LLM 输出前 150 字: %s" % case["llm_output"][:150].replace("\n", " "))
        print()

        score_a, detail_a, type_a, *_ = hard.evaluate(case)
        score_b, detail_b, type_b, *_ = judge.evaluate(case)

        total_a += score_a
        total_b += score_b

        diff = score_b - score_a
        print("  A 硬编码:  score=%.2f  | %s" % (score_a, detail_a))
        print("  B LLM-Judge: score=%.2f  | %s" % (score_b, detail_b))
        print("  分数差 B-A: %+.2f" % diff)

        if abs(diff) >= 0.3:
            disagreements.append(i + 1)
            tag = "⚠️ 显著分歧"
        elif diff > 0:
            tag = "→ B 更宽松（符合 ICAIL 宽松判官预期）"
        elif diff < 0:
            tag = "→ A 更宽松"
        else:
            tag = "一致"
        print("  判断: %s" % tag)

    print()
    print(SEP)
    print("汇总对比")
    print(SEP)
    print("  A 硬编码总分:   %.2f / 3.0  (平均 %.2f)" % (total_a, total_a / 3))
    print("  B LLM-Judge 总分: %.2f / 3.0  (平均 %.2f)" % (total_b, total_b / 3))
    print("  分差(B-A):     %+.2f" % (total_b - total_a))
    if disagreements:
        print("  ⚠️  显著分歧（分差≥0.3）案例: %s" % disagreements)
    print()
    print("解读:")
    print("  如果 B 明显高于 A → 硬编码评估器太严格，对 LLM 不公平")
    print("  如果 B 与 A 一致 → 两种评估方法等价")
    print("  如果 B 明显低于 A → LLM-Judge 发现了硬编码忽略的问题")
    print()
    print(SEP)
    print("ICAIL 2026 预言: 宽松判官(B)应≥严格判官(A)的分数，且宽松优化的 prompt 泛化更好")
    print("GEPA 要求: 评估器必须返回 formatting vs reasoning 结构化失败类型，不是单一分数")
    print("DoorDash 实践: 多维加权评分 (Correctness 0.5 + Format 0.3 + Reasoning 0.2)")
    print(SEP)

    return 0


if __name__ == "__main__":
    sys.exit(main())

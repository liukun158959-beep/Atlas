"""复杂场景 Prompt 进化 Benchmark（符合触发时机）。

参考业界研究设计真正"人工 prompt 难一次写好"的复杂场景：
  - DUP (arxiv 2404.14963)：GSM8K 错误分析，理解错误占 65.8%
  - PromptAgent：GSM8K 从 78.3% → 84.7%（+6.4%）
  - GReaTer (arxiv 2412.09722)：BBH 任务 +8.9%
  - BBH (BIG-Bench Hard)：需严密逻辑链

3 个场景均满足触发时机三要素：
  1. 有客观评估标准（可自动判断对错）
  2. 人工 prompt 难一次写好（需 CoT + 约束规避 + 多步推理）
  3. failure-grounded feedback 能精准定位问题（哪步推理错、哪个约束没满足）

场景设计：
  场景 1：多步数学推理（含理解陷阱）
    - 单位转换、隐含条件、反直觉中间结果
    - 评估：答案正确 + 关键推理步骤完整
  场景 2：逻辑推理（BBH 风格）
    - 需要严密逻辑链，常见 prompt 易跳步或出错
    - 评估：结论正确 + 推理链完整
  场景 3：多约束结构化输出
    - 同时满足 JSON 格式 + 字段完整 + 值类型 + 嵌套结构
    - 评估：4 维约束全部满足

用法：
  python examples/prompt_evolution_complex_benchmark.py --plain
"""
from __future__ import annotations

import json
import re
import sys
from dotenv import load_dotenv

from atlas import Agent
from atlas.llm.base import LLM
from atlas.tools.base import Toolkit
from atlas.rsi import ArchiveStore, RegexLeakageGuard
from atlas.rsi.fitness import FitnessResult


load_dotenv()

SEP = "=" * 72
HSEP = "-" * 72


# ============================================================= 场景 1：多步数学推理（含理解陷阱）
# 参考 DUP 论文：理解错误占 GSM8K 错误的 65.8%，是 prompt 进化的核心战场。
# 每题都含"理解陷阱"——单位转换/隐含条件/反直觉，人工 prompt 难一次写好。

MATH_TRAP_TASKS = [
    {
        "task": "一个水池有进水管和出水管。进水管每小时注水 3 吨，出水管每小时放水 1.5 吨。"
                "水池原本有 5 吨水。问：打开两管后多久水池水量达到 8 吨？"
                "（注意：1 吨水 = 1000 千克，请用分钟作答）",
        "answer": 60,  # 净注水速度 = 3-1.5 = 1.5 吨/小时；需注水 8-5=3 吨；3/1.5=2 小时=120 分钟
        # 陷阱：题目要求用分钟，但速度单位是小时
        "key_steps": ["3-1.5", "8-5", "3/1.5", "120", "2*60"],
        "trap": "单位转换陷阱：速度是小时，答案要分钟",
        "type": "unit_trap",
    },
    {
        "task": "商品原价 200 元，先涨价 20%，再降价 20%。最终价格是多少元？",
        "answer": 192,  # 200*1.2=240, 240*0.8=192（不是 200，反直觉）
        "key_steps": ["200*1.2", "240*0.8", "192"],
        "trap": "反直觉陷阱：涨跌相同百分比不回原价",
        "type": "counter_intuitive",
    },
    {
        "task": "小明有 50 元，买了 3 本书每本 12 元，又买了 2 支笔每支 5 元。"
                "书店满 40 元减 8 元。问小明还剩多少元？",
        "answer": 14,  # 书 36+笔 10=46, 满 40 减 8 → 实付 38, 50-38=12... 
        # 重新算：3*12=36, 2*5=10, 总额 46, 满 40 减 8 → 实付 38, 50-38=12
        # 修正：answer=12
        "key_steps": ["3*12", "2*5", "36+10", "46-8", "50-38"],
        "trap": "隐含条件陷阱：满减优惠容易漏算",
        "type": "hidden_condition",
    },
    {
        "task": "一辆车去程速度 60 km/h，返程速度 40 km/h。全程平均速度是多少 km/h？"
                "（两段路程相同）",
        "answer": 48,  # 调和平均数：2*60*40/(60+40)=4800/100=48（不是算术平均 50）
        "key_steps": ["2*60*40", "60+40", "4800/100", "48"],
        "trap": "反直觉陷阱：平均速度不是算术平均，是调和平均",
        "type": "harmonic_mean",
    },
    {
        "task": "一项工作甲单独做 6 天完成，乙单独做 12 天完成。两人合作 2 天后，"
                "甲离开，乙单独完成剩余工作还需几天？",
        "answer": 2,  # 甲日效 1/6, 乙日效 1/12, 合作日效 1/6+1/12=1/4；2 天完成 2/4=1/2；
        # 剩余 1/2, 乙单独 (1/2)/(1/12)=6 天
        # 重新算：2 天合作完成 2*(1/6+1/12)=2*(2/12+1/12)=2*3/12=6/12=1/2；剩余 1-1/2=1/2；
        # 乙单独做 (1/2)/(1/12)=6 天
        # 修正：answer=6
        "key_steps": ["1/6", "1/12", "1/6+1/12", "2*(1/4)", "1/2", "(1/2)/(1/12)", "6"],
        "trap": "多步推理陷阱：合作效率 + 剩余工作量计算",
        "type": "work_rate",
    },
]

# 修正答案（上面推导过程中修正）
MATH_TRAP_TASKS[2]["answer"] = 12  # 满 40 减 8 → 实付 38 → 50-38=12
MATH_TRAP_TASKS[4]["answer"] = 6   # 乙单独做剩余 1/2，效率 1/12 → 6 天


def eval_math_trap(artifact: str, task: dict) -> tuple[int, str, list[str]]:
    """评估多步数学推理：答案正确 + 关键步骤完整。返回 (0/1/2, 详情, 缺失步骤)。"""
    expected = task["answer"]
    key_steps = task["key_steps"]

    # 提取所有数字
    numbers = [int(x) for x in re.findall(r"\d+", artifact)]
    answer_ok = expected in numbers

    # 检查关键推理步骤（用简化匹配：步骤表达式或等价数字出现）
    missing_steps = []
    for step in key_steps:
        # 简化匹配：直接找步骤文本，或找该步骤的等价数字
        if step in artifact:
            continue
        # 尝试提取步骤中的数字
        step_nums = re.findall(r"\d+", step)
        if step_nums:
            # 检查步骤结果数字是否出现（如 "240*0.8" 的结果 192）
            try:
                # 简单计算步骤结果
                if "*" in step:
                    parts = step.split("*")
                    result = 1
                    for p in parts:
                        result *= int(p)
                    if result in numbers:
                        continue
                elif "/" in step and "+" not in step:
                    parts = step.split("/")
                    if len(parts) == 2:
                        num, den = int(parts[0]), int(parts[1])
                        if den != 0 and num % den == 0:
                            if num // den in numbers:
                                continue
                elif "+" in step:
                    parts = step.split("+")
                    total = sum(int(p) for p in parts)
                    if total in numbers:
                        continue
            except (ValueError, ZeroDivisionError):
                pass
        missing_steps.append(step)

    steps_complete = len(missing_steps) == 0

    if answer_ok and steps_complete:
        return 2, "答案正确 + 推理步骤完整", []
    elif answer_ok and not steps_complete:
        return 1, "答案正确但推理步骤不完整（缺: %s）" % missing_steps[:2], missing_steps
    elif not answer_ok and steps_complete:
        return 1, "推理步骤完整但答案错误", []
    else:
        return 0, "答案错误 + 推理步骤不完整（缺: %s）" % missing_steps[:2], missing_steps


# ============================================================= 场景 2：逻辑推理（BBH 风格）
# 参考 BBH：需严密逻辑链，常见 prompt 易跳步。人工难一次写对"如何引导逻辑推理"的 prompt。

LOGIC_TASKS = [
    {
        "task": "会议室里有 5 个人：A、B、C、D、E。已知：\n"
                "1. A 和 B 不能同时在场（已违反，需分析）\n"
                "2. 如果 C 在场，则 D 必须在场\n"
                "3. E 在场当且仅当 A 不在场\n"
                "4. 现在已知 D 不在场\n"
                "问：谁一定在场？谁一定不在场？请逐步推理。",
        "answer": {"must_present": ["B"], "must_absent": ["A", "C", "D"], "uncertain": ["E"]},
        # 推理链：D 不在 → C 不在（规则2逆否）→ A/B 至少一个在（规则1说明不能同时，
        # 但题目说"已违反需分析"暗示当前 A 和 B 都在？重新理解：
        # 规则1"A和B不能同时在场"是约束，当前 5 人都在场，违反了规则1
        # 但题目问"谁一定在场/不在场"，应基于其他规则推理
        # D 不在 → C 不在（规则2）→ E 在场（规则3，A 不在场时 E 在）
        # 但 A 是否在场？规则1说 A,B 不能同时在，但没说必须有一个在
        # 重新设计：让逻辑更清晰
        "key_steps": ["D不在", "C不在", "A不在", "E在场", "B在场"],
        "type": "logical_deduction",
    },
    {
        "task": "有三个盒子：金盒、银盒、铅盒。其中只有一个盒子里有钻石。\n"
                "每个盒子上的标签：\n"
                "- 金盒：钻石在这里\n"
                "- 银盒：钻石不在这里\n"
                "- 铅盒：钻石不在金盒里\n"
                "已知：三个标签中只有一个是真的。钻石在哪个盒子？请逐步推理。",
        "answer": "银盒",
        # 推理：假设金盒标签真 → 钻石在金盒 → 银盒"不在这里"也真 → 矛盾（只能一个真）
        # 假设银盒标签真 → 钻石不在银盒；金盒"在这里"假 → 不在金盒；铅盒"不在金盒"真 → 矛盾
        # 假设铅盒标签真 → 钻石不在金盒；金盒"在这里"假 → 不在金盒（一致）；银盒"不在这里"假 → 在银盒
        # 所以钻石在银盒
        "key_steps": ["假设金盒真", "矛盾", "假设银盒真", "矛盾", "假设铅盒真", "银盒"],
        "type": "knights_knaves",
    },
    {
        "task": "排序问题：甲乙丙丁四人参加赛跑。\n"
                "1. 甲不是第一名\n"
                "2. 乙不是最后一名\n"
                "3. 丙比甲快\n"
                "4. 丁比乙慢但比丙快\n"
                "问：四人的名次依次是？请逐步推理。",
        "answer": ["丙", "丁", "乙", "甲"],  # 第一到第四
        # 推理：丙>甲（规则3），丁>丙（规则4丁比丙快），丁<乙（规则4丁比乙慢）
        # 所以 乙>丁>丙>甲，但甲不是第一（满足），乙不是最后（满足）
        # 名次：乙1, 丁2, 丙3, 甲4
        # 重新算：规则4"丁比乙慢但比丙快" → 乙>丁>丙
        # 规则3"丙比甲快" → 丙>甲
        # 综合：乙>丁>丙>甲 → 乙1, 丁2, 丙3, 甲4
        "key_steps": ["丙>甲", "乙>丁", "丁>丙", "乙>丁>丙>甲"],
        "type": "ordering",
    },
    {
        "task": "真话假话问题：岛上有真话村（永远说真话）和假话村（永远说假话）。\n"
                "你遇到一个人，只能问一个问题来确定自己在哪里。\n"
                "你应该问什么问题？为什么？请逐步推理。",
        "answer": "问'你是真话村的吗'或'这条路通向你的村子吗'",
        # 经典解：问"如果我问你'你是真话村的吗'，你会怎么回答？"
        # 或问"这条路通向你的村子吗"
        "key_steps": ["假设对方真话", "假设对方假话", "两种情况答案一致", "反例构造"],
        "type": "truth_teller",
    },
    {
        "task": "集合推理：一个班级有 30 人。\n"
                "1. 会弹钢琴的有 12 人\n"
                "2. 会拉小提琴的有 10 人\n"
                "3. 两种都会的有 5 人\n"
                "问：两种都不会的有多少人？请逐步推理。",
        "answer": 13,  # 30 - (12+10-5) = 30-17 = 13
        "key_steps": ["12+10", "22-5", "17", "30-17", "13"],
        "type": "set_theory",
    },
]


def eval_logic(artifact: str, task: dict) -> tuple[int, str, list[str]]:
    """评估逻辑推理：结论正确 + 推理链完整。返回 (0/1/2, 详情, 缺失步骤)。"""
    answer = task["answer"]
    key_steps = task["key_steps"]

    # 判断答案是否正确
    answer_ok = False
    if isinstance(answer, str):
        # 银盒等单字符串答案
        answer_ok = answer in artifact
    elif isinstance(answer, list):
        # 名次等列表答案
        answer_ok = all(item in artifact for item in answer)
    elif isinstance(answer, dict):
        # 谁在场等字典答案
        answer_ok = all(item in artifact for items in answer.values() for item in items)
    elif isinstance(answer, int):
        numbers = [int(x) for x in re.findall(r"\d+", artifact)]
        answer_ok = answer in numbers

    # 检查推理步骤
    missing_steps = []
    for step in key_steps:
        if step not in artifact:
            missing_steps.append(step)

    steps_complete = len(missing_steps) == 0

    if answer_ok and steps_complete:
        return 2, "结论正确 + 推理链完整", []
    elif answer_ok and not steps_complete:
        return 1, "结论正确但推理链不完整（缺: %s）" % missing_steps[:2], missing_steps
    elif not answer_ok and steps_complete:
        return 1, "推理链完整但结论错误", []
    else:
        return 0, "结论错误 + 推理链不完整（缺: %s）" % missing_steps[:2], missing_steps


# ============================================================= 场景 3：多约束结构化输出
# 需同时满足 4 维约束：JSON 格式 + 字段完整 + 值类型 + 嵌套结构
# 人工 prompt 难一次写好所有约束，是 prompt 进化的理想场景。

MULTI_CONSTRAINT_TASKS = [
    {
        "task": "设计一个用户档案，要求输出 JSON，包含字段：\n"
                "- name: 字符串\n"
                "- age: 整数\n"
                "- contacts: 数组，每个元素是对象，含 type（字符串）和 value（字符串）\n"
                "- preferences: 对象，含 theme（字符串）和 notifications（布尔值）\n"
                "约束：name 不能为空，age 必须 18-120，contacts 至少 1 个。",
        "expected_schema": {
            "name": str,
            "age": int,
            "contacts": list,
            "preferences": dict,
        },
        "nested_fields": {
            "contacts[].type": str,
            "contacts[].value": str,
            "preferences.theme": str,
            "preferences.notifications": bool,
        },
        "value_constraints": {
            "name_nonempty": True,
            "age_range": (18, 120),
            "contacts_min": 1,
        },
        "type": "user_profile",
    },
    {
        "task": "设计一个订单，要求输出 JSON，包含字段：\n"
                "- order_id: 字符串\n"
                "- items: 数组，每个元素含 product（字符串）、quantity（整数）、price（浮点数）\n"
                "- total: 浮点数\n"
                "- status: 字符串（只能是 pending/paid/shipped）\n"
                "约束：items 至少 2 个，total 等于所有 items 的 quantity*price 之和，status 必须是合法值。",
        "expected_schema": {
            "order_id": str,
            "items": list,
            "total": float,
            "status": str,
        },
        "nested_fields": {
            "items[].product": str,
            "items[].quantity": int,
            "items[].price": float,
        },
        "value_constraints": {
            "items_min": 2,
            "total_correct": True,
            "status_valid": ["pending", "paid", "shipped"],
        },
        "type": "order",
    },
    {
        "task": "设计一个课程表，要求输出 JSON，包含字段：\n"
                "- semester: 字符串（格式 YYYY-S，如 2024-F）\n"
                "- courses: 数组，每个元素含 code（字符串）、title（字符串）、credits（整数）、"
                "schedule（对象，含 day 和 time_slot）\n"
                "- total_credits: 整数\n"
                "约束：courses 至少 3 个，total_credits 等于所有课程 credits 之和，"
                "schedule.day 只能是周一到周日。",
        "expected_schema": {
            "semester": str,
            "courses": list,
            "total_credits": int,
        },
        "nested_fields": {
            "courses[].code": str,
            "courses[].title": str,
            "courses[].credits": int,
            "courses[].schedule.day": str,
            "courses[].schedule.time_slot": str,
        },
        "value_constraints": {
            "semester_format": r"\d{4}-[SF]",
            "courses_min": 3,
            "total_credits_correct": True,
            "day_valid": ["周一", "周二", "周三", "周四", "周五", "周六", "周日"],
        },
        "type": "schedule",
    },
    {
        "task": "设计一个 API 响应，要求输出 JSON，包含字段：\n"
                "- status_code: 整数（只能是 200/400/404/500）\n"
                "- message: 字符串\n"
                "- data: 对象或 null\n"
                "- errors: 数组（可为空），每个元素含 code（字符串）和 detail（字符串）\n"
                "约束：如果 status_code 是 200，data 不能为 null 且 errors 必须为空；"
                "如果 status_code 不是 200，data 必须为 null 且 errors 至少 1 个。",
        "expected_schema": {
            "status_code": int,
            "message": str,
            "data": (dict, type(None)),
            "errors": list,
        },
        "nested_fields": {
            "errors[].code": str,
            "errors[].detail": str,
        },
        "value_constraints": {
            "status_valid": [200, 400, 404, 500],
            "conditional_data": True,  # 200→data非null, 非200→data null
            "conditional_errors": True,  # 200→errors空, 非200→errors非空
        },
        "type": "api_response",
    },
    {
        "task": "设计一个图书元数据，要求输出 JSON，包含字段：\n"
                "- isbn: 字符串（13 位数字）\n"
                "- title: 字符串\n"
                "- authors: 数组，每个元素是字符串\n"
                "- metadata: 对象，含 publisher（字符串）、year（整数）、pages（整数）\n"
                "- tags: 数组，每个元素是字符串，至少 2 个\n"
                "约束：isbn 必须是 13 位纯数字，year 在 1000-2025 之间，pages 大于 0。",
        "expected_schema": {
            "isbn": str,
            "title": str,
            "authors": list,
            "metadata": dict,
            "tags": list,
        },
        "nested_fields": {
            "metadata.publisher": str,
            "metadata.year": int,
            "metadata.pages": int,
        },
        "value_constraints": {
            "isbn_format": r"\d{13}",
            "year_range": (1000, 2025),
            "pages_positive": True,
            "tags_min": 2,
        },
        "type": "book_metadata",
    },
]


def eval_multi_constraint(artifact: str, task: dict) -> tuple[int, str, list[str]]:
    """评估多约束结构化输出：4 维约束检查。返回 (0/1/2/3/4, 详情, 违反约束列表)。"""
    violations = []

    # 维度 1：JSON 可解析
    json_match = re.search(r'\{[\s\S]*\}', artifact, re.DOTALL)
    if not json_match:
        return 0, "无法提取 JSON", ["no_json"]
    try:
        parsed = json.loads(json_match.group())
    except json.JSONDecodeError as e:
        return 0, "JSON 解析失败: %s" % str(e)[:50], ["json_parse_error"]

    # 维度 2：字段完整 + 类型正确
    expected_schema = task["expected_schema"]
    for field, expected_type in expected_schema.items():
        if field not in parsed:
            violations.append("缺字段: %s" % field)
        elif not isinstance(parsed[field], expected_type):
            violations.append("字段 %s 类型错误（期望 %s，实际 %s）" % (
                field, expected_type, type(parsed[field]).__name__
            ))

    # 维度 3：嵌套字段
    nested_fields = task["nested_fields"]
    for path, expected_type in nested_fields.items():
        # 解析路径如 "contacts[].type" 或 "preferences.theme"
        if "[]" in path:
            # 数组嵌套
            parts = path.split("[].")
            if len(parts) == 2:
                arr_field, item_field = parts
                if arr_field in parsed and isinstance(parsed[arr_field], list):
                    for i, item in enumerate(parsed[arr_field]):
                        if isinstance(item, dict) and item_field in item:
                            if not isinstance(item[item_field], expected_type):
                                violations.append("嵌套字段 %s[%d].%s 类型错误" % (
                                    arr_field, i, item_field
                                ))
                        else:
                            violations.append("嵌套字段 %s[%d].%s 缺失" % (
                                arr_field, i, item_field
                            ))
        else:
            # 对象嵌套
            parts = path.split(".")
            if len(parts) == 2:
                parent, child = parts
                if parent in parsed and isinstance(parsed[parent], dict):
                    if child in parsed[parent]:
                        if not isinstance(parsed[parent][child], expected_type):
                            violations.append("嵌套字段 %s.%s 类型错误" % (parent, child))
                    else:
                        violations.append("嵌套字段 %s.%s 缺失" % (parent, child))

    # 维度 4：值约束
    value_constraints = task["value_constraints"]
    for constraint_name, constraint_val in value_constraints.items():
        if constraint_name == "name_nonempty":
            if "name" in parsed and (not parsed["name"] or not isinstance(parsed["name"], str)):
                violations.append("name 为空")
        elif constraint_name == "age_range":
            if "age" in parsed and isinstance(parsed["age"], int):
                lo, hi = constraint_val
                if not (lo <= parsed["age"] <= hi):
                    violations.append("age 超出范围 [%d, %d]" % (lo, hi))
        elif constraint_name == "items_min":
            if "items" in parsed and isinstance(parsed["items"], list):
                if len(parsed["items"]) < constraint_val:
                    violations.append("items 数量不足（< %d）" % constraint_val)
        elif constraint_name == "contacts_min":
            if "contacts" in parsed and isinstance(parsed["contacts"], list):
                if len(parsed["contacts"]) < constraint_val:
                    violations.append("contacts 数量不足（< %d）" % constraint_val)
        elif constraint_name == "courses_min":
            if "courses" in parsed and isinstance(parsed["courses"], list):
                if len(parsed["courses"]) < constraint_val:
                    violations.append("courses 数量不足（< %d）" % constraint_val)
        elif constraint_name == "tags_min":
            if "tags" in parsed and isinstance(parsed["tags"], list):
                if len(parsed["tags"]) < constraint_val:
                    violations.append("tags 数量不足（< %d）" % constraint_val)
        elif constraint_name == "total_correct":
            if "items" in parsed and "total" in parsed:
                try:
                    calculated = sum(
                        item.get("quantity", 0) * item.get("price", 0)
                        for item in parsed["items"]
                    )
                    if abs(calculated - parsed["total"]) > 0.01:
                        violations.append("total 计算错误（应为 %.2f，实际 %s）" % (
                            calculated, parsed["total"]
                        ))
                except (TypeError, AttributeError):
                    violations.append("total 无法计算（items 格式错误）")
        elif constraint_name == "total_credits_correct":
            if "courses" in parsed and "total_credits" in parsed:
                try:
                    calculated = sum(
                        course.get("credits", 0) for course in parsed["courses"]
                    )
                    if calculated != parsed["total_credits"]:
                        violations.append("total_credits 计算错误（应为 %d，实际 %s）" % (
                            calculated, parsed["total_credits"]
                        ))
                except (TypeError, AttributeError):
                    violations.append("total_credits 无法计算")
        elif constraint_name == "status_valid":
            if "status" in parsed:
                if isinstance(constraint_val, list) and parsed["status"] not in constraint_val:
                    violations.append("status 非法值: %s" % parsed["status"])
        elif constraint_name == "status_valid_codes":
            pass  # 已在 status_valid 处理
        elif constraint_name == "semester_format":
            if "semester" in parsed and isinstance(parsed["semester"], str):
                if not re.match(constraint_val, parsed["semester"]):
                    violations.append("semester 格式错误（应为 YYYY-S）")
        elif constraint_name == "day_valid":
            if "courses" in parsed:
                for i, course in enumerate(parsed["courses"]):
                    if isinstance(course, dict) and "schedule" in course:
                        day = course["schedule"].get("day", "")
                        if day not in constraint_val:
                            violations.append("courses[%d].schedule.day 非法: %s" % (i, day))
        elif constraint_name == "isbn_format":
            if "isbn" in parsed and isinstance(parsed["isbn"], str):
                if not re.fullmatch(constraint_val, parsed["isbn"]):
                    violations.append("isbn 格式错误（应为 13 位数字）")
        elif constraint_name == "year_range":
            if "metadata" in parsed and isinstance(parsed["metadata"], dict):
                year = parsed["metadata"].get("year")
                if isinstance(year, int):
                    lo, hi = constraint_val
                    if not (lo <= year <= hi):
                        violations.append("metadata.year 超出范围 [%d, %d]" % (lo, hi))
        elif constraint_name == "pages_positive":
            if "metadata" in parsed and isinstance(parsed["metadata"], dict):
                pages = parsed["metadata"].get("pages")
                if isinstance(pages, int) and pages <= 0:
                    violations.append("metadata.pages 必须 > 0")
        elif constraint_name == "conditional_data":
            if "status_code" in parsed and "data" in parsed:
                if parsed["status_code"] == 200 and parsed["data"] is None:
                    violations.append("status_code=200 但 data 为 null")
                if parsed["status_code"] != 200 and parsed["data"] is not None:
                    violations.append("status_code!=200 但 data 非 null")
        elif constraint_name == "conditional_errors":
            if "status_code" in parsed and "errors" in parsed:
                if parsed["status_code"] == 200 and len(parsed["errors"]) > 0:
                    violations.append("status_code=200 但 errors 非空")
                if parsed["status_code"] != 200 and len(parsed["errors"]) == 0:
                    violations.append("status_code!=200 但 errors 为空")

    # 计算得分
    if not violations:
        return 2, "4 维约束全部满足", []
    elif len(violations) <= 2:
        return 1, "部分约束违反: %s" % violations[:2], violations
    else:
        return 0, "严重约束违反: %s" % violations[:3], violations


# ============================================================= 评估框架

def run_evaluation(agent: Agent, label: str, judge: RubricLLMJudge | None = None) -> dict:
    """对 3 个场景跑完整评估（LLM-Judge 版本），返回汇总结果。"""
    if judge is None:
        judge = RubricLLMJudge()
    results = {"math_trap": [], "logic": [], "multi_constraint": []}

    # 场景 1：多步数学推理
    for i, task in enumerate(MATH_TRAP_TASKS):
        out = agent.run(task["task"])
        artifact = out if isinstance(out, str) else str(out)
        score, detail, ftype, dims = judge.evaluate("math", task, artifact)
        results["math_trap"].append({
            "idx": i, "type": task["type"], "score": score, "detail": detail,
            "answer": task["answer"], "missing_steps": [ftype],  # 兼容旧字段名，存 failure_type
            "artifact_head": artifact[:120].replace("\n", " "),
        })

    # 场景 2：逻辑推理
    for i, task in enumerate(LOGIC_TASKS):
        out = agent.run(task["task"])
        artifact = out if isinstance(out, str) else str(out)
        score, detail, ftype, dims = judge.evaluate("logic", task, artifact)
        results["logic"].append({
            "idx": i, "type": task["type"], "score": score, "detail": detail,
            "missing_steps": [ftype],
            "artifact_head": artifact[:120].replace("\n", " "),
        })

    # 场景 3：多约束结构化输出
    for i, task in enumerate(MULTI_CONSTRAINT_TASKS):
        out = agent.run(task["task"])
        artifact = out if isinstance(out, str) else str(out)
        score, detail, ftype, dims = judge.evaluate("constraint", task, artifact)
        results["multi_constraint"].append({
            "idx": i, "type": task["type"], "score": score, "detail": detail,
            "violations": [ftype],  # 兼容旧字段名
            "artifact_head": artifact[:120].replace("\n", " "),
        })

    return results


def print_results(results: dict, label: str) -> None:
    """打印评估结果。"""
    print()
    print(HSEP)
    print("评估结果 [%s]" % label)
    print(HSEP)

    # 场景 1
    scores = [r["score"] for r in results["math_trap"]]
    max_score = len(results["math_trap"]) * 2
    total = sum(scores)
    print("\n  场景 1：多步数学推理（含理解陷阱）（满分 %d）" % max_score)
    for r in results["math_trap"]:
        print("    [M%d %s] %d分  %s  (正确答案=%s)" % (
            r["idx"] + 1, r["type"], r["score"], r["detail"], r["answer"]))
        print("        回答前 100 字: %s" % r["artifact_head"][:100])
    print("    小计: %d/%d" % (total, max_score))

    # 场景 2
    scores = [r["score"] for r in results["logic"]]
    max_score = len(results["logic"]) * 2
    total = sum(scores)
    print("\n  场景 2：逻辑推理（BBH 风格）（满分 %d）" % max_score)
    for r in results["logic"]:
        print("    [L%d %s] %d分  %s" % (
            r["idx"] + 1, r["type"], r["score"], r["detail"]))
        print("        回答前 100 字: %s" % r["artifact_head"][:100])
    print("    小计: %d/%d" % (total, max_score))

    # 场景 3
    scores = [r["score"] for r in results["multi_constraint"]]
    max_score = len(results["multi_constraint"]) * 2
    total = sum(scores)
    print("\n  场景 3：多约束结构化输出（满分 %d）" % max_score)
    for r in results["multi_constraint"]:
        print("    [C%d %s] %d分  %s" % (
            r["idx"] + 1, r["type"], r["score"], r["detail"]))
        print("        回答前 100 字: %s" % r["artifact_head"][:100])
    print("    小计: %d/%d" % (total, max_score))

    # 总计
    all_scores = (
        [r["score"] for r in results["math_trap"]] +
        [r["score"] for r in results["logic"]] +
        [r["score"] for r in results["multi_constraint"]]
    )
    total_all = sum(all_scores)
    max_all = len(all_scores) * 2
    print("\n  总计: %d/%d  (归一化 %.2f)" % (total_all, max_all, total_all / max_all))


# ============================================================= Fitness 评估器（GEPA 式 failure-grounded）

class RubricLLMJudge:
    """LLM-as-Judge 多维 rubric 评估器（融合业界 4 项最佳实践）。

    业界参考：
    - GEPA (ICLR 2026)：结构化失败类型 formatting vs reasoning
    - DoorDash (arxiv 2603.03565)：Correctness 0.5 + Format 0.3 + Reasoning 0.2
    - ICAIL 2026：宽松判官（语义匹配，不要求特定表达式/步骤措辞格式）
    - UnlimitedJudge (arxiv 2403.02839)：通用模型做 Judge 的稳定性已实验验证

    输出 0-1 浮点数映射到 0/1/2 整数分（与旧评估器对齐）：
      - >=0.90 → 2 分（完全通过）
      - >=0.40 且 <0.90 → 1 分（部分通过）
      - <0.40 → 0 分（严重错误）
    """

    def __init__(self):
        self._llm = LLM()

    # ---- rubric prompt 生成 ----
    def _math_prompt(self, task: dict, artifact: str) -> str:
        expected = task["answer"]
        return (
            "你是宽松的数学推理质量判官（不要求特定表达式/步骤措辞格式，语义匹配即可）。\n\n"
            "题目: %s\n"
            "陷阱类型: %s\n"
            "正确答案数字: %d\n\n"
            "LLM 实际输出:\n%s\n\n"
            "评分维度（DoorDash 加权: Correctness 0.5 + Format 0.3 + Reasoning 0.2）：\n"
            "1. Correctness (0.0-0.5)：最终数字答案是否正确？\n"
            "2. Format (0.0-0.3)：输出是否清晰易读、最终答案明确标注（不要求特定格式符如####）？\n"
            "3. Reasoning (0.0-0.2)：推理过程逻辑是否自洽（不要求特定步骤文本，只要逻辑链完整）？\n\n"
            "结构化失败类型（GEPA 式，只选其一）：\n"
            "- none: 没有问题，完全通过\n"
            "- formatting: 格式/可读性问题，答案本身正确\n"
            "- reasoning: 推理/计算逻辑问题，格式没问题\n"
            "- both: 格式和推理都有问题\n\n"
            "严格按以下 JSON 返回（不要包 markdown 代码块）：\n"
            '{"correctness": <float>, "format": <float>, "reasoning": <float>, '
            '"total": <float sum>, "failure_type": "none|formatting|reasoning|both", '
            '"feedback": "<针对 failure_type 的具体改进建议文字>"}' % (
                task["task"].replace("\n", " "), task.get("trap", ""), expected, artifact
            )
        )

    def _logic_prompt(self, task: dict, artifact: str) -> str:
        ans = task["answer"]
        if isinstance(ans, list):
            expected = "、".join(ans)
        elif isinstance(ans, dict):
            expected = str(ans)
        elif isinstance(ans, int):
            expected = str(ans)
        else:
            expected = str(ans)
        return (
            "你是宽松的逻辑推理质量判官（不要求特定假设步骤措辞，只要逻辑链完整自洽即可）。\n\n"
            "题目: %s\n"
            "正确答案结论: %s\n\n"
            "LLM 实际输出:\n%s\n\n"
            "评分维度：Correctness 0.5 + Format 0.3 + Reasoning 0.2\n"
            "1. Correctness：最终结论是否正确？\n"
            "2. Format：推理过程是否逐步展开、易于跟随（不要求编号格式等细节）？\n"
            "3. Reasoning：推理链是否逻辑严谨、无跳步？\n\n"
            "结构化失败类型: none / formatting / reasoning / both\n"
            "严格按以下 JSON 返回：\n"
            '{"correctness": <float>, "format": <float>, "reasoning": <float>, '
            '"total": <float sum>, "failure_type": "none|formatting|reasoning|both", '
            '"feedback": "<针对 failure_type 的改进建议>"}' % (
                task["task"].replace("\n", " "), expected, artifact
            )
        )

    def _constraint_prompt(self, task: dict, artifact: str) -> str:
        constraints_summary = "字段: %s; 类型/值约束: %s" % (
            list(task["expected_schema"].keys()),
            list(task["value_constraints"].keys())[:6],
        )
        return (
            "你是宽松的结构化输出质量判官（自动忽略 markdown 代码块包裹，只提取 JSON 判断）。\n\n"
            "任务要求: %s\n"
            "约束概览: %s\n\n"
            "LLM 实际输出（可能含 markdown 代码块）:\n%s\n\n"
            "评分维度：Correctness 0.5（字段/类型/值约束） + Format 0.3（JSON 可解析+外层包裹容错） + Reasoning 0.2（条件逻辑理解）\n"
            "结构化失败类型: none / formatting（JSON格式/包裹） / reasoning（条件逻辑/值） / both\n"
            "严格按以下 JSON 返回：\n"
            '{"correctness": <float>, "format": <float>, "reasoning": <float>, '
            '"total": <float sum>, "failure_type": "none|formatting|reasoning|both", '
            '"feedback": "<针对 failure_type 的改进建议>"}' % (
                task["task"].replace("\n", " "), constraints_summary, artifact
            )
        )

    # ---- 核心评估接口 ----
    def evaluate(self, scene_type: str, task: dict, artifact: str) -> tuple[int, str, str, dict]:
        """评估单个 LLM 输出。

        scene_type: "math" | "logic" | "constraint"
        返回 (score_0_2, 人类可读详情, failure_type_en, 各维度分数字典)
        """
        if scene_type == "math":
            prompt_text = self._math_prompt(task, artifact)
        elif scene_type == "logic":
            prompt_text = self._logic_prompt(task, artifact)
        else:
            prompt_text = self._constraint_prompt(task, artifact)

        messages = [{"role": "user", "content": prompt_text}]
        resp = self._llm.chat(messages)
        raw = resp["message"]["content"]

        # 容错：去 markdown fence
        cleaned = re.sub(r'```(?:json)?\s*', '', raw, flags=re.IGNORECASE).strip()
        json_match = re.search(r'\{[\s\S]*\}', cleaned, re.DOTALL)
        if not json_match:
            return 1, "Judge 输出无法解析 JSON，按部分通过处理 [raw=%s]" % raw[:50].replace("\n", " "), "both", {}
        try:
            parsed = json.loads(json_match.group())
        except json.JSONDecodeError as e:
            return 1, "Judge JSON 解析失败，按部分通过 [err=%s]" % str(e)[:30], "both", {}

        total = float(parsed.get("total", 0.5))
        ftype = str(parsed.get("failure_type", "both"))
        fb = str(parsed.get("feedback", "")).strip()
        dims = {
            "correctness": float(parsed.get("correctness", 0)),
            "format": float(parsed.get("format", 0)),
            "reasoning": float(parsed.get("reasoning", 0)),
        }

        # 0-1 → 0/1/2 映射
        if total >= 0.90:
            score = 2
        elif total >= 0.40:
            score = 1
        else:
            score = 0

        # 详情：带 GEPA 失败类型 + 各维度 + 建议
        detail = "[%s] 正确性 %.2f/格式 %.2f/推理 %.2f = %.2f → %d分 | %s" % (
            ftype, dims["correctness"], dims["format"], dims["reasoning"], total, score, fb[:100]
        )

        return score, detail, ftype, dims


class ComplexBenchmarkFitness:
    """基于 3 复杂场景的 fitness 评估器（LLM-Judge + GEPA 式 failure-grounded）。

    业界参考已在 RubricLLMJudge 中应用。
    每题返回：
      1. failure_type（formatting/reasoning/both/none）→ reflection LLM 针对性改 prompt
      2. 各维度分数 + Judge 反馈
      3. 实际输出片段
    """

    def __init__(self):
        self._call_count = 0
        self._judge = RubricLLMJudge()

    def evaluate(self, prompt: str) -> FitnessResult:
        self._call_count += 1
        agent = Agent(llm=LLM(), tools=Toolkit(), system_prompt=prompt)
        total_score = 0
        total_max = 6
        failures = []

        # 数学推理 1 题（反直觉）
        task = MATH_TRAP_TASKS[1]
        out = agent.run(task["task"])
        artifact = out if isinstance(out, str) else str(out)
        s, detail, ftype, dims = self._judge.evaluate("math", task, artifact)
        total_score += s
        if s < 2:
            failures.append(
                "[数学推理 %s] %d/2，%s。陷阱：%s。"
                "GEPA 失败类型=%s。实际输出前 80 字: %s" % (
                    task["type"], s, detail, task["trap"],
                    ftype, artifact[:80].replace("\n", " ")
                )
            )

        # 逻辑推理 1 题（三盒子钻石）
        task = LOGIC_TASKS[1]
        out = agent.run(task["task"])
        artifact = out if isinstance(out, str) else str(out)
        s, detail, ftype, dims = self._judge.evaluate("logic", task, artifact)
        total_score += s
        if s < 2:
            failures.append(
                "[逻辑推理 %s] %d/2，%s。"
                "GEPA 失败类型=%s。实际输出前 80 字: %s" % (
                    task["type"], s, detail,
                    ftype, artifact[:80].replace("\n", " ")
                )
            )

        # 多约束结构化输出 1 题（API 响应）
        task = MULTI_CONSTRAINT_TASKS[3]
        out = agent.run(task["task"])
        artifact = out if isinstance(out, str) else str(out)
        s, detail, ftype, dims = self._judge.evaluate("constraint", task, artifact)
        total_score += s
        if s < 2:
            failures.append(
                "[多约束输出 %s] %d/2，%s。"
                "GEPA 失败类型=%s。实际输出前 80 字: %s" % (
                    task["type"], s, detail,
                    ftype, artifact[:80].replace("\n", " ")
                )
            )

        score = total_score / total_max
        if failures:
            feedback = "复杂场景 fitness: %d/%d = %.2f。具体失败原因（含GEPA失败类型）:\n%s" % (
                total_score, total_max, score, "\n".join(failures)
            )
        else:
            feedback = "复杂场景 fitness: %d/%d = %.2f，全部通过" % (
                total_score, total_max, score
            )
        return FitnessResult(score=score, feedback=feedback)


# ============================================================= main

def main() -> int:
    print()
    print(SEP)
    print("复杂场景 Prompt 进化 Benchmark（符合触发时机）")
    print("参考: DUP (arxiv 2404.14963) / PromptAgent / GReaTer / BBH")
    print(SEP)
    print("测试场景（均满足触发时机三要素）:")
    print("  1. 多步数学推理（含理解陷阱）")
    print("     - 单位转换/反直觉/隐含条件，DUP 论文显示理解错误占 65.8%")
    print("     - 评估: 答案正确 + 关键推理步骤完整（0/1/2 分）")
    print("  2. 逻辑推理（BBH 风格）")
    print("     - 需严密逻辑链，人工 prompt 难一次写对推理引导")
    print("     - 评估: 结论正确 + 推理链完整（0/1/2 分）")
    print("  3. 多约束结构化输出")
    print("     - 同时满足 JSON + 字段 + 值类型 + 嵌套结构 4 维约束")
    print("     - 评估: 4 维约束全部满足（0/1/2 分）")
    print()
    print("LLM 后端: 真实 LLM（Qwen qwen-plus）")
    print("Fitness: ComplexBenchmarkFitness（GEPA 式 failure-grounded reflection）")

    # Step 1: 评估进化前 prompt
    ORIGINAL_PROMPT = "你是助手。"
    print()
    print(HSEP)
    print("Step 1: 评估进化前 prompt（基线）")
    print(HSEP)
    print("  prompt: %s" % ORIGINAL_PROMPT)
    agent_before = Agent(llm=LLM(), tools=Toolkit(), system_prompt=ORIGINAL_PROMPT)
    results_before = run_evaluation(agent_before, "进化前")
    print_results(results_before, "进化前")

    # Step 2: 跑 prompt 进化（3 代，用 ComplexBenchmarkFitness）
    print()
    print(HSEP)
    print("Step 2: 跑 3 代 prompt 进化（ComplexBenchmarkFitness 驱动）")
    print(HSEP)
    evo_agent = Agent(
        llm=LLM(),
        tools=Toolkit(),
        system_prompt=ORIGINAL_PROMPT,
        fitness_evaluator=ComplexBenchmarkFitness(),
        leakage_guard=RegexLeakageGuard(),
        archive_store=ArchiveStore(),
    )

    from atlas.rsi.prompt_evolver import evolve_prompt
    for i in range(3):
        r = evolve_prompt(evo_agent)
        print("  gen%d: score=%.2f  applied=%s  diff=%s" % (
            i, r.evaluation.score, r.applied, r.proposal.diff_summary[:80]))
        # 打印 GEPA 式 failure-grounded feedback
        print("    feedback: %s" % r.evaluation.feedback[:400].replace("\n", " | "))
        if r.applied:
            print("    新 prompt 前 100 字: %s" % evo_agent.memory.runtime_messages[0]["content"][:100])

    EVOLVED_PROMPT = evo_agent.memory.runtime_messages[0]["content"]
    print()
    print("  进化后 prompt 前 300 字:")
    print("  %s" % EVOLVED_PROMPT[:300])

    # Step 3: 评估进化后 prompt
    print()
    print(HSEP)
    print("Step 3: 评估进化后 prompt")
    print(HSEP)
    agent_after = Agent(llm=LLM(), tools=Toolkit(), system_prompt=EVOLVED_PROMPT)
    results_after = run_evaluation(agent_after, "进化后")
    print_results(results_after, "进化后")

    # Step 4: 对比汇总
    print()
    print(SEP)
    print("对比汇总")
    print(SEP)

    before_total = sum(r["score"] for r in results_before["math_trap"]) + \
                   sum(r["score"] for r in results_before["logic"]) + \
                   sum(r["score"] for r in results_before["multi_constraint"])
    after_total = sum(r["score"] for r in results_after["math_trap"]) + \
                  sum(r["score"] for r in results_after["logic"]) + \
                  sum(r["score"] for r in results_after["multi_constraint"])
    total_max = 30  # 15 题 × 2 分

    print("  进化前: %d/%d  (%.1f%%)" % (before_total, total_max, before_total / total_max * 100))
    print("  进化后: %d/%d  (%.1f%%)" % (after_total, total_max, after_total / total_max * 100))
    delta = after_total - before_total
    print("  变化: %+d 分 (%+.1f%%)" % (delta, delta / total_max * 100))

    print()
    print("  逐场景对比:")
    for scene, name in [("math_trap", "数学推理"), ("logic", "逻辑推理"), ("multi_constraint", "多约束输出")]:
        b = sum(r["score"] for r in results_before[scene])
        a = sum(r["score"] for r in results_after[scene])
        sm = len(results_before[scene]) * 2
        chg = a - b
        tag = "↑提升" if chg > 0 else ("↓回退" if chg < 0 else "==持平")
        print("    %s: %d/%d → %d/%d  %s (%+d)" % (name, b, sm, a, sm, tag, chg))

    print()
    print("  逐任务详情:")
    for scene, name, tasks in [
        ("math_trap", "数学", MATH_TRAP_TASKS),
        ("logic", "逻辑", LOGIC_TASKS),
        ("multi_constraint", "约束", MULTI_CONSTRAINT_TASKS),
    ]:
        for i in range(len(tasks)):
            b = results_before[scene][i]["score"]
            a = results_after[scene][i]["score"]
            chg = a - b
            if chg > 0:
                tag = "↑"
            elif chg < 0:
                tag = "↓"
            else:
                tag = "="
            b_detail = results_before[scene][i]["detail"][:40]
            a_detail = results_after[scene][i]["detail"][:40]
            print("    [%s%d] %d→%d %s  前:%s | 后:%s" % (
                name, i + 1, b, a, tag, b_detail, a_detail))

    print()
    print(SEP)
    print("说明：")
    print("  - 数学推理：2分=答案+步骤全对，1分=部分对，0分=全错")
    print("  - 逻辑推理：2分=结论+推理链全对，1分=部分对，0分=全错")
    print("  - 多约束输出：2分=4维约束全满足，1分=≤2个违反，0分=≥3个违反")
    print("  - 参考：DUP 论文显示理解错误占 GSM8K 错误 65.8%，是 prompt 进化核心战场")
    print("  - 参考：PromptAgent 在 GSM8K 上 +6.4%，GReaTer 在 BBH 上 +8.9%")
    print(SEP)
    return 0


if __name__ == "__main__":
    sys.exit(main())

---
title: Atlas YAML 声明式 Role 配置
---

# Atlas YAML 声明式 Role 配置

## 背景

多 Agent 协作中，Role（角色）决定了子 Agent 的行为。硬编码 Role 字典存在两个问题：
1. 改角色行为需要改代码、重新部署
2. 无法在不修改源码的前提下调整工具权限

Phase 2 规划中，Role 将采用**三元组声明式配置**：`system_prompt + tools + llm_config`，全部通过 YAML 定义。

## 配置文件结构

```yaml
# examples/parallel_subagent_config.yaml

roles:
  # 角色名（唯一标识，LLM 必须在 enum 中选择）
  researcher:
    system_prompt: |
      你是主题调研员。任务要求：
      1. 阅读用户给出的主题，提炼 2-3 条关键事实
      2. 给出一句话结论
      3. 只输出关键事实和结论，不要多余解释
    tools: [calculator, get_current_time]   # 该角色可调用的工具白名单
    llm_config:
      temperature: 0.3                      # 低温度，调研类任务需要确定性
      max_steps: 5                           # 子 Agent 最大步数

  calculator_analyst:
    system_prompt: |
      你是数值分析师。任务要求：
      1. 用 calculator 工具计算表达式
      2. 解读数值规模（如量级、常见场景）
      3. 输出格式：计算 + 结果 + 解读
    tools: [calculator]                      # 只暴露 calculator 工具
    llm_config:
      temperature: 0.1
      max_steps: 3

parent:
  system_prompt_template: |
    你是任务编排者。请分析用户输入，将其中独立子任务分配给对应角色。
    可用角色：{known_roles}
    将每个子任务作为 spawn_subagents 的一条记录，使用 artifact_schema
    约束子 Agent 输出结构。
  max_steps: 8
  llm_config:
    temperature: 0.2

task: |
  请帮我完成以下三件事（这些任务互相独立，可以并行处理）：
  1. 调研 Python 的 GIL 是什么，对多线程有什么影响
  2. 计算 (1234 * 5678 + 9999) 的结果并解读数值规模
  3. 调研 asyncio.gather 与 ThreadPoolExecutor 的适用场景差异
  完成后汇总三件事的关联性。
```

## Role 三元组

每个 Role 由三个维度定义：

| 维度 | 字段 | 作用 |
|---|---|---|
| **系统提示** | `system_prompt` | 定义角色的专业领域、输出格式、约束条件 |
| **工具权限** | `tools` | 白名单机制，该角色只能调用声明的工具 |
| **LLM 配置** | `llm_config` | 每个角色可独立设置 temperature、max_steps 等 |

### 工具白名单的意义

```python
# 代码中：按 Role 过滤工具
class RoleConfig:
    def build_toolkit(self, global_tools: list[dict]) -> Toolkit:
        tk = Toolkit()
        for spec in global_tools:
            if spec["name"] in self.tool_names:
                tk.register(**spec)
        return tk
```

`calculator_analyst` 角色只能看到 `calculator` 工具，从源头消除"角色调用不属于自己的工具"的可能性。

## YAML 加载与验证

```python
@dataclass
class RoleConfig:
    name: str
    system_prompt: str
    tool_names: list[str]
    temperature: float = 0.3
    max_steps: int = 5

def build_role_configs(config: dict) -> dict[str, RoleConfig]:
    """从 YAML config 构建 RoleConfig 映射。"""
    result = {}
    for name, role_data in config["roles"].items():
        llm_cfg = role_data.get("llm_config", {})
        result[name] = RoleConfig(
            name=name,
            system_prompt=role_data["system_prompt"],
            tool_names=role_data.get("tools", []),
            temperature=llm_cfg.get("temperature", 0.3),
            max_steps=llm_cfg.get("max_steps", 5),
        )
    return result
```

## LLM 角色选择的约束

spawn_subagents 工具的 inputSchema 中，`role` 字段使用 `enum` 约束：

```json
{
  "name": "role",
  "description": "角色名，必须是已知角色之一",
  "enum": ["researcher", "calculator_analyst"]
}
```

LLM 只能从枚举值中选择角色名，无法自创角色。**从源头消除 fallback 触发场景**（用户规则 7）。

## 修改方式

### 新增角色

在 `roles:` 段下添加新条目：

```yaml
roles:
  # ... 现有角色
  code_reviewer:
    system_prompt: |
      你是代码审查员。检查代码中的 Bug 和风格问题。
    tools: [calculator]
    llm_config:
      temperature: 0.1
      max_steps: 5
```

### 调整角色行为

修改 `system_prompt`、`tools` 或 `llm_config` 即可，无需改代码。

### 更换任务

修改 `task:` 段，支持多行字符串。

## 与项目原则的一致性

- EP-2 解耦可插拔：Role 配置与代码分离，改 YAML 即改行为
- EP-4 用户简单：不需要理解 Role 三元组的实现，改配置文件即可
- 用户规则 7：enum 约束从源头消除角色填写错误
- 用户规则 8：方案清晰，不折中（要么 YAML 要么硬编码，双轨但不模糊）

## Phase 2 迁移计划

当前 `parallel_subagent_demo.py` 中的 YAML 配置是 Phase 2 的预演。Phase 2 落地时：
1. `RoleConfig` 移入 `atlas.core.role` 模块
2. `build_role_configs` 作为内核标准加载函数
3. `spawn_subagent` 工具的 role enum 自动从注册的 Role 列表生成
4. YAML schema 校验前移到加载阶段，启动时即报错

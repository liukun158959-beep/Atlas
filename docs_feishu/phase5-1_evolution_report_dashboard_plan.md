# Phase 5.1 实施计划：进化报告导出 + Dashboard 进化面板

> grill-me 对齐后冻结。2026-08-24 定稿。
>
> 本轮范围：只做 5.1 两件。5.2（train/val / Safety / `atlas ab_test` / 编码与多轮 benchmark）不做。
> 不做 CLI `atlas evolve` / `atlas evolve-report`。不做 Chart.js CDN。

---

## 0. 决策速查表（冻结）

| # | 决策点 | 最终选择 |
|---|--------|---------|
| Q1 | 本轮范围 | 只做 5.1：进化报告导出 + Dashboard 进化面板 |
| Q2 | 报告触发 | 库函数 `export_evolution_report(path, summary, initial_prompt)`。调用方传路径。不做 CLI。不在 `evolve_prompt_until_converged` 里自动写文件 |
| Q3 | 报告数据源 | 只看本次 `EvolutionSummary.generations`。`initial_prompt` 必传。不传 `ArchiveStore` |
| Q4 | Dashboard 布局 | 顶部切页：运行 / 进化。运行页保持原三面板 + RESILIENCE。进化页：fitness 曲线 + 每代 diff + 回滚按钮 |
| Q5 | 回滚机制 | 同进程：`register_evolution(agent)` 挂活 Agent；`publish_evolution(agent, summary)` 推快照；`POST /rsi/rollback`。未注册 → 409 + 恢复指引 |
| Q6 | 曲线实现 | 原生 SVG，不引 Chart.js（避免 CDN 失败再造第二套） |
| Q7 | Demo | 报告写入 `prompt_converge_rollback_demo.py`（Part 1 结束后写临时 md + 断言）。Dashboard 新建 `examples/prompt_evolution_dashboard_demo.py` |

缺必填直接失败并给出恢复指引。不静默跳过、不默认路径、不换路继续。

---

## 1. 文件变更清单

### 1.1 新建

| 文件 | 说明 |
|------|------|
| `atlas/rsi/evolution_report.py` | `unified_diff_text` + `export_evolution_report` |
| `examples/prompt_evolution_dashboard_demo.py` | FakeLLM 进化 + 同进程看板 + urllib 自测回滚 POST |
| `docs_feishu/phase5-1_evolution_report_dashboard_plan.md` | 本计划 |

### 1.2 修改

| 文件 | 修改内容 |
|------|---------|
| `atlas/rsi/__init__.py` | 导出 `export_evolution_report` |
| `examples/prompt_converge_rollback_demo.py` | Part 1 结束后写报告；第 ⑨ 项断言 |
| `atlas/cli/dashboard.py` | DATASTATE.evolution；`register_evolution` / `publish_evolution`；切页 HTML；POST `/rsi/rollback`；`publish()` 不覆盖 evolution |
| `README.md` | 5.1 两项勾选 + 能力摘要 |

不改 `evolve_prompt` / `evolve_prompt_until_converged` 签名。不改 `ArchiveStore`。

---

## 2. 报告导出

签名：

```python
def export_evolution_report(
    path: str | Path,
    summary: EvolutionSummary,
    initial_prompt: str,
) -> Path:
```

失败条件（均带恢复指引）：

- `path` 空
- `summary` 不是 `EvolutionSummary`
- `summary.generations` 为空（恢复：先跑 `evolve_prompt_until_converged`）
- `initial_prompt` 不是非空 `str`
- 父目录不存在（恢复：先创建目录）
- 写入失败按 OS 异常上抛

Markdown 结构：

1. 元信息：停止原因 / 最终代数 / 最终分数
2. 初始 Prompt（代码块）
3. 每一次提案（按 `generations` 顺序）：applied、score、feedback、leakage、reasoning、diff_summary、current→proposed unified diff
4. 最终 Prompt：最后一次 `applied=True` 的 `proposed_value`；若全程未 apply，最终 = 初始（这是事实，不是兜底默认）
5. 前后对比：初始 vs 最终 unified diff

编码 UTF-8。返回写成的 `Path`。

---

## 3. Dashboard 进化面板

### 3.1 数据面

`DATASTATE` 新增 `evolution`（默认 `None`）。

- `publish()` 只更新 runs/events/trace/resilience，**不覆盖** `evolution`
- `publish_evolution(agent, summary)` 只更新 `evolution` + `last_publish_ts`
- `register_evolution(agent)` 把活 Agent 放到模块全局；`agent is None` 失败

`evolution` 快照字段：

- `current_generation` / `current_prompt`
- `stop_reason` / `final_generation` / `final_score`
- `can_rollback`（已 register 为 true）
- `generations[]`：`attempt`、`generation`、`applied`、`score`、`feedback`、`diff_summary`、`reasoning`、`unified_diff`、`leakage_passed`

`summary.generations` 为空 → `publish_evolution` 失败。

### 3.2 回滚 HTTP

`POST /rsi/rollback`，JSON `{"generation": <int>}`。

- 缺字段 / 非 int → 400 + 恢复指引
- 未 `register_evolution` → 409 +「进化与 Dashboard 放同一进程，先 register_evolution」
- 成功：`agent.rollback_to(N)`，刷新 `evolution` 的 live 字段，200 `{"ok": true, "generation": ..., "current_prompt": ...}`
- `rollback_to` 自身的 ValueError/RuntimeError → 409，正文带原错误

前端按钮对每个可回滚目标（0 + 所有 `applied` 的 generation）发 POST。未 `can_rollback` 时按钮禁用并显示恢复指引。

### 3.3 前端

- 顶栏切页「运行 / 进化」
- 进化页三列：SVG fitness 曲线（attempt vs score，apply 绿 / 未 apply 红）+ 每代卡片（展开 unified diff）+ 当前 prompt + 回滚按钮
- 单 HTML 字符串内嵌，原生 JS，不引 npm / Chart.js

---

## 4. Demo 与验证

### 4.1 报告

`python examples/prompt_converge_rollback_demo.py --plain`

Part 1 结束后 `export_evolution_report` 写到临时目录。第 ⑨ 项：文件存在、UTF-8、含「初始 Prompt」「最终 Prompt」「停止原因」。原 8 项断言不改语义。

### 4.2 Dashboard

`python examples/prompt_evolution_dashboard_demo.py --plain`

流程：后台 `run_server` → Fake 进化若干代 → `register_evolution` + `publish_evolution` → urllib `POST /rsi/rollback` 断言 generation 回到 0 → 打印 URL，短驻留后退出。

浏览器（若本机可开）：打开进化页，确认曲线、diff、回滚按钮。

### 4.3 回归

原 `python examples/dashboard_demo.py` 仍只走运行页；`evolution` 保持 `None`。
`python examples/prompt_converge_rollback_demo.py --plain` 原 8 项 + 新 ⑨。

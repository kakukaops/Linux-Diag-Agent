# M23 — Eval Feedback Loop（评测反馈环）设计文档

> **⚠️ 设计规格文档**：本文档为 v2 实施前的设计规格（2026-05-20）。

| 字段 | 值 |
|------|---|
| 模块编号 | M23（v2 新增）|
| 状态 | Design Draft |
| 关联文档 | [../PRD.md](../PRD.md) · [../Architecture.md](../Architecture.md) · [M8](../../v1/modules/M8_evaluation_observability.md) |
| 关联 ADR | [ADR-024](../adr/ADR-024-insufficient-evidence-exit.md) |
| 最后更新 | 2026-05-20 |

---

## 1. 目标与边界

### 1.1 目标

回答两个产品命题：

1. **"agent 输出对不对？"** —— 不知道对错就没法迭代。M23 提供自动质量度量 + 人工 review 工具。
2. **"知识图谱召回准不准？"** —— v2 数据壁垒的真壁垒（[AgentThink §5.5](../../AgentThink.md)）；M23 周度跑 Recall@10 看趋势。

### 1.2 关键决策

| # | 决策 |
|---|------|
| D1 | 复用 v1 `eval/` 框架，扩展为周度自动跑批 |
| D2 | 真实案例数据集 ≥30 例（v2.0 末 15 例 → v2.2 末 30 例）|
| D3 | 双裁判 + tiebreak（继承 v1 评测协议）|
| D4 | 自动指标 + 人工指标分开度量：自动跑 Recall@10，人工 review 根因正确性 |
| D5 | 工具调用轨迹（`agent_tool_trace`）参与 eval（评估"工具调用效率"）|

### 1.3 在范围

- 真实案例数据集 schema + 采集协议
- 自动 Recall@10 跑批 + CI
- meta-eval：把"agent 输出对不对"做成定期 review
- 自动质量曲线 dashboard
- 工具调用轨迹 audit UI（CLI + 简易 web）
- `evidence_strength_score` 权重再校准（[ADR-024](../adr/ADR-024-insufficient-evidence-exit.md)）

### 1.4 不在范围

- 自动诊断质量打分 LLM-as-judge（v3）
- A/B 测试框架（先用人工对比 v1/v2 报告即可）
- 用户行为分析（v3）

---

## 2. 整体架构

```
┌──────────────── M23 Eval Feedback Loop ────────────────┐
│                                                         │
│  数据源                                                  │
│   ├── eval_cases (PG)            ← 真实案例数据集        │
│   ├── eval_results (PG)          ← 自动跑批结果          │
│   ├── eval_human_judgments (PG)  ← 人工 review 结果      │
│   └── agent_tool_trace (PG)      ← ReAct 轨迹           │
│                                                         │
│  组件                                                    │
│   ├── runner.py            ← 周度自动跑批（CI）          │
│   ├── recall_calculator.py ← Recall@10 计算             │
│   ├── meta_judge.py        ← 人工 review CLI            │
│   ├── dashboard/           ← 质量曲线（grafana 或简易 web）│
│   └── audit_ui/            ← 工具调用轨迹审计 UI         │
│                                                         │
│  输出                                                    │
│   ├── 周报 Markdown                                     │
│   ├── 月度评审：30 例完整人工评测                          │
│   └── evidence_score 权重 retrain（季度）                │
└─────────────────────────────────────────────────────────┘
```

---

## 3. 真实案例数据集

### 3.0 PG 表汇总（v2 新增）

M23 涉及 3 张表，2 张新建 + 1 张扩展：

```sql
-- 扩展 v1 eval_cases
ALTER TABLE eval_cases ADD COLUMN v2_fields JSONB;
-- 内容 schema 见 §3.1

-- 新建:人工评测结果（M-5 修复:之前文档只 reference 此表名,本节首次给出 DDL）
CREATE TABLE eval_human_judgments (
    id                BIGSERIAL PRIMARY KEY,
    case_id           TEXT NOT NULL REFERENCES eval_cases(case_id),
    diagnosis_id      UUID NOT NULL,              -- 关联 agent_tool_trace
    judge             TEXT NOT NULL,              -- 评测者 email
    judged_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    root_cause_correct        TEXT,               -- 'y' | 'n' | 'partial'
    evidence_score            INTEGER CHECK (evidence_score BETWEEN 1 AND 10),
    fix_recommendation_score  INTEGER CHECK (fix_recommendation_score BETWEEN 1 AND 10),
    ie_call_assessment        TEXT,               -- 'correct' | 'missed' | 'false_positive' | 'n/a'
    tool_efficiency_score     INTEGER CHECK (tool_efficiency_score BETWEEN 1 AND 10),
    notes             TEXT,
    consensus_tier    INTEGER                     -- 1 = 单标 / 2 = 双标 / 3 = tiebreak 后
);

CREATE INDEX idx_ehj_case ON eval_human_judgments (case_id, judged_at DESC);
CREATE INDEX idx_ehj_judge ON eval_human_judgments (judge, judged_at);

-- 新建:影子模式对比（v2 灰度上线用,详见 V2_v1_modules_supplements.md §4.5）
CREATE TABLE shadow_comparison (
    id              BIGSERIAL PRIMARY KEY,
    diagnosis_id    UUID NOT NULL,
    v1_report       JSONB NOT NULL,
    v2_report       JSONB NOT NULL,
    recall_v1       REAL,
    recall_v2       REAL,
    verdict_v1      TEXT,
    verdict_v2      TEXT,
    diff_summary    TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_sc_created ON shadow_comparison (created_at DESC);
```

### 3.1 数据集 schema（扩展 v1 `eval_cases`）

```sql
ALTER TABLE eval_cases ADD COLUMN v2_fields JSONB;
-- v2_fields 内容:
{
  "fault_kind": "panic | hardlockup | oom | ...",
  "route_expected": "kernel | kernel+vmcore | hardware | change | unknown",
  "is_insufficient_evidence_case": false,  // 该案例是不是"应该 say IDK"的
  "ground_truth": {
    "fix_commits": ["abc123...", "def456..."],
    "related_bugs": [12345, 67890],
    "related_cves": ["CVE-2026-..."],
    "root_cause_summary": "OOM triggered by cgroup memory.high throttle bug...",
    "kernel_version_affected": ["OLK-6.6", "OLK-5.10"]
  },
  "input_artifacts": {
    "dmesg_path": "data/eval/cases/case_001/dmesg.txt",
    "sosreport_path": "data/eval/cases/case_001/sosreport.tar.xz",
    "vmcore_path": null,
    "natural_language_question": "..."
  },
  "hardware_signals": ["taint_M", "mce"],   // 用于 hw 路由测试
  "source": "production_incident | syzbot_reproducer | synthetic",
  "annotation_date": "2026-05-20",
  "annotator": "alice@example.com"
}
```

### 3.2 数据集采集协议

**v2.0 目标 ≥15 例**（T-021），分布要求：

| 类别 | 数量 | 来源 |
|------|------|------|
| OOM | 3-4 | 生产 OOM kill log（去敏感后）|
| Lockup (soft + hard) | 3 | 生产 lockup 案例 |
| Panic | 2-3 | 生产 vmcore 案例 |
| Hardware 误诊 | ≥3 | 历史上被误指 kernel 的 hw 故障（[ADR-023](../adr/ADR-023-hardware-first-sop-routing.md)）|
| Hardware 反例 | ≥2 | dmesg 有 hw signal 但实际是软件 bug（防 hw 路由过激）|
| 证据不足 | ≥2 | 故意残缺的输入（验证 insufficient_evidence 出口，[ADR-024](../adr/ADR-024-insufficient-evidence-exit.md)）|

**v2.2 目标 ≥30 例**：扩展上述各类 + 新增 "升级回归" 类 + "容器 OOM"。

**ground truth 标注**：

- `fix_commits`：由 SRE / 内核维护者确认（不能凭 BM25 排第一就标）
- `is_insufficient_evidence_case`：标注者主观判断"这案例是不是该 say IDK"
- 双标注 + 不一致时讨论

### 3.3 数据隐私

- 生产 dmesg / sosreport 去敏感（hostname / IP / 用户名替换）
- 标注前过 PII 检测脚本
- 数据集仓库与代码仓库分离（不混到 git）

---

## 4. 自动 Recall@10 跑批

### 4.1 Runner（扩展 v1 `eval/runner.py`）

```python
# eval/runner_v2.py
def run_batch(dataset: str, output_dir: str) -> RunSummary:
    cases = load_v2_cases(dataset)
    for case in cases:
        # 跑完整 diagnose（含 ReAct）
        result = diagnose(case.input_artifacts.dmesg or case.natural_language_question)

        # 计算 Recall@10:在 top-10 evidence 中命中多少 ground_truth commits/bugs
        recall = compute_recall_at_10(result.evidence, case.ground_truth)

        # 路由正确性
        route_correct = (result.route == case.route_expected)

        # Insufficient-evidence 出口正确性
        ie_correct = (result.verdict == "insufficient_evidence") == case.is_insufficient_evidence_case

        # 工具调用效率(ReAct 迭代数 / 工具调用数)
        tool_efficiency = compute_tool_efficiency(result.tool_call_trace)

        save_result(case.id, recall, route_correct, ie_correct, tool_efficiency)

    return summarize(output_dir)
```

### 4.2 CI 集成

```yaml
# .github/workflows/eval_weekly.yml(或 GitLab CI)
on:
  schedule:
    - cron: '0 6 * * MON'  # 每周一 06:00 UTC
jobs:
  eval:
    runs-on: self-hosted
    steps:
      - run: python -m eval.runner_v2 --dataset eval/data/v2_real_cases.json --output eval/results_v2/$(date +%Y-%m-%d)
      - run: python -m eval.weekly_report > eval/reports/$(date +%Y-%m-%d).md
      - uses: actions/upload-artifact@v3
        with: { name: eval-report, path: eval/reports/ }
```

### 4.3 周报输出

```markdown
# v2 Eval Report — 2026-06-10

## 关键指标（与上周对比）

| 指标 | 本周 | 上周 | Δ |
|------|------|------|---|
| Recall@10 平均 | 67% | 65% | +2pp |
| Recall@10 中位数 | 70% | 68% | +2pp |
| 路由正确率 | 87% | 85% | +2pp |
| Insufficient-evidence 出口召回 | 80% | 75% | +5pp |
| ReAct 平均迭代数 | 6.2 | 6.8 | -0.6 |
| ReAct max_iter 触发率 | 8% | 11% | -3pp |
| Hardware 误诊率 | 4% | 5% | -1pp |

## 趋势图（最近 4 周）
![chart](charts/recall_at_10_4w.png)

## 退化案例
- case-042: Recall 80% → 35%(本周)
  - 原因:LangGraph prebuilt 升级导致 ToolCall 序列化 bug
  - 行动:已 pin 版本,跟踪 https://github.com/...
```

---

## 5. Meta-Eval（人工 review）

### 5.1 工具：`meta_judge.py` CLI

```bash
diag-agent meta-eval review \
  --since 2026-05-15 \
  --sample 5            # 随机抽 5 例最近的诊断
```

进入交互式 review：

```
Case ID: case-067 (run 2026-05-20 14:33)
Question: "OOM kill of java process in OLK-6.6 with cgroup limit 4GB"
v2 Report:
  Root cause: ...
  Evidence:
    - commit 038ff4e1... (mm/vmscan: wake up flushers...)
    - LKML message <abc@...>
  Tool calls: 7 (search_commits, get_commit_diff, check_backport_status, ...)

Judge:
  [r] Root cause correct? (y/n/partial): _
  [e] Evidence traceability? (1-10): _
  [f] Fix recommendation? (1-10): _
  [c] Insufficient_evidence call (if applicable)?  (correct/missed/false_positive): _
  [t] Tool efficiency (too many redundant calls?): _

Notes: _
```

输出落 `eval_human_judgments` 表。

### 5.2 月度 30 例评测

继承 v1 协议（双裁判 + tiebreak）；v2 加：

- 评估路由正确性
- 评估"工具调用效率"
- 评估 insufficient_evidence 出口适用性

---

## 6. evidence_strength_score 权重 retrain

[ADR-024](../adr/ADR-024-insufficient-evidence-exit.md) 规定的校准流程在本模块实现：

```python
# eval/calibrate_evidence_score.py
def retrain(min_samples: int = 30):
    # 1. 从 eval_human_judgments 取标注:
    #    X: case 的 evidence features (has_vmcore, has_full_dmesg, ...)
    #    y: human judge "is_insufficient_evidence_case" (True/False)
    cases = load_annotated_cases()
    if len(cases) < min_samples:
        return {"status": "insufficient_data", "have": len(cases), "need": min_samples}

    # 2. logistic regression
    model = sklearn.linear_model.LogisticRegression()
    X, y = build_features(cases), [c.is_insufficient for c in cases]
    model.fit(X, y)

    # 3. 输出新权重 + 阈值
    new_weights = dict(zip(FEATURE_NAMES, model.coef_[0]))
    new_threshold = compute_threshold_from_pr_curve(model, X, y, target_precision=0.8)

    # 4. 写 configs/evidence_score.yaml + 触发 ADR-024 增订
    write_config(new_weights, new_threshold)
    return {"status": "ok", "weights": new_weights, "threshold": new_threshold}
```

季度触发 + 数据量阈值（≥30 例）。新权重必须 PR review 才能 merge。

---

## 7. Audit UI

### 7.1 CLI 形式

```bash
# 列最近诊断
diag-agent trace list --since 2026-05-19

# 查看一次诊断的完整工具调用轨迹
diag-agent trace view <diagnosis_id>

# 输出:
# Step 1 (14:33:01, +0.0s): triage_classify
#   → route="kernel+vmcore"
# Step 2 (14:33:02, +1.2s): search_commits(keywords=["oom", "vmscan"], kernel_version="OLK-6.6")
#   → 8 results
# Step 3 (14:33:04, +2.5s): check_backport_status(upstream="038ff4e1...", version="OLK-6.6")
#   → {"backported": True, ...}
# ...
```

### 7.2 简易 web 形式（可选,v2.2 末）

FastAPI + 静态 HTML，列 + 详情两个页面。不做花哨 UI（这是审计工具不是产品）。

---

## 8. 与其他模块的集成

| 模块 | 集成方式 |
|------|---------|
| M22 ReAct Loop | 工具轨迹 → `agent_tool_trace` 表 → M23 dashboard |
| M7 generate_report | report json 中的 evidence + verdict → eval_results |
| Triage `classify_fault_and_route` | route 选择 → eval 路由正确性指标 |
| ADR-024 evidence_strength_score | 校准流程在 M23 实现 |

---

## 9. 风险与缓解

| 风险 | 缓解 |
|------|------|
| 数据集采集慢（生产案例难拿）| v2.0 仅要 15 例,可以来自 syzbot reproducer + 历史 incident 报告 |
| 标注质量参差 | 双标注 + tiebreak;季度 review 标注一致性 |
| 自动 Recall@10 与人工评分背离 | 双指标都看;Recall 仅是检索质量信号,人工评分是终极标准 |
| CI 跑批失败影响日常 dev | eval_weekly.yml 失败不阻塞 main 分支;只发周报警告 |
| evidence_score 数据不足无法 retrain | 用初始权重至 ≥30 例标注;允许手动调权重作为过渡 |
| 工具调用轨迹数据量爆炸 | `agent_tool_trace` 90 天保留;大对象不入表 |

---

## 10. 实施任务对照（[ProjectPlan](../ProjectPlan.md)）

| Task ID | 内容 | PD | 阶段 |
|---------|------|----|----|
| T-021 | 真实案例数据集 v2.0（15 例）| 10 | v2.0（M22-M15）|
| T-204 | 自动 Recall@10 跑批 + CI | 4 | v2.2 |
| T-205 | 质量曲线 dashboard | 3 | v2.2 |
| T-206 | meta-eval 框架（裁判 review CLI） | 4 | v2.2 |
| T-207 | 数据集扩展到 30 例 | 5 | v2.2 |
| T-209 | 工具调用轨迹 audit UI（CLI + 简易 web）| 5 | v2.2 |

合计 v2.0 10 PD + v2.2 21 PD = 31 PD（M15 数据集 + M20 反馈环）。

---

*参考：[Architecture.md](../Architecture.md) · [PRD.md](../PRD.md) · [ADR-024](../adr/ADR-024-insufficient-evidence-exit.md)*

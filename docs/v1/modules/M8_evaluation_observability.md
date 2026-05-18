# M8 — 评测与可观测性 设计文档

| 字段 | 值 |
|------|---|
| 模块编号 | M8 |
| 状态 | Design Locked（待实施） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [M1-M7](.) · [KnowledgeGraph_Overview](../KnowledgeGraph_Overview.md) |
| 关联 ADR | [ADR-004](../adr/ADR-004-claude-code-provider.md) · [ADR-015](../adr/ADR-015-m7-diagnosis-strategy.md) · [ADR-016](../adr/ADR-016-m8-minimal-observability.md) |
| 最后更新 | 2026-05-14 |

---

## 1. 目标与边界

### 1.1 目标

为 v1 提供**最小化的评测体系 + 文件级可观测性**：
- 30 例公开案例 + 人工评测，验证 v1.0/v1.2 验收门槛
- 各模块结构化日志（JSON Lines）+ 文件 metrics，覆盖关键指标
- 评测报告自动生成，便于复盘
- Smoke 测试（mock LLM）保证 CI 不退化

### 1.2 关键决策（[ADR-016](../adr/ADR-016-m8-minimal-observability.md)）

| # | 决策 |
|---|------|
| D1 | v1.0 **不部署 Prometheus / Grafana / Jaeger**，仅用 **文件日志 + JSON metrics** |
| D2 | 30 例评测样本 **静态锁定 + git 版本化**（tag `v1.0-eval-dataset`） |
| D3 | 评测节奏 = **仅 v1.0/v1.2 交付时各跑一次**，**不做周度 / 月度回归** |
| D4 | Smoke 测试保留（CI 跑 mock LLM，零成本）|
| D5 | OpenTelemetry tracing 写本地 jsonl，不接 Jaeger backend |
| D6 | Prometheus + Grafana 推迟到 **v1.1+ 评估**，触发条件见 ADR-016 |

### 1.3 在范围

| 子能力 | 形态 |
|--------|------|
| 30 例评测数据集（静态 git tracked）| 数据 + PG `eval_cases` 表 |
| 人工评测 rubric（PRD 锁定 5 维度）| 文档 + CLI 裁判工具 |
| 评测批量执行脚本（`eval/runner.py`）| 跑 N 例 + 收集 |
| 2 裁判 + tiebreak 流程 | 评测协议 + CLI |
| 文件结构化日志（JSON Lines） | 各模块写 `data/logs/<date>/<module>.jsonl` |
| 文件 metrics（counter / gauge 写 JSON）| `data/metrics/<module>.json` |
| OTEL span jsonl（本地写）| `data/traces/<trace_id>.jsonl` |
| 评测报告自动生成（Markdown）| `data/eval/runs/<date>/REPORT.md` |
| Smoke 测试套件（mock LLM）| CI 友好 |

### 1.4 不在范围（v1.0）

| 项 | 处理位置 |
|----|---------|
| Prometheus / Grafana 部署 | v1.1+（[ADR-016](../adr/ADR-016-m8-minimal-observability.md) 留触发条件）|
| Jaeger / Tempo backend | v1.1+ |
| 周度 / 月度回归测试 | v1.3+ 评估 |
| LLM-as-judge 自动评测 | 永不做 |
| 实时用户反馈循环 | v2+ |
| 告警接 Slack / 钉钉 | v1.1+（v1.0 仅日志 + 邮件）|

## 2. 30 例评测数据集

### 2.1 样本结构（PG `eval_cases`，schema 见 M2）

每例文件结构：

```
data/eval/cases/case-XXX/
├── manifest.json                # 元数据（fault_domain, kernel_version, source, ...）
├── inputs/
│   ├── dmesg.txt                # 必有
│   ├── journalctl.json          # 可选
│   ├── sosreport.tar.xz         # 大文件，gitignored；附 .url 指向原始下载位置
│   └── description.txt          # 自然语言描述
└── ground_truth.json            # 期望根因 + 修复 commit + expected_evidence_refs
```

### 2.2 v1.0 样本分布（PRD 锁定）

| 来源 | 数量 | 备注 |
|------|------|------|
| syzbot 已 Fixed | ≥ 15 | 主源；含 C reproducer / kernel config |
| LKML 已修复 incident | ≥ 10 | 完整讨论 + 修复 commit 已 land |
| bugzilla.kernel.org RESOLVED-FIXED | ≥ 5 | 含 see_also commit 引用 |
| **总计** | **30** | 8 个故障域均衡覆盖（每类 ≥ 2 例）|

### 2.3 静态锁定 + git 版本化（[ADR-016](../adr/ADR-016-m8-minimal-observability.md)）

```
1. 2 名裁判筹备阶段（v1.0 准备期）：
   - 从 M3 ingest 进来的数据中各自筛 30 候选
   - 共审 → 共识 30 例
   - 写 ground_truth.json（含 expected_evidence_refs）
2. 文件化：
   - data/eval/cases/case-001 .. case-030/
   - 大文件（sosreport）gitignored + 留 .url 指向 fixture 仓库
   - 其他全 git tracked
3. 锁定：
   - git commit + tag v1.0-eval-dataset
   - eval_cases 表 INSERT，locked_at 设为 tag 日期
4. 变更协议：
   - 任何样本改动 = 新 tag（v1.0-eval-dataset-v2）
   - 历次评测结果保留对应 dataset 版本，便于对比
```

## 3. 人工评测 rubric

### 3.1 5 维度（PRD 锁定）

| 维度 | 取值 | 权重 | 备注 |
|------|------|------|------|
| **A. 根因结论正确性** | 0 / 0.5 / 1 | 0.4 | 1 = 与 ground_truth 完全一致；0.5 = 方向对但欠精确；0 = 错 |
| **B. 证据可溯率** | 0-100% | 硬约束 | 必须 100%（每条 claim 都能在 evidence 中验证）；不达 = 整例 fail |
| **C. 排查建议可执行性** | 0 / 0.5 / 1 | 0.2 | 命令准确、顺序合理 |
| **D. 修复建议合理性** | 0 / 0.5 / 1 | 0.2 | 短/中/长期建议对症 |
| **E. 整体可读性 / 可解释性** | 1-10 分 | 0.2 | 报告结构、推理链可读 |

**综合分**：`A*0.4 + C*0.2 + D*0.2 + (E/10)*0.2`，max 1.0

### 3.2 v1.2 验收门槛

| 项 | 阈值 |
|----|------|
| B（证据可溯率 100% 的例数）| 30/30 |
| A=1（完全正确）占比 | ≥ 50% |
| 综合分均值 | ≥ 0.6 |
| 可读性均值 E | ≥ 7/10 |
| Speculative claims 占比 | ≤ 10% |

### 3.3 裁判 CLI 工具

```bash
diag-agent eval judge \
  --run-id <run_id> \
  --case-id case-001 \
  --judge-name "alice@kernel-team" \
  --interactive
```

Interactive 流程：
1. 显示 case 上下文（脱敏 ground_truth）
2. 加载 agent 生成的 `report.md`
3. 逐项问：
   - A 根因正确？(0/0.5/1)
   - B 抽 3 条 claim 验证证据 ref（pass/fail，全部 pass 才记 100%）
   - C 排查建议（0/0.5/1）
   - D 修复建议（0/0.5/1）
   - E 可读性（1-10）
4. 收集后写入 `eval_results` 表

### 3.4 2 裁判 + tiebreak

```
1. 每例 2 名裁判独立打分（盲审，互不看对方）
2. 5 维度比较：
   - 全部一致 → 共识值
   - 某维度差异 ≥ 0.5 → 进入 tiebreak
3. Tiebreak：
   - 第 3 名裁判（高级 kernel 工程师）独立打分
   - 该维度取 3 人中位数
4. 综合分 = 加权 5 维平均
5. 所有打分 + 备注落 PG eval_results
```

## 4. 评测批量执行

```bash
diag-agent eval run \
  --dataset v1.0-eval-dataset \
  --agent-version v1.0 \
  --llm-provider claude_code \
  --llm-model claude-sonnet-4-x \
  --self-consistency-k 3 \              # 评测期开 K=3
  --output-dir data/eval/runs/2026-05-14/
```

内部：
1. 读 30 个 case
2. 对每例：构造 manifest → 调 M7 DiagnosisSession → 写 report.md + report.json + trace.jsonl
3. 写 eval_results 行（status='pending_judge'）
4. 输出运行摘要 + 自动指标

**Pro 配额预算**（K=3）：每例 ~12 messages × 30 = **360 messages → 跨 8 个 5h 窗口（约 2-3 天）**。

## 5. 文件日志 + Metrics（替代 Prometheus）

### 5.1 结构化日志

各模块写 JSON Lines 到 `data/logs/<YYYYMMDD>/<module>.jsonl`：

```jsonl
{"ts":"2026-05-14T12:00:00Z","level":"INFO","module":"m1.provider","trace_id":"abc","event":"chat_request","provider":"claude_code","model":"claude-sonnet-4-x","input_tokens":4523}
{"ts":"2026-05-14T12:00:02Z","level":"INFO","module":"m1.provider","trace_id":"abc","event":"chat_response","duration_ms":2340,"output_tokens":312}
{"ts":"2026-05-14T12:00:03Z","level":"INFO","module":"m5.retrieval","trace_id":"abc","event":"retrieve_start","query":"...","routes":["code","docs","lkml",...]}
```

字段约定：
- `ts`：ISO 8601 UTC
- `level`：DEBUG / INFO / WARN / ERROR
- `module`：模块.子模块，便于 grep
- `trace_id`：贯穿全链（M7 创建，所有下游继承）
- `event`：事件类型
- 其他字段按事件类型自由扩展

### 5.2 文件 metrics

每个模块进程定期（默认每 60s）把 counter / gauge 写到 `data/metrics/<module>.json`：

```json
{
  "ts": "2026-05-14T12:00:00Z",
  "module": "m7.diagnosis",
  "counters": {
    "diagnosis_runs_total": 23,
    "diagnosis_success_total": 19,
    "diagnosis_failed_total": 4,
    "invalid_claims_total": 12,
    "regenerate_attempts_total": 18,
    "speculative_claims_total": 3
  },
  "gauges": {
    "llm_rate_budget_remaining": 28
  },
  "histograms": {
    "diagnosis_duration_seconds": {
      "p50": 32, "p95": 78, "p99": 145,
      "count": 23
    }
  }
}
```

### 5.3 查询工具

```bash
# 日志查询
diag-agent logs query --module m7.diagnosis --since 1h --trace-id abc
diag-agent logs query --event chat_request --since 24h --module m1.provider

# Metrics 快照
diag-agent metrics show --module m7.diagnosis --field diagnosis_runs_total

# 历史趋势
diag-agent metrics trend --module m1.provider --field llm_rate_budget_remaining --since 7d
```

实现：CLI 直接 grep / jq 后端文件，无需服务化。

### 5.4 何时升级到 Prometheus

[ADR-016](../adr/ADR-016-m8-minimal-observability.md) 留触发条件：
- 文件 grep 耗时 > 10s（数据量大到不便）
- 需要多人协同看 dashboard
- 进入 v1.3 生产化阶段

满足时 v1.1+ 部署 Prometheus + Grafana。

## 6. OpenTelemetry Tracing（本地 jsonl）

LangGraph 自带 OTEL；v1.0 写本地：

```
data/traces/<trace_id>.jsonl
```

每行一个 span，含 `span_id` / `parent_span_id` / `operation` / `duration_ms` / `attributes`。

调试：
```bash
diag-agent trace view <trace_id>      # 渲染 ASCII tree
diag-agent trace export <trace_id>    # 导出 Jaeger 兼容 JSON（v1.1 接 backend 时用）
```

v1.1 可一键将历史 traces 批量导入 Jaeger，无数据丢失。

## 7. Smoke 测试（CI 友好）

### 7.1 范围

5 个固定 fixture（每个故障域 ≥ 1），CI 用 **mock LLM**（Ollama 本地小模型或纯 mock 响应）跑：

```
tests/smoke/fixtures/
├── oom_simple.json
├── lockup_simple.json
├── panic_simple.json
├── deadlock_simple.json
└── generic_unknown.json
```

### 7.2 CI 集成

`.github/workflows/smoke.yml` (PR / push 触发)：

```yaml
- name: Smoke test
  env:
    LLM_BACKEND: ollama
    LLM_MODEL: qwen2.5:7b
  run: |
    diag-agent test smoke
    # 验证 5 例都 OK，duration < 60s/例
```

**关键约束**：smoke 不消耗真实 LLM 配额（mock 或 Ollama），CI 可频繁跑。

### 7.3 Smoke 的验证范围

- 进程正常启动 + 退出
- report.md / report.json 字段齐全
- 关键 metrics 在合理范围
- 不验证 root cause 正确性（mock 不可信）

## 8. 评测报告自动生成

### 8.1 单次评测报告（`data/eval/runs/<date>/REPORT.md`）

跑完 30 例 + 裁判完成后自动生成：

```markdown
# Eval Report — 2026-05-14

## 总览
- Dataset: v1.0-eval-dataset (30 cases)
- Agent: v1.0, Claude Sonnet 4.x, K=3
- Total duration: 18h 23m（跨 4 个 5h 窗口）

## 综合指标
| 指标 | 值 | v1.2 验收门槛 | 是否达标 |
|------|---|-------------|---------|
| 根因正确率（A=1 占比）| 18/30 = 60% | ≥ 50% | ✅ |
| 证据可溯率（B = 100% 例数）| 30/30 | 100% | ✅ |
| 综合分均值 | 0.68 | ≥ 0.6 | ✅ |
| 可读性均值 | 7.4/10 | ≥ 7 | ✅ |
| Speculative 占比 | 4/30 | ≤ 10% | ✅ |

## 按故障域分组
| Fault Domain | N | 正确率 | 综合分 |
|--------------|---|------|------|
| OOM | 6 | 5/6 (83%) | 0.81 |
| ...

## 失败例分析
- case-007 (panic in net): 漏召回关键 commit；建议 v1.1 加 MAINTAINERS 解析
- ...

## 资源消耗
- Total LLM messages: 360
- Total retrieval calls: 204
- Pro budget windows used: 8 × 5h = 40h
- ...
```

### 8.2 历次评测对比（v1.0 → v1.2 → v1.3）

跑过 ≥ 2 次后，生成对比表：

| 指标 | v1.0 eval (2026-05) | v1.2 eval (2026-08) | Delta |
|------|---------|---------|-------|
| 根因正确率 | 60% | 73% | +13% |
| ...

## 9. 文件结构

```
eval/
├── __init__.py
├── runner.py                        # 批量跑 N 例
├── judge_cli.py                     # 裁判 interactive
├── tiebreak.py                      # tiebreak 流程
├── report_generator.py
├── rubric.py                        # 5 维度 schema
├── log_query.py                     # logs query CLI
├── metrics_show.py                  # metrics show CLI
├── trace_view.py                    # trace view CLI
├── fixtures/                        # smoke 测试 mock
│   ├── oom_simple.json
│   ├── lockup_simple.json
│   ├── panic_simple.json
│   ├── deadlock_simple.json
│   └── generic_unknown.json
└── tests/

data/eval/
├── cases/                           # v1.0-eval-dataset (git tracked)
│   ├── case-001/
│   │   ├── manifest.json
│   │   ├── inputs/
│   │   └── ground_truth.json
│   └── ...
├── runs/
│   ├── 2026-05-14/
│   │   ├── REPORT.md
│   │   ├── metrics.json
│   │   └── case-001/
│   │       ├── report.md
│   │       ├── report.json
│   │       └── trace.jsonl
│   └── ...

data/
├── logs/<YYYYMMDD>/<module>.jsonl   # 结构化日志
├── metrics/<module>.json            # 文件 metrics
└── traces/<trace_id>.jsonl          # OTEL spans

.github/workflows/
└── smoke.yml                        # CI mock 跑 5 例
```

## 10. 测试策略

| 层 | 测什么 |
|----|--------|
| 单元 | rubric schema、tiebreak 算法、log/metrics 读写 |
| 集成 | mock LLM 跑 1 例完整流程，校验 report.md + json |
| Smoke | CI 跑 5 fixture（zero LLM cost） |
| 验收 | v1.0 跑 30 例 / v1.2 跑 30 例（含人工裁判）|

## 11. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| 文件日志膨胀 | data/logs 目录大小 | 月度清理 30 天前；gzip 归档 |
| 文件 metrics 不实时 | 60s 写一次 | 影响小（v1.0 不需要实时 dashboard） |
| 评测 30 例 Pro 配额超支 | 评测前预算检查 | 分多天跑 + 跨 5h 窗口 |
| 裁判分歧高 | tiebreak 触发率 | 第 3 裁判介入；记录 rubric 含糊点；下次评测 prompt 调优 |
| ground_truth 误差 | v1.2 评测时复审 | 必要时更新 dataset 到 v2 |

## 12. v1.0 → v1.3 演进（[ADR-016](../adr/ADR-016-m8-minimal-observability.md)）

| 阶段 | 主要变化 |
|------|---------|
| v1.0 | 文件日志 + JSON metrics + 本地 traces；smoke CI；交付时跑 30 例评测 |
| v1.1 | 评估是否部署 Prometheus + Grafana（满足 ADR-016 触发条件时）|
| v1.2 | 30 例完整人工评测 + 验收门槛；评测报告自动生成 |
| v1.3 | A/B 框架（Claude vs vLLM 对照 30 例）；OTEL backend 评估 Jaeger / Tempo；样本扩到 60 例 |

## 13. 与外部组件的契约

- **M1-M7**：所有模块按 §5.1 日志格式输出 JSONL；按 §5.2 写文件 metrics
- **M2 存储**：`eval_cases` / `eval_results` 表；评测产物在 `data/eval/`
- **M3 Ingestion**：候选样本筛选时配合 `eval/runner.py --candidate-selection`
- **M7 诊断 Agent**：评测时 `--self-consistency-k 3`；trace_id 写 `data/traces/<trace_id>.jsonl`

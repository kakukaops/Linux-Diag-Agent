# ADR-016 — M8 v1.0 最小化：文件日志 + 一次性评测，无 Prometheus / 无回归

| 字段 | 值 |
|------|---|
| 状态 | Accepted |
| 日期 | 2026-05-14 |
| 决策者 | 用户 + Architect |
| 关联模块 | M8 |
| 相关 ADR | [ADR-004](ADR-004-claude-code-provider.md) · [ADR-015](ADR-015-m7-diagnosis-strategy.md) |

## 上下文

M8 评测与可观测性模块原计划部署 Prometheus + Grafana + Jaeger 全栈，并设定周度 / 月度回归。但 v1.0 MVP 资源紧张：

- Pro 5h 配额受限（[ADR-004](ADR-004-claude-code-provider.md)），月度跑 30 例真实评测会持续吃配额
- v1.0 团队精力优先在核心诊断能力（M1-M7），ops 复杂度需要压缩
- 文件日志 + grep 在 MVP 阶段足以支撑调试与单次评测分析
- 后续若进入生产化（v1.3+）再升级 Prometheus / Grafana，迁移成本低

## 决策

**v1.0 M8 范围最小化**：

| # | 决策 |
|---|------|
| D1 | **不部署 Prometheus / Grafana / Jaeger**；用**文件日志 (JSONL) + JSON metrics** 替代 |
| D2 | 30 例评测样本 **静态锁定 + git 版本化**（tag `v1.0-eval-dataset`） |
| D3 | 评测节奏 = **仅 v1.0/v1.2 交付时各跑一次**；**不做周度 / 月度回归** |
| D4 | Smoke 测试保留（CI 跑 mock LLM 或 Ollama，零真实成本） |
| D5 | OpenTelemetry tracing 写**本地 jsonl**，不接 backend；v1.1 评估是否接 Jaeger / Tempo |
| D6 | 不接 Slack / 钉钉告警；v1.0 严重错误仅写日志 + 邮件 |

## 影响

### 工作量大幅缩减

| 子模块 | 原估算 | 调整后 |
|--------|--------|-------|
| Prometheus + Grafana 部署 + dashboard | ~300 | **0** |
| Jaeger / Tempo 集成 | ~200 | **0** |
| 周度 / 月度 cron + 回归脚本 | ~200 | **0** |
| 文件日志 + metrics CLI | 0 | ~250 |
| 30 例 eval runner + judge CLI + tiebreak | ~400 | ~400 |
| Smoke 测试 fixture + CI | ~150 | ~150 |
| **M8 总计** | **~1250** | **~800** |

v1.0 累计自研 ~9000 + 800 = **~9800 行 Python**。

### v1.0 操作模式

- **调试**：grep `data/logs/<date>/<module>.jsonl`；按 trace_id 串联
- **指标查询**：`diag-agent metrics show --module <m> --field <f>`
- **trace 可视化**：`diag-agent trace view <trace_id>`（ASCII tree）
- **告警**：严重错误立即写日志，邮件每日聚合（cron）

### 评测周期

- **v1.0 交付时**：手工触发一次 30 例评测，2-3 天跑完（含 2 裁判 + tiebreak）
- **v1.0 → v1.2 间**：不做回归；遇到问题时手工 grep 日志
- **v1.2 交付时**：再跑一次 30 例评测，对比 v1.0 结果
- **v1.3+**：评估是否引入回归（同 Prometheus 触发条件）

### 失去的能力（与代价）

| 能力 | 代价 |
|------|------|
| 实时 dashboard | 需要 ad-hoc 跑 metrics_show；不能多人协作看实时图表 |
| 历史趋势可视化 | 仅有 JSON 文件，需 jq + 自写脚本 |
| 跨 trace 关联查询 | 仅 grep；复杂查询慢 |
| 自动告警 | 严重错误依赖人工查看邮件 |
| 持续回归 | 退化可能在 v1.0 → v1.2 间不被发现 |

这些代价在 MVP 阶段可接受。

## 备选方案与拒绝理由

| 备选 | 拒绝理由 |
|------|---------|
| **D1 v1.0 全部署 Prometheus + Grafana + Jaeger** | Ops 复杂度对 MVP 过重；额外 3 个 service 维护；v1.0 团队精力应优先核心模块 |
| **D1 v1.0 部署 Prometheus，Jaeger 留 v1.1** | 仍引入一个 service；与 D1 比仅省 Jaeger 不显著 |
| **D3 周度 5 例 smoke + 月度 30 例完整** | 周度 5 例若用真实 LLM 持续吃 Pro 配额；CI mock 已能覆盖大部分回归需求 |
| **D3 仅周度回归，不做月度** | 周度成本高且回归发现的问题需要月度完整跑才能定性 |
| **D4 砍 smoke 测试** | smoke 用 mock 零成本，且能 catch CI 级回归；保留无伤害 |

## v1.1+ 升级触发条件

满足以下任一时，启动 Prometheus + Grafana 部署：

1. 文件 grep 调试耗时 > 10s（数据量大到不便）
2. 多人协同分析需求（≥ 3 人同时调试）
3. 进入 v1.3 生产化阶段
4. 评测样本扩到 ≥ 100 例（单次评测覆盖范围广，需要更细 dashboard）
5. 告警需求出现（如想接 Slack / 钉钉）

满足时 v1.1 实施 ~3-5 PD：
- 部署 Prometheus + Grafana docker-compose
- 各模块 metrics endpoint 改造（从文件 → HTTP `/metrics`）
- 6 个核心 dashboard（System / LLM / Retrieval / Diagnosis / Ingestion / Storage）

## 升级触发条件 — Jaeger / Tempo backend

满足以下任一时，接入 trace backend：

1. trace 跨 process（v1.3 切本地 vLLM 时）
2. trace 查询频次 ≥ 10 次/天（grep jsonl 不便）
3. 需要保留 ≥ 30 天历史 trace（文件归档管理复杂）

## 监控指标（v1.0 简化版）

虽然不部署 Prometheus，关键指标仍要在文件 metrics 中暴露，便于人工查看：

```json
{
  "module": "m1.provider",
  "counters": {
    "llm_request_total": 234,
    "llm_local_cache_hit_total": 89,
    "llm_errors_total": 3
  },
  "gauges": {
    "llm_rate_budget_remaining": 28
  }
}
```

```json
{
  "module": "m7.diagnosis",
  "counters": {
    "diagnosis_runs_total": 30,
    "diagnosis_success_total": 26,
    "speculative_claims_total": 4,
    "regenerate_attempts_total": 18
  }
}
```

## 参考

- M8 模块设计：[M8_evaluation_observability.md](../modules/M8_evaluation_observability.md)
- [ADR-004 claude_code provider + Pro 订阅](ADR-004-claude-code-provider.md)
- [ADR-015 M7 诊断 Agent 核心策略](ADR-015-m7-diagnosis-strategy.md)

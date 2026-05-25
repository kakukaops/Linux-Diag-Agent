# Linux-Diag-Agent v1 — 项目计划

| 字段 | 值 |
|------|---|
| 文档类型 | Project Plan |
| 版本 | v1 |
| 状态 | Design Locked（待实施，2026-05-15 同步全部 ADR）|
| 关联文档 | [PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [Architecture.md](Architecture.md) · [adr/](adr/) |
| 起始 | M0 = 项目立项后第 1 个月 |
| 总周期 | 12 个月（M0 → M12） |
| 最后更新 | 2026-05-15 |

---

## 1. 里程碑分解

### 1.1 v1.0 — 知识库地基 + 检索 CLI (M0-M3)

| 里程碑 | 月份 | 关键交付 |
|--------|------|---------|
| **M0**：立项 + 前置文档 + 跨项目协调 | M0 | PRD / Architecture / ProjectPlan 评审；仓库脚手架；CI 雏形；**与 codesearch 团队协调 HTTP transport（[ADR-012](adr/ADR-012-codesearch-http-transport.md)）+ 双 repo 索引（[ADR-009](adr/ADR-009-multi-version-via-codesearch-repos.md)）** |
| **M1**：基础设施 + LLM Provider | M1 | PG + Neo4j docker-compose；M1 LLM Provider 抽象层（claude_code + ollama + openai_compat 三 backend）；M2 schema + 迁移 |
| **M2**：Ingestion 主线（4 个网络源 + git）| M2 | M3 ingestion：lkml + bugzilla.kernel.org + syzbot + nvd + zenodo + kernel_commit 全部跑通；周度 cron 雏形 |
| **M3**：M4 + M5 + 检索 CLI | M3 | M4 CodeGraph HTTP client + 健康检查；M4 Cross-Graph Linker 雏形（trailer 抽取 + commit-bug 关联）；M5 7 路 always-fire-all 召回 + LLM 重排；CLI `diag-agent search` 跑通 |

**v1.0 验收**：CLI 输入"v5.10 上 order=4 的 normal zone OOM 怎么诊断？"，输出 top-10 相关源码 + LKML + bug + 文档节文；5 类典型问题 × 5 例样本 Recall@10 ≥ 70%。

### 1.2 v1.1 — Cross-Graph Linker 加深 + M6 主机工具 (M4-M6)

| 里程碑 | 月份 | 关键交付 |
|--------|------|---------|
| **M4**：Cross-Graph 完整 | M4 | patch-commit 反查（git patch-id + subject 兜底）；NVD-Bug CVE 桥接；subsystem 推断；Neo4j 周度 rebuild + 对账 |
| **M5**：M6 主机工具 | M5 | mcp-dmesg-journal（oops/OOM/lockup/lockdep 抽取）+ mcp-sosreport（解包 + SystemSummary）|
| **M6**：CodeGraph 集成稳定化 + 优化 | M6 | CodeGraph healthcheck 完善；MCP 调用 metrics；PageIndex 走查 prompt 微调；BM25 调参 |

**v1.1 验收**：从 30 例公开案例抽 10 例做检索验证，top-5 命中真实修复 commit ≥ 70%；CodeGraph HTTP MCP 稳定无故障。

### 1.3 v1.2 — 诊断 Agent + 评测 (M7-M9)

| 里程碑 | 月份 | 关键交付 |
|--------|------|---------|
| **M7**：M7 Triage + Diagnosis 框架 | M7 | LangGraph 双层 graph；OOM SOP yaml；Claim-Evidence Binding（Reject + Regenerate ≤2）|
| **M8**：3 SOP 全集 + 报告生成 | M8 | lockup + panic SOP；Markdown + JSON 双格式输出；CLI `diag-agent diagnose` 全功能 |
| **M9**：30 例完整人工评测 | M9 | M8 30 例数据集筹备 + 锁定；2 裁判 + tiebreak；评测报告生成 |

**v1.2 验收**（[M8 §3.2](modules/M8_evaluation_observability.md)）：30 例案例根因正确率 ≥ 50%（A=1 占比）；证据可溯率 100%；综合分均值 ≥ 0.6；可读性 ≥ 7/10。

### 1.4 v1.3 — 生产化 + 离线化 + SOP 扩展 (M10-M12)

| 里程碑 | 月份 | 关键交付 |
|--------|------|---------|
| **M10**：vLLM 本地部署 + A/B 评测 | M10 | 单机 / 集群 vLLM 起服；同 30 例评测 Claude vs vLLM 对照 |
| **M11**：SOP 扩到 8 + LoRA（可选） | M11 | + deadlock + I/O hang + network + perf + sched SOP；commit msg + LKML + kernel-doc LoRA 数据集 |
| **M12**：反馈循环 + 复盘 + 评估升级 | M12 | 用户反馈数据回流；v1 完整复盘文档；**评估 Prometheus / Jaeger 升级触发条件（[ADR-016](adr/ADR-016-m8-minimal-observability.md)）** |

**v1.3 验收**：30 例评测两 backend 正确率差距 < 10%；反馈数据 ≥ 50 条标注；8 类 SOP 全集。

---

## 2. 任务 WBS + 工时估算

### 2.1 估算口径

- 单位：人日（PD），1 名"熟悉 Linux + Python + LLM"工程师全职
- 含开发 + 自测 + 模块文档；不含集中评测
- ±30% 浮动正常

### 2.2 v1.0 (M0-M3) 任务清单

| WBS | 任务 | PD | 关键依赖 |
|-----|------|----|---------|
| **M0** | | | |
| 0.1 | PRD / Architecture / ProjectPlan / 16 ADR 评审定稿 | 3 | — |
| 0.2 | 仓库脚手架 + Python 项目结构 + CI (lint / unit test) | 3 | 0.1 |
| 0.3 | **与 codesearch 团队协调 HTTP transport PR**（[ADR-012](adr/ADR-012-codesearch-http-transport.md)）| 3 | — |
| 0.4 | **codesearch olk-kernel-v6.6 + olk-kernel-v5.10 双 repo 索引**（[ADR-009](adr/ADR-009-multi-version-via-codesearch-repos.md)） | 5 | 0.3 |
| **M1** | | | |
| 1.1 | `configs/default.yaml` schema + 加载器 | 1 | 0.2 |
| 1.2 | PostgreSQL + Neo4j docker-compose（**无 FAISS / 无 TEI**）| 2 | 0.2 |
| 1.3 | M2 Alembic schema migrations（14 表）| 4 | 1.2 |
| 1.4 | M1 LLM Provider 抽象层 Protocol + ChatRequest/Response schema | 4 | 1.1 |
| 1.5 | M1 `claude_code` provider（subprocess 包 `claude -p` + OpenAI Chat ↔ Anthropic Messages 翻译）| 8 | 1.4 |
| 1.6 | M1 `openai_compat` + `ollama` provider | 3 | 1.4 |
| 1.7 | M1 PG 缓存层 + rate limit + Pro budget 追踪 | 4 | 1.5 |
| **M2** | | | |
| 2.1 | M3 Ingester 基类 + RunReport + checkpoint 框架 | 3 | 1.3 |
| 2.2 | M3 lkml ingester（lore mbox 下载 + 解析 + 线程 DAG） | 8 | 2.1 |
| 2.3 | M3 LKML 长线程 LLM 摘要 pipeline | 3 | 2.2, 1.5 |
| 2.4 | M3 bugzilla.kernel.org ingester（**仅 kernel.org**，[ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md)）| 4 | 2.1 |
| 2.5 | M3 syzbot HTML scraper | 5 | 2.1 |
| 2.6 | M3 nvd CVE feed ingester | 3 | 2.1 |
| 2.7 | M3 zenodo 数据集一次性导入 | 2 | 2.1 |
| 2.8 | M3 kernel_commit ETL（**双仓库** git log + trailer 抽取 + **OLK inclusion 解析** + patch_id 计算，[ADR-018](adr/ADR-018-commit-source-olk-kernel.md)）| 9 | 2.1 |
| 2.9 | M3 `scripts/weekly_sync.sh` + cron + 日志/告警 | 3 | 2.2-2.8 |
| **M3** | | | |
| 3.1 | M4 CodeGraph HTTP MCP client（含健康检查 + repo 映射）| 5 | 0.4, 1.4 |
| 3.2 | M4 Cross-Graph Linker 雏形：trailer 抽取 + commit-bug 链接 + olk↔upstream 桥接（ADR-018）| 6 | 2.8 |
| 3.3 | M4 NVD-Bug CVE 桥接 | 2 | 2.6, 3.2 |
| 3.4 | M4 subsystem 推断（file_path → subsystem）| 2 | 3.2 |
| 3.5 | M4 PG 实时写 link_* 表（无 Neo4j 同步，v1.1 加） | 2 | 3.2 |
| 3.6 | M5 query parser（全 LLM 解析） | 3 | 1.5 |
| 3.7 | M5 7 路 always-fire-all 召回（code/docs/lkml/bug/syzbot/commit/cve）| 8 | 3.1 |
| 3.8 | M5 PageIndex 走查高阶封装 + 低阶 passthrough | 4 | 3.1 |
| 3.9 | M5 LLM rerank（候选 > 10 时触发） | 3 | 1.5 |
| 3.10 | M5 RetrievalQuery / Evidence schema + 归一化 | 3 | 3.7 |
| 3.11 | CLI `diag-agent search` 子命令 + 文件日志 + JSON metrics | 4 | 3.6-3.10 |
| 3.12 | v1.0 验收评测（5 类问题 × 5 例 = 25 例） | 3 | 3.11 |
| **v1.0 小计** | | **~119 PD** | |

### 2.3 v1.1 (M4-M6) 任务清单

| WBS | 任务 | PD |
|-----|------|----|
| 4.1 | M4 patch-commit 反查（git patch-id + subject 兜底）| 8 |
| 4.2 | M4 commit-message 链接（Link: trailer + Message-ID 解析）| 3 |
| 4.3 | M4 Neo4j 周度全量 rebuild | 5 |
| 4.4 | M4 Neo4j ↔ PG 对账脚本 + drift alert | 3 |
| 4.5 | M6 mcp-dmesg-journal（oops/OOM/lockup/lockdep 全集抽取）| 12 |
| 4.6 | M6 mcp-sosreport（解包 + SystemSummary + kernel_version 映射）| 8 |
| 4.7 | M6 CLI `--upload` 多文件支持 + 上传 manifest | 3 |
| 4.8 | M6 fixture 准备（各类 oops 样本 ≥ 10）| 5 |
| 4.9 | M5 PageIndex 走查 prompt 微调 + few-shot | 3 |
| 4.10 | M5 7 路 routing 评测 + 资源占用分析 | 3 |
| 4.11 | M4 CodeGraph healthcheck 完善 + 降级模式 | 3 |
| 4.12 | v1.1 验收（30 例抽 10 例检索）| 3 |
| **v1.1 小计** | | **~60 PD** |

### 2.4 v1.2 (M7-M9) 任务清单

| WBS | 任务 | PD |
|-----|------|----|
| 7.1 | M7 LangGraph TriageState + 节点实现 | 8 |
| 7.2 | M7 LangGraph DiagnosisState + 节点实现 | 10 |
| 7.3 | M7 SOP yaml schema + registry + generic_diagnosis fallback | 4 |
| 7.4 | M7 OOM SOP yaml + fixture（≥ 2 例）| 4 |
| 7.5 | M7 lockup SOP yaml + fixture | 4 |
| 7.6 | M7 panic SOP yaml + fixture | 4 |
| 7.7 | M7 Hypothesis generation + verification prompts | 5 |
| 7.8 | M7 Self-Consistency K=3 实现 + 投票 | 3 |
| 7.9 | M7 Claim-Evidence Binding（claim parser + ref validator + regenerate）| 6 |
| 7.10 | M7 报告生成（Markdown 模板 + JSON schema）| 5 |
| 7.11 | M7 CLI `diag-agent diagnose` 子命令完整化 | 3 |
| 7.12 | M7 LangGraph checkpointing 落 PG | 3 |
| 7.13 | M8 30 例数据集筹备（2 裁判共审 + 文件化 + git tag）| 10 |
| 7.14 | M8 `eval/runner.py` 批量跑脚本 | 4 |
| 7.15 | M8 `eval/judge_cli.py` interactive 工具 | 5 |
| 7.16 | M8 tiebreak 流程 + 评测报告生成 | 4 |
| 7.17 | M8 完整 30 例评测执行（含 2 裁判 + 可能的 tiebreak）| 15 |
| 7.18 | v1.2 验收 + bug fix 迭代 | 5 |
| **v1.2 小计** | | **~102 PD** |

### 2.5 v1.3 (M10-M12) 任务清单

| WBS | 任务 | PD |
|-----|------|----|
| 10.1 | vLLM 部署（4-8×A100/H100）+ 模型下载 | 4 |
| 10.2 | M1 vLLM provider 实跑测试 | 3 |
| 10.3 | A/B 30 例评测（Claude vs vLLM 对照）| 8 |
| 11.1 | M7 deadlock SOP + fixture | 3 |
| 11.2 | M7 I/O hang SOP + fixture | 3 |
| 11.3 | M7 network SOP + fixture | 4 |
| 11.4 | M7 perf regression SOP + fixture | 4 |
| 11.5 | M7 sched anomaly SOP + fixture | 3 |
| 11.6 | LoRA 微调数据集准备（可选）| 8 |
| 11.7 | LoRA 微调实跑 + 评测（可选）| 6 |
| 11.8 | Ingestion pipeline 异常处理 + 自愈 | 5 |
| 12.1 | 用户反馈循环（CLI 标注接口 + 数据回流）| 5 |
| 12.2 | **评估升级 Prometheus / Grafana / Jaeger**（[ADR-016](adr/ADR-016-m8-minimal-observability.md)） | 3-8 |
| 12.3 | v1 完整复盘文档 | 4 |
| **v1.3 小计** | | **~60-65 PD** |

### 2.6 全 v1 总览

| 阶段 | PD | 月数（单人 ~20 PD/月）|
|------|----|---------|
| v1.0 | ~115 | 5-6 月（单人）/ 3 月（双人）/ 2 月（三人）|
| v1.1 | ~60 | 3 月（单人）/ 1.5 月（双人）|
| v1.2 | ~102 | 5 月（单人）/ 2.5 月（双人）|
| v1.3 | ~60-65 | 3 月（单人）/ 1.5 月（双人）|
| **总计** | **~340 PD** | 12 月单人 / 7 月双人 / 4-5 月三人 |

---

## 3. 依赖关系图

```
                          M0 (前置文档 + codesearch 协调)
                                  │
                       ┌──────────┼──────────┐
                       ▼          ▼          ▼
                  Provider 抽象  PG/Neo4j   codesearch 双 repo
                                  │              │
                       ┌──────────┴──────────┐   │
                       ▼                     ▼   ▼
                  Ingester 框架        CodeGraph MCP client (M4)
                       │                     │
              ┌────────┼────────┐            │
              ▼        ▼        ▼            │
            lkml   bugzilla   syzbot         │
              │        │        │            │
              └────────┼────────┘            │
                       ▼                     │
                  kernel_commit ETL          │
                       │                     │
                       └────┬────────────────┘
                            ▼
                  Cross-Graph Linker (M4 雏形)
                            │
                            ▼
                  M5 7 路召回 + LLM 重排
                            │
                            ▼
                  CLI search (v1.0 验收)
                            │
                            ▼  (v1.1)
              Cross-Graph 加深 + M6 主机工具
                            │
                            ▼  (v1.2)
                  M7 LangGraph + 3 SOP
                            │
                            ▼
                  M8 30 例评测
                            │
                            ▼  (v1.3)
                  vLLM + 8 SOP + 反馈
```

**关键 path**：M0 codesearch 协调 → Provider → Ingester → CrossGraph → M5 检索 → M7 Agent → 评测。

**可并行段**：M2 期间 lkml / bugzilla / syzbot / nvd 四路 ingester 可并行；v1.1 M4/M5/M6 三人分摊。

---

## 4. 风险与缓解

| ID | 风险 | 等级 | 缓解 |
|----|------|------|------|
| R1 | **codesearch 团队 HTTP transport PR 延期** | 高 | M0 阶段提前推进，预留 1 个月 buffer；如卡死可临时用 stdio subprocess |
| R2 | **codesearch OLK kernel v5.10 索引未就绪** | 中 | 与 codesearch 团队对齐时间表；codesearch 已 v6.6 ready，v5.10 是增量 |
| R3 | LKML 5 年数据抓取慢、断点续传难 | 中 | [ADR-010](adr/ADR-010-lkml-2y-bootstrap.md) 锁定 2 年起步；并发拉取 + 断点 checkpoint |
| R4 | Claude Pro 配额（45 msg/5h）成为评测瓶颈 | 高 | [ADR-013](adr/ADR-013-m5-retrieval-strategy.md)/[ADR-015](adr/ADR-015-m7-diagnosis-strategy.md) 已预算化；评测分 2-3 天跑；budget < 10 强制降级 |
| R5 | patch-commit 反查命中率低（< 70%）| 中 | git patch-id 主 + subject 兜底；评测后调时间窗 |
| R6 | LangGraph 与 OpenAI Chat schema 不完全兼容 | 中 | M7 测试早；如冲突可降到自写状态机 |
| R7 | Claim-Evidence Binding 重生成耗配额 | 中 | [ADR-015](adr/ADR-015-m7-diagnosis-strategy.md) 限 2 次；超限标 speculative |
| R8 | 30 例评测裁判一致性低 | 中 | rubric 公开 + 裁判培训；tiebreak 流程；每例附说明 |
| R9 | CodeGraph HTTP API 行为变化 | 中 | 锁版本；smoke test 监控；跨项目升级流程 |
| R10 | 内核版本演进，知识库过期 | 低 | 每周同步；docs 标注 ingestion 日期 |
| R11 | 文件日志膨胀（无 Prometheus）| 低 | 月度清理 30 天前；gzip 归档 |
| R12 | 仅 1 名开发者，进度延期 | 高 | 总 PD 340 单人 12 月可勉强完成；推荐双人 7 月或三人 5 月 |
| R13 | vmcore 缺失导致深内核 panic 不能诊断 | 中 | [ADR-014](adr/ADR-014-m6-scope.md) 已明示 v1 范围；如评测发现需求高 v1.1 重启 |
| R14 | Red Hat BZ 缺失导致 CVE 关联覆盖率低 | 中 | NVD 补强；[ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md) 留 v1.1+ 接入路径 |

---

## 5. 人力 & 算力需求

### 5.1 人力（推荐双人）

| 角色 | 主要职责 | 投入 |
|------|---------|------|
| 主力工程师（Linux 内核 + Python） | M2 schema、M3 ingester、M4 Linker、M6 主机工具 | 100%（12 月）|
| 第二工程师（LLM / Agent / 数据）| M1 Provider、M5 检索、M7 LangGraph Agent、M8 评测 | 100%（12 月）|
| 内核领域顾问（评测裁判）| v1.2 评测、SOP 评审、v1.0 数据集筹备 | ~15% × 4 月 |
| **codesearch 团队对接**（跨项目）| ADR-012 HTTP transport PR + 双 repo 索引 | M0 集中协调 ~5 PD |

单人配置：总周期保持 12 月，但需要砍 v1.2 SOP 至 2 个、评测从 30 例压到 20 例。

### 5.2 算力 & 存储

| 阶段 | CPU/RAM | GPU | 存储 |
|------|---------|-----|------|
| v1.0-v1.2 开发 | 16C / 64GB | 无（claude_code 走云）| 500 GB SSD |
| v1.3 本地 LLM | 同上 + GPU 节点 | **4-8× A100/H100 80GB** | 1 TB SSD（含模型权重） |

### 5.3 Claude Pro 订阅成本

| 阶段 | 用法 | 月度成本 |
|------|------|---------|
| v1.0-v1.2 开发 + 评测 | $20/月 Claude Pro | **$20/月 × 9 月 = $180** |
| v1.2 评测期可临时升 Max 5x | $100/月（如 30 例评测周期紧）| 可选 |

总 LLM 成本估算：**~$200-500**（含 Max 升级备用）。

### 5.4 知识库存储估算（v1.2 末）

| 项 | 大小 |
|----|------|
| 我方 PostgreSQL | ~40 GB |
| 我方 Neo4j | ~8 GB |
| 我方 数据文件（lkml/bugzilla/syzbot/zenodo/nvd/kernel-git）| ~80 GB |
| CodeGraph（独立维护）| ~40 GB |
| 评测产物 + 日志 + traces | ~10 GB |
| **总计** | **~180 GB**（vs 单 v1.0 估算 ~150GB）|

v1.3 加本地模型权重后 ~330 GB。

---

## 6. 度量与汇报机制

### 6.1 v1.0 极简（[ADR-016](adr/ADR-016-m8-minimal-observability.md)）

- 各模块写 JSON Lines 日志到 `data/logs/<date>/<module>.jsonl`
- 文件 metrics 写 `data/metrics/<module>.json`（60s 一次）
- 本地 OTEL spans 写 `data/traces/<trace_id>.jsonl`
- 查询：`diag-agent logs query / metrics show / trace view` CLI

### 6.2 周报 / 月度评审

- 每周向 stakeholder 报告：本周完成 WBS / 知识库同步状态 / 风险变化
- 每月评审：里程碑完成度 + 下月预排 + ADR 决策记录（新 ADR 或修订）

### 6.3 里程碑评审

每个 M：
- 验收清单逐项过
- 未通过项进 risk register
- stakeholder 决定是否进入下个 milestone

### 6.4 度量指标

| 维度 | 指标 | 来源 | 阈值 |
|------|------|------|------|
| 知识库覆盖 | LKML 入库率 | PG `lkml_message` count | ≥ 95%（2024-05 后）|
| 知识库新鲜度 | 最新邮件距今 | PG `max(sent_date)` | ≤ 8 天 |
| CodeGraph 健康 | healthcheck status | 启动时 + 每小时 | healthy |
| 检索质量（v1.0）| Recall@10 | 5×5 人工评测 | ≥ 70% |
| 检索质量（v1.1）| top-5 命中 commit | 30 例抽 10 | ≥ 70% |
| 诊断质量（v1.2）| 根因正确率 | 30 例人工 | ≥ 50%（A=1 占比）|
| 诊断质量（v1.2）| 证据可溯率 | 自动 + 人工抽 | 100%（硬约束）|
| 综合分（v1.2）| 5 维加权 | 人工 | ≥ 0.6 均值 |
| 性能 | M5 retrieve P95（无 PageIndex）| 文件 metrics | ≤ 1s |
| 性能 | M7 diagnose P95（K=1）| 文件 metrics | ≤ 60s |
| 成本 | Pro 配额 / 诊断 | 文件 metrics | ~4 msg/diagnose 平均 |
| 可靠性 | 每周同步成功率 | `ingest_runs` 表 | ≥ 95% |
| Agent 质量 | speculative claims 占比 | M7 metrics | ≤ 10% |

### 6.5 ADR 记录

每个关键技术决策写一份 ADR 存 `docs/v1/adr/`。v1 当前 16 份（详见 [adr/](adr/) 索引）。

---

## 7. 开始 v1 实施前的最后检查清单

### 7.1 文档 & 决策

- [x] PRD / Architecture / ProjectPlan / KnowledgeGraph_Overview 四份顶层文档定稿
- [x] M1-M8 八份模块文档定稿
- [x] 16 份 ADR 全部 Accepted（ADR-005 Superseded）
- [ ] 评测样本来源初步圈定（syzbot + LKML + bugzilla.kernel.org 候选清单）

### 7.2 跨项目协调（[ADR-012](adr/ADR-012-codesearch-http-transport.md)）

- [ ] codesearch 团队接受 HTTP transport PR 请求
- [ ] CodeGraph HTTP server 模式上线（开发环境验证）
- [ ] codesearch 加 olk-kernel-v6.6 + olk-kernel-v5.10 双 repo 索引
- [ ] codesearch Sphinx JSON kernel-docs-builder 对 v5.10 验证

### 7.3 资源 & 凭据

- [ ] Claude Pro 订阅有效（[ADR-004](adr/ADR-004-claude-code-provider.md)）
- [ ] Claude Code CLI 在开发机已登录
- [ ] 开发硬件就绪（16C / 64GB / 500GB SSD）
- [ ] 2 名内核工程师裁判候选已沟通（v1.2 评测）

### 7.4 工程基础

- [ ] 仓库与 CI 准备（GitHub / GitLab + lint + unit test 工作流）
- [ ] PostgreSQL + Neo4j docker-compose 起跑
- [ ] secrets 管理方式确定（环境变量 / vault）

---

## 8. 附录：常用命令速查

### 8.1 每周同步 cron

```cron
# /etc/cron.d/linux-diag-agent
0 3 * * 0  diag-agent  /opt/linux-diag-agent/scripts/weekly_sync.sh >> /var/log/diag-agent/sync.log 2>&1
```

### 8.2 LLM backend 切换

```bash
# MVP 默认（Claude Pro 订阅）
export DIAG_AGENT_LLM_BACKEND=claude_code

# v1.3 本地 vLLM
export DIAG_AGENT_LLM_BACKEND=vllm
export DIAG_AGENT_VLLM_ENDPOINT=http://gpu-node:8000/v1
```

### 8.3 检索 CLI 示例（v1.0）

```bash
diag-agent search "v5.10 上 order=4 的 normal zone OOM" \
  --kernel-version v5.10 \
  --top-k 10
```

### 8.4 诊断 CLI 示例（v1.2 起）

```bash
diag-agent diagnose \
  --upload dmesg=dmesg.txt \
  --upload sosreport=sosreport.tar.xz \
  --description "kernel panic during boot" \
  --self-consistency-k 1 \
  --output report.md \
  --output-json report.json
```

### 8.5 评测命令

```bash
# v1.0 检索评测
diag-agent eval search --cases 25 --output data/eval/runs/v1.0-search/

# v1.2 完整 30 例
diag-agent eval run --dataset v1.0-eval-dataset --self-consistency-k 3
diag-agent eval judge --run-id <id> --case-id case-001 --judge-name "alice"
```

### 8.6 日志 / 指标查询（[ADR-016](adr/ADR-016-m8-minimal-observability.md)）

```bash
diag-agent logs query --module m7.diagnosis --trace-id <id> --since 1h
diag-agent metrics show --module m1.provider --field llm_rate_budget_remaining
diag-agent trace view <trace_id>
```

# Linux-Diag-Agent v1 — 产品需求文档 (PRD)

| 字段 | 值 |
|------|---|
| 产品名 | Linux-Diag-Agent |
| 版本 | v1 |
| 状态 | Design Locked（待实施，2026-05-15 同步全部 ADR）|
| 关联文档 | [Architecture.md](Architecture.md) · [ProjectPlan.md](ProjectPlan.md) · [Architecture.md](Architecture.md) · [adr/](adr/) |
| 最后更新 | 2026-05-15 |

---

## 1. 背景与目标用户

### 1.1 背景

Linux 内核故障（panic / oops / deadlock / 性能回归 / 内存与 IO 异常）的根因调查通常需要工程师手工跨越多个信息源：内核源码、`MAINTAINERS`、LKML 历史讨论、Bugzilla 报告、syzbot 复现、kernel-doc、以及现场的 dmesg / sosreport。

痛点：
- 信息源**割裂**，每次调查都要重新关联；
- LLM 通用模型对内核 internals **知识浅、易幻觉**，难以直接采信；
- 业界 AIOps 产品（Datadog Watchdog / Dynatrace Davis / K8sGPT / HolmesGPT）面向**应用层 + K8s**，对内核场景适用性弱；
- 真正的"答案"长期沉淀在源码 commit、LKML 讨论、Bug 报告中，是被现有 LLM **欠开发**的高价值语料。

### 1.2 目标用户

| 用户群 | 描述 | v1 是否服务 |
|--------|------|-----------|
| 内核开发者 | 阅读、修改内核源码；提交 patch；上游 LKML 互动；处理 syzbot 报告 | ★ **核心目标** |
| 内核驱动 / 子系统维护者 | 负责特定子系统（mm / net / fs / sched / drivers）的稳定性 | ★ **核心目标** |
| 内核测试 / 性能工程师 | 跑 stress test / fuzz；分析回归 | 兼容覆盖 |
| 系统级 SRE | 主机层运维，需要内核诊断辅助 | v2 可扩展 |
| 应用开发者 / 普通 Linux 用户 | 偶发遇到内核相关问题 | 不在 v1 目标 |

### 1.3 产品定位一句话

> 一个面向内核开发者、**架构上可完全离线部署**、**所有诊断结论都能溯源到内核函数行号 / commit / LKML message-id / bug ID** 的故障调查智能体；MVP 阶段通过 **Claude Code 订阅授权**调用 LLM，v1.3 切换到本地 vLLM。

### 1.4 关键架构决策摘要

| 类别 | 决策 | ADR |
|------|------|-----|
| 检索哲学 | 全项目无 embedding；LLM 重排替代 cross-encoder | [ADR-001](adr/ADR-001-no-embedding.md) · [ADR-002](adr/ADR-002-no-reranker.md) |
| LLM 接入 | OpenAI Chat 内部 schema；MVP 默认 `claude_code` provider | [ADR-003](adr/ADR-003-openai-chat-schema.md) · [ADR-004](adr/ADR-004-claude-code-provider.md) |
| 代码与文档 | 复用 codesearch 作为 MCP 服务 | [ADR-008](adr/ADR-008-reuse-codesearch.md) · [ADR-009](adr/ADR-009-multi-version-via-codesearch-repos.md) · [ADR-012](adr/ADR-012-codesearch-http-transport.md) |
| 数据源 scope | LKML 2 年；仅 kernel.org Bugzilla；无 vmcore | [ADR-010](adr/ADR-010-lkml-2y-bootstrap.md) · [ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md) · [ADR-014](adr/ADR-014-m6-scope.md) |
| 诊断 Agent | LangGraph 双层；3 SOP；K=1 默认 | [ADR-015](adr/ADR-015-m7-diagnosis-strategy.md) |
| 可观测性 | v1.0 仅文件日志；一次性评测 | [ADR-016](adr/ADR-016-m8-minimal-observability.md) |

---

## 2. 典型场景（User Stories）

按优先级排序的 7 个故事。

### US-1（P0）— 自然语言提问历史 OOM 行为

> 作为内核开发者，我希望能问 "v5.10 上 order=4 的 normal zone OOM 通常什么原因？"，agent 列出 top-5 相关源码位置、3-5 个相关 LKML 讨论、2-3 个相似 bug 报告，并标注每条结果与查询的关联度。

### US-2（P0）— 粘贴 oops trace 求诊断

> 作为内核开发者，我手上有一段 v6.6 上的 oops trace（kernel panic），希望粘进 agent，它告诉我栈顶函数是什么、属于哪个子系统、近期 LKML 有无同样讨论、是否存在已修复的 commit。

### US-3（P1）— 跨版本 backport 状态查询

> 作为 LTS 维护者，我希望问 "mainline 的 commit abc1234 是否已 backport 到 OLK-5.10？" agent 通过 OLK commit 的 `upstream_commit` 反查（[ADR-018](adr/ADR-018-commit-source-olk-kernel.md)）给出答案。

### US-4（P1）— 子系统文档定向问答

> 作为新接触 PSI 的开发者，我希望问 "PSI 是什么、它的内核实现路径在哪？" agent 通过 CodeGraph PageIndex 在 kernel-doc 章节树上定位到 `Documentation/accounting/psi.rst`，并配套 `kernel/sched/psi.c` 源码引用。

### US-5（P1）— 完整 incident 端到端诊断

> 作为内核开发者，我把现场的 dmesg + sosreport 交给 agent，它生成 3-5 个竞争假设，逐一收集证据驳斥 / 支持，给出一份带证据链的根因诊断报告与排查 / 修复建议。（v1.2 起支持；v1 不依赖 vmcore，深内核态钻取留 v2）

### US-6（P2）— 历史相似 incident 检索

> 作为内核开发者，我希望问 "过去 2 年和这个栈相似的 syzbot 报告有哪些？" agent 用 stack 签名做相似度匹配，返回 top-k。

### US-7（P2）— 知识库每周同步审计

> 作为维护者，我希望每周自动同步后能拿到 changelog（新增多少 LKML 邮件、commits、bug 报告），并对失败的 ingestion 收到告警。

---

## 3. 功能需求

### 3.1 P0（v1.0 必交付）

| ID | 功能 | 描述 | 验收 | 模块 |
|----|------|------|------|------|
| F-001 | 双版本内核索引（通过 codesearch）| 在 codesearch 中注册 olk-kernel-v6.6 + olk-kernel-v5.10 两个 repo | `list_repos` 显示两 repo + SCIP ready | M4 |
| F-002 | LKML 增量抓取 | lore.kernel.org，**2024-05 起 2 年**（[ADR-010](adr/ADR-010-lkml-2y-bootstrap.md)）| mbox + Message-ID DAG 入库 | M3 |
| F-003 | Bugzilla / syzbot 接入 | **仅 bugzilla.kernel.org** + syzbot（[ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md)）| 每条 bug 可按 status / component 查询 | M3 |
| F-004 | kernel-doc PageIndex（通过 codesearch）| codesearch Sphinx JSON backend，OLK kernel 已就位 | `browse_docs` 返回章节树 | M4 |
| F-005 | 多源检索 CLI | 自然语言 → 7 路并行召回（code/docs/lkml/bug/syzbot/commit/cve）+ LLM 重排 | top-10 命中相关源码/邮件/bug；Recall@10 ≥ 70% | M5 |
| F-006 | LLM provider 抽象 | OpenAI Chat schema + 5 backend（claude_code / anthropic / openai_compat / vllm / ollama）| 切后端不改业务代码 | M1 |
| F-007 | 自然语言提问入口 | CLI `diag-agent diagnose` 接受 `--description` + `--upload` | 解析为 RetrievalQuery 触发检索 | M7 |
| F-008 | 每周增量同步 | cron 触发 6 个 ingester（lkml/bugzilla/syzbot/zenodo/nvd/kernel_commit），3-4h | `ingest_runs` 表记录成功；失败有日志 | M3 |
| F-009 | 主机文件解析 | mcp-dmesg-journal + mcp-sosreport（[ADR-014](adr/ADR-014-m6-scope.md)）| 上传文件 → 结构化 OopsReport / SystemSummary | M6 |
| F-010 | CodeGraph HTTP MCP 客户端 | 独立 MCP HTTP service 接入 | 启动时 healthcheck 通过 | M4 |

### 3.2 P1（v1.1 - v1.2 交付）

| ID | 功能 | 描述 | 模块 |
|----|------|------|------|
| F-101 | Cross-Graph Linking | commit ↔ bug ↔ LKML message-id ↔ CVE 关联；Neo4j 周度 rebuild + 对账 | M4 |
| F-102 | 诊断 Agent（Triage）| LangGraph ReAct 解析输入 + 抽 fault_domain + 简单/复杂分流 | M7 |
| F-103 | 诊断 Agent（Diagnosis）| Hypothesis-Driven + Self-Consistency K=1 默认 + Claim-Evidence Binding（Reject + Regenerate ≤2）| M7 |
| F-104 | 故障域 SOP（v1.0 = 3 类，v1.2 = 5 类） | OOM / lockup / panic（v1.0） + deadlock / I/O hang（v1.2 扩）| M7 |
| F-105 | 报告结构化输出 | Markdown + JSON 双格式 | M7 |
| F-106 | 30 例评测体系 | 静态锁定数据集 + 5 维度人工 rubric + 2 裁判 + tiebreak | M8 |

### 3.3 P2（v1.3 交付）

| ID | 功能 | 描述 | 模块 |
|----|------|------|------|
| F-201 | 本地 vLLM 部署验证 | 同 30 例评测在 Claude vs 本地大模型对照 | M1 / M8 |
| F-202 | LoRA 微调（可选） | commit msg + LKML + kernel-doc 训练集 | M1 |
| F-203 | 反馈循环 | 用户标注正确性 → 数据回流 | M8 |
| F-204 | 故障域 SOP 扩到 8 类 | + 网络 + 性能回归 + 调度 | M7 |
| F-205 | Prometheus + Grafana（评估）| 满足 [ADR-016](adr/ADR-016-m8-minimal-observability.md) 触发条件时部署 | M8 |

---

## 4. 非功能需求

### 4.1 性能

| 指标 | v1.0 | v1.2 |
|------|------|------|
| 单次混合检索（M5 retrieve 不含 PageIndex 走查）P95 | ≤ 1s | ≤ 0.8s |
| 含 PageIndex 走查 P95 | ≤ 5s | ≤ 4s |
| 端到端诊断 P95（K=1）| n/a | ≤ 60s |
| 端到端诊断 P95（K=3 评测）| n/a | ≤ 180s |
| 我方 PG 存储（v1.2 末估算）| ≤ 50 GB | 同 |
| 我方 Neo4j 存储 | ≤ 10 GB | 同 |
| 数据文件（mbox / syzbot / kernel-git） | ≤ 80 GB | 同 |
| codesearch 总占用 | ~30-50 GB | 同 |
| 每周增量同步运行时间 | ≤ 4h（M3 6 ingester 并行 + commit ETL）| ≤ 4h |
| Pro 5h 配额下日吞吐（K=1）| ~40 诊断/天（混合场景）| 同 |

### 4.2 隐私 & 安全

- **架构隐私要求**：所有数据流可在企业内网闭环，知识库副本 100% 本地（v1.3 后）
- **MVP 例外**：v1.0-v1.2 通过 **Claude Code OAuth 订阅授权**调用 Claude（[ADR-004](adr/ADR-004-claude-code-provider.md)）；用户上传的 vmcore / sosreport / dmesg 等**原始文件不直接喂 LLM**，仅经 M6 解析后的**结构化摘要**进入上下文
- **v1.3 切到本地 vLLM 后**：所有数据流不出企业网
- **工具执行**：所有 MCP server **默认只读**；M6 工具仅写到 `data/uploads/<run_id>/` 解包目录
- **PII / 敏感字段脱敏**：输入侧支持 regex 黑名单（IP / 密钥 / 邮箱），可配置扩展；M7 在送 LLM 前应用

### 4.3 可解释性（硬约束）

- 所有诊断结论必须挂证据；无证据 claim → Reject + Regenerate ≤ 2 次；超限标 `speculative`（[ADR-015](adr/ADR-015-m7-diagnosis-strategy.md)）
- **100% 证据可溯率**：v1.2 评测的硬性门槛
- 报告必须展示**竞争假设 + 驳斥理由**，而非仅最终结论
- 检索结果保留 `route` 标签（哪一路召回），便于审计

### 4.4 可扩展性

- **LLM provider 抽象**：新增 backend 只需实现 OpenAI Chat 兼容接口
- **新内核版本**：codesearch 加 repo 即可（[ADR-009](adr/ADR-009-multi-version-via-codesearch-repos.md)），我方无 schema 改动
- **新数据源**：实现 `Ingester` Protocol；M3 配置加一行
- **新 SOP**：yaml 配置热加载

### 4.5 可运维性

- **v1.0 极简观测**（[ADR-016](adr/ADR-016-m8-minimal-observability.md)）：
  - 结构化 JSON Lines 日志（`data/logs/<date>/<module>.jsonl`）
  - 文件 metrics（`data/metrics/<module>.json`，60s 写一次）
  - 本地 OTEL spans（`data/traces/<trace_id>.jsonl`）
  - 不部署 Prometheus / Grafana / Jaeger（v1.1+ 评估）
- **每周同步**：cron + `scripts/weekly_sync.sh`；失败写日志 + 邮件
- **存储**：PostgreSQL + Neo4j Community + 文件存储（**无 FAISS**，[ADR-001](adr/ADR-001-no-embedding.md)）
- **配置**：全 YAML，secrets 仅从环境变量读

---

## 5. 范围与不范围

### 5.1 在范围（v1）

- **内核源码 / 文档检索**（通过 CodeGraph MCP）：olk-kernel-v6.6 + olk-kernel-v5.10
- **LKML**（lore.kernel.org，**2024-05 起 2 年**）
- **Bugzilla**：**仅 bugzilla.kernel.org**（[ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md)）
- **syzbot 已 Fixed bug 与 reproducer**
- **Zenodo `linux-kernel-bugs` 数据集**
- **NVD CVE feed**
- **kernel commit metadata 自有 git clone**（OLK 内核仓 `OLK-6.6`/`OLK-5.10` 为主 + kernel.org mainline 为辅，[ADR-018](adr/ADR-018-commit-source-olk-kernel.md)）
- **双层诊断 agent**（Triage + Diagnosis）
- **故障域 SOP**：v1.0 = 3 类（OOM / lockup / panic）；v1.2 = 5 类
- **M6 主机 MCP 工具**：mcp-dmesg-journal + mcp-sosreport（仅这两个）
- **CLI 形态**：`diag-agent diagnose` + 文件上传

### 5.2 不在范围（v1）

- **vmcore / crash / drgn 工具**（[ADR-014](adr/ADR-014-m6-scope.md)，v2 重启）
- **perf.data / ftrace 解析**（v1.1+ 评估）
- **SSH 远程访问目标主机**（永不做）
- **Web UI / IDE 集成**（v2+）
- **应用层 / K8s / 微服务故障诊断**
- **自动修复执行**（PRD 永久锁定：永不做）
- **Red Hat / Debian / Ubuntu Bugzilla**（[ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md)，v1.1+ 评估）
- **多发行版 enterprise kernel 分支**（如 RHEL 自有 patch）
- **LWN.net 全量爬取**
- **多语言用户界面**（先中文）
- **商业化 SaaS 化部署**
- **实时 on-host 监控（daemon 模式）**
- **Prometheus / Grafana / Jaeger 部署**（v1.1+ 评估，[ADR-016](adr/ADR-016-m8-minimal-observability.md)）

### 5.3 关键外部依赖

- **CodeGraph MCP 服务**（[ADR-008](adr/ADR-008-reuse-codesearch.md)）：
  - HTTP server 模式启用（[ADR-012](adr/ADR-012-codesearch-http-transport.md)）— **跨项目协调点**
  - olk-kernel-v6.6 + olk-kernel-v5.10 已索引（含 Sphinx JSON 文档）
- **Claude Pro 订阅**（MVP 阶段；Claude Code OAuth 授权）
- **lore.kernel.org API** 与 mbox 接口稳定
- **bugzilla.kernel.org** 公开 REST API
- **syzbot 公开数据结构**未发生 breaking change
- **至少一台机器**：16C / 64GB / 500GB NVMe SSD + 1TB 备份盘

---

## 6. 验收准则

### 6.1 v1.0 (M3 末) 验收

| 项 | 准则 |
|----|------|
| 双版本内核索引（codesearch）| codesearch `list_repos` 显示 olk-kernel-v6.6 + olk-kernel-v5.10 + SCIP ready |
| LKML 抓取 | 2024-05 至今的邮件入库 ≥ 95%，Message-ID DAG 可遍历 |
| Bugzilla / syzbot | bugzilla.kernel.org ≥ 50K bugs 入库；syzbot ≥ 70K crash 入库 |
| CodeGraph healthcheck | 启动时 `list_repos` 通过 + 工具 smoke 测试通过 |
| 多源检索 CLI Recall@10 | ≥ 70%（5 类典型问题 × 5 例样本） |
| LLM provider | claude_code（默认）+ ollama（CI 用）跑通；anthropic / vllm 接口预留 |
| 文件日志 + metrics | 各模块按 schema 输出 JSONL + JSON metrics |

### 6.2 v1.2 (M9 末) 验收（基于人工评测）

| 项 | 准则 |
|----|------|
| 30 例公开案例完整人工评测 | 根因正确率（A=1 占比）≥ 50% |
| 证据可溯率 | 100%（每条 claim 必有有效 ref）|
| 综合分均值 | ≥ 0.6（5 维度加权）|
| 可读性均值 | ≥ 7/10 |
| Speculative 占比 | ≤ 10% |
| 5 类故障域 SOP（OOM / lockup / panic / deadlock / I/O hang）| 每类至少 1 个完整 demo |
| 每周同步 | 连续 4 周无 ingestion 失败 |

### 6.3 v1.3 (M12 末) 验收

| 项 | 准则 |
|----|------|
| Claude API vs 本地 vLLM 对照 | 30 例两后端正确率差距 < 10% |
| 反馈数据回流 | ≥ 50 条用户标注样本 |
| SOP 扩到 8 类 | 网络 + 性能 + 调度补齐 |

### 6.4 评测方法

- **数据集**：30 例公开案例**静态锁定 + git tag**（[ADR-016](adr/ADR-016-m8-minimal-observability.md)），来源：
  - syzbot 已 Fixed 报告：≥ 15 例
  - LKML 完整讨论 + 修复 commit：≥ 10 例
  - bugzilla.kernel.org RESOLVED-FIXED：≥ 5 例
  - 覆盖 8 类故障域，每类至少 2 例
- **裁判**：≥ 2 名内核工程师独立打分（盲审），分歧时第 3 人 tiebreak
- **Rubric**：5 维度（[M8 文档](modules/M8_evaluation_observability.md)）：
  - A 根因正确性（0/0.5/1，权重 0.4）
  - B 证据可溯率（必须 100%，硬约束）
  - C 排查建议可执行性（0/0.5/1，权重 0.2）
  - D 修复建议合理性（0/0.5/1，权重 0.2）
  - E 整体可读性（1-10，权重 0.2）
- **节奏**：v1.0 / v1.2 各跑一次，**v1.0 → v1.2 间不做回归**

---

## 7. 关键术语 (Glossary)

| 术语 | 含义 |
|------|------|
| Hypothesis-Driven | 生成多个竞争假设并并行验证的诊断范式 |
| SOP | Standard Operating Procedure，故障域结构化诊断决策树（yaml） |
| Claim-Evidence Binding | 强制每条诊断结论挂证据引用的 grounding 机制 |
| PageIndex | Vectify AI 提出的 reasoning-based RAG，用树形结构 + LLM 走查替代 embedding |
| MCP | Model Context Protocol，Anthropic 提出的工具接入标准 |
| Cross-Graph Linking | 跨知识子图（commit / bug / message / CVE）建立关联 |
| Self-Consistency | LLM 多轨迹独立推理 + 投票降幻觉 |
| LTS | Long-Term Support 内核版本（v6.6, v5.10 等） |
| codesearch | 已有项目（`/home/mqq/github/codesearch`），通过 MCP 提供代码 + 文档检索 |
| claude_code provider | 通过 `claude -p` subprocess（v1.0）或 claude-agent-sdk（6/15 后）调用 Claude Pro 订阅 |
| Pro 5h 窗口 | Claude Pro 订阅的限速窗口（~45 messages / 5 hours） |
| ground_truth | 评测样本的期望根因 + 修复 commit，由裁判私下确认 |

# 00 · 系统概览

## 一句话

**离线优先的 Linux 内核故障诊断智能体**——给定一段 dmesg / sosreport / 自由文本，自动定位根因（或如实承认证据不足），输出可执行的修复建议，全程引用真实的内核 commit / CVE / 邮件讨论作为证据。

## 目标用户

| 角色 | 场景 |
|---|---|
| 内核 / 后端工程师 | 拿到一段崩溃 dmesg，想知道**是不是已知 bug**、**改哪个 commit** |
| 值班 SRE | 收到 OOM kill / softlockup 告警，想快速判断**是内核 bug 还是配置问题** |
| OS 维护团队 | 评估上游某个 commit **是否需要 backport** 到 OLK 内核 |

## 不做什么

- **不做 in-production live debugging**：当前没有 SSH 到生产机器执行命令的能力，纯 KB 检索 + 文档分析
- **不跨 Linux 发行版**：只覆盖 OLK（openEuler）6.6 + 5.10，**不**做 Ubuntu / RHEL / Android 内核（FAQ Q9 论证：跨 distro 后 70 %+ 数据重复，运维成本远大于收益）
- **不做 root cause attribution to specific code paths beyond what KG can prove**：每条结论必须能追溯到 `search_commits` / `get_commit_detail` 等工具的真实返回，不允许凭训练记忆引用 commit hash
- **不内置 vector embeddings / semantic search**：ADR-001 拒绝，全部用 PG BM25 (`tsvector` + GIN) + 图谱关系 + LLM rerank 替代

## 输入 / 输出

```
输入                                    输出
─────────────────                        ────────────────────────
1) dmesg 片段（最常见）                  结构化诊断报告（Markdown + JSON）
2) sosreport 归档路径                       - 根因（候选假设 + 自我批驳 + 选定）
3) 自由文本问题描述                          - 修复建议（Form A backport /
4) 上述任意组合                                 Form B revert warning /
                                                 Form C config/hardware/by-design）
                                            - 置信度（high / medium / low）
                                            - 证据 trace（每条结论引用的工具输出 ID）
                                            - 完整工具调用轨迹（折叠区）
```

## 核心技术选型

| 维度 | 选择 | 拒绝的方案 + 理由 |
|---|---|---|
| 索引引擎 | PostgreSQL 16 `tsvector` + GIN | ❌ Elasticsearch / Tantivy（运维成本，PG 够用） |
| 语义检索 | LLM rerank over BM25 候选 | ❌ Vector embeddings（ADR-001：内核领域术语稀疏，BM25 ≥ embedding） |
| 图谱存储 | PG 关系表（主） + Neo4j（拓扑视图，副） | ❌ Neo4j 单独存储（主数据漂移风险） |
| LLM | OpenAI-compatible（DeepSeek-V3 / Claude / OpenRouter）| ❌ 锁定单一 vendor |
| Agent 框架 | LangGraph + 自实现 ReAct loop | ❌ `create_react_agent`（不兼容 PG cache + rate limit guard，见 docs/v2/M22_spike_findings.md） |
| 前端 | FastAPI + 单页 HTML + SSE | ❌ React/Vue SPA（过度工程化） |

完整决策见 `03_ADR_collection.md`。

## 8 大模块

```
M1 LLM Provider          抽象 3 个 backend（claude_code / openai_compat / vllm）
M2 Storage Schema        16 表 PG + Neo4j 拓扑
M3 Ingestion             6 个数据源 pipeline（lkml / bug / syzbot / nvd / gitee+atomgit / kernel_commit）
M4 Cross-Graph Linker    把 commit↔bug↔message↔CVE↔symbol↔fixes↔revert 织成关系网
M5 Retrieval             7 路并行 BM25 + AND-priority + LLM rerank
M6 MCP Host Tools        dmesg / sosreport 解析器（独立 MCP server）
M7 Diagnosis Agent       LangGraph triage + ReAct investigation + bind claims + render
M8 Evaluation            22 cases + process compliance KPI + LLM-as-judge
+ M9 Web UI              FastAPI + SSE + 双语 i18n（产品交付层）
```

每模块详见 `10_MODULE_SPECS/`。

## 关键运行特性

- **token 预算**：单次诊断 250K token 上限，超过自动 force_finalize
- **rate limit**：每路 LLM 有独立 rpm 限流，跨进程共享（同 endpoint 计数）
- **缓存**：PG 缓存 LLM 调用（30 天 TTL，按 model + prompt 哈希 key）
- **进程合规度 KPI**：以"agent 走了哪些 KG 路径"作为主指标（确定性，可复测）；答案质量为副指标（LLM-noisy，仅作健康检查）

## 版本基准

| 项 | 值 |
|---|---|
| 文档基线 git 提交 | `dd62a61` 或更晚（v2.4 收尾阶段） |
| 文档基线时间 | 2026-06-01 |
| PG 表结构迁移 head | `0014_link_syzbot_commit` |
| Python 版本 | 3.12（异步 + 类型注解依赖） |
| OLK 内核覆盖 | OLK-6.6 + OLK-5.10 |
| 数据规模快照 | 1.5M kernel_commit · 115K lkml_message · 8K bug · 16K CVE · 6.8K syzbot · 7 张 link 表共 ~260K 边 |

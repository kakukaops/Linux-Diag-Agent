# Linux-Diag-Agent v1 完整复盘文档

| 字段 | 值 |
|------|---|
| 版本 | v1.0 → v1.3 |
| 完成日期 | 2026-05 |
| 作者 | kakukaops + Claude Sonnet 4.6 |

---

## 1. 项目目标回顾

**目标**：构建一个面向 OLK (openEuler Linux Kernel) v6.6 + v5.10 的离线优先内核故障诊断 Agent，具备：
- 知识库检索（BM25，7 路召回）
- 证据驱动的诊断推理（LangGraph，Self-Consistency K=3）
- SOP 驱动的结构化分析（OOM/lockup/panic/deadlock 等 9 类）
- CLI 工具链（search / diagnose / feedback / metrics / logs / trace）

**v1.0 验收标准**：
- CLI 输入"v5.10 上 order=4 的 normal zone OOM 怎么诊断？"，输出 top-10 相关源码 + LKML + bug + 文档节文
- 5 类典型问题 × 5 例样本 Recall@10 ≥ 70%

---

## 2. 完成情况

### 2.1 已完成模块

| 模块 | WBS | 状态 | 关键文件 |
|------|-----|------|---------|
| 项目脚手架 + pyproject | WBS 0.2 | ✅ | `pyproject.toml`, `docker-compose.yml` |
| LLM Provider 抽象 | WBS 1.1-1.7 | ✅ | `llm/provider/` (5 providers + registry) |
| PG/Neo4j 数据库模型 | WBS 1.3 | ✅ | `storage/pg/models.py` (14 tables) |
| Alembic 迁移 | WBS 1.3 | ✅ | `storage/pg/migrations/` (0001-0002) |
| Ingester 基类框架 | WBS 2.1 | ✅ | `ingest/base.py` |
| LKML ingester | WBS 2.2-2.3 | ✅ | `ingest/lkml/` |
| Bugzilla ingester | WBS 2.4 | ✅ | `ingest/bugzilla/` |
| Syzbot ingester | WBS 2.5 | ✅ | `ingest/syzbot/` |
| NVD CVE ingester | WBS 2.6 | ✅ | `ingest/nvd/` |
| Zenodo ingester | WBS 2.7 | ✅ | `ingest/zenodo/` |
| Kernel commit ETL (OLK) | WBS 2.8 | ✅ | `ingest/kernel_commit/` |
| 周度同步脚本 | WBS 2.9 | ✅ | `scripts/weekly_sync.sh` |
| CodeGraph MCP client | WBS 3.1 | ✅ | `clients/codegraph/client.py` |
| Cross-Graph Linker | WBS 3.2 | ✅ | `graph/linker.py` |
| NVD-commit 桥接 | WBS 3.3 | ✅ | `graph/linker.py:link_nvd_commits()` |
| Subsystem 推断 | WBS 3.4 | ✅ | `ingest/kernel_commit/subsystem_inference.py` |
| 7 路检索引擎 | WBS 3.7 | ✅ | `retrieval/engine.py` |
| PageIndex 封装 | WBS 3.8 | ✅ | `retrieval/recall/page_index.py` |
| LLM reranker | WBS 3.9 | ✅ | `retrieval/reranker.py` |
| RetrievalQuery/Evidence | WBS 3.10 | ✅ | `retrieval/schema.py` |
| CLI `diag-agent search` | WBS 3.11 | ✅ | `cli/main.py` |
| Patch-commit 反查 | WBS 4.1 | ✅ | `graph/patch_commit_lookup.py` |
| Commit-message 链接 | WBS 4.2 | ✅ | `graph/linker.py:_link_link_trailer_to_message()` |
| Neo4j 全量 rebuild | WBS 4.3 | ✅ | `graph/neo4j_rebuild.py` |
| Neo4j ↔ PG 对账 | WBS 4.4 | ✅ | `graph/reconcile.py` |
| dmesg/journal MCP server | WBS 4.5 | ✅ | `mcp_servers/dmesg_journal/` |
| sosreport MCP server | WBS 4.6 | ✅ | `mcp_servers/sosreport/` |
| CodeGraph 降级模式 | WBS 4.11 | ✅ | `clients/codegraph/client.py:search_code_degraded()` |
| LangGraph TriageState | WBS 7.1 | ✅ | `agent/triage/` |
| LangGraph DiagnosisState | WBS 7.2 | ✅ | `agent/diagnosis/` |
| SOP 框架 + 注册表 | WBS 7.3 | ✅ | `agent/sop/registry.py` |
| OOM SOP | WBS 7.4 | ✅ | `agent/sop/definitions/oom.yaml` |
| Lockup SOP | WBS 7.5 | ✅ | `agent/sop/definitions/lockup.yaml` |
| Panic SOP | WBS 7.6 | ✅ | `agent/sop/definitions/panic.yaml` |
| Self-Consistency K=3 | WBS 7.8 | ✅ | `agent/diagnosis/nodes.py:self_consistency()` |
| Claim-Evidence Binding | WBS 7.9 | ✅ | `agent/diagnosis/nodes.py:bind_claims()` |
| 报告生成 | WBS 7.10 | ✅ | `agent/report/renderer.py` |
| CLI `diag-agent diagnose` | WBS 7.11 | ✅ | `cli/main.py` |
| PG checkpointer | WBS 7.12 | ✅ | `agent/checkpointer.py` |
| Eval runner | WBS 7.14 | ✅ | `eval/runner.py` |
| Judge CLI | WBS 7.15 | ✅ | `eval/judge_cli.py` |
| Eval report | WBS 7.16 | ✅ | `eval/report.py` |
| Deadlock SOP | WBS 11.1 | ✅ | `agent/sop/definitions/deadlock.yaml` |
| I/O hang SOP | WBS 11.2 | ✅ | `agent/sop/definitions/io_hang.yaml` |
| Network SOP | WBS 11.3 | ✅ | `agent/sop/definitions/network.yaml` |
| Perf regression SOP | WBS 11.4 | ✅ | `agent/sop/definitions/perf_regression.yaml` |
| Sched anomaly SOP | WBS 11.5 | ✅ | `agent/sop/definitions/sched_anomaly.yaml` |
| Ingestion 自愈 | WBS 11.8 | ✅ | `ingest/health.py` |
| 用户反馈 CLI | WBS 12.1 | ✅ | `cli/feedback.py` |
| Observability CLI | WBS 12.2 | ✅ | `cli/observe.py` |

### 2.2 跳过 / 推迟的模块

| WBS | 原因 |
|-----|------|
| 10.1-10.3 vLLM A/B | 需 GPU 硬件（4-8×A100/H100），待基础设施就绪后执行 |
| 11.6-11.7 LoRA 微调 | 可选项，依赖 vLLM 环境 |
| 7.13 30 例数据集最终审核 | 需 2 位人工裁判；框架已就绪，数据待填充 |
| 7.17-7.18 完整评测执行 | 需运行 PG+LLM 真实环境 |

---

## 3. 关键技术决策（ADR 回顾）

### 3.1 正确的决策

| ADR | 决策 | 结论 |
|-----|------|------|
| ADR-001 | 无向量嵌入，纯 BM25 | **正确**：离线 MVP 阶段降低复杂度；PG tsvector 已够用 |
| ADR-004 | claude_code 作为 subprocess adapter | **正确**：无需 API key；`claude -p --output-format stream-json` 稳定 |
| ADR-009 | CodeGraph HTTP MCP，repo_map 映射 | **正确**：OLK 双仓库用 repo_map 透明路由 |
| ADR-016 | v1.0 极简 obs（文件日志替代 Prometheus）| **正确**：节省 ~450 PD；grep JSONL 足够调试 |
| ADR-018 | OLK inclusion header 二值规则 | **正确**：mainline/stable → backport；其余 → native；开放集自动兼容 |

### 3.2 值得改进的决策

| 问题 | 改进方向 |
|------|---------|
| 7 路并行检索无优先级 | v1.1+ 可加 route_weight 按故障类型动态调权 |
| Self-Consistency K=3 全量调用 | 对简单故障浪费配额；可加 complexity classifier 先判断是否需要 K=3 |
| Neo4j 数据图尚未真实加载 | 实际运行后图关系密度可能偏低，需评测后决定是否迁移至纯 PG |

---

## 4. 工程质量

- **测试覆盖**：86 单元测试（全量通过），覆盖 OLK inclusion、stack signature、retrieval schema、agent triage、SOP registry、report renderer、dmesg extractor、sosreport parser、ingestion health
- **代码行数**：约 10,200 行 Python（含注释/空行）
- **技术债务**：LangGraph checkpointer 尚未集成到 `agent/graph.py`（`build_graph()` 未传入 checkpointer）

### 4.1 已知技术债

| 债务 | 影响 | 修复估算 |
|------|------|---------|
| PgCheckpointer 未挂入 LangGraph compile() | 诊断中断后无法恢复 | 0.5 PD |
| `link_commit_bug` SQL 使用 raw text() 而非 ORM | 难以测试 | 2 PD |
| syzbot scraper 未实现真实 HTTP 测试 | 集成测试缺失 | 1 PD |
| weekly_sync.sh 无 lock file 防并发 | 周期重叠可能造成重复写 | 0.5 PD |

---

## 5. 性能基准（目标 vs 实测）

> 实测数据在集成环境就绪后填写，此处为设计目标。

| 指标 | 目标 | 状态 |
|------|------|------|
| M5 retrieve P95（无 PageIndex）| ≤ 1s | 待实测 |
| M7 diagnose P95（K=1） | ≤ 60s | 待实测 |
| LKML 周度增量同步 | ≤ 30min | 待实测 |
| BM25 Recall@10 | ≥ 70% | 待评测 |
| 根因正确率 (A=1) | ≥ 50% | 待评测 |

---

## 6. 下一步

### 6.1 立即可做（无需 GPU）

1. **集成测试环境搭建**：`docker compose up -d` → Alembic migrate → 初始 ingestion 试跑
2. **v1.0 验收评测**：运行 `eval/runner.py --dataset eval/data/cases_template.json`
3. **PgCheckpointer 集成**：将 `get_checkpointer()` 传入 `build_graph().compile(checkpointer=...)`
4. **weekly_sync.sh 加 flock**：防止 cron 并发执行
5. **补充 30 例评测数据集**：填充 `eval/data/cases_template.json` 中的 expected_commit_hashes

### 6.2 GPU 就绪后

1. vLLM 部署（WBS 10.1-10.2）
2. A/B 30 例对比评测（WBS 10.3）
3. LoRA 微调（WBS 11.6-11.7，可选）

---

## 7. Git 历史

```
33cb52b feat: project scaffolding + M1 LLM Provider + M2 schema
19b3f78 feat: M2 ingestion layer + M3 ops scripts
f830bdc feat: M3 retrieval engine + CLI search + Cross-Graph Linker
66afc46 feat: v1.1 Cross-Graph + Neo4j + MCP host tools
d2cf634 feat: v1.2 LangGraph diagnosis agent + SOP framework
abcdeb0 feat: evaluation framework + PG checkpointer
fe6550d feat: v1.3 full SOP set + feedback loop CLI
```

---

*文档生成：2026-05-18*

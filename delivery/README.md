# Linux-Diag-Agent · 交付文档包

> 本包**不包含源代码**。它包含一个 Linux 内核故障诊断智能体的**完整设计契约**，足以让一个有 Python + LangGraph 经验的编程智能体（或人类工程师）**从零重建**等价系统。

---

## 这是什么

一份按"vibe code"理念组织的可重建文档：把系统拆成**契约 + 提示词资产 + 测试基准 + 已知陷阱**四类，每一类都用一个编程智能体能照着实现的语言写。

阅读完本包，重建者应能：

- 复现 16 张 PG 表 + 7 张关系表 + 1 个 Neo4j 图谱
- 复现 6 个数据源 ingester（lkml、bugzilla、syzbot、nvd、gitee/atomgit、kernel commit）
- 复现 7 路 BM25 检索 + LLM rerank + 多 prompt 路由
- 复现 LangGraph triage → ReAct investigation → bind claims → render report 的拓扑
- 复现 32 个 ReAct 工具（含 26 个 route=kernel）
- 复现 web UI（FastAPI + SSE，双语）
- 通过本包附带的 22 个回归 case + 1 个 coverage QA case 验收

## 这**不是**什么

- 不是源代码替代品。代码本身有不可言传的 micro-decision，看代码会更快——但我们不交付代码。
- 不是逐行 spec。我们写**契约级**而非**实现级**——`f(X) -> Y, 失败时抛 Z`，而不是"用 SQLAlchemy 的 X 方法"。
- 不是持续维护承诺。本包是一次性快照（见 `00_OVERVIEW.md` § 版本基准）。

---

## 阅读顺序

| 顺序 | 文件 | 给谁读 | 时长 |
|---|---|---|---|
| 1 | `00_OVERVIEW.md` | 决策者 + 重建者 | 5 min |
| 2 | `01_DOMAIN_GLOSSARY.md` | 重建者（不熟悉 OLK / kernel 术语的）| 10 min |
| 3 | `02_ARCHITECTURE.md` | 重建者 | 30 min |
| 4 | `03_ADR_collection.md` | 重建者，**理解为什么**这样选 | 1 h |
| 5 | `04_DATA_MODEL.md` | DB 重建者 | 1 h |
| 6 | `10_MODULE_SPECS/*` | 各模块重建者 | 各 1-2 h |
| 7 | `20_PROMPT_ASSETS/*` | **直接复制使用**，不要重写 | — |
| 8 | `30_TOOL_CONTRACTS.md` | ReAct 工具实现者 | 1 h |
| 9 | `40_BEHAVIORAL_CONTRACTS.md` | Agent 行为重建者 | 1 h |
| 10 | `50_TEST_FIXTURES/*` | **每个模块自验时引用** | — |
| 11 | `60_GOTCHAS.md` | 全员必读——**这些是只有踩过坑才知道的硬限制** | 30 min |
| 12 | `70_OPERATIONAL.md` | 部署 / 运行环境配置者 | 30 min |
| 13 | `80_KNOWN_LIMITS.md` | 决策者，了解边界 | 10 min |
| 14 | `90_FAQ.md` | 答疑 | 按需 |

---

## 重建策略

推荐**自底向上 + 测试先行**：

1. **先建数据层**：按 `04_DATA_MODEL.md` 起一个 PostgreSQL 16 实例，创建 16 张表 + 索引。用 `50_TEST_FIXTURES/seed_data/` 灌一份最小测试数据。
2. **建 LLM provider 抽象**：按 `10_MODULE_SPECS/M1_llm_provider.md` 实现 `LLMProvider` 协议 + 至少一个 backend（推荐 openai_compat 接 DeepSeek 官方 API）。
3. **建 ingester**（按需）：完整 ingest 数据量大（几小时）。如果只想跑诊断，跳过这步，用客户已有 DB 快照。
4. **建检索层**：按 `10_MODULE_SPECS/M5_retrieval.md` 实现 7 路召回。用 `cases_v2.json` 第 1 条 case 自验（recall@10）。
5. **建 ReAct 工具**：按 `30_TOOL_CONTRACTS.md` 实现 32 个工具的 JSON Schema + 调用契约。
6. **建 Agent 拓扑**：按 `10_MODULE_SPECS/M7_diagnosis_agent.md` + `40_BEHAVIORAL_CONTRACTS.md` 实现 LangGraph 节点。直接复用 `20_PROMPT_ASSETS/` 里的 prompt 文件，**不要重写**。
7. **建 web UI**：按 `10_MODULE_SPECS/M9_web_ui.md` 实现 FastAPI + SSE。
8. **跑验收**：`50_TEST_FIXTURES/ACCEPTANCE_CRITERIA.md` 逐项验证。

---

## 验收基线

| 测试 | 文件 | 通过标准 |
|---|---|---|
| 单元 | `50_TEST_FIXTURES/cases_smoke.json` (1 case) | 端到端跑完，verdict ∈ {diagnosed, insufficient_evidence}，无异常 |
| 覆盖度 | `50_TEST_FIXTURES/cases_coverage.json` (1 case) | route=kernel 工具集 32 个里至少 12 个被调用（Phase 0-4 全覆盖） |
| 回归 | `50_TEST_FIXTURES/cases_v2.json` (22 cases) | route_accuracy ≥ 85 %，process compliance（KG path coverage）≥ 60 % |

具体每个 case 的 expected_kg_paths / expected_route / expected_fault_kind 见 fixture JSON 文件。

---

## 维护策略

本包**不与代码持续同步**。客户如有以下需求：

| 场景 | 建议 |
|---|---|
| 文档对应代码版本是？ | 见 `00_OVERVIEW.md` § 版本基准 |
| 想跟踪新版本 | 联系交付方申请增量包，或直接订阅代码仓库 |
| 文档里某段不清楚 / 与重建结果矛盾 | 邮件提问，附"读 §X 后我的理解是 Y" |

---

## 给"另一个编程智能体"的提示

如果你（AI 编程智能体）正在读这个包来重建系统，记住：

1. **prompt 是资产，不是代码**——`20_PROMPT_ASSETS/` 里的 `.md` 文件**直接拷贝**作为系统提示词。重写它们会改变智能体行为。
2. **schema 是契约，不是建议**——`04_DATA_MODEL.md` 的列名、类型、生成列定义都要精确匹配；否则 SQL 查询会失效。
3. **gotchas 是真坑，不是注释**——`60_GOTCHAS.md` 每一条都是踩过的坑（psycopg3 `::` 解析、lore.kernel.org 必须用 Atom feed、DeepSeek `tool_choice=none` 会幻觉等）。一条不读会让你白白搜索数小时。
4. **acceptance 是验收，不是参考**——`50_TEST_FIXTURES/ACCEPTANCE_CRITERIA.md` 是验收门槛，重建版本必须通过。
5. **WHY 比 WHAT 重要**——ADR 解释了为什么不用 embeddings、为什么 OLK-only、为什么 BM25 而不是 Elasticsearch。**理解了 WHY 才不会做相反的选择。**

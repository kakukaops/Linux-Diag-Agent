# Linux-Diag-Agent v1 — 架构文档

| 字段 | 值 |
|------|---|
| 文档类型 | Architecture |
| 版本 | v1 |
| 状态 | Design Locked（待实施，2026-05-15 同步全部 ADR）|
| 关联文档 | [PRD.md](PRD.md) · [ProjectPlan.md](ProjectPlan.md) · [KnowledgeGraph_Overview.md](KnowledgeGraph_Overview.md) · [adr/](adr/) |
| 最后更新 | 2026-05-15 |

---

## 1. 架构概览

### 1.1 系统分层

```
┌──────────────────────────────────────────────────────────────────────┐
│ L0 — 用户接口层                                                       │
│   CLI: diag-agent diagnose --upload ...                              │
│   (Web UI / IDE 集成 → v2+)                                          │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L1 — 诊断 Agent 层 (LangGraph 双层)                                   │
│   Triage Agent (ReAct) → Diagnosis Agent (Hypothesis-Driven + SOP)   │
│   Claim-Evidence Binding · Self-Consistency K=1 默认                  │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L2 — 主机 MCP 工具层 (仅文件解析，全部只读)                            │
│   mcp-dmesg-journal · mcp-sosreport                                  │
│   ✗ 不含 crash/drgn/perf/ftrace（v1 外，ADR-014）                    │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L3 — 检索编排层 (Always-fire-all 7 路并行)                            │
│   query parse (全 LLM) → 7 路并行召回 → LLM 重排 (Navigator)         │
│   ✗ 无 embedding / 无 cross-encoder reranker (ADR-001/002)            │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L4 — 知识层 (我方自有部分)                                            │
│   Discussion Graph (LKML) · Bug Graph · Commit Graph · Cross-Linker  │
│   PG tsvector BM25 (LKML/Bug/syzbot/commit/cve)                      │
│   Neo4j Community (commit↔bug↔message↔CVE 关系)                      │
│   ※ Code Graph + Doc Tree 在 codesearch (ADR-008)                    │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L5 — Ingestion 层 (我方 6 个 ingester)                                │
│   lkml · bugzilla.kernel.org · syzbot · zenodo · nvd · kernel_commit │
│   ✗ 不含 Elixir / kernel-doc（codesearch 维护，ADR-008）              │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L6 — LLM Provider 抽象层 (OpenAI Chat schema 内部统一)                │
│   claude_code (MVP 默认) · anthropic · openai_compat · vllm · ollama │
│   ✗ 不含 embedding / reranker 服务 (ADR-001/002)                     │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L7 — 存储层                                                           │
│   PostgreSQL (14 表) · Neo4j Community · 文件存储                     │
│   ✗ 不含 FAISS (ADR-001)                                             │
└──────────────────────────────────────────────────────────────────────┘

                  ┌─────────────────────────────┐
                  │  外部依赖: CodeGraph MCP   │ ◀── HTTP MCP (ADR-012)
                  │  Zoekt + SCIP + PageIndex   │
                  │  olk-kernel-v6.6 / v5.10    │
                  └─────────────────────────────┘
```

### 1.2 主要数据流

**问询路径（M5 retrieve）**：

```
用户自然语言 → CLI 入口 → M5 retrieve(query)
  ├─ Query 解析（全 LLM，Navigator backend）
  ├─ 7 路并行召回（always-fire-all，ADR-013）：
  │   ├─ code   → codegraph.search_code + lookup_symbol
  │   ├─ docs   → codegraph.search_docs + PageIndex 走查
  │   ├─ lkml   → 我方 PG tsvector
  │   ├─ bug    → 我方 PG tsvector
  │   ├─ syzbot → 我方 PG + stack 签名
  │   ├─ commit → 我方 PG + Neo4j 关系遍历
  │   └─ cve    → 我方 PG
  └─ LLM 重排（候选 > 10 时，Navigator backend）
→ Evidence 列表 (含规范 ref)
```

**诊断路径（M7，v1.2 起完整）**：

```
用户上传（CLI --upload dmesg=... sosreport=...）
  → M6 解析：mcp-dmesg-journal + mcp-sosreport → 结构化字段
  → Triage Agent (LangGraph ReAct)：
      抽 fault_domain / kernel_version / stack_frames
      已知模式 → 直答
      复杂 → 转 Diagnosis
  → Diagnosis Agent (LangGraph Hypothesis-Driven)：
      SOP 路由 → 生成 3-5 假设 → M5 retrieve 并行验证
      Self-Consistency K=1 默认（评测 K=3）
      Claim-Evidence Binding（Reject + Regenerate ≤2）
  → 报告生成（Markdown + JSON）
```

**Ingestion 路径（每周日 03:00 UTC cron，3-4h）**：

```
weekly_sync.sh
  ├─ Phase 1 (并行)：lkml + bugzilla + syzbot + nvd
  ├─ Phase 2：kernel_commit ETL (含 git patch-id 计算)
  ├─ Phase 3 (每月首周日)：Zenodo 数据集刷新
  ├─ Phase 4：Cross-Graph Linker run_all
  └─ Phase 5：Neo4j 全量 rebuild + 对账
※ codesearch ingestion 由 codesearch 项目独立调度，不在 weekly_sync 中
```

---

## 2. 组件清单与职责

### 2.1 L1 诊断 Agent 层（M7）

| 组件 | 职责 | v1 状态 |
|------|------|--------|
| `agent/triage/` | LangGraph ReAct triage（输入解析、分类）| v1.2 |
| `agent/diagnosis/` | LangGraph Hypothesis-Driven 主诊断 | v1.2 |
| `agent/sop/*.yaml` | 故障域 SOP（v1.0 = 3 + generic） | v1.2 |
| `agent/binding/` | Claim-Evidence Binding 强制 + Regenerate | v1.2 |
| `agent/report/` | Markdown + JSON 双格式生成 | v1.2 |

### 2.2 L2 主机 MCP 工具层（M6）

| MCP Server | 范围 | v1 状态 |
|-----------|------|--------|
| `mcp_servers/dmesg_journal/` | dmesg 解析（oops/OOM/lockup/lockdep）+ journalctl 查询 | v1.0 |
| `mcp_servers/sosreport/` | sosreport.tar.xz 解包 + 关键字段提取 | v1.0 |

**注意**：不包含 perf / ftrace / vmcore 工具（[ADR-014](adr/ADR-014-m6-scope.md)）；LKML / Bug 不单独建 MCP（由 M5 retrieve 提供）。

### 2.3 L3 检索编排层（M5）

| 组件 | 职责 | v1 状态 |
|------|------|--------|
| `retrieval/api.py` | `retrieve(query)` 主入口 | v1.0 |
| `retrieval/query_parser.py` | 全 LLM query 解析 | v1.0 |
| `retrieval/recall/*.py` | 7 路 always-fire-all（code/docs/lkml/bug/syzbot/commit/cve）| v1.0 |
| `retrieval/pageindex.py` | PageIndex 走查（高阶封装 + 低阶 passthrough）| v1.0 |
| `retrieval/rerank.py` | LLM listwise rerank | v1.0 |

### 2.4 L4 知识层（M2 + M4）

| 组件 | 职责 | v1 状态 |
|------|------|--------|
| `storage/pg/` | 14 张表（lkml_* / bug / cve / syzbot_crash / kernel_commit / link_* / 系统表） | v1.0 |
| `storage/neo4j/` | Commit / Bug / Message / Thread / CVE / Subsystem 节点 + 关系 | v1.0 |
| `graph/linker.py` | Cross-Graph Linker（trailer 抽取 + patch-commit 反查）| v1.0 起 → v1.1 完善 |
| `graph/neo4j_rebuild.py` | 周度全量 rebuild + 对账 | v1.0 |

### 2.5 L5 Ingestion 层（M3）

| Ingester | 数据源 | 频率 |
|---------|--------|------|
| `ingest/lkml/` | lore.kernel.org（2024-05 起 2 年）| 周度增量 |
| `ingest/bugzilla/` | bugzilla.kernel.org 唯一 | 周度增量 |
| `ingest/syzbot/` | syzkaller.appspot.com HTML | 周度 |
| `ingest/zenodo/` | Zenodo 数据集 | 季度 |
| `ingest/nvd/` | NVD JSON Feed | 周度 |
| `ingest/kernel_commit/` | 我方独立 git clone（OLK 内核仓 `OLK-6.6`/`OLK-5.10` + mainline 辅仓，ADR-018） | 周度增量 |

**注意**：内核源码 + kernel-doc 索引由 codesearch 独立维护，不在我方 ingester 列表。

### 2.6 L6 LLM Provider 抽象层（M1）

| Provider | 用途 | v1 状态 |
|----------|------|--------|
| `claude_code` | **MVP 默认**：包 `claude -p` subprocess（6/15 后 Agent SDK）| ★ v1.0 |
| `anthropic` | 独立 API key 路径（用户当前未用）| v1.0 接口 |
| `openai_compat` | OpenAI / DeepSeek / Moonshot / 智谱 / 百炼 | v1.0 接口 |
| `vllm` | 本地大模型 | v1.0 接口 / v1.3 实跑 |
| `ollama` | 轻量本地（CI / 开发）| v1.0 |

**Chat / Navigator 双档**：Chat = Sonnet（主推理）；Navigator = Haiku（PageIndex 走查 + 重排，便宜频繁调用）。

### 2.7 L7 存储层

| 存储 | 用途 |
|------|------|
| PostgreSQL ≥ 15 | 14 张表，含 tsvector + GIN 索引 |
| Neo4j Community ≥ 5 | Cross-Graph 关系遍历 |
| 文件存储（`<repo>/data/`，[ADR-007](adr/ADR-007-data-dir-in-repo.md)） | mbox / syzbot / sosreport / kernel-git |
| ~~FAISS~~ | **不部署**（[ADR-001](adr/ADR-001-no-embedding.md)） |

### 2.8 外部依赖：CodeGraph MCP 服务（[ADR-008](adr/ADR-008-reuse-codesearch.md)）

| CodeGraph 工具 | 用途 | 我方调用模块 |
|---------------|------|------------|
| `browse_docs` / `browse_doc_sections` / `read_doc_section` | PageIndex 文档导航 | M5 / M7 |
| `search_docs` | BM25 文档关键词 | M5 |
| `search_code` | Zoekt 代码全文 | M5 |
| `lookup_symbol` | SCIP 精确符号 | M5 / M7 |
| `search_symbol_docs` | 文档↔代码桥接 | M5 |
| `get_outline` | tree-sitter 文件大纲 | M7 |
| `read_file` | 按路径读源码 | M7 |
| `list_repos` | 健康检查 | M4 startup |

**通信**：HTTP MCP（[ADR-012](adr/ADR-012-codesearch-http-transport.md)，需 codesearch 启用 streamable-http transport）

---

## 3. 接口契约

### 3.1 M1 LLM Provider（OpenAI Chat Completions schema）

```python
class LLMProvider(Protocol):
    def chat(self, req: ChatRequest) -> ChatResponse: ...
    def chat_stream(self, req: ChatRequest) -> Iterator[ChatChunk]: ...

class ChatRequest(TypedDict, total=False):
    messages: list[Message]
    model: str
    tools: list[ToolSchema] | None
    temperature: float       # 默认 0.0（确定性 → M1 PG cache 命中）
    stream: bool             # 默认 True
    response_format: dict | None     # JSON mode
    cache_control: list | None       # 扩展字段，对应 Anthropic Prompt Caching
    metadata: dict           # trace_id, sop_id, ...
```

详见 [M1 文档](modules/M1_llm_provider.md) §3。

### 3.2 M5 Retrieval API

```python
class RetrievalQuery:
    natural_language: str
    kernel_version: str | None      # 'v6.6' | 'v5.10' (映射到 CodeGraph repo)
    fault_domain: str | None
    stack_frames: list[str] | None
    error_keywords: list[str] | None
    time_window: tuple[datetime, datetime] | None
    routes: set[Route] | None       # None = 全部 7 路 (always-fire-all)
    top_k: int = 10
    enable_rerank: bool = True
    trace_id: str

class Evidence:
    source: Literal['code', 'doc', 'lkml', 'bug', 'syzbot', 'commit', 'cve']
    ref: str                        # 规范引用，详见各模块 §3.3
    snippet: str
    score: float
    cite: dict
```

### 3.3 M6 主机工具输出 schema

主要输出类型：
- `OopsReport` / `OOMReport` / `LockupReport` / `LockdepReport`（dmesg）
- `SystemSummary` / `SosManifest`（sosreport）

详见 [M6 文档](modules/M6_host_mcp_tools.md) §3.3 / §4.3。

### 3.4 CodeGraph MCP HTTP

```python
class CodeGraphClient:
    async def list_repos(self) -> list[RepoInfo]: ...
    async def search_code(self, query, repos, mode, limit) -> list[CodeHit]: ...
    async def lookup_symbol(self, symbol, action, repo) -> list[SymbolHit]: ...
    async def browse_docs(self, repo) -> str: ...
    # ... 共 9 个工具
```

详见 [M4 文档](modules/M4_cross_graph_linker.md) §4.2。

### 3.5 M7 报告 JSON schema

```json
{
  "run_id": "...",
  "fault_domain": "oom",
  "sop_id": "oom_diagnosis",
  "self_consistency_k": 1,
  "winning_hypothesis": {
    "id": "H2",
    "title": "...",
    "confidence": 0.85,
    "evidence_refs": [...]
  },
  "rejected_hypotheses": [...],
  "evidence_chain": [...],
  "investigation_steps": [...],
  "fix_suggestions": {"short_term": [...], "medium_term": [...], "long_term": [...]},
  "metadata": {"llm_messages_used": 4, "speculative_claims": [], ...}
}
```

详见 [M7 文档](modules/M7_diagnosis_agent.md) §9.2。

---

## 4. 技术选型与开源依据

| 组件 | 选型 | License | 选型理由 |
|------|------|------|---------|
| 关系数据库 | PostgreSQL 15+ | PostgreSQL License | 通用、成熟、tsvector + GIN BM25 |
| 图数据库 | Neo4j Community 5+ | GPLv3 | 生态成熟；NebulaGraph (Apache 2.0) 作为 v2 备选 |
| ~~向量库~~ | ~~FAISS~~ | — | **不部署**（[ADR-001](adr/ADR-001-no-embedding.md)） |
| BM25 | PostgreSQL `tsvector` + GIN | 内置 | [ADR-006](adr/ADR-006-pg-tsvector-bm25.md)；v1.1 评估切 Tantivy |
| Agent 编排 | **LangGraph** | MIT | [ADR-015](adr/ADR-015-m7-diagnosis-strategy.md)；状态机 + tools + checkpointing 原生 |
| LLM Provider（MVP 默认）| **claude_code provider** | — | [ADR-004](adr/ADR-004-claude-code-provider.md)；Claude Pro 订阅授权 |
| LLM Provider（本地）| vLLM | Apache 2.0 | 高吞吐；v1.3 切到本地后用 |
| MCP 协议 | Anthropic MCP spec | 开放规范 | 工具接入标准化 |
| codesearch (外部) | Zoekt + SCIP + PageIndex | 已有项目 | [ADR-008](adr/ADR-008-reuse-codesearch.md)；OLK kernel 已验证 |
| ~~Embedding 模型~~ | ~~BGE-M3~~ | — | **不部署**（[ADR-001](adr/ADR-001-no-embedding.md)）|
| ~~Cross-encoder reranker~~ | ~~bge-reranker-v2-m3~~ | — | **不部署**（[ADR-002](adr/ADR-002-no-reranker.md)）|
| 内核源码索引 | Bootlin Elixir-style (在 codesearch 内) | — | codesearch 负责 |
| AST 解析 | Tree-sitter (在 codesearch 内) | MIT | codesearch 负责 |
| C/C++ 语义 | scip-clang (在 codesearch 内) | Apache 2.0 | codesearch 负责 |
| Mailing list 抓取 | mailbox/email Python stdlib + lore HTTP | PSF | 标准库 |
| Bugzilla 客户端 | python-bugzilla | GPLv2 | kernel.org BZ |
| 编排 / 调度 | systemd timer + bash | 系统自带 | v1.0 极简 |
| ~~Monitor~~ | ~~Prometheus + Grafana~~ | — | **v1.0 不部署**（[ADR-016](adr/ADR-016-m8-minimal-observability.md)） |
| 日志 | JSONL 文件 | — | 取代 Prometheus；CLI grep / jq |
| Schema 迁移 | Alembic | MIT | PG schema 演进 |

**开源原则**：业务核心路径不依赖任何闭源服务（claude_code provider 是 MVP 受控例外，v1.3 可切本地 vLLM）。

---

## 5. 扩展点

### 5.1 LLM Provider 扩展

新增 backend 只需实现 `LLMProvider` Protocol（M1 §3.1）+ 在 config 注册。无业务层改动。

### 5.2 检索 routes 扩展

新增数据源（如 LWN）只需：
1. M3 加 ingester
2. M5 `retrieval/recall/<source>.py` 加 recall 函数
3. M5 `RetrievalQuery.routes` 加枚举值

### 5.3 SOP 扩展

新增故障域 SOP：
1. 写 `agent/sop/<domain>.yaml`
2. 注册到 `SOP_REGISTRY`
3. 加 fixture 进 `data/eval/cases/`

v1.0 → v1.2 → v1.3 路径：3 → 5 → 8 SOPs。

### 5.4 内核版本扩展

在 codesearch 加 repo（如 `olk-kernel-v6.12`），我方 `clients/codegraph/repos.py` 加映射，零代码改动。

### 5.5 MCP 工具扩展

主机侧新工具（如 mcp-perf v1.1）：
1. 实现 FastMCP server
2. M7 SOP yaml 中引用
3. 配置注册

---

## 6. 安全 & 隐私边界

### 6.1 输入侧

- 用户上传的 sosreport / dmesg 等**仅本地解析**（M6 工具运行在 agent 主机）
- PII / 密钥黑名单（regex）由 M7 在送 LLM 前应用
- 敏感字段（hostname / 内部 IP / username）可配置打码

### 6.2 LLM 通信侧

- **v1.0-v1.2 MVP**：通过 Claude Code OAuth 调 Claude；**原始 vmcore / 完整 sosreport 不上传**，仅结构化摘要进 LLM context
- **v1.3 全离线**：切到本地 vLLM，所有数据在企业内网
- API key 仅环境变量；禁止入 git
- 调用日志可审计（M8 文件日志）

### 6.3 工具执行侧

- 所有 MCP server **默认只读**
- 修复建议输出命令清单，**不自动执行**
- 任何写操作（如 `echo > /proc/sys/...`）**永不**由 agent 触发

### 6.4 知识库存储侧

- LKML / BZ / kernel 公开数据，无敏感问题
- 内部 incident（如有）单独 namespace，与公开数据物理隔离

### 6.5 v1.3 全离线模式

切到 `vllm` backend 后，所有数据流不出企业网；可在隔离环境运行。

---

## 7. 部署拓扑

### 7.1 v1.0-v1.2 MVP 拓扑

```
                    ┌──────────────────────────────────────────────┐
                    │  开发者工作站 / 单台服务器                     │
                    │                                              │
                    │  ┌──────┐  ┌──────────────┐  ┌────────────┐ │
                    │  │ CLI  │  │ Linux-Diag-  │  │ PostgreSQL │ │
                    │  │      │  │ Agent        │  │ + Neo4j    │ │
                    │  │      │  │ (M1-M8)      │  │            │ │
                    │  └──────┘  └──────┬───────┘  └────────────┘ │
                    │                   │                          │
                    │                   │ HTTP MCP                 │
                    │                   ▼                          │
                    │              ┌──────────────┐                │
                    │              │ codesearch   │                │
                    │              │ MCP Server   │                │
                    │              │ (port 8765)  │                │
                    │              └──────────────┘                │
                    │                                              │
                    │  ★ 知识库副本本地（含 codesearch 索引）       │
                    └────────────────────┬─────────────────────────┘
                                         │
                                  ┌──────▼──────┐
                                  │ Claude Code │  (Pro 订阅 OAuth)
                                  │ → Anthropic │
                                  └─────────────┘
```

- 单机部署，PostgreSQL + Neo4j + codesearch 同机
- LLM 走 `claude_code` provider（subprocess 包 `claude -p`）
- 数据 100% 本地副本

### 7.2 v1.3 全离线拓扑

```
       ┌──────────────────────────────────────────────────────┐
       │           企业内网                                    │
       │                                                      │
       │   ┌──────┐  ┌──────────┐  ┌──────────────┐          │
       │   │ CLI  │  │Linux-Diag│  │ PostgreSQL   │          │
       │   │      │  │  -Agent  │  │ + Neo4j      │          │
       │   └──────┘  └─────┬────┘  └──────────────┘          │
       │                   │                                  │
       │             ┌─────┼─────┐                            │
       │             ▼           ▼                            │
       │       ┌──────────┐ ┌──────────┐                      │
       │       │codesearch│ │ vLLM     │                      │
       │       │ MCP      │ │ 4×A100   │                      │
       │       │ Server   │ │          │                      │
       │       └──────────┘ └──────────┘                      │
       │                                                      │
       │       ★ 全部在内网；无对外网络                        │
       └──────────────────────────────────────────────────────┘
```

- 加一个 GPU 节点跑 vLLM
- 业务节点指 `llm.backend: vllm` + `endpoint: http://gpu-node:8000/v1`
- 完全断网仍可工作

### 7.3 Ingestion 拓扑

```
cron (周日 03:00 UTC) → scripts/weekly_sync.sh
                          ├─ Phase 1 并行：lkml / bugzilla / syzbot / nvd
                          ├─ Phase 2：kernel_commit ETL
                          ├─ Phase 3 (每月首周日)：zenodo
                          ├─ Phase 4：Cross-Graph Linker
                          └─ Phase 5：Neo4j rebuild + 对账

※ CodeGraph 自身 ingestion（Zoekt + SCIP + Sphinx）独立调度，不在此 cron 中
```

---

## 8. 演进路径

| 版本 | 主题 | 关键里程碑 |
|------|------|-----------|
| v1.0 (M0-M3) | 知识库地基 + 检索 CLI | M3 ingestion 跑通；M5 retrieve 7 路；CodeGraph 集成；文件日志 |
| v1.1 (M4-M6) | Cross-Graph + 路由 + 工具 | Cross-Graph Linker 全填；ColBERT-v2 可选切（如评测显示需要）；MAINTAINERS 解析；mcp-perf（如需）|
| v1.2 (M7-M9) | 诊断 Agent + 评测 | Triage + Diagnosis Agent 上线；3 SOP；30 例完整人工评测；SOP 扩到 5 |
| v1.3 (M10-M12) | 生产化 + 离线化 | vLLM 本地部署；A/B 对照评测；SOP 扩到 8；Prometheus + Jaeger（如触发）|

详见 [ProjectPlan.md](ProjectPlan.md)。

---

## 9. 约束与权衡记录

| 约束 | 选择 | 出处 ADR |
|------|------|---------|
| 不做 embedding（项目级哲学）| 牺牲离线索引速度，换取可解释性 | [ADR-001](adr/ADR-001-no-embedding.md) |
| 不做 cross-encoder reranker | 用 LLM 重排替代；准确率上限更高，成本略增 | [ADR-002](adr/ADR-002-no-reranker.md) |
| 双版本 v6.6 + v5.10 | 维护 codesearch 两个 repo；存储与时间翻倍 | [ADR-009](adr/ADR-009-multi-version-via-codesearch-repos.md) |
| MVP 用 claude_code provider | 牺牲"v1.0 完全离线"承诺；保留 provider 抽象层 | [ADR-004](adr/ADR-004-claude-code-provider.md) |
| 仅 bugzilla.kernel.org | 失去 RHBZ 的 CVE 关联完整性；NVD 补强 | [ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md) |
| LKML 2 年起步 | 失去 2020-2024 早期历史；可后续回补 | [ADR-010](adr/ADR-010-lkml-2y-bootstrap.md) |
| 无 vmcore 工具 | 失去深内核态钻取；其他故障类型不受影响 | [ADR-014](adr/ADR-014-m6-scope.md) |
| 仅文件上传，不接 SSH | 凭据管理简化；失去实时状态访问 | [ADR-014](adr/ADR-014-m6-scope.md) |
| Always-fire-all 7 路召回 | 资源开销大；保障召回完整性 | [ADR-013](adr/ADR-013-m5-retrieval-strategy.md) |
| 全 LLM query 解析 | 每查询 1 message；省维护规则集 | [ADR-013](adr/ADR-013-m5-retrieval-strategy.md) |
| Claim-Evidence Binding Reject + Regenerate ≤2 | 平衡幻觉控制与配额 | [ADR-015](adr/ADR-015-m7-diagnosis-strategy.md) |
| v1.0 SOP = 3 类 | 收敛 MVP；v1.2 扩 5 / v1.3 扩 8 | [ADR-015](adr/ADR-015-m7-diagnosis-strategy.md) |
| v1.0 仅文件日志 | Ops 极简；v1.1+ 视触发条件升级 | [ADR-016](adr/ADR-016-m8-minimal-observability.md) |
| 评测仅 v1.0/v1.2 各跑一次 | 无回归；新工程的退化可能延迟发现 | [ADR-016](adr/ADR-016-m8-minimal-observability.md) |
| codesearch HTTP transport | 需跨项目协调；解耦 codesearch 与 diag-agent 生命周期 | [ADR-012](adr/ADR-012-codesearch-http-transport.md) |

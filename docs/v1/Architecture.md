# Linux-Diag-Agent v1 — 架构与知识图谱

| 字段 | 值 |
|------|---|
| 文档类型 | Architecture + Knowledge Graph |
| 版本 | v1 |
| 状态 | Design Locked（待实施，2026-05-15 同步全部 ADR）|
| 关联文档 | [PRD.md](PRD.md) · [ProjectPlan.md](ProjectPlan.md) · [adr/](adr/) |
| 最后更新 | 2026-05-18（合并自 Architecture.md + KnowledgeGraph_Overview.md）|

> **⚠️ 设计规格文档**：本文档为实施前的原始设计规格（定稿于 2026-05-15）。实际实现以代码为准，两者可能存在偏差。如需了解当前实现状态，请阅读对应目录下的 `CLAUDE.md` 和源代码。

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
│   PostgreSQL (16 表) · Neo4j Community · 文件存储                     │
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

### 1.3 知识图谱设计哲学

核心差异化壁垒是**把内核生态的公开知识（源码 + 邮件 + Bug + 文档）构建成一张可推理的多层关联图**。整体由 **4 张子图 + 1 个 Cross-Graph Linker** 组成。

**关键架构决策**（[ADR-008](adr/ADR-008-reuse-codesearch.md)）：**Code Graph 与 Doc Tree 两块复用已有项目 `codesearch`**（独立 MCP 服务，已在 OLK Kernel 6.6 跑通）；Linux-Diag-Agent 自建 LKML / Bug Graph + Cross-Graph Linker。

```
┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐  ┌────────────────┐
│ Code Graph      │  │ Discussion      │  │ Bug Graph       │  │ Doc Tree       │
│ (源码图谱)       │  │ Graph (邮件)    │  │ (缺陷/崩溃)      │  │ (文档章节树)    │
│                 │  │                 │  │                 │  │                │
│ ★ 复用          │  │ Message-ID DAG  │  │ bug + CVE       │  │ ★ 复用         │
│ CodeGraph       │  │ + 线程摘要      │  │ + syzbot crash   │  │ CodeGraph      │
│ (Zoekt + SCIP)  │  │ + patch 系列    │  │ + stack 签名    │  │ (PageIndex     │
│                 │  │ + Reviewed-by  │  │ + 修复 commit    │  │  / Sphinx JSON)│
└────────┬────────┘  └────────┬────────┘  └────────┬────────┘  └────────┬───────┘
         │                    │                    │                    │
         └────────────────────┴────────┬───────────┴────────────────────┘
                                       ▼
                          ┌────────────────────────┐
                          │ Cross-Graph Linker     │
                          │ commit↔bug↔message↔CVE │
                          │ + 运行时 CodeGraph     │
                          │   lookup_symbol 调用   │
                          └────────────────────────┘
                                       ▼
              我方 PG + Neo4j (LKML/Bug/commit + 关系)
```

**共同原则**：

| 原则 | 说明 |
|------|------|
| 全项目**无 embedding**（[ADR-001](adr/ADR-001-no-embedding.md)）| 召回靠 BM25 + 图遍历 + LLM 树形走查 |
| **结构化优先** | 能用自然键、外键、tag、正则的，不用 LLM 推断；减少幻觉，所有关联可审计 |
| **Code Graph + Doc Tree 通过 MCP 复用 codesearch** | 避免重复造轮子；codesearch 已 production-validated |
| **我方 PG 存详情、Neo4j 只存图谱拓扑** | 各取所长 |
| 所有 ingest 任务**增量 + 断点续传** | 每周同步成本可控 |
| 所有链接都带 `confidence` 与 `source` 字段 | 调试与对账可追溯 |

---

## 2. 知识图谱构建

### 2.1 子图一：Code Graph（复用 codesearch）

**完全由 CodeGraph 承担**，Linux-Diag-Agent 仅作为 MCP 消费者：

| 层 | 在 codesearch 内的实现 |
|----|-----------------------|
| 文件全文 + symbol 文本搜索 | **Zoekt** trigram 索引（v6.6 + v5.10 两个 repo）|
| 精确符号定义 / 引用 / 实现 | **SCIP**（scip-clang 基于 LLVM 21）|
| 文件级大纲 | tree-sitter（按需解析）|
| 跨 repo 符号查询 | codesearch 的 PG `scip_symbols` 表 |

**CodeGraph MCP 调用示例**：

| 调用 | 返回 |
|------|------|
| `search_code(query, repos=['olk-kernel-v6.6'], mode='symbol')` | Zoekt 命中的文件:行 列表 |
| `lookup_symbol('oom_kill_process', action='definition', repo='olk-kernel-v6.6')` | SCIP 精确定义（file:line + 签名 + doc-comment）|
| `lookup_symbol('oom_kill_process', action='references')` | 所有调用点 |
| `get_outline(repo='olk-kernel-v6.6', path='mm/oom_kill.c')` | 文件级函数/结构体大纲 |

**我方在 Code Graph 上的工作**（保留少量）：

| 工作 | 实现位置 |
|------|---------|
| CodeGraph MCP 客户端封装 | `clients/codegraph/client.py` |
| 内核版本 → CodeGraph repo 名映射 | `clients/codegraph/repos.py` |
| commit 元数据 ETL（git log + Fixes: 抽取）| `ingest/kernel_commit/` |
| 子系统推断（file_path 推 subsystem）| `ingest/kernel_commit/subsystem_inference.py` |

### 2.2 子图二：Discussion Graph（LKML 邮件讨论）

**我方自建**，lore.kernel.org 邮件重建为可多跳遍历的讨论 DAG：

| 层 | 内容 | PG 表 |
|----|------|-------|
| 单封邮件 | 元数据 + body + tsvector | `lkml_message` |
| 线程 DAG | Message-ID + In-Reply-To 反向树 | Neo4j `(:Message)-[:IN_REPLY_TO]->()` |
| Patch 系列 | `[PATCH vN]` + Fixes: tag + landed_commit | `lkml_patch` |
| 评审信号 | `Reviewed-by:` / `Acked-by:` / `NACK` | `lkml_review` |
| 长线程摘要 | LLM "设计动机 + 争议点 + 结论" | `lkml_thread.summary` |

**处理流程**：

```
lore.kernel.org Atom feed（?q=d:YYYYMMDD..&x=A）→ 收集消息 URL
  ↓
每封 /{list}/{msg-id}/raw 下载（1.5s/请求限速）
  ↓
mbox 解析（mailbox + email Python stdlib）→ 单封邮件元数据 + body
  ↓
存入 PG lkml_message（含 body_tsv BM25 全文索引）
  ↓
基于 In-Reply-To / References 构建线程 DAG → PG lkml_thread + Neo4j 关系
  ↓
[PATCH vN] / Fixes: / Reported-by: trailer 解析 → lkml_patch
  ↓
Reviewed-by/Tested-by/Acked-by 解析 → lkml_review
  ↓
对长线程（> 30 封）调用 LLM → 三段摘要存入 lkml_thread.summary
```

### 2.3 子图三：Bug Graph（缺陷与崩溃）

**我方自建**，统一多源 bug 数据：

数据源（v1，[ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md)）：
- **bugzilla.kernel.org**（v1 仅此源）
- syzbot.kernel.org
- Zenodo `linux-kernel-bugs` 数据集（120 万 commits + 9 万 bug-fix 对，一次性导入）
- NVD JSON Feed（CVE 元数据）— 通过 commit hash 关联补强 CVE-kernel 链路

**处理流程**：

```
Bugzilla REST API（last_change_time 增量）→ bug 元数据 + description
  ↓ 归一化
PG bug 表（title / severity / status / body_tsv）

syzbot HTML 抓取 → crash 标题 + reproducer + stack trace
  ↓
PG syzbot_crash 表 + 栈帧签名（ingest/syzbot/signature.py）

NVD JSON Feed（lastModStartDate/lastModEndDate 滑动窗口，119 天）→ CVE 元数据
  ↓
PG cve 表（cvss_v3_score / "references" / fix_commits）
fix_commits：从 NVD references 的 GitHub/kernel.org commit URL 中提取 SHA
```

**关键变化（vs 原计划）**：stack frame → function 解析改为**运行时调用 CodeGraph `lookup_symbol`，不持久化**（原计划自建 `link_stackframe_function` 表）。

### 2.4 子图四：Doc Tree（复用 codesearch）

**完全由 CodeGraph 承担**，Linux-Diag-Agent 仅作为 MCP 消费者：

- kernel-doc 通过 **Sphinx JSON backend** 接入
- 三层导航：`browse_docs()` → `browse_doc_sections()` → `read_doc_section()`
- 我方工作量约 ~100 行（路由决策：什么时候查文档 vs 查源码 vs 查 LKML）

| 调用 | 返回 |
|------|------|
| `browse_docs(repo='olk-kernel-v6.6')` | 顶层文档目录树 |
| `browse_doc_sections(repo, source_path='admin-guide/mm/concepts.rst')` | 单文档章节树 |
| `read_doc_section(repo, source_path, section='Fragmentation')` | 节文 |
| `search_docs(query='OOM killer', repo='olk-kernel-v6.6')` | BM25 文档命中 |

### 2.5 Cross-Graph Linker（核心壁垒）

把四个子图焊成一张可多跳推理的网。**所有关联都有明确证据，无相似度匹配**：

| 关系 | 链接方式 | 证据 | 存储 |
|------|---------|------|------|
| `commit ↔ bug` | 扫描 commit body 中的 `Fixes: bsc#N`、`Closes: bugzilla.kernel.org/...` | trailer 文本 | PG `link_commit_bug` |
| `commit ↔ LKML message` | 扫描 commit body 中的 `Link: https://lore.kernel.org/.../<msg-id>` | Link: trailer | PG `link_commit_message` |
| `commit ↔ CVE` | NVD fix_commits 字段中的 commit SHA ↔ kernel_commit.hash | NVD references 中的 git URL | PG `link_commit_cve` |
| `commit ↔ function` | git diff 文件 → CodeGraph `lookup_symbol` | 运行时解析，不持久化 | — |
| `stack frame ↔ function` | 函数名 → CodeGraph `lookup_symbol` | 运行时解析，不持久化 | — |
| `OLK commit ↔ upstream` | inclusion 头中的 mainline/stable SHA 锚点 | commit message 内嵌 | `kernel_commit.upstream_commit` |

**OLK commit 的特殊处理**：OLK 内核 ~78% 的 commit 是 backport，commit body 顶部有 inclusion 头：

```
mainline inclusion
from mainline-v6.12-rc1
commit d1877cc7270302081a   ← 上游 mainline SHA
category: bugfix
CVE: CVE-2024-50022
```

| 类型判定 | 桥接方式 |
|---------|---------|
| `olk_inclusion_type ∈ {mainline, stable}` | backport，有上游 SHA，可链到 kernel.org 生态 |
| 其他任意值（hulk / openEuler / 厂商名…）或 NULL | openEuler 原生，无上游讨论 |

**不要枚举 openEuler 原生类型**，它是开放集合（40+ 种）。只判断是否属于 `{mainline, stable}`。

**Spike 验证（2026-05-15）**：3 个真实案例（CVE-2024-35892 / mm OOM regression / CVE-2024-40998）人工走 KG 端到端链路 **3/3 走通**。关键发现：

| ID | 发现 | 落地 |
|----|------|------|
| F3 | Fixes 链是**嵌套多层**的（实证三层：fix → introduce-1 → introduce-2）| M4 §3.7 |
| F4 | `Fixes:` 用 short SHA（7-12 位），需三级兜底解析为 full SHA | M4 §3.1 |
| F5 | **部分 fix commit 没有 `Fixes:` trailer**，需反向关联 | M4 §3.8 |
| F6 | Stack frame 行号跨小版本漂移 ~20 行；禁止依赖行号，必须用函数名 + offset | ADR-008 |

---

## 3. 组件清单与职责

### 3.1 L1 诊断 Agent 层（M7）

| 组件 | 职责 | v1 状态 |
|------|------|--------|
| `agent/triage/` | LangGraph ReAct triage（输入解析、分类）| v1.2 |
| `agent/diagnosis/` | LangGraph Hypothesis-Driven 主诊断 | v1.2 |
| `agent/sop/*.yaml` | 故障域 SOP（v1 = 9 个：oom/lockup/panic/generic/deadlock/io_hang/network/perf_regression/sched_anomaly）| v1.2 |
| `agent/binding/` | Claim-Evidence Binding 强制 + Regenerate | v1.2 |
| `agent/report/` | Markdown + JSON 双格式生成 | v1.2 |

### 3.2 L2 主机 MCP 工具层（M6）

| MCP Server | 范围 | v1 状态 |
|-----------|------|--------|
| `mcp_servers/dmesg_journal/` | dmesg 解析（oops/OOM/lockup/lockdep）+ journalctl 查询 | v1.0 |
| `mcp_servers/sosreport/` | sosreport.tar.xz 解包 + 关键字段提取 | v1.0 |

不包含 perf / ftrace / vmcore 工具（[ADR-014](adr/ADR-014-m6-scope.md)）。

### 3.3 L3 检索编排层（M5）

| 组件 | 职责 |
|------|------|
| `retrieval/api.py` | `retrieve(query)` 主入口 |
| `retrieval/query_parser.py` | 全 LLM query 解析 |
| `retrieval/recall/*.py` | 7 路 always-fire-all（code/docs/lkml/bug/syzbot/commit/cve）|
| `retrieval/rerank.py` | LLM listwise rerank |

### 3.4 L4 知识层（M2 + M4）

| 组件 | 职责 |
|------|------|
| `storage/pg/` | 16 张表（lkml_* / bug / cve / syzbot_crash / kernel_commit / link_* / 系统表）|
| `storage/neo4j/` | Commit / Bug / Message / Thread / CVE 节点 + 关系 |
| `graph/linker.py` | Cross-Graph Linker（trailer 抽取 + patch-commit 反查）|
| `graph/neo4j_rebuild.py` | 周度全量 rebuild + 对账 |

### 3.5 L5 Ingestion 层（M3）

| Ingester | 数据源 | 频率 |
|---------|--------|------|
| `ingest/lkml/` | lore.kernel.org（2024-05 起 2 年）| 周度增量 |
| `ingest/bugzilla/` | bugzilla.kernel.org | 周度增量 |
| `ingest/syzbot/` | syzkaller.appspot.com HTML | 周度 |
| `ingest/zenodo/` | Zenodo 数据集 | 季度 |
| `ingest/nvd/` | NVD JSON Feed | 周度 |
| `ingest/kernel_commit/` | OLK 内核仓（OLK-6.6 / OLK-5.10）+ mainline 辅仓（[ADR-018](adr/ADR-018-commit-source-olk-kernel.md)）| 周度增量 |

### 3.6 L6 LLM Provider 抽象层（M1）

| Provider | 用途 |
|----------|------|
| `claude_code` | **MVP 默认**：包 `claude -p` subprocess |
| `openai_compat` | OpenAI / DeepSeek / Moonshot 等 |
| `vllm` | 本地大模型（v1.3 实跑）|
| `ollama` | 轻量本地（CI / 开发）|

**Chat / Navigator 双档**：Chat = Sonnet（主推理）；Navigator = Haiku（PageIndex 走查 + 重排）。

### 3.7 外部依赖：CodeGraph MCP 服务（[ADR-008](adr/ADR-008-reuse-codesearch.md)）

| CodeGraph 工具 | 用途 | 我方调用模块 |
|---------------|------|------------|
| `browse_docs` / `browse_doc_sections` / `read_doc_section` | PageIndex 文档导航 | M5 / M7 |
| `search_docs` | BM25 文档关键词 | M5 |
| `search_code` | Zoekt 代码全文 | M5 |
| `lookup_symbol` | SCIP 精确符号 | M5 / M7 |
| `get_outline` | tree-sitter 文件大纲 | M7 |
| `read_file` | 按路径读源码 | M7 |

**通信**：HTTP MCP（[ADR-012](adr/ADR-012-codesearch-http-transport.md)）

---

## 4. 接口契约

### 4.1 M1 LLM Provider（OpenAI Chat Completions schema）

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
    metadata: dict           # trace_id, sop_id, ...
```

### 4.2 M5 Retrieval API

```python
class RetrievalQuery:
    natural_language: str
    kernel_version: str | None      # 'v6.6' | 'v5.10'
    fault_domain: str | None
    stack_frames: list[str] | None
    error_keywords: list[str] | None
    routes: set[Route] | None       # None = 全部 7 路
    top_k: int = 10
    enable_rerank: bool = True
    trace_id: str

class Evidence:
    source: Literal['code', 'doc', 'lkml', 'bug', 'syzbot', 'commit', 'cve']
    ref: str                        # 规范引用
    snippet: str
    score: float
    cite: dict
```

### 4.3 M7 报告 JSON schema

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
  "fix_suggestions": {"short_term": [...], "medium_term": [...], "long_term": [...]}
}
```

---

## 5. 技术选型

| 组件 | 选型 | 选型理由 |
|------|------|---------|
| 关系数据库 | PostgreSQL 15+ | 通用、成熟、tsvector + GIN BM25 |
| 图数据库 | Neo4j Community 5+ | 生态成熟；NebulaGraph (Apache 2.0) 作为 v2 备选 |
| ~~向量库~~ | ~~FAISS~~ | **不部署**（[ADR-001](adr/ADR-001-no-embedding.md)）|
| BM25 | PostgreSQL `tsvector` + GIN | [ADR-006](adr/ADR-006-pg-tsvector-bm25.md)；v1.1 评估切 Tantivy |
| Agent 编排 | **LangGraph** | [ADR-015](adr/ADR-015-m7-diagnosis-strategy.md)；状态机 + tools + checkpointing 原生 |
| LLM Provider（MVP）| **claude_code provider** | [ADR-004](adr/ADR-004-claude-code-provider.md)；Claude Pro 订阅授权 |
| LLM Provider（本地）| vLLM | v1.3 切到本地后用 |
| codesearch (外部) | Zoekt + SCIP + PageIndex | [ADR-008](adr/ADR-008-reuse-codesearch.md)；OLK kernel 已验证 |
| ~~Embedding 模型~~ | ~~BGE-M3~~ | **不部署**（[ADR-001](adr/ADR-001-no-embedding.md)）|
| 日志 | JSONL 文件 | 取代 Prometheus；CLI grep / jq |
| Schema 迁移 | Alembic | PG schema 演进 |

**开源原则**：业务核心路径不依赖任何闭源服务（claude_code provider 是 MVP 受控例外，v1.3 可切本地 vLLM）。

---

## 6. 安全 & 隐私边界

- 用户上传的 sosreport / dmesg **仅本地解析**（M6 工具运行在 agent 主机）
- **v1.0-v1.2 MVP**：原始 vmcore / 完整 sosreport 不上传，仅结构化摘要进 LLM context
- **v1.3 全离线**：切到本地 vLLM，所有数据在企业内网
- 所有 MCP server **默认只读**；修复建议**不自动执行**

---

## 7. 部署拓扑

### 7.1 v1.0-v1.2 MVP 拓扑

```
                    ┌──────────────────────────────────────────────┐
                    │  开发者工作站 / 单台服务器                     │
                    │                                              │
                    │  ┌──────┐  ┌──────────────┐  ┌────────────┐ │
                    │  │ CLI  │  │ Linux-Diag-  │  │ PostgreSQL │ │
                    │  │      │  │ Agent (M1-M8)│  │ + Neo4j    │ │
                    │  └──────┘  └──────┬───────┘  └────────────┘ │
                    │                   │ HTTP MCP                 │
                    │                   ▼                          │
                    │              ┌──────────────┐                │
                    │              │ codesearch   │                │
                    │              │ MCP Server   │                │
                    │              └──────────────┘                │
                    └────────────────────┬─────────────────────────┘
                                         │
                                  ┌──────▼──────┐
                                  │ Claude Code │  (Pro 订阅 OAuth)
                                  └─────────────┘
```

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
       │       └──────────┘ └──────────┘                      │
       │  ★ 全部在内网；无对外网络                             │
       └──────────────────────────────────────────────────────┘
```

### 7.3 Ingestion 调度

```
cron (周日 03:00 UTC) → scripts/weekly_sync.sh
  ├─ Phase 1 并行：lkml / bugzilla / syzbot / nvd
  ├─ Phase 2：kernel_commit ETL
  ├─ Phase 3 (每月首周日)：zenodo
  ├─ Phase 4：Cross-Graph Linker
  └─ Phase 5：Neo4j rebuild + 对账
※ CodeGraph 自身 ingestion（Zoekt + SCIP + Sphinx）独立调度
```

---

## 8. 演进路径

| 版本 | 主题 | 关键里程碑 |
|------|------|-----------|
| v1.0 (M0-M3) | 知识库地基 + 检索 CLI | M3 ingestion 跑通；M5 retrieve 7 路；CodeGraph 集成 |
| v1.1 (M4-M6) | Cross-Graph + 路由 + 工具 | Cross-Graph Linker 全填；MAINTAINERS 解析；mcp-perf（如需）|
| v1.2 (M7-M9) | 诊断 Agent + 评测 | Triage + Diagnosis Agent；9 SOP；30 例完整人工评测 |
| v1.3 (M10-M12) | 生产化 + 离线化 | vLLM 本地部署；A/B 对照评测；Prometheus + Jaeger（如触发）|

详见 [ProjectPlan.md](ProjectPlan.md)。

---

## 9. 约束与权衡

| 约束 | 选择 | ADR |
|------|------|-----|
| 不做 embedding | 牺牲离线索引速度，换取可解释性 | [ADR-001](adr/ADR-001-no-embedding.md) |
| 不做 cross-encoder reranker | 用 LLM 重排替代；准确率上限更高，成本略增 | [ADR-002](adr/ADR-002-no-reranker.md) |
| 双版本 v6.6 + v5.10 | 维护 codesearch 两个 repo；存储与时间翻倍 | [ADR-009](adr/ADR-009-multi-version-via-codesearch-repos.md) |
| MVP 用 claude_code provider | 牺牲"v1.0 完全离线"承诺；保留 provider 抽象层 | [ADR-004](adr/ADR-004-claude-code-provider.md) |
| 仅 bugzilla.kernel.org | 失去 RHBZ 的 CVE 关联完整性；NVD 补强 | [ADR-011](adr/ADR-011-bugzilla-kernel-org-only.md) |
| LKML 2 年起步 | 失去 2020-2024 早期历史；可后续回补 | [ADR-010](adr/ADR-010-lkml-2y-bootstrap.md) |
| 无 vmcore 工具 | 失去深内核态钻取；其他故障类型不受影响 | [ADR-014](adr/ADR-014-m6-scope.md) |
| Always-fire-all 7 路召回 | 资源开销大；保障召回完整性 | [ADR-013](adr/ADR-013-m5-retrieval-strategy.md) |
| Claim-Evidence Binding Reject + Regenerate ≤2 | 平衡幻觉控制与配额 | [ADR-015](adr/ADR-015-m7-diagnosis-strategy.md) |
| v1.0 仅文件日志 | Ops 极简；v1.1+ 视触发条件升级 | [ADR-016](adr/ADR-016-m8-minimal-observability.md) |

**已知限制（v1 范围内不解决）**：

| 限制 | 原因 |
|------|------|
| 仅 OLK Kernel 6.6 + 5.10 | codesearch 当前覆盖 |
| LKML 只回溯 2024-05 起 2 年 | 全量 25 年抓取耗时 |
| MAINTAINERS 解析延后 | v1.0 用 file_path 推断够用 |
| Cross-Graph 链接置信度不区分等级 | v1 用二值（有/无）；v1.2 引入 weighted edges |

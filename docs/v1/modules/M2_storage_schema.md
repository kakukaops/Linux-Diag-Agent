# M2 — 存储层与 Schema 设计文档

> **⚠️ 设计规格文档**：本文档为实施前的原始设计规格（定稿于 2026-05-15）。实际实现以代码为准，两者可能存在偏差。如需了解当前实现状态，请阅读对应目录下的 `CLAUDE.md` 和源代码。


| 字段 | 值 |
|------|---|
| 模块编号 | M2 |
| 状态 | Design Locked（待实施，2026-05-14 后大改） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [M1](M1_llm_provider.md) · [Architecture](../Architecture.md) |
| 关联 ADR | [ADR-001](../adr/ADR-001-no-embedding.md) · [ADR-006](../adr/ADR-006-pg-tsvector-bm25.md) · [ADR-007](../adr/ADR-007-data-dir-in-repo.md) · [ADR-008](../adr/ADR-008-reuse-codesearch.md) · [ADR-009](../adr/ADR-009-multi-version-via-codesearch-repos.md) |
| 最后更新 | 2026-05-14（v2：因 [ADR-008](../adr/ADR-008-reuse-codesearch.md) 复用 codesearch 重写） |

---

## 1. 目标与边界

集中管理 Linux-Diag-Agent **自有的**持久化数据：LKML 邮件、Bug 报告、CVE、syzbot crash、commit 元数据 + Fixes: 关联、Cross-Graph Link、LLM 缓存、评测样本、运行记录。

**不在范围**（由 CodeGraph MCP 服务承担，见 [ADR-008](../adr/ADR-008-reuse-codesearch.md)）：

- 内核源码 全文 / 符号定义 / 调用关系（Zoekt + SCIP）
- 内核文档（kernel-doc / man-pages）解析与章节存储（CodeGraph `chunks` + Sphinx JSON backend）
- 跨 repo 代码文件读取（CodeGraph Zoekt `read_file`）

| 在范围 | 不在范围 |
|--------|---------|
| PostgreSQL schema（LKML / Bug / commit / 关联 / 系统表）| 内核源码 schema（codesearch 内）|
| Neo4j 图模型（commit / bug / message / CVE / 关系）| 函数 / 文档章节 节点（codesearch 内）|
| BM25 索引（PG tsvector + GIN，仅对我方数据）| Zoekt / SCIP（codesearch 内）|
| 文件存储约定（mbox / 评测样本 / vmcore-summary 等）| 内核源码 checkout 与文档 checkout（codesearch 数据盘）|
| 备份与恢复 | codesearch 自身备份（其 ops 手册负责）|
| Schema 迁移工具 | — |
| 并发与隔离 | — |

## 2. 关键决策摘要

| # | 决策 | ADR |
|---|------|-----|
| D1 | Code Graph + Doc Tree 复用 codesearch | [ADR-008](../adr/ADR-008-reuse-codesearch.md) |
| D2 | 多版本 = codesearch 两个独立 repo（`olk-kernel-v6.6` + `olk-kernel-v5.10`） | [ADR-009](../adr/ADR-009-multi-version-via-codesearch-repos.md)（supersedes ADR-005）|
| D3 | 我方 PG 内**不引入** `kernel_version` 列；版本通过 commit.affected_versions 数组或 codesearch repo 路由表达 | [ADR-009](../adr/ADR-009-multi-version-via-codesearch-repos.md) |
| D4 | Neo4j Community 5+ 用于 Cross-Graph 关系遍历（commit/bug/message/CVE）；不存内核函数节点 | 本文 §4 |
| D5 | BM25 = PG `tsvector` + GIN，仅作用于 LKML/Bug/syzbot 文本 | [ADR-006](../adr/ADR-006-pg-tsvector-bm25.md) |
| D6 | 数据目录 = `<repo>/data/`，可配置重定向 | [ADR-007](../adr/ADR-007-data-dir-in-repo.md) |
| D7 | PG 隔离级别 `READ COMMITTED`；ingestion 增量 + 断点续传 | 本文 §8 |
| D8 | 备份：PG 每日 `pg_dump` + WAL；Neo4j 每日 dump；文件每周 rsync | 本文 §7 |

## 3. PostgreSQL Schema

### 3.1 表总览（v2，简化后）

| 类别 | 表名 | 估算行数（v1.2 末）| 备注 |
|------|------|------------------|------|
| **Commit** | `kernel_commit` | ~1.2M | 含 Fixes: tag + affected_versions |
| **Discussion** | `lkml_message` | ~7M | |
|  | `lkml_thread` | ~3M | 含 LLM 三段式摘要 |
|  | `lkml_patch` | ~600K | |
|  | `lkml_review` | ~3M | Reviewed-by / Tested-by / Acked-by / NACK |
| **Bug** | `bug` | ~250K | 多源归一化 |
|  | `cve` | ~30K | NVD 同步 |
|  | `syzbot_crash` | ~80K | 含 reproducer |
| **关联** | `link_commit_bug` | ~200K | Fixes:/Reported-by:/Closes: 抽取 |
|  | `link_commit_message` | ~1.5M | patch ↔ commit |
| **系统** | `llm_response_cache` | 动态 | 由 M1 维护 |
|  | `ingest_runs` | ~500 | |
|  | `eval_cases` | 30 | |
|  | `eval_results` | ~300 | |

**总表数：14**（v1 设计中由 21 表，简化 33%）。

**已删除（CodeGraph 接管或不再需要）**：
- ❌ `kernel_function`, `kernel_macro`, `kernel_struct`, `kernel_field`, `kernel_file`（CodeGraph SCIP/Zoekt 接管）
- ❌ `doc_node`, `doc_content`（CodeGraph `chunks` 表接管，含 Sphinx JSON backend）
- ❌ `link_function_subsystem`（v1.0 使用 CodeGraph 的 file_path 推断子系统；MAINTAINERS 解析延后至 v1.1+）
- ❌ `link_stackframe_function`（运行时通过 CodeGraph `lookup_symbol(symbol=...)` 解析，不持久化）

### 3.2 Commit DDL

```sql
CREATE TABLE kernel_commit (
    hash            TEXT PRIMARY KEY,                 -- SHA1，全局唯一（OLK 或 mainline 的 commit）
    short_hash      TEXT GENERATED ALWAYS AS (substr(hash, 1, 12)) STORED,
    author_name     TEXT,
    author_email    TEXT,
    commit_date     TIMESTAMPTZ NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT,
    fixes_refs      TEXT[],                           -- 解析 Fixes: tag 得到的 hash 列表
    reported_by     TEXT[],
    closes_refs     TEXT[],
    subsystem       TEXT,                             -- 从 file_path 推断（M3 ingestion 填）
    origin          TEXT NOT NULL DEFAULT 'olk',      -- 'olk'（OLK 内核仓，主）| 'mainline'（kernel.org linux-stable.git 辅仓）—— ADR-018 D1/D2
    upstream_commit TEXT,                             -- mainline SHA：mainline inclusion 取头部 commit、stable inclusion 取 [Upstream commit]；NULL=原生/未解析 —— ADR-018 D3
    olk_inclusion_type TEXT,                          -- inclusion 头 tag（开放集合：mainline/stable/hulk/driver/urma/…）；NULL=无 inclusion 头 —— ADR-018 C1
    affected_versions TEXT[],                         -- OLK 分支列表，如 ['OLK-6.6','OLK-5.10']；origin='mainline' 时为 ['mainline']
    metadata        JSONB DEFAULT '{}'::jsonb,
    body_tsv        TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(body, ''))
    ) STORED
);
CREATE INDEX idx_kc_date ON kernel_commit (commit_date);
CREATE INDEX idx_kc_subsystem ON kernel_commit (subsystem);
CREATE INDEX idx_kc_fixes ON kernel_commit USING GIN (fixes_refs);
CREATE INDEX idx_kc_versions ON kernel_commit USING GIN (affected_versions);
CREATE INDEX idx_kc_upstream ON kernel_commit (upstream_commit);   -- 给 mainline SHA 反查 OLK backport
CREATE INDEX idx_kc_origin ON kernel_commit (origin);
CREATE INDEX idx_kc_body_fts ON kernel_commit USING GIN (body_tsv);
```

**注意**：
- commit 不带 `kernel_version` 列（commit hash 全局唯一），用 `affected_versions[]` 描述影响范围。
- **commit 来源双仓库（[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md)）**：诊断目标是 OLK 内核，故 `origin='olk'` 为主体（与 CodeGraph `olk-kernel` 同源）；`origin='mainline'` 为辅，仅作上游知识源。
- **OLK backport commit** 通过 `upstream_commit` 指向其内嵌的 mainline SHA，由此链回 mainline commit → LKML / bug / CVE 生态；**openEuler 原生 commit** 的 `upstream_commit` 为 NULL，诊断时标注「无上游讨论」。
- 旧字段 `is_in_mainline BOOLEAN` 已删除——其语义被 `upstream_commit IS NOT NULL` 完全覆盖且更精确（不仅知道"是否来自 mainline"，还知道"来自哪个 commit"）。

### 3.3 Discussion DDL（LKML）

```sql
-- 单封邮件
CREATE TABLE lkml_message (
    message_id      TEXT PRIMARY KEY,
    list_name       TEXT NOT NULL,
    subject         TEXT NOT NULL,
    from_name       TEXT,
    from_email      TEXT,
    to_emails       TEXT[],
    cc_emails       TEXT[],
    sent_date       TIMESTAMPTZ NOT NULL,
    in_reply_to     TEXT,
    references_chain TEXT[],
    body            TEXT,
    mbox_path       TEXT,
    thread_id       BIGINT,
    is_patch        BOOLEAN DEFAULT false,
    patch_version   INT,
    metadata        JSONB DEFAULT '{}'::jsonb,
    body_tsv        TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(subject, '') || ' ' || coalesce(body, ''))
    ) STORED
);
CREATE INDEX idx_lm_thread ON lkml_message (thread_id);
CREATE INDEX idx_lm_sent ON lkml_message (sent_date);
CREATE INDEX idx_lm_list ON lkml_message (list_name);
CREATE INDEX idx_lm_in_reply ON lkml_message (in_reply_to);
CREATE INDEX idx_lm_from_email ON lkml_message (from_email);
CREATE INDEX idx_lm_body_fts ON lkml_message USING GIN (body_tsv);

-- 线程
CREATE TABLE lkml_thread (
    id              BIGSERIAL PRIMARY KEY,
    root_message_id TEXT NOT NULL REFERENCES lkml_message(message_id),
    subject         TEXT NOT NULL,
    list_name       TEXT NOT NULL,
    start_date      TIMESTAMPTZ NOT NULL,
    last_activity   TIMESTAMPTZ NOT NULL,
    message_count   INT,
    is_patch_series BOOLEAN DEFAULT false,
    summary         TEXT,                             -- LLM 三段式摘要
    summary_model   TEXT,
    summary_at      TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'::jsonb,
    summary_tsv     TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(summary, '') || ' ' || subject)
    ) STORED
);
CREATE INDEX idx_lt_root ON lkml_thread (root_message_id);
CREATE INDEX idx_lt_activity ON lkml_thread (last_activity);
CREATE INDEX idx_lt_summary_fts ON lkml_thread USING GIN (summary_tsv);

-- patch metadata
CREATE TABLE lkml_patch (
    id              BIGSERIAL PRIMARY KEY,
    message_id      TEXT NOT NULL REFERENCES lkml_message(message_id),
    series_id       TEXT,
    patch_index     INT,
    patch_total     INT,
    fixes_refs      TEXT[],
    reported_by     TEXT[],
    affected_files  TEXT[],
    landed_commit   TEXT,
    metadata        JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX idx_lp_message ON lkml_patch (message_id);
CREATE INDEX idx_lp_fixes ON lkml_patch USING GIN (fixes_refs);
CREATE INDEX idx_lp_landed ON lkml_patch (landed_commit);

-- 评审信号
CREATE TABLE lkml_review (
    id              BIGSERIAL PRIMARY KEY,
    target_message_id TEXT NOT NULL REFERENCES lkml_message(message_id),
    review_message_id TEXT REFERENCES lkml_message(message_id),
    reviewer_name   TEXT,
    reviewer_email  TEXT,
    review_type     TEXT NOT NULL,                    -- 'reviewed-by' | 'tested-by' | 'acked-by' | 'nack'
    extracted_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_lr_target ON lkml_review (target_message_id);
CREATE INDEX idx_lr_reviewer ON lkml_review (reviewer_email);
```

### 3.4 Bug Graph DDL

```sql
-- 统一 bug 表
CREATE TABLE bug (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,                    -- 'bugzilla.kernel.org' | 'bugzilla.redhat.com' | 'syzbot' | 'debian' | 'launchpad'
    source_id       TEXT NOT NULL,
    title           TEXT NOT NULL,
    description     TEXT,
    status          TEXT,
    severity        TEXT,
    product         TEXT,
    component       TEXT,
    affected_kernel_versions TEXT[],
    reporter        TEXT,
    created_at      TIMESTAMPTZ,
    resolved_at     TIMESTAMPTZ,
    fix_commit_hashes TEXT[],
    cve_ids         TEXT[],
    raw_metadata    JSONB DEFAULT '{}'::jsonb,
    body_tsv        TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(title, '') || ' ' || coalesce(description, ''))
    ) STORED,
    UNIQUE (source, source_id)
);
CREATE INDEX idx_bug_status ON bug (status);
CREATE INDEX idx_bug_component ON bug (product, component);
CREATE INDEX idx_bug_kver ON bug USING GIN (affected_kernel_versions);
CREATE INDEX idx_bug_fix_commits ON bug USING GIN (fix_commit_hashes);
CREATE INDEX idx_bug_cve ON bug USING GIN (cve_ids);
CREATE INDEX idx_bug_body_fts ON bug USING GIN (body_tsv);

-- CVE
CREATE TABLE cve (
    cve_id          TEXT PRIMARY KEY,
    description     TEXT,
    cvss_score      NUMERIC,
    cvss_vector     TEXT,
    published_at    TIMESTAMPTZ,
    affected_kernel_versions TEXT[],
    fix_commits     TEXT[],
    raw_nvd         JSONB
);

-- syzbot 特殊字段
CREATE TABLE syzbot_crash (
    id              BIGSERIAL PRIMARY KEY,
    crash_id        TEXT NOT NULL UNIQUE,
    bug_id          BIGINT REFERENCES bug(id),
    crash_type      TEXT,                             -- 'KASAN' | 'OOPS' | 'WARNING' | 'lockdep' | ...
    crash_signature TEXT,                             -- 栈签名，规范化栈帧 sha256
    stack_top_function TEXT,
    full_report     TEXT,
    reproducer_c    TEXT,
    reproducer_syz  TEXT,
    kernel_config   TEXT,
    first_seen      TIMESTAMPTZ,
    last_seen       TIMESTAMPTZ,
    metadata        JSONB DEFAULT '{}'::jsonb,
    report_tsv      TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english', coalesce(full_report, '') || ' ' || coalesce(crash_signature, ''))
    ) STORED
);
CREATE INDEX idx_sc_bug ON syzbot_crash (bug_id);
CREATE INDEX idx_sc_signature ON syzbot_crash (crash_signature);
CREATE INDEX idx_sc_stack ON syzbot_crash (stack_top_function);
CREATE INDEX idx_sc_report_fts ON syzbot_crash USING GIN (report_tsv);
```

### 3.5 跨图关联表

```sql
-- commit ↔ bug
CREATE TABLE link_commit_bug (
    id              BIGSERIAL PRIMARY KEY,
    commit_hash     TEXT NOT NULL REFERENCES kernel_commit(hash),
    bug_id          BIGINT NOT NULL REFERENCES bug(id),
    link_type       TEXT NOT NULL,                    -- 'fixes' | 'reported_by' | 'closes' | 'caused_by'
    confidence      NUMERIC DEFAULT 1.0,
    source          TEXT,                             -- 'fixes_tag' | 'zenodo_dataset' | 'manual'
    extracted_at    TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (commit_hash, bug_id, link_type)
);
CREATE INDEX idx_lcb_commit ON link_commit_bug (commit_hash);
CREATE INDEX idx_lcb_bug ON link_commit_bug (bug_id);

-- commit ↔ LKML message
CREATE TABLE link_commit_message (
    id              BIGSERIAL PRIMARY KEY,
    commit_hash     TEXT NOT NULL REFERENCES kernel_commit(hash),
    message_id      TEXT NOT NULL REFERENCES lkml_message(message_id),
    link_type       TEXT NOT NULL,                    -- 'patch_origin' | 'discussion' | 'review'
    UNIQUE (commit_hash, message_id, link_type)
);
CREATE INDEX idx_lcm_commit ON link_commit_message (commit_hash);
CREATE INDEX idx_lcm_message ON link_commit_message (message_id);
```

**注意**：
- 不存 `link_function_subsystem`：子系统通过 commit/bug 的 file_path 推断，运行时算
- 不存 `link_stackframe_function`：调用 CodeGraph `lookup_symbol` 运行时解析

### 3.6 系统表

```sql
-- LLM 响应缓存（M1）
CREATE TABLE llm_response_cache (
    cache_key       TEXT PRIMARY KEY,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    schema_version  INT NOT NULL DEFAULT 1,
    response        JSONB NOT NULL,
    usage           JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    last_hit_at     TIMESTAMPTZ DEFAULT NOW(),
    hit_count       INT DEFAULT 0,
    cost_saved_usd  NUMERIC DEFAULT 0
);
CREATE INDEX idx_lrc_created ON llm_response_cache (created_at);
CREATE INDEX idx_lrc_last_hit ON llm_response_cache (last_hit_at);

-- Ingestion 运行
CREATE TABLE ingest_runs (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,                    -- 'lkml' | 'bugzilla.kernel.org' | 'syzbot' | 'kernel-commit' | 'zenodo' | 'nvd'
    started_at      TIMESTAMPTZ NOT NULL,
    completed_at    TIMESTAMPTZ,
    status          TEXT NOT NULL,                    -- 'running' | 'success' | 'failed'
    items_processed INT DEFAULT 0,
    items_added     INT DEFAULT 0,
    items_updated   INT DEFAULT 0,
    items_failed    INT DEFAULT 0,
    error_message   TEXT,
    checkpoint_data JSONB
);
CREATE INDEX idx_ir_source ON ingest_runs (source, started_at);

-- 评测案例
CREATE TABLE eval_cases (
    id              TEXT PRIMARY KEY,                 -- 'case-001', ...
    title           TEXT NOT NULL,
    fault_domain    TEXT NOT NULL,
    kernel_version  TEXT NOT NULL,                    -- 'v6.6' | 'v5.10'，仅用于映射到 CodeGraph repo
    source          TEXT,
    source_url      TEXT,
    input_artifacts JSONB,
    ground_truth    JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- 评测结果
CREATE TABLE eval_results (
    id              BIGSERIAL PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES eval_cases(id),
    run_id          TEXT NOT NULL,
    agent_version   TEXT NOT NULL,
    llm_provider    TEXT NOT NULL,
    llm_model       TEXT NOT NULL,
    self_consistency_k INT,
    diagnosis_output JSONB,
    rubric_scores   JSONB,
    rubric_judges   JSONB,
    overall_score   NUMERIC,
    completed_at    TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX idx_er_case ON eval_results (case_id);
CREATE INDEX idx_er_run ON eval_results (run_id);
```

## 4. Neo4j 图模型（简化版）

仅承担 Linux-Diag-Agent **自有**实体之间的关系遍历（commit、bug、message、CVE、thread）。**不存内核函数节点**（那是 codesearch 的领域）。

### 4.1 节点定义

| Label | 关键属性 | PG join 字段 |
|-------|---------|------------|
| `Commit` | `hash`（自然键）, `short_hash`, `subject`, `date`, `subsystem` | `kernel_commit.hash` |
| `Bug` | `source`, `source_id`（复合自然键）, `pg_id`, `title`, `status` | `bug.id = pg_id` |
| `Message` | `message_id`（自然键）, `list`, `date`, `is_patch` | `lkml_message.message_id` |
| `Thread` | `pg_id`, `root_message_id`, `subject` | `lkml_thread.id = pg_id` |
| `CVE` | `id`（自然键）, `cvss` | `cve.cve_id = id` |
| `Subsystem` | `name`（自然键） | — |

**不存 Function / StackFrame 节点** — Function 信息来自 CodeGraph SCIP，StackFrame 信息运行时计算。

### 4.2 关系定义

```cypher
// commit ↔ bug
(c:Commit)-[:FIXES {confidence, source}]->(b:Bug)
(c:Commit)-[:INTRODUCED_BUG]->(b:Bug)

// commit ↔ message
(c:Commit)-[:DISCUSSED_IN]->(m:Message)
(c:Commit)-[:PATCH_FROM]->(m:Message)

// commit ↔ subsystem
(c:Commit)-[:BELONGS_TO]->(s:Subsystem)

// 邮件结构
(m:Message)-[:IN_REPLY_TO]->(m2:Message)
(m:Message)-[:IN_THREAD]->(t:Thread)
(m:Message)-[:REVIEWS {type: 'reviewed-by'|'tested-by'|'acked-by'|'nack'}]->(m2:Message)

// bug ↔ CVE / message
(b:Bug)-[:HAS_CVE]->(cve:CVE)
(b:Bug)-[:REPORTED_IN]->(m:Message)

// thread ↔ commit
(t:Thread)-[:RESOLVED_TO]->(c:Commit)
```

### 4.3 索引与约束

```cypher
CREATE CONSTRAINT commit_unique IF NOT EXISTS
  FOR (c:Commit) REQUIRE c.hash IS UNIQUE;
CREATE CONSTRAINT bug_unique IF NOT EXISTS
  FOR (b:Bug) REQUIRE (b.source, b.source_id) IS UNIQUE;
CREATE CONSTRAINT message_unique IF NOT EXISTS
  FOR (m:Message) REQUIRE m.message_id IS UNIQUE;
CREATE CONSTRAINT cve_unique IF NOT EXISTS
  FOR (cve:CVE) REQUIRE cve.id IS UNIQUE;
CREATE CONSTRAINT subsystem_unique IF NOT EXISTS
  FOR (s:Subsystem) REQUIRE s.name IS UNIQUE;

CREATE INDEX commit_subsystem IF NOT EXISTS
  FOR (c:Commit) ON (c.subsystem);
CREATE INDEX message_date IF NOT EXISTS
  FOR (m:Message) ON (m.date);
```

### 4.4 典型查询

**"给定一段 oops 栈，找到相关 commit + 已修 bug + LKML 讨论"**：

```
Step 1: 客户端代码调用 CodeGraph lookup_symbol(stack_top_function) → 取得 SCIP 符号 + file_path
Step 2: 客户端代码用 file_path 推断 subsystem（如 "mm/oom_kill.c" → "mm"）
Step 3: 在 Neo4j 查询：
    MATCH (s:Subsystem {name: 'mm'})<-[:BELONGS_TO]-(c:Commit)
    OPTIONAL MATCH (c)-[:FIXES]->(b:Bug)
    OPTIONAL MATCH (c)-[:DISCUSSED_IN]->(m:Message)
    WHERE c.date > date('2024-01-01')
    RETURN c, b, m
    ORDER BY c.date DESC
    LIMIT 50
Step 4: 回 PG 查 commit/bug/message 详情
```

Code Graph 部分（Step 1-2）完全走 CodeGraph；图谱遍历部分（Step 3）走 Neo4j；详情回查走 PG。

## 5. BM25 文本检索（仅作用于我方数据）

我方 PG 中所有大文本表（`kernel_commit.body`、`lkml_message.body`、`lkml_thread.summary`、`bug.description`、`syzbot_crash.full_report`）都有 `*_tsv` 列 + GIN 索引。

**代码与文档的 BM25 检索由 CodeGraph 提供**（其 chunks 表 + Zoekt），我们不重复实现。

## 6. 文件存储约定（[ADR-007](../adr/ADR-007-data-dir-in-repo.md)）

```
<repo>/data/                          ★ 数据根（gitignored）
├── lkml/<year>/<month>/*.mbox.gz     LKML 增量保存
├── bugzilla/<source>/<year>/*.json
├── syzbot/<crash_id>/
│   ├── report.txt
│   ├── reproducer.c
│   ├── reproducer.syz
│   └── config
├── sosreport/                        用户提交样本
├── vmcore-summary/                   解析后摘要 JSON
└── backup/                           本地备份

注意：以下数据由 codesearch 维护，不在我方 data/ 内：
- 内核源码 checkout（v6.6 + v5.10）
- 内核文档 Sphinx JSON 构建产物
- Zoekt 索引文件
- SCIP 索引文件
```

## 7. 备份与恢复

| 数据 | 频率 | 工具 | 保留 |
|------|------|------|------|
| 我方 PostgreSQL | 每日 | `pg_dump -Fc` + WAL archiving | 7 日 + 月底归档 12 月 |
| 我方 Neo4j | 每日 | `neo4j-admin database dump` | 7 日 + 月底归档 12 月 |
| 我方 `<repo>/data/` 文件 | 每周 | `rsync` | 4 周 |
| codesearch 自身数据 | 由 codesearch ops 手册负责 | — | — |
| Schema migrations | 实时 | git | 永久 |

**RTO / RPO**：4 小时 / 1 天

## 8. 并发与隔离

PG 默认 `READ COMMITTED`：
- 读不阻塞写、写不阻塞读
- ingestion 用 `INSERT ... ON CONFLICT ... DO UPDATE` 增量写
- 不需要临时表 swap（数据量未到必要级别）

Neo4j 多版本并发；写事务期间读不阻塞。

文件并发：mbox 单 ingestion 进程写、agent 多进程读、备份 rsync 与 ingestion 不冲突。

## 9. Schema 迁移管理

**PostgreSQL**：Alembic

```
storage/migrations/
├── alembic.ini
├── env.py
└── versions/
    ├── 0001_initial_schema.py        ★ v2 schema（无 kernel_* 表，无 doc_* 表）
    ├── 0002_add_eval_tables.py
    └── ...
```

**Neo4j**：自建 cypher 脚本，记录已应用版本到 PG `neo4j_migrations` 表。

## 10. 容量与扩容路径（修订）

### 10.1 单机容量预估

| 阶段 | PG（我方）| Neo4j（我方）| 文件（我方）| codesearch 总盘 | 模型权重 | 主机总计 |
|------|----|-------|------|----|------|-----|
| v1.0 末 | ~15 GB | ~3 GB | ~30 GB | ~30 GB | 0 | **~80 GB** |
| v1.2 末 | ~40 GB | ~8 GB | ~80 GB | ~40 GB | 0 | **~170 GB** |
| v1.3 末 | ~50 GB | ~10 GB | ~80 GB | ~50 GB | ~150 GB | **~340 GB** |

### 10.2 硬件建议

| 配置 | 建议 |
|------|------|
| 主盘 | 500 GB NVMe SSD（PG + Neo4j + codesearch + 模型权重）|
| 备份盘 | 1 TB HDD |
| RAM | 64 GB |
| CPU | 16 核 |

### 10.3 扩容触发

| 信号 | 动作 |
|------|------|
| PG 单表 > 5000 万行 | 评估 partitioning |
| Neo4j > 1 亿节点 | 评估 NebulaGraph |
| 文件存储 > 500 GB | 加盘或对象存储 |
| codesearch BM25 ranking 不达预期 | 切 Tantivy（见 [ADR-006](../adr/ADR-006-pg-tsvector-bm25.md)）|

## 11. 文件结构

```
storage/
├── __init__.py
├── pg/
│   ├── __init__.py
│   ├── models.py
│   ├── connection.py
│   └── queries/
├── neo4j/
│   ├── __init__.py
│   ├── client.py
│   └── queries/
├── filestore/
│   ├── __init__.py
│   ├── paths.py
│   └── mbox_handler.py
├── migrations/
│   ├── alembic.ini
│   ├── env.py
│   └── versions/
│       └── 0001_initial_schema.py
└── tests/
    ├── unit/
    └── integration/

clients/
└── codegraph/                        ★ CodeGraph MCP 客户端
    ├── __init__.py
    ├── client.py                     # MCP stdio/HTTP wrapper
    ├── repos.py                      # repo 名映射 (v6.6 / v5.10 → CodeGraph repo)
    └── healthcheck.py

scripts/
├── init_db.sh
├── backup_daily.sh
├── backup_files.sh
├── migrate.sh
└── restore.sh
```

## 12. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| GIN 索引膨胀 | `pg_total_relation_size` | 月度 VACUUM FULL |
| CodeGraph 不可用 | MCP `list_repos` ping | 启动时检测；优雅降级（无源码场景仍能跑 LKML/Bug 检索） |
| CodeGraph repo 列表与我们配置不一致 | 启动时校验 olk-kernel-v6.6/v5.10 | 报错并要求人工解决 |
| commit 与 LKML/Bug 不一致 | 周度对账（commit.fixes_refs ↔ link_commit_bug 行数）| 重跑 Cross-Graph Linker |
| Neo4j 死锁 | error log | Cypher MERGE 重试 |
| Schema 漂移 | 启动时 Alembic check | 自动 migrate or 报错 |

## 13. v1.0 → v1.3 演进

| 阶段 | 主要变化 |
|------|---------|
| v1.0 | 全部我方 schema 落地（14 表）；LKML/Bug/syzbot ingestion 跑通；与 CodeGraph 建立 MCP 集成 |
| v1.1 | Cross-Graph Linker 全填；评测 PG/Neo4j 查询性能 |
| v1.2 | 评测结果回填 `eval_results`；schema 优化；CodeGraph 集成稳定化 |
| v1.3 | 评估 codesearch 是否需要补充新工具（如 commit history 暴露）；本地 vLLM 上线 |

---

## 附：与 v1（原版）的主要差异

| 项 | v1 原版 | v2（本版）|
|----|---------|----------|
| 表数 | 21 | **14**（删 5 个 kernel_* + 2 个 doc_* + link_function_subsystem + link_stackframe_function = 9 个；增量 0）|
| `kernel_version` 列 | 有（在 kernel_function 等 5 张表）| 无（删除所有 kernel_* 表后不需要）|
| Neo4j Function 节点 | 有 | 无 |
| Neo4j StackFrame 节点 | 有 | 无（运行时通过 CodeGraph 解析） |
| FAISS / 嵌入索引 | 无（ADR-001）| 无（同）|
| Code Graph 由 | 自建 Tree-sitter + libclang | **CodeGraph SCIP + Zoekt** |
| Doc Tree 由 | 自建 PageIndex + Sphinx 解析 | **CodeGraph chunks + Sphinx JSON backend** |
| 双版本（v6.6/v5.10）隔离 | PG 单库 + kernel_version 列（[ADR-005](../adr/ADR-005-single-db-multi-version.md)，已 superseded）| codesearch 两个独立 repo（[ADR-009](../adr/ADR-009-multi-version-via-codesearch-repos.md)）|

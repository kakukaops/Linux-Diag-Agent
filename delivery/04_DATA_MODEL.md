# 04 · Data Model

> 16 + 4 个 PG 表的完整 DDL 契约。AI agent 重建 schema 时**严格按此列名 / 类型 / 索引 / 约束**——下游 SQL 全部依赖这些细节。
>
> 不直接给 SQLAlchemy 代码，而是 vendor-neutral SQL DDL + 关键索引——AI 可翻译到 SQLAlchemy / Drizzle / Diesel / sqlx 等。
>
> 建议用 Alembic 或等价 migration 工具分 14 个增量 migration（参考原仓库 `storage/pg/migrations/versions/0001..0014_*.py`）。

---

## 表清单

```
内容表（5）        系统表（5）        关系表（7）         评测表（2）
─────────         ─────────         ─────────          ─────────
kernel_commit     llm_response_     link_commit_bug    eval_cases
lkml_thread       cache             link_commit_       eval_results
lkml_message      ingest_runs       message
lkml_patch        dmesg_event       link_commit_cve
lkml_review       fault_taxonomy    link_commit_symbol
bug               maintainers       link_commit_fixes
cve                                 link_commit_revert
syzbot_crash                        link_syzbot_commit
```

总计 19 张表（5 内容 + 5 系统 + 7 关系 + 2 评测）。

---

## A. 内容表

### A.1 `kernel_commit`

```sql
CREATE TABLE kernel_commit (
    hash             TEXT PRIMARY KEY,                  -- full 40-hex SHA
    short_hash       TEXT GENERATED ALWAYS AS
                     (substring(hash, 1, 12)) STORED,    -- B-tree 索引
    subject          TEXT NOT NULL,
    body             TEXT,                              -- 可能 NULL（stub commit）
    author_name      TEXT,
    author_email     TEXT,
    commit_date      TIMESTAMPTZ,
    inclusion_type   TEXT,                              -- 'mainline'/'stable'/NULL/厂商 tag
    upstream_commit  TEXT,                              -- 当 inclusion_type ∈ {mainline,stable} 时
    affected_versions TEXT[],                            -- ['OLK-6.6', 'OLK-5.10']
    subsystem        TEXT,                              -- 'mm', 'mm/memcg', 'net/ipv4' etc.
    fixes_refs       TEXT[],                            -- body 中 'Fixes: <sha>' trailer 抽出
    body_tsv         TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(subject, '') || ' ' || coalesce(body, ''))
    ) STORED
);

CREATE INDEX ix_kc_body_tsv         ON kernel_commit USING GIN (body_tsv);
CREATE INDEX ix_kc_short_hash       ON kernel_commit (short_hash);
CREATE INDEX ix_kc_commit_date      ON kernel_commit (commit_date);
CREATE INDEX ix_kc_subsystem        ON kernel_commit (subsystem);
CREATE INDEX ix_kc_inclusion        ON kernel_commit (inclusion_type);
CREATE INDEX ix_kc_upstream         ON kernel_commit (upstream_commit) WHERE upstream_commit IS NOT NULL;
CREATE INDEX ix_kc_affected_versions ON kernel_commit USING GIN (affected_versions);
```

### A.2 `lkml_thread`

```sql
CREATE TABLE lkml_thread (
    id                BIGSERIAL PRIMARY KEY,
    root_message_id   TEXT UNIQUE NOT NULL,
    subject           TEXT NOT NULL,
    list_name         TEXT NOT NULL,                    -- 'linux-mm', 'stable', etc.
    started_at        TIMESTAMPTZ,
    last_activity_at  TIMESTAMPTZ,
    message_count     INT DEFAULT 0,
    -- LLM-summarised (lazy)
    summary_problem   TEXT,
    summary_solution  TEXT,
    summary_outcome   TEXT
);
CREATE INDEX ix_lt_list           ON lkml_thread (list_name);
CREATE INDEX ix_lt_last_activity  ON lkml_thread (last_activity_at);
```

### A.3 `lkml_message`

```sql
CREATE TABLE lkml_message (
    id              BIGSERIAL PRIMARY KEY,
    message_id      TEXT UNIQUE NOT NULL,                -- RFC822 Message-ID
    thread_id       BIGINT REFERENCES lkml_thread(id),
    in_reply_to     TEXT,
    author_name     TEXT,
    author_email    TEXT,
    date            TIMESTAMPTZ,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    fetched_via     TEXT,                                -- 'atom' | 'raw' | 'mbox'
    cached_at       TIMESTAMPTZ DEFAULT now(),
    stack_signature TEXT,                                -- 当 body 含 call trace 时计算
    body_tsv        TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(subject,'') || ' ' || coalesce(body,''))
    ) STORED
);
CREATE INDEX ix_lm_body_tsv     ON lkml_message USING GIN (body_tsv);
CREATE INDEX ix_lm_thread       ON lkml_message (thread_id);
CREATE INDEX ix_lm_date         ON lkml_message (date);
CREATE INDEX ix_lm_signature    ON lkml_message (stack_signature) WHERE stack_signature IS NOT NULL;
```

### A.4 `lkml_patch`（subject 匹配 `[PATCH ...]` 的邮件）

```sql
CREATE TABLE lkml_patch (
    patch_id            BIGINT PRIMARY KEY REFERENCES lkml_message(id),
    files_changed       TEXT[],
    insertions          INT,
    deletions           INT,
    series_index        INT,                              -- "[PATCH 3/7]" → 3
    series_total        INT                               -- "[PATCH 3/7]" → 7
);
CREATE INDEX ix_lp_files ON lkml_patch USING GIN (files_changed);
```

### A.5 `lkml_review`（Reviewed-by / Acked-by / NACK 的邮件）

```sql
CREATE TABLE lkml_review (
    id              BIGSERIAL PRIMARY KEY,
    message_id      BIGINT REFERENCES lkml_message(id),
    review_type     TEXT NOT NULL,                       -- 'reviewed-by'/'acked-by'/'nack'/...
    reviewer_email  TEXT NOT NULL,
    reviewer_name   TEXT
);
CREATE INDEX ix_lr_message ON lkml_review (message_id);
```

### A.6 `bug`

```sql
CREATE TABLE bug (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL,                       -- 'kernel.org' | 'gitee' | 'atomgit'
    external_id     TEXT NOT NULL,                       -- bugzilla id / gitee issue number
    UNIQUE (source, external_id),
    title           TEXT NOT NULL,                       -- ✗ 不是 summary
    status          TEXT,                                -- 'open' | 'fixed' | 'closed' | etc.
    component       TEXT,
    subsystem       TEXT,
    severity        TEXT,
    reporter        TEXT,
    assignee        TEXT,
    created_at      TIMESTAMPTZ,
    updated_at      TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    kernel_versions TEXT[],                              -- 'OLK-6.6', 'OLK-5.10', etc.
    description     TEXT,                                -- 主体内容
    resolution      TEXT,
    stack_signature TEXT,
    body_tsv        TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title,'') || ' ' || coalesce(description,''))
    ) STORED
);
CREATE INDEX ix_bug_body_tsv     ON bug USING GIN (body_tsv);
CREATE INDEX ix_bug_source       ON bug (source);
CREATE INDEX ix_bug_subsystem    ON bug (subsystem);
CREATE INDEX ix_bug_updated_at   ON bug (updated_at);
CREATE INDEX ix_bug_signature    ON bug (stack_signature) WHERE stack_signature IS NOT NULL;
```

### A.7 `cve`

```sql
CREATE TABLE cve (
    cve_id           TEXT PRIMARY KEY,
    description      TEXT,
    published        TIMESTAMPTZ,
    last_modified    TIMESTAMPTZ,
    cvss_v3_score    REAL,                               -- ✗ 不是 cvss_score / severity
    cvss_v3_vector   TEXT,
    "references"     JSONB,                              -- ⚠ PG 保留字必须双引号
    fix_commits      JSONB,                              -- 从 references 抽的 commit SHA 数组
    body_tsv         TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(cve_id,'') || ' ' || coalesce(description,''))
    ) STORED
);
CREATE INDEX ix_cve_body_tsv  ON cve USING GIN (body_tsv);
CREATE INDEX ix_cve_published ON cve (published);
```

### A.8 `syzbot_crash`

```sql
CREATE TABLE syzbot_crash (
    id                BIGSERIAL PRIMARY KEY,
    syzbot_id         TEXT UNIQUE NOT NULL,
    title             TEXT NOT NULL,
    status            TEXT,                              -- 'open'/'fixed'/'invalid'/'dup'
    subsystem         TEXT,
    first_seen        TIMESTAMPTZ,
    last_seen         TIMESTAMPTZ,
    fix_commit        TEXT,                              -- legacy 单 commit
    fix_commits       JSONB,                             -- v2 多 commit 列表
    reproducer_c      TEXT,
    reproducer_syz    TEXT,
    kernel_config_url TEXT,
    stack_trace       TEXT,
    stack_signature   TEXT,
    body_tsv          TSVECTOR GENERATED ALWAYS AS (
        to_tsvector('english',
            coalesce(title,'') || ' ' || coalesce(stack_trace,''))
    ) STORED
);
CREATE INDEX ix_sc_body_tsv     ON syzbot_crash USING GIN (body_tsv);
CREATE INDEX ix_sc_signature    ON syzbot_crash (stack_signature) WHERE stack_signature IS NOT NULL;
CREATE INDEX ix_sc_subsystem    ON syzbot_crash (subsystem);
CREATE INDEX ix_sc_last_seen    ON syzbot_crash (last_seen);
```

---

## B. 关系表（7 张）

通用约定：
- 每张表都有 `id BIGSERIAL PRIMARY KEY`
- 每张表都有 `link_type TEXT NOT NULL`（区分证据类型，如 `'trailer'` / `'llm_inferred'` / `'nvd_ref'`）
- 每张表都有 `confidence REAL NOT NULL` ∈ [0,1]
- 每张表都有 `source TEXT NOT NULL`（证据来源）
- 每张表都有 `created_at TIMESTAMPTZ DEFAULT now()`
- 每张表都有 **复合 unique constraint** 保证幂等

### B.1 `link_commit_bug`

```sql
CREATE TABLE link_commit_bug (
    id           BIGSERIAL PRIMARY KEY,
    commit_hash  TEXT NOT NULL REFERENCES kernel_commit(hash),
    bug_id       BIGINT NOT NULL REFERENCES bug(id),
    link_type    TEXT NOT NULL,                          -- 'trailer'/'llm_inferred'
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,                          -- 'trailer'/'llm'
    created_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_lcb UNIQUE (commit_hash, bug_id, link_type)
);
CREATE INDEX ix_lcb_commit ON link_commit_bug (commit_hash);
CREATE INDEX ix_lcb_bug    ON link_commit_bug (bug_id);
```

### B.2 `link_commit_message`

```sql
CREATE TABLE link_commit_message (
    id           BIGSERIAL PRIMARY KEY,
    commit_hash  TEXT NOT NULL REFERENCES kernel_commit(hash),
    message_id   TEXT NOT NULL,                          -- lkml_message.message_id (TEXT)
    link_type    TEXT NOT NULL,                          -- 'trailer'/'subject_match'
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_lcm UNIQUE (commit_hash, message_id, link_type)
);
CREATE INDEX ix_lcm_commit  ON link_commit_message (commit_hash);
CREATE INDEX ix_lcm_message ON link_commit_message (message_id);
```

### B.3 `link_commit_cve`

```sql
CREATE TABLE link_commit_cve (
    id           BIGSERIAL PRIMARY KEY,
    commit_hash  TEXT NOT NULL REFERENCES kernel_commit(hash),
    cve_id       TEXT NOT NULL REFERENCES cve(cve_id),
    link_type    TEXT NOT NULL,                          -- 'nvd_ref'/'trailer'
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,
    created_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_lcc UNIQUE (commit_hash, cve_id, link_type)
);
CREATE INDEX ix_lcc_commit ON link_commit_cve (commit_hash);
CREATE INDEX ix_lcc_cve    ON link_commit_cve (cve_id);
```

### B.4 `link_commit_symbol`

```sql
CREATE TABLE link_commit_symbol (
    id           BIGSERIAL PRIMARY KEY,
    commit_hash  TEXT NOT NULL REFERENCES kernel_commit(hash),
    symbol       TEXT NOT NULL,                          -- 函数名（含 !sentinel 占位行）
    change_type  TEXT,                                   -- 'added'/'removed'/'modified'
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,                          -- 'git_diff'
    created_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_lcs UNIQUE (commit_hash, symbol)
);
CREATE INDEX ix_lcs_commit ON link_commit_symbol (commit_hash);
CREATE INDEX ix_lcs_symbol ON link_commit_symbol (symbol);
```

> **sentinel 行**：见 `60_GOTCHAS.md` §6.2。merge commit 或抽不到有效符号的 commit **必须**写一行 `symbol='!sentinel'`，否则增量 backfill 死循环。

### B.5 `link_commit_fixes`

```sql
CREATE TABLE link_commit_fixes (
    id           BIGSERIAL PRIMARY KEY,
    commit_hash  TEXT NOT NULL REFERENCES kernel_commit(hash),    -- the fix
    fixes_hash   TEXT NOT NULL,                                    -- the buggy commit
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,                                    -- 'fixes_trailer'
    created_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_lcf UNIQUE (commit_hash, fixes_hash)
);
CREATE INDEX ix_lcf_commit ON link_commit_fixes (commit_hash);
CREATE INDEX ix_lcf_fixes  ON link_commit_fixes (fixes_hash);
```

### B.6 `link_commit_revert`

```sql
CREATE TABLE link_commit_revert (
    id           BIGSERIAL PRIMARY KEY,
    revert_hash  TEXT NOT NULL REFERENCES kernel_commit(hash),    -- the revert commit
    target_hash  TEXT NOT NULL,                                    -- what was reverted
    reason       TEXT,                                             -- 引自 revert body
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,                                    -- 'revert_subject'/'revert_body'
    created_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_lcr UNIQUE (revert_hash, target_hash)
);
CREATE INDEX ix_lcr_revert ON link_commit_revert (revert_hash);
CREATE INDEX ix_lcr_target ON link_commit_revert (target_hash);
```

### B.7 `link_syzbot_commit`

```sql
CREATE TABLE link_syzbot_commit (
    id           BIGSERIAL PRIMARY KEY,
    syzbot_id    TEXT NOT NULL REFERENCES syzbot_crash(syzbot_id),
    commit_hash  TEXT NOT NULL REFERENCES kernel_commit(hash),
    confidence   REAL NOT NULL DEFAULT 1.0,
    source       TEXT NOT NULL,                                    -- 'syzbot_ref'
    created_at   TIMESTAMPTZ DEFAULT now(),
    CONSTRAINT uq_lsc UNIQUE (syzbot_id, commit_hash, source)
);
CREATE INDEX ix_lsc_syzbot ON link_syzbot_commit (syzbot_id);
CREATE INDEX ix_lsc_commit ON link_syzbot_commit (commit_hash);
```

---

## C. 系统表

### C.1 `llm_response_cache`

```sql
CREATE TABLE llm_response_cache (
    cache_key       TEXT PRIMARY KEY,                    -- sha256(model + sha256(messages_json))
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_hash     TEXT NOT NULL,
    response        JSONB NOT NULL,                      -- 完整 ChatResponse dict
    tokens_in       INT,
    tokens_out      INT,
    cost_usd        REAL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    ttl_until       TIMESTAMPTZ NOT NULL                 -- 默认 created_at + 30 days
);
CREATE INDEX ix_lrc_ttl ON llm_response_cache (ttl_until);
```

### C.2 `ingest_runs`（**只在 run 结束写**）

```sql
CREATE TABLE ingest_runs (
    id              BIGSERIAL PRIMARY KEY,
    ingester        TEXT NOT NULL,
    status          TEXT NOT NULL,                       -- 'success'/'partial'/'failed'
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ NOT NULL,
    rows_processed  INT,
    rows_inserted   INT,
    rows_skipped    INT,
    errors          JSONB,
    checkpoint      JSONB                                -- ingester-specific cursor
);
CREATE INDEX ix_ir_ingester    ON ingest_runs (ingester);
CREATE INDEX ix_ir_finished_at ON ingest_runs (finished_at);
```

### C.3 `dmesg_event`（实时诊断时持久化的 dmesg 事件）

```sql
CREATE TABLE dmesg_event (
    event_id          TEXT PRIMARY KEY,                  -- uuid-like
    ingested_at       TIMESTAMPTZ DEFAULT now(),
    source            TEXT,                              -- 'web' / 'cli' / case_id
    fault_kind        TEXT,                              -- oom/panic/lockup/...
    kernel_version    TEXT,
    olk_version_tag   TEXT,
    raw_text          TEXT,
    call_trace        TEXT,
    stack_signature   TEXT,
    metadata          JSONB
);
CREATE INDEX ix_de_signature  ON dmesg_event (stack_signature) WHERE stack_signature IS NOT NULL;
CREATE INDEX ix_de_fault_kind ON dmesg_event (fault_kind);
```

### C.4 `fault_taxonomy`（可选；fault_kind 与子系统映射）

```sql
CREATE TABLE fault_taxonomy (
    id            BIGSERIAL PRIMARY KEY,
    fault_kind    TEXT NOT NULL,
    sop_name      TEXT NOT NULL,
    subsystem_hint TEXT,
    description   TEXT
);
```

### C.5 `maintainers`（可选；MAINTAINERS 文件解析后的 cache）

```sql
CREATE TABLE maintainers (
    id           BIGSERIAL PRIMARY KEY,
    section      TEXT NOT NULL,                          -- 'AMD GPU DRIVERS', etc.
    status       TEXT,                                   -- 'Maintained'/'Orphan'/'Odd Fixes'/...
    list         TEXT,                                   -- mailing list
    file_pattern TEXT NOT NULL,                          -- 'F: drivers/gpu/drm/amd/'
    maintainers  TEXT[]                                  -- ['Name <email>', ...]
);
CREATE INDEX ix_mt_pattern ON maintainers (file_pattern);
```

---

## D. 评测表

### D.1 `eval_cases`

```sql
CREATE TABLE eval_cases (
    case_id              TEXT PRIMARY KEY,
    category             TEXT,
    raw_input            TEXT NOT NULL,
    expected_route       TEXT,
    expected_fault_kind  TEXT,
    primary_goal         TEXT,
    ground_truth_commits TEXT[],
    ground_truth_bugs    BIGINT[],
    ground_truth_root_cause TEXT,
    expected_kg_paths    TEXT[],
    notes                TEXT
);
```

### D.2 `eval_results`

```sql
CREATE TABLE eval_results (
    id                  BIGSERIAL PRIMARY KEY,
    run_id              TEXT NOT NULL,                   -- 一次 eval 跑的标识
    case_id             TEXT NOT NULL REFERENCES eval_cases(case_id),
    started_at          TIMESTAMPTZ,
    elapsed_ms          REAL,
    -- 流程指标（PRIMARY）
    phase_coverage      REAL,
    kg_path_coverage    REAL,
    -- triage 指标
    route_correct       BOOLEAN,
    fault_kind_correct  BOOLEAN,
    -- 副指标
    recall_at_10        REAL,
    groundedness        TEXT,
    judge_correct       BOOLEAN,
    judge_reasoning     TEXT,
    -- 全 result JSON
    full_result         JSONB
);
CREATE INDEX ix_er_run_id  ON eval_results (run_id);
CREATE INDEX ix_er_case_id ON eval_results (case_id);
```

---

## E. 必备 PG 扩展

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;     -- 模糊文本匹配 fallback
CREATE EXTENSION IF NOT EXISTS btree_gin;   -- 数组 + B-tree 复合索引
```

---

## F. 数据规模基线（2026-06 快照）

| 表 | 行数 |
|---|---|
| kernel_commit | 1.5 M |
| lkml_message | 115 K |
| lkml_thread | ~30 K（按 thread 聚合） |
| lkml_patch | ~25 K |
| bug | 8 K |
| cve | 16 K |
| syzbot_crash | 6.8 K |
| link_commit_bug | 58 K |
| link_commit_message | 38 K |
| link_commit_cve | 17 K |
| link_commit_symbol | ~3 M（含 sentinel 行） |
| link_commit_fixes | ~80 K |
| link_commit_revert | ~5 K |
| link_syzbot_commit | 13 K |
| llm_response_cache | 累积变化 |
| ingest_runs | ~500（每周 6 行 × 几月） |

总磁盘：**约 12-20 GB**（视 LKML body 平均长度）。

---

## G. 验收

```sql
-- 19 张表都存在
SELECT count(*) FROM information_schema.tables
 WHERE table_schema = 'public'
   AND table_name IN (
   'kernel_commit', 'lkml_thread', 'lkml_message', 'lkml_patch', 'lkml_review',
   'bug', 'cve', 'syzbot_crash',
   'link_commit_bug', 'link_commit_message', 'link_commit_cve',
   'link_commit_symbol', 'link_commit_fixes', 'link_commit_revert',
   'link_syzbot_commit',
   'llm_response_cache', 'ingest_runs', 'dmesg_event',
   'eval_cases', 'eval_results');
-- 期望 = 20（含 dmesg_event）

-- 所有 body_tsv 是 GENERATED STORED
SELECT count(*) FROM information_schema.columns
 WHERE column_name = 'body_tsv' AND is_generated = 'ALWAYS';
-- 期望 = 5（kernel_commit / lkml_message / bug / cve / syzbot_crash）

-- 所有 5 张内容表都有 GIN body_tsv 索引
SELECT count(*) FROM pg_indexes
 WHERE indexdef LIKE '%USING gin%body_tsv%';
-- 期望 = 5
```

完整 grep 见 `M2_storage_schema.md § 8`。

---

> AI agent 重建时：每个表的 DDL 复制到 migration；CREATE 顺序按 §A→§B→§C→§D（B 表引用 A 表的 FK）。完成后跑 § G 三段 SQL 全部 PASS。

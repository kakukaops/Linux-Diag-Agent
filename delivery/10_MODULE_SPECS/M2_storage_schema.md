# M2 · Storage Schema

## 1. 目的

提供持久化层：**PostgreSQL 16** 作为主存储（16 张表），**Neo4j**（可选）作为图谱拓扑视图。

**WHY PG 作主存而不是 Neo4j**：内核诊断 95 % 的查询是"按关键字 BM25 + 按版本/时间过滤"，PG `tsvector` + GIN 索引完胜图查询。Neo4j 只做"4 跳关系展开"这类拓扑查询，主数据漂移风险大，所以**只做派生视图**，定期从 PG 重建（见 M4 `neo4j_rebuild.py`）。

## 2. 16 张表（按子图分组）

```
── Commit Graph ────────────────────────────────────────
kernel_commit       OLK + mainline + stable git commit (PK: hash TEXT)

── Discussion Graph (LKML) ─────────────────────────────
lkml_thread         邮件线程
lkml_message        单封邮件（body_tsv GIN）
lkml_patch          Patch 邮件（files_changed, series_total）
lkml_review         Reviewed-by / Acked-by / NACK

── Bug Graph ───────────────────────────────────────────
bug                 Bugzilla / gitee / atomgit issue（body_tsv GIN）
cve                 NVD CVE（cvss_v3_score, fix_commits JSONB, body_tsv GIN）
syzbot_crash        syzbot 崩溃（stack_signature, body_tsv GIN）

── Cross-Graph Links（详见 M4） ────────────────────────
link_commit_bug          commit ↔ bug                unique uq_lcb
link_commit_message      commit ↔ lkml_message       unique uq_lcm
link_commit_cve          commit ↔ cve                unique uq_lcc
link_commit_symbol       commit ↔ symbol             (commit_hash, symbol)
link_commit_fixes        commit ↔ commit (Fixes:)    (commit_hash, fixes_hash)
link_commit_revert       commit ↔ commit (revert)    (revert_hash, target_hash)
link_syzbot_commit       syzbot ↔ kernel_commit      (syzbot_id, commit_hash)

── System Tables ──────────────────────────────────────
llm_response_cache  LLM 响应缓存 (cache_key, provider, model, response JSONB)
ingest_runs         Ingestion 审计 (ingester, status, checkpoint JSONB)
dmesg_event         实时 dmesg 事件持久化 (event_id, stack_signature)
eval_cases          评测数据集 (case_id, ground_truth_*)
eval_results        评测结果（5 维评分）
```

## 3. 关键表字段契约

> 完整 DDL 见 `04_DATA_MODEL.md`。这里只列**易写错的列名**和**生成列**。

### 3.1 `kernel_commit`（主 PK 设计）

```sql
hash                TEXT PRIMARY KEY,        -- 40-hex full SHA
short_hash          TEXT GENERATED ALWAYS AS (substring(hash, 1, 12)) STORED,
                                             -- B-tree 索引；大幅提速 short-SHA JOIN
subject             TEXT NOT NULL,
body                TEXT,                    -- 可能 NULL（stub commit）
author_name         TEXT,
author_email        TEXT,
commit_date         TIMESTAMPTZ,
inclusion_type      TEXT,                    -- 'mainline' / 'stable' / NULL / 其它（厂商 tag）
upstream_commit     TEXT,                    -- 当 inclusion_type ∈ {mainline,stable} 时指向上游 SHA
affected_versions   TEXT[],                  -- ['OLK-6.6', 'OLK-5.10'] 等
subsystem           TEXT,                    -- 'mm', 'net', 'fs/ext4', 'mm/memcg' 等
fixes_refs          TEXT[],                  -- commit body 中 'Fixes: <sha>' trailer 抽出
body_tsv            TSVECTOR GENERATED ALWAYS AS
    (to_tsvector('english', coalesce(subject,'') || ' ' || coalesce(body,''))) STORED
                                             -- GIN 索引：CREATE INDEX ix_kc_body_tsv ON kernel_commit USING GIN(body_tsv)
```

### 3.2 `lkml_message`

```sql
id              BIGSERIAL PRIMARY KEY,
message_id      TEXT UNIQUE NOT NULL,        -- RFC822 Message-ID
thread_id       BIGINT REFERENCES lkml_thread(id),
in_reply_to     TEXT,
author_name     TEXT,
author_email    TEXT,
date            TIMESTAMPTZ,
subject         TEXT NOT NULL,
body            TEXT NOT NULL,
body_tsv        TSVECTOR GENERATED ...,      -- GIN
fetched_via     TEXT,                        -- 'atom' / 'raw' / 'mbox'
cached_at       TIMESTAMPTZ DEFAULT now(),
stack_signature TEXT                         -- 当 body 含 call trace 时计算
```

### 3.3 `cve`

```sql
cve_id           TEXT PRIMARY KEY,
description      TEXT,
published        TIMESTAMPTZ,
last_modified    TIMESTAMPTZ,
cvss_v3_score    REAL,                       -- ✗ 不是 cvss_score / severity
cvss_v3_vector   TEXT,
"references"     JSONB,                      -- ⚠ PG 保留字必须双引号
fix_commits      JSONB,                      -- 从 references 抽的 commit SHA 列表
body_tsv         TSVECTOR GENERATED ...
```

### 3.4 `bug`

```sql
id              BIGSERIAL PRIMARY KEY,
source          TEXT NOT NULL,               -- 'kernel.org' / 'gitee' / 'atomgit'
external_id     TEXT NOT NULL,
                UNIQUE (source, external_id),
title           TEXT NOT NULL,               -- ✗ 不是 summary
status          TEXT,
component       TEXT,
subsystem       TEXT,
description     TEXT,                        -- 主体内容
created_at      TIMESTAMPTZ,
updated_at      TIMESTAMPTZ,
body_tsv        TSVECTOR GENERATED ...
```

### 3.5 `syzbot_crash`

```sql
id                  BIGSERIAL PRIMARY KEY,
syzbot_id           TEXT UNIQUE NOT NULL,
title               TEXT NOT NULL,
status              TEXT,                    -- 'open' / 'fixed' / 'invalid' / 'closed as dup'
subsystem           TEXT,
first_seen          TIMESTAMPTZ,
last_seen           TIMESTAMPTZ,
fix_commit          TEXT,                    -- legacy 单 commit 字段（兼容）
fix_commits         JSONB,                   -- v2 多 commit 列表
reproducer_c        TEXT,
reproducer_syz      TEXT,
kernel_config_url   TEXT,
stack_trace         TEXT,
stack_signature     TEXT,                    -- top-4 distinguishing function names hash
body_tsv            TSVECTOR GENERATED ...
```

### 3.6 7 张 link 表通用 schema

每张 link 表：
- 复合 unique constraint（防重复插入）
- `confidence` REAL 0-1
- `source` TEXT（证据来源类型，如 `'trailer'` / `'llm_inferred'` / `'nvd_ref'`）
- `created_at` TIMESTAMPTZ DEFAULT now()

| 表 | 列 | unique 名 |
|---|---|---|
| `link_commit_bug` | `(commit_hash, bug_id, link_type)` | `uq_lcb` |
| `link_commit_message` | `(commit_hash, message_id, link_type)` | `uq_lcm` |
| `link_commit_cve` | `(commit_hash, cve_id, link_type)` | `uq_lcc` |
| `link_commit_symbol` | `(commit_hash, symbol)` | `uq_lcs` |
| `link_commit_fixes` | `(commit_hash, fixes_hash)` | `uq_lcf` |
| `link_commit_revert` | `(revert_hash, target_hash)` | `uq_lcr` |
| `link_syzbot_commit` | `(syzbot_id, commit_hash, source)` | `uq_lsc` |

### 3.7 `llm_response_cache`

```sql
cache_key       TEXT PRIMARY KEY,            -- sha256(model + sha256(messages_json))
provider        TEXT NOT NULL,               -- 'openai_compat' / 'claude_code' / 'vllm'
model           TEXT NOT NULL,
prompt_hash     TEXT NOT NULL,
response        JSONB NOT NULL,              -- 完整 ChatResponse dict
tokens_in       INT,
tokens_out      INT,
cost_usd        REAL,
created_at      TIMESTAMPTZ DEFAULT now(),
ttl_until       TIMESTAMPTZ NOT NULL         -- 默认 created_at + 30 days
```

### 3.8 `ingest_runs`（**只在 run 结束时写**）

```sql
id              BIGSERIAL PRIMARY KEY,
ingester        TEXT NOT NULL,               -- 'lkml' / 'nvd' / 'bugzilla' / 'syzbot' / 'kernel_commit' / 'gitee' / 'atomgit'
status          TEXT NOT NULL,               -- 'success' / 'partial' / 'failed'
started_at      TIMESTAMPTZ NOT NULL,
finished_at     TIMESTAMPTZ NOT NULL,
rows_processed  INT,
rows_inserted   INT,
rows_skipped    INT,
errors          JSONB,                       -- 错误明细
checkpoint      JSONB                        -- 见 M3 § 3 各 ingester 的 schema
```

## 4. 索引清单

| 索引 | 类型 | 用途 |
|---|---|---|
| `kernel_commit(body_tsv)` | GIN | BM25 |
| `kernel_commit(short_hash)` | B-tree | short-SHA JOIN（M4 link_nvd_commits 用） |
| `kernel_commit(commit_date)` | B-tree | 时间窗口过滤 |
| `kernel_commit(affected_versions)` | GIN | `:ver = ANY(...)` 查询 |
| `lkml_message(body_tsv)` | GIN | BM25 |
| `lkml_message(thread_id)` | B-tree | thread roll-up |
| `lkml_message(date)` | B-tree | 时间窗口 |
| `lkml_message(stack_signature)` | B-tree | find_similar_crashes |
| `bug(body_tsv)` | GIN | BM25 |
| `bug(stack_signature)` | B-tree | find_similar_crashes |
| `cve(body_tsv)` | GIN | BM25 |
| `syzbot_crash(body_tsv)` | GIN | BM25 |
| `syzbot_crash(stack_signature)` | B-tree | find_similar_crashes |
| `link_commit_*(commit_hash)` | B-tree | reverse lookup |
| `link_commit_symbol(symbol)` | B-tree | symbol → commits |
| `link_commit_revert(target_hash)` | B-tree | "X 是否被 revert" |
| `llm_response_cache(ttl_until)` | B-tree | 过期清理 |

## 5. Migration 策略

用 Alembic。当前 head = `0014_link_syzbot_commit`。每个 migration 单一职责：

```
0001_initial_schema.py            16 张表 + GIN 索引 + body_tsv 生成列
0002_link_commit_cve.py           CVE 关系表
0003_cve_fix_commits.py           cve.fix_commits 列
0004_dmesg_event.py
0005_bug_subsystem.py
...
0014_link_syzbot_commit.py        最新
```

升级：`alembic -c storage/pg/alembic.ini upgrade head`。

## 6. 行为契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **body_tsv 是生成存储列**：`GENERATED ALWAYS AS ... STORED`，**不要**用普通列 + 触发器 | trigger 维护成本高，PG 12+ 原生 generated 列开销低 |
| C2 | **upsert 必须用 `ON CONFLICT ... DO UPDATE`**：所有 ingester 都是幂等的，重跑不重复插入 | 数据修复 + 增量回填的前提 |
| C3 | **`ingest_runs` 不写 'pending'**：只在 ingester 完成时插入 success / failed 行 | 它是审计日志不是调度器；见 `60_GOTCHAS.md` §1.3 |
| C4 | **link 表必须有 confidence + source 字段**：不区分这两个，下游无法过滤"高置信"vs"启发式" | 见 M4 各 link 表 derivation |
| C5 | **stub commit 在 kernel_commit 表里存活**（不删）：M4 linker 用 stub 填补 upstream chain；M5 查询时显式过滤 | 删 stub 会丢 upstream link |

## 7. 已知陷阱（M2 独有）

### #1 · PG 16 必须开 `pg_trgm` 扩展

部分 fallback 模糊匹配（如 `subject ILIKE '%foo%'` 优化）依赖 `pg_trgm` GIN 索引。初始化 schema 前先：

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS btree_gin;
```

### #2 · `tsvector` 列需要触发 ANALYZE 才稳定

新建表插入数据后，PG 优化器没统计信息会选错索引（seq scan）。**migration 完成后跑一次** `VACUUM ANALYZE` 是必要的。

### #3 · `affected_versions[]` 用 GIN 索引，查询用 `= ANY`

```sql
-- ✓ 走 GIN
WHERE 'OLK-6.6' = ANY(affected_versions)

-- ✗ 不走索引
WHERE affected_versions @> ARRAY['OLK-6.6']    -- 也对但不优
WHERE 'OLK-6.6' IN (SELECT unnest(affected_versions))    -- 全表扫
```

> **see also**：`60_GOTCHAS.md` §1（PG / psycopg3 整章）

## 8. 验收

### Schema 完整性

```sql
SELECT COUNT(*) FROM information_schema.tables
 WHERE table_schema = 'public' AND table_name IN (
   'kernel_commit', 'lkml_thread', 'lkml_message', 'lkml_patch', 'lkml_review',
   'bug', 'cve', 'syzbot_crash',
   'link_commit_bug', 'link_commit_message', 'link_commit_cve',
   'link_commit_symbol', 'link_commit_fixes', 'link_commit_revert',
   'link_syzbot_commit',
   'llm_response_cache', 'ingest_runs', 'dmesg_event',
   'eval_cases', 'eval_results'
);
-- 期望 = 20（16 核心 + 4 系统/eval/dmesg）
```

### 生成列 + 索引存在

```sql
-- 所有 body_tsv 列都是 GENERATED + STORED
SELECT table_name, column_name FROM information_schema.columns
 WHERE column_name = 'body_tsv' AND is_generated = 'ALWAYS';
-- 期望: kernel_commit, lkml_message, bug, cve, syzbot_crash

-- GIN 索引覆盖所有 body_tsv
SELECT indexname FROM pg_indexes
 WHERE indexdef LIKE '%USING gin%body_tsv%';
-- 期望: 5 个索引
```

### 关键操作幂等

跑 ingester 两次同一时段，第二次 `rows_inserted = 0`，`ingest_runs` 多一行 success。

## 9. 上下游

| 关系 | 模块 |
|---|---|
| **依赖** | PostgreSQL 16 + pg_trgm + btree_gin 扩展 |
| **依赖**（可选） | Neo4j（仅当用 M4 拓扑视图时） |
| **被调用** | M3 全部 ingester（写） / M4 linker（读 + 写 link 表）/ M5 retrieval（读）/ M7 agent（读 + 写 llm_response_cache）/ M8 eval（写 eval_results）|
| **配置 keys** | `storage.pg.dsn` / `storage.neo4j.{uri,user,password}` |

---

> **重建校对**：跑 `alembic upgrade head` 完成后，运行 § 8 三段 SQL 全部通过；用 `cases_smoke.json` 的 raw_input 调一次 M5 retrieve，确认 evidence pool 非空。

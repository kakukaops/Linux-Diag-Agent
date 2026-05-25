# storage/ — 数据库层

完整 ORM 定义见 @storage/pg/models.py。

## 16 张表速览

```
── Commit Graph ─────────────────────────────────────────────────────────
kernel_commit       OLK/mainline git commit（主 PK: hash TEXT）

── Discussion Graph（LKML）─────────────────────────────────────────────
lkml_thread         邮件线程（root_message_id, summary_problem/solution/outcome）
lkml_message        单封邮件（message_id, thread_id FK, body_tsv GIN）
lkml_patch          Patch 邮件（patch_id, files_changed, series_total）
lkml_review         Reviewed-by / Acked-by / NACK（review_type, reviewer_email）

── Bug Graph ────────────────────────────────────────────────────────────
bug                 Bugzilla bug（source, external_id, title, body_tsv GIN）
cve                 NVD CVE（cve_id, cvss_v3_score, fix_commits JSONB, body_tsv GIN）
syzbot_crash        syzbot 崩溃（syzbot_id, stack_signature, body_tsv GIN）

── Cross-Graph Links（M4）──────────────────────────────────────────────
link_commit_bug     commit ↔ bug     unique: (commit_hash, bug_id, link_type)   → uq_lcb
link_commit_message commit ↔ lkml   unique: (commit_hash, message_id, link_type) → uq_lcm
link_commit_cve     commit ↔ CVE    unique: (commit_hash, cve_id, link_type)    → uq_lcc

── System Tables ────────────────────────────────────────────────────────
llm_response_cache  LLM 响应缓存（cache_key, provider, model, response JSONB）
ingest_runs         Ingestion 审计（ingester, status, checkpoint JSONB）
eval_cases          评测数据集（case_id, ground_truth_commits, ground_truth_bugs）
eval_results        评测结果（5维评分: root_cause/evidence/fix/readability/safety）
```

## 关键列名（容易写错）

| 表 | 正确列名 | 错误写法 |
|----|---------|---------|
| `cve` | `cvss_v3_score`、`cvss_v3_vector` | `cvss_score`、`severity` |
| `bug` | `title` | `summary` |
| `kernel_commit` | `subject`、`body` | `message`、`description` |
| `link_commit_bug` | unique constraint = `uq_lcb` | — |
| `link_commit_cve` | unique constraint = `uq_lcc` | — |

## BM25 全文检索

所有含大文本的表均有 `body_tsv TSVECTOR` 列（DB 生成，GIN 索引）。**始终通过 body_tsv 查询，不要 inline `to_tsvector()`**：

```sql
-- 正确（走 GIN 索引）
WHERE body_tsv @@ to_tsquery('english', :q)

-- 错误（全表扫描）
WHERE to_tsvector('english', description) @@ to_tsquery('english', :q)
```

## SQL 约定

- jsonb 参数：`CAST(:name AS jsonb)`，不能用 `:name::jsonb`（psycopg3 解析失败）
- `"references"` 列名必须双引号（PG 保留字）
- 所有 upsert：`ON CONFLICT (...) DO UPDATE SET` 或 `DO NOTHING`

## Migrations

```bash
alembic -c storage/pg/alembic.ini upgrade head   # 应用所有迁移
alembic -c storage/pg/alembic.ini revision --autogenerate -m "描述"  # 新建迁移
```

迁移文件在 `storage/pg/migrations/versions/`，命名规则 `NNNN_<desc>.py`。当前最新：`0003_cve_fix_commits.py`。

## Engine 获取

```python
from storage.pg.engine import get_engine
engine = get_engine()  # 单例，使用 configs/ 中的 DSN
```

Neo4j 连接在 `storage/neo4j/`（v1.0 阶段仅用于图谱拓扑，主数据在 PG）。

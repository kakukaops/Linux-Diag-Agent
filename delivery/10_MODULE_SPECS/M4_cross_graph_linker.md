# M4 · Cross-Graph Linker

## 1. 目的

把 4 张内容表（kernel_commit / lkml_message / bug / cve / syzbot_crash）织成可多跳推理的关系网。**所有关联必须有明确文本证据**——不用相似度匹配，全靠 trailer / URL / 显式 SHA 引用。

## 2. 7 张派生关系表

| 表 | 边语义 | 数据来源 |
|---|---|---|
| `link_commit_bug` | commit 修了哪些 bug | commit body 中的 `Fixes: bsc#N` / `bugzilla.kernel.org/.../N` / `gitee.com/openeuler/kernel/issues/NNN` |
| `link_commit_message` | commit 对应哪封 LKML patch 邮件 | commit body 中的 `Link: https://lore.kernel.org/.../<msg-id>` |
| `link_commit_cve` | commit 修了哪些 CVE | `cve.fix_commits[]` JSONB 反向 join + `Fixes: CVE-...` trailer |
| `link_commit_symbol` | commit 改了哪些内核符号 | git diff hunk 中 `+/-static <type> <name>(...)` 的函数符号抽取 |
| `link_commit_fixes` | commit 修复了哪个先前 commit | `Fixes: <12+ hex>` trailer |
| `link_commit_revert` | commit 是对哪个先前 commit 的 revert | subject 含 `Revert "..."` + body 中 `This reverts commit <sha>` |
| `link_syzbot_commit` | syzbot crash 被哪些 commit 修过 | `syzbot_crash.fix_commits[]` JSONB（来自 syzbot 详情页） |

## 3. 公共接口

```python
def run_linker(engine: Engine, *, batch_size: int = 1000) -> LinkReport:
    """主入口：依次跑下面 3 步。"""

def link_nvd_commits(engine: Engine, *, batch_size: int | None = None) -> int:
    """独立跑：CVE.fix_commits → link_commit_cve."""

def link_syzbot_commits(engine: Engine) -> int:
    """独立跑：syzbot_crash.fix_commits → link_syzbot_commit."""

def link_gitee_atomgit_bugs(engine: Engine) -> dict[str, int]:
    """ADR-022：OLK commit 引用 gitee/atomgit issues → link_commit_bug."""

@dataclass
class LinkReport:
    commit_to_bug: int               # 新增行数
    commit_to_message: int
    olk_upstream_stub: int           # _link_olk_upstream 新建 stub 数
    started_at: datetime
    finished_at: datetime
```

## 4. `run_linker()` 三步详解

### 4.1 第 1 步 · trailer → bug

扫 `kernel_commit.body` 中的 bugzilla 引用：

```
正则匹配:
  Fixes:\s+bsc#(\d+)                                              -- SUSE bsc#NNN
  bugzilla\.kernel\.org/show_bug\.cgi\?id=(\d+)                   -- 主 Bugzilla
  gitee\.com/openeuler/kernel/issues/(\d+|I[A-Z0-9]+)              -- gitee issue
  atomgit\.com/openeuler/kernel/issues/(\d+)                       -- atomgit issue
```

每个匹配 → 查 `bug` 表是否有对应 `(source, external_id)` → 有就 `INSERT INTO link_commit_bug (commit_hash, bug_id, link_type='trailer', source='trailer', confidence=1.0) ON CONFLICT DO NOTHING`。

**降级**：未匹配到 bug 行 → 不写 link（不要写"挂空"边）。

### 4.2 第 2 步 · `Link:` trailer → lkml_message

```
扫 commit.body 中:
  Link:\s+https://lore\.kernel\.org/[^/]+/(<message-id>)/?
  Link:\s+https://lore\.kernel\.org/<list>/<message-id>/
```

抽出 `message_id` 串，查 `lkml_message` → 有就写 `link_commit_message`。

**未命中**时**降级用 subject 模糊匹配**：
- 取 commit subject
- 找 lkml_message 里 subject 包含 commit subject（or vice versa）且时间窗 ±60 天
- 模糊命中标 `link_type='subject_match'`, `confidence=0.5`

### 4.3 第 3 步 · OLK upstream 桥接

对 OLK commit（`inclusion_type ∈ {mainline, stable}`）：
- 读其 `upstream_commit` 字段
- 若 `upstream_commit` 不在 `kernel_commit` 表中（Phase B linux-stable 还没 ingest 到该 commit），**插入 stub 行**：

```sql
INSERT INTO kernel_commit (hash, subject, body, commit_date, inclusion_type)
VALUES (
  :upstream_full_sha,
  '[stub upstream for OLK ' || :olk_short_sha || ']',
  NULL,
  '1970-01-01'::timestamptz,            -- 哨兵日期
  NULL                                   -- inclusion 留空
);
```

**WHY**：让"OLK commit ↔ upstream commit"图谱连续，即使 mainline ingester 没 ingest 完。

⚠ stub 的 subject 格式**固定**为 `[stub upstream for OLK <hash[:12]>]` —— 对账脚本依赖此格式识别 stub。**不要改**。

## 5. `link_nvd_commits()` 详解

NVD 的 `cve.fix_commits[]` JSONB 数组里是 short SHA（8-40 hex）。

```sql
-- 走 short_hash B-tree 索引（不要用 LIKE，慢 100×）
INSERT INTO link_commit_cve (commit_hash, cve_id, link_type, source, confidence)
SELECT kc.hash, c.cve_id, 'nvd_ref', 'nvd', 1.0
  FROM cve c, jsonb_array_elements_text(c.fix_commits) sha
  JOIN kernel_commit kc
    ON kc.short_hash = substring(lower(sha), 1, 12)
   AND kc.subject NOT LIKE '[stub upstream%'    -- 不要 link 到 stub
ON CONFLICT ON CONSTRAINT uq_lcc DO NOTHING;
```

**性能基线**：~17 K 行写入耗时 ≤ 30 s（之前用 LIKE 跑 45 min；改成 short_hash JOIN 后秒级）。

## 6. `link_syzbot_commits()` 详解

类似 `link_nvd_commits`，但源是 `syzbot_crash.fix_commits[]`：

```sql
INSERT INTO link_syzbot_commit (syzbot_id, commit_hash, source, confidence)
SELECT s.syzbot_id, kc.hash, 'syzbot_ref', 1.0
  FROM syzbot_crash s, jsonb_array_elements_text(s.fix_commits) sha
  JOIN kernel_commit kc
    ON kc.short_hash = substring(lower(sha), 1, 12)
   AND kc.subject NOT LIKE '[stub upstream%'
ON CONFLICT DO NOTHING;
```

## 7. link_commit_fixes / link_commit_revert / link_commit_symbol

这 3 张表通过**独立的 backfill 脚本**填充（不在 `run_linker()` 主流程，跑频率更低）：

### 7.1 link_commit_fixes

扫 `kernel_commit.fixes_refs[]`（ingester 写入时已抽好）→ join 自己（fixes_hash → kernel_commit.hash 12-prefix）→ 写表。

### 7.2 link_commit_revert

```python
# subject 模式
revert_subject_re = r'^Revert "(.+)"$'
# body 模式
this_reverts_re = r"This reverts commit ([0-9a-f]{12,40})"

# 算法
for commit where subject matches revert_subject_re:
    target_sha = re.search(this_reverts_re, commit.body)
    if target_sha:
        INSERT link_commit_revert (revert_hash=commit.hash, target_hash=resolve(target_sha), source='revert_body')
```

`resolve()` 用 short_hash 索引精确匹配。**找不到 target 就跳过**（不写挂空边）。

### 7.3 link_commit_symbol

最复杂。用 git diff hunk 抽取改了哪些符号：

```python
# 算法
for commit in kernel_commit:
    diff = git_show(commit.hash)
    for hunk in parse_hunks(diff):
        if hunk.is_function_modification:
            symbol = extract_c_function_name(hunk.header_or_body)
            if _is_plausible_kernel_symbol(symbol):    # 见 § 9 #3
                INSERT link_commit_symbol (commit_hash, symbol)
```

**sentinel 行约定**：如果 commit 是 merge commit 或没有有效符号抽取出来，**必须**写一行 `(commit_hash, symbol='!sentinel')` —— 否则下次增量 backfill 还会重新扫这个 commit（死循环）。见 `60_GOTCHAS.md` §6.2。

## 8. 行为契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **所有 link 都有 confidence + source 字段** | 下游能按 confidence ≥ X 过滤"高置信链路" |
| C2 | **每张 link 表有 unique constraint**（见 M2 § 3.6）→ `ON CONFLICT DO NOTHING` 实现幂等 | 重跑安全 |
| C3 | **不写挂空边**：reference 找不到目标行就跳过，不要插入 target_hash=NULL | 否则 join 时拿到一堆死链 |
| C4 | **stub commit subject 格式固定** = `[stub upstream for OLK <hash[:12]>]` | 对账 + M5 过滤都依赖此格式 |
| C5 | **OLK inclusion 类型判定**：`tag ∈ {mainline, stable}` → backport，其它都当 native | 见 `60_GOTCHAS.md` §3.2 |
| C6 | **必须等 M3 ingester 完成**：linker 跑在 commit / bug / cve 表填充后；提前跑 → 0 边 | M3 是 linker 的硬依赖 |
| C7 | **增量 backfill 必须写 sentinel** | 见 `60_GOTCHAS.md` §6.2 |

## 9. 已知陷阱（M4 独有）

### #1 · `link_commit_bug` 长期为 0 不是 bug 是数据缺口

OLK 项目大部分 issue 在 gitee（~63 K）/ atomgit（~7.9 K），少数在 bugzilla.kernel.org（~1.6 K）。ADR-022 之前只 ingest bugzilla，所以 link_commit_bug 永远 ~0；ADR-022 后 gitee/atomgit ingester 上线，linker 才有 bug 行可 join。

详见 `60_GOTCHAS.md` §6.1。

### #2 · short_hash JOIN 而不是 LIKE

```sql
-- ✓ B-tree 等值，毫秒
JOIN kernel_commit kc ON kc.short_hash = substring(lower(sha), 1, 12)

-- ✗ LIKE 全表扫，30+ 秒
JOIN kernel_commit kc ON kc.hash LIKE substring(lower(sha), 1, 12) || '%'
```

short_hash 是 `kernel_commit` 的 generated stored 列，B-tree 索引（M2 § 3.1）。

### #3 · 符号抽取要过滤噪声

`link_commit_symbol` extractor 必须拒绝：
- 通用 keyword（`config`、`obj`、`do`、`get`、`set`、`init`、`exit`、`fini` 等）
- 短串（< 4 字符）
- 全数字 / 含特殊符号

要求 symbol 至少满足以下之一：
- snake_case（≥ 2 段，每段非数字开头）
- UPPER_MACRO（ALL_CAPS_WITH_UNDERSCORES）
- 长 CamelCase（≥ 6 字符）

实现见 `graph/git_diff.py::_is_plausible_kernel_symbol()`。

### #4 · `_link_olk_upstream` stub 不要被后续 ingest 覆盖

如果之后 mainline ingester 真的把那个 upstream commit ingest 进来了，应该用 `UPSERT` 把 stub 行的 subject / body / inclusion_type 全部覆盖：

```sql
INSERT INTO kernel_commit (hash, subject, body, commit_date, inclusion_type, ...)
VALUES (...)
ON CONFLICT (hash) DO UPDATE SET
  subject = EXCLUDED.subject,           -- ⚠ EXCLUDED 是新值
  body = EXCLUDED.body,
  commit_date = EXCLUDED.commit_date,
  inclusion_type = EXCLUDED.inclusion_type
WHERE kernel_commit.subject LIKE '[stub upstream%';   -- 只覆盖 stub，不覆盖真行
```

### #5 · 增量 backfill 循环陷阱（详见 `60_GOTCHAS.md` §6.2）

backfill `link_commit_symbol` 时如果 extract 函数对某 commit 返回空（没找到任何有效符号），下次还会扫这条记录、还是空、永远循环。**必须**写 sentinel 行（`symbol='!sentinel'` 或单行占位）破循环。

> **see also**：`60_GOTCHAS.md` §1.1（psycopg3 cast）/ §6（数据完整性）

## 10. Neo4j 拓扑视图（可选）

`graph/neo4j_rebuild.py::rebuild()` 把上面 7 张 PG link 表全量 dump 到 Neo4j 作只读视图。**Neo4j 不是主存储**——所有写入都进 PG，Neo4j 定期 rebuild。

用途：`MATCH (c:Commit)-[:FIXES]->(b:Bug)-[:RELATES_TO]->(cve:CVE) RETURN ...` 这种 4 跳图查询，比 PG 写 4 层 join 简单。

跑频率：每周一次（`scripts/weekly_sync.sh`）。

## 11. 验收

### 单元

```python
from graph.linker import _extract_bugzilla_ids
assert _extract_bugzilla_ids("Fixes: bsc#12345\nLink: https://gitee.com/openeuler/kernel/issues/I3J87Y") == \
    [("kernel.org", "12345"), ("gitee", "I3J87Y")]
```

### 集成（在 M3 完成后）

```bash
python -c "from graph.linker import run_linker, link_nvd_commits, link_syzbot_commits; \
           from storage.pg.engine import get_engine; \
           e = get_engine(); \
           print(run_linker(e)); \
           print('nvd:', link_nvd_commits(e)); \
           print('syzbot:', link_syzbot_commits(e))"
```

期望（2026-06 数据快照）：
- `link_commit_bug` ≥ 58 K
- `link_commit_message` ≥ 38 K
- `link_commit_cve` ≥ 17 K
- `link_syzbot_commit` ≥ 13 K

### 对账（reconcile）

```bash
python -m graph.reconcile
```

PG link 表 vs Neo4j 关系数应一致（容差 ±5 行，因为对账时仍有写入并发）。

## 12. 上下游

| 关系 | 模块 |
|---|---|
| **依赖** | M2 Storage（5 张内容表填充 + 7 张 link 表 schema） |
| **依赖** | M3 Ingestion（必须先完成）|
| **被调用** | `scripts/weekly_sync.sh`（cron）/ M8 eval 准备阶段 / 手动 `python -m graph.linker` |
| **被消费** | M5 Retrieval（间接：从 link 表读派生关系）/ M7 ReAct 工具（`get_regression_fixes` / `check_backport_status` / `find_syzbot_fixed_by_commit` 直读 link 表）|
| **配置 keys** | `storage.neo4j.*`（仅 Neo4j rebuild 用） |
| **重要 ADRs** | ADR-022 (gitee/atomgit issue ingest) |

---

> **重建校对**：在 M3 完成后跑 `run_linker(engine)`，验证 § 11 集成期望；跑 `python -m graph.reconcile` 确认 PG ↔ Neo4j 对账误差 ≤ 5 行。

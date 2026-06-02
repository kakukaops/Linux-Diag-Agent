# M3 · Ingestion

## 1. 目的

6 个独立 pipeline 把外部知识源拉进 PG。每个 ingester 是**幂等 + 增量**的：用 `ingest_runs` 表的 `checkpoint` JSONB 字段记录 cursor，下次跑接着 cursor 之后开始。

```
ingest/
├── base.py                BaseIngester 抽象 + Quarantine + CheckpointStore
├── lkml/                  lore.kernel.org Atom feed + raw fetcher  → lkml_message
├── bugzilla/              bugzilla.kernel.org REST                  → bug (source='kernel.org')
├── gitee/                 gitee.com issue REST (ADR-022)            → bug (source='gitee')
├── atomgit/               atomgit.com issue REST (ADR-022)          → bug (source='atomgit')
├── syzbot/                syzbot.kernel.org HTML scraper             → syzbot_crash
├── nvd/                   NVD CVE REST API v2.0                      → cve
└── kernel_commit/         git log over OLK + linux-stable            → kernel_commit
```

## 2. 公共接口

```python
class BaseIngester(ABC):
    name: str                          # 'lkml' / 'nvd' / etc.

    @abstractmethod
    def incremental(self, checkpoint: dict | None) -> tuple[RunReport, dict]:
        """运行一次增量。
        Args:
            checkpoint: 上次结束时的 cursor，第一次跑为 None。
        Returns:
            (report, new_checkpoint)
        """

    def run(self) -> RunReport:
        """完整生命周期：load checkpoint → incremental() → save run + checkpoint"""

@dataclass
class RunReport:
    ingester: str
    status: Literal["success", "partial", "failed"]
    started_at: datetime
    finished_at: datetime
    rows_processed: int
    rows_inserted: int
    rows_skipped: int
    errors: list[dict]                 # 每条 {row_id, error_type, message}
```

## 3. 各 ingester 契约

### 3.1 LKML（`ingest/lkml/`）

| 项 | 值 |
|---|---|
| 数据源 | `https://lore.kernel.org/<list>/?q=d:YYYYMMDD..&x=A&o=N` Atom feed + `/<list>/<msg-id>/raw` |
| **写入表** | `lkml_message`（主）+ `lkml_thread`（去重）+ `lkml_patch`（subject 匹配 `[PATCH ...]`）+ `lkml_review`（subject 含 `Re:` 且 body 含 Reviewed-by/Acked-by/NACK）|
| 增量 cursor | `{"since_per_list": {"linux-mm": "2026-05-30", ...}, "last_message_id_per_list": {...}, "summary_deferred_queue": [...]}` |
| 限流 | **1.5 s / 请求**（包括 Atom 分页 + raw 拉取共享）—— `_REQUEST_DELAY_S = 1.5`，不要降 |
| 大列表性能 | linux-mm 30 天 ≈ 1-2 h |

**关键 gotcha**（详见 `60_GOTCHAS.md` §2.2）：
- `?x=mbox&since=` 返回 HTML，**不能用**
- 正确：Atom feed `?q=d:YYYYMMDD..&x=A&o=N`，每页 200 条，循环到 < 200 停
- 然后 per-msg fetch `/<msg-id>/raw`

**stack_signature 计算**：body 含 call trace 时，调 `kg/signature.py::stack_signature(body)`。

### 3.2 NVD（`ingest/nvd/`）

| 项 | 值 |
|---|---|
| 数据源 | `https://services.nvd.nist.gov/rest/json/cves/2.0` |
| 写入表 | `cve` |
| 增量 cursor | `{"last_modified": "ISO datetime"}` |
| 限流 | **6.0 s / 请求** —— `_DELAY_S = 6.0`，无 API key 时 NVD 强制 |
| 最大日期窗 | **120 天** → 实现切成 ≤ 119 天滑窗（`_MAX_WINDOW = 119`） |
| 分页 | `startIndex` + `resultsPerPage=2000` |
| 过滤参数 | **`keywordSearch=linux kernel`**（不要用 `virtualMatchString` + CPE，返回 404） |

**`fix_commits` 列填充**：从 `references[]` 抓 GitHub / kernel.org commit URL，正则：
```
github\.com/[^/]+/[^/]+/commit/([0-9a-f]{8,40})
git\.kernel\.org/.*commit/?(?:\?id=)?([0-9a-f]{8,40})
```
实现见 `ingest/nvd/extractor.py`。

### 3.3 Bugzilla（`ingest/bugzilla/`）

| 项 | 值 |
|---|---|
| 数据源 | `https://bugzilla.kernel.org/rest/bug` |
| 写入表 | `bug`（source = `'kernel.org'`） |
| 增量 cursor | `{"last_change_time": "YYYY-MM-DD"}` |
| 限流 | 1.5 s / 请求 |
| 日期字段名 | **`last_change_time`**（不是 `changed_after`，不是 ISO datetime；格式严格 `YYYY-MM-DD` 字符串） |
| 范围 | 仅 kernel.org（ADR-011）。Red Hat BZ deferred 到 v1.1+，**不要扩** |

### 3.4 syzbot（`ingest/syzbot/`）

| 项 | 值 |
|---|---|
| 数据源 | `https://syzkaller.appspot.com/upstream` HTML（无 REST API） |
| 写入表 | `syzbot_crash` |
| 增量 cursor | `{"last_seen_ids": ["set", "of", "crash", "ids"]}` |
| 限流 | 0.5 s / 请求（用户已批准从 1.5 s 降低） |
| CSS selector | `list_table`（2026 改版后；老版本是 `bug_list`） |
| 详情 URL 参数 | **`extid=<id>`**（不是 `id=`） |

**`stack_signature`**：scrape 完 stack_trace 后调 `kg/signature.py::stack_signature()`，归一化后哈希 top-4 distinguishing function names。

**`fix_commits` 提取**：从详情页 "Fix commit:" / "Patched on:" 段下面的 `git.kernel.org/...commit/?id=<sha>` URL 抓。**不要**用旧版的 `(Fix:|Fixed by:) + hex` 正则——0 命中。

### 3.5 gitee / atomgit（`ingest/gitee/` + `ingest/atomgit/`，ADR-022）

| 项 | 值 |
|---|---|
| 数据源 | `https://gitee.com/api/v5/repos/openeuler/kernel/issues` 和 `https://atomgit.com/api/...` |
| 写入表 | `bug`（source = `'gitee'` / `'atomgit'`，external_id = issue number） |
| 鉴权 | Personal Access Token（写在 `configs/local.yaml::ingestion.gitee.token` / `.atomgit.token`） |
| 限流 | gitee 匿名 60/min，token 5000/h；atomgit 类似 |
| **不要走主机代理**：直接连 `gitee.com` / `atomgit.com`（用户指示） |

写入 `bug.description` 时**保留**完整 issue body（不截断），下游 BM25 才能召回。

### 3.6 kernel_commit（`ingest/kernel_commit/`）

| 项 | 值 |
|---|---|
| 数据源 | git log on local clone of OLK kernel + linux-stable (mainline) |
| 写入表 | `kernel_commit` |
| 增量 cursor | `{"last_sha_per_branch": {"OLK-6.6": "<sha>", "OLK-5.10": "<sha>", "linux-stable/master": "<sha>", ...}}` |
| 仓库 path | `configs/default.yaml::ingestion.kernel_commit.olk_repos[].local_path` |

**两阶段**：
- **Phase A**: OLK-6.6 + OLK-5.10（项目主目标）
- **Phase B**: linux-stable mainline（filler，用于 backport chain）

**关键实现细节**（详见 `60_GOTCHAS.md` §3）：
- subprocess 调用 git 时 **不要** `text=True`：用 `result.stdout.decode("utf-8", errors="replace")`
- 解析 commit body 时按"从尾扫"找 trailers（Fixes: / Link: / bugzilla: / Reviewed-by: / 等），避免把 trailers 当成 file paths
- OLK inclusion 类型判定：`tag ∈ {mainline, stable}` → backport，其它 → openEuler-native（**不要枚举 native 类型**）
- `affected_versions[]` 由 `subject` 前缀 + branch name 推断

### 3.7 ingester 公共契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **幂等**：重跑同 checkpoint 必须 0 新行 | 数据修复时必须能反复跑 |
| C2 | **增量 cursor 由 ingester 决定**：checkpoint dict 的 schema 由 ingester 自己定义；BaseIngester 只 dump/load JSON | 各源 cursor 形态不同 |
| C3 | **失败行进隔离区**：用 `Quarantine` 类把无法解析的源数据写到 `data/quarantine/<source>/<run_id>/<id>.txt`，**不要**丢主 pipeline | 后续人工 / 修补 parser 再回填 |
| C4 | **限流不准降**：每个 ingester 限流值是踩坑得来的（NVD 限制 / lore IP 封禁风险）；降低 = 被远程站点拉黑 |
| C5 | **`ingest_runs` 只在结束时写**（详见 `60_GOTCHAS.md` §1.3） |
| C6 | **stack_signature 用 kg/signature.py 共享**：syzbot / bug / lkml / dmesg_event 四张表的 signature 必须由**同一个**算法计算，否则跨表 find_similar_crashes 失效 | 见 `60_GOTCHAS.md` §5.3 |

## 4. checkpoint schema 速览

| ingester | checkpoint keys |
|---|---|
| lkml | `since_per_list`, `last_message_id_per_list`, `summary_deferred_queue` |
| nvd | `last_modified` (ISO datetime) |
| bugzilla | `last_change_time` (YYYY-MM-DD) |
| syzbot | `last_seen_ids` (set of crash IDs) |
| gitee / atomgit | `since_per_repo` (ISO datetime) |
| kernel_commit | `last_sha_per_branch` (branch → full SHA) |

## 5. 重置某个 ingester

```sql
-- 删 cursor，下次 incremental() 等同首次全量
DELETE FROM ingest_runs WHERE ingester = '<name>';
```

⚠ 这**不**删 ingester 写入的数据行（kernel_commit / lkml_message 等）。如果要彻底重置，配合 `TRUNCATE <target_table> CASCADE`。

## 6. 已知陷阱（M3 独有）

### #1 · syzbot URL 改版

2026 年 syzbot 把 CSS class 从 `bug_list` 改成 `list_table`；URL 参数从 `id=` 改成 `extid=`。需要正则 + selector 同步更新。

### #2 · NVD 120 天日期窗硬限

`pubStartDate=...&pubEndDate=...` 跨度 > 120 天 → 400。实现切成 119 天滑窗（保留 1 天 buffer 防边界）。

### #3 · git log 输出含非 UTF-8 字节

```python
# ✓
result.stdout.decode("utf-8", errors="replace")
# ✗
subprocess.run(..., text=True)    # 抛 UnicodeDecodeError
```

### #4 · gitee/atomgit issue body 大小差异大

OLK 项目里有 3-11 KB issue body，比 Bugzilla 短描述长得多。`bug.description` 列**不要**截断；下游 BM25 + LLM rerank 依赖完整 body 内容。token budget 已经为此从 150K 调到 200K → 250K。

> **see also**：`60_GOTCHAS.md` §2（外部 API quirks 整章 4 条）/ §3（git 子进程整章 2 条）/ §6（backfill 增量循环陷阱）

## 7. 验收

### 单 ingester 验证

```bash
# 跑一次增量
python -m ingest.nvd incremental

# 验证 row count 增长（首次跑大约 1000 行 / 6 个月 lookback）
psql -c "SELECT COUNT(*) FROM cve;"

# 验证 checkpoint 写入
psql -c "SELECT ingester, status, rows_inserted FROM ingest_runs ORDER BY id DESC LIMIT 3;"
```

### 全套 weekly sync

```bash
./scripts/weekly_sync.sh
```

期望：6 个 ingester 全 success，耗时 1-4 h（视 lkml 列表数）。

### 数据规模基线（2026-06 快照）

| 表 | 行数 |
|---|---|
| kernel_commit | 1.5 M |
| lkml_message | 115 K |
| bug | 8 K |
| cve | 16 K |
| syzbot_crash | 6.8 K |

## 8. 上下游

| 关系 | 模块 |
|---|---|
| **依赖** | M2 Storage（写入 5 张内容表 + ingest_runs） |
| **依赖**（间接） | M1 LLM Provider（仅 lkml summary_deferred_queue 用 LLM 总结线程；其它 ingester 纯 HTTP/git） |
| **被调用** | 调度器 / cron / `scripts/weekly_sync.sh` |
| **下游消费** | M4 linker（必须等 M3 完成后跑） / M5 retrieval（读）|
| **配置 keys** | `ingestion.{lkml,bugzilla,gitee,atomgit,syzbot,nvd,kernel_commit}.{...}` — 详见 `70_OPERATIONAL.md` |

---

> **重建校对**：跑 `python -m ingest.nvd incremental` 一次，验证 `cve` 表至少多 100 行，`ingest_runs` 多 1 行 status='success'，且 `cve.fix_commits` JSONB 非空率 ≥ 30 %。

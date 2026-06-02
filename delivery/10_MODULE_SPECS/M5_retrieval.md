# M5 · 7-Route Retrieval

> **示范模块**——展示交付包"vibe code"体例。其他 8 个模块按此 8-section 模板。
> _本模块负责"种子证据检索"，不是诊断的全部：ReAct loop（M7）会在此基础上继续深挖。_

---

## 1. 目的

在 triage 阶段给出 `fault_summary` 后，**并行**检索全部知识源，组装一个 `Evidence` 池喂给 ReAct loop 和 bind_claims。

**为什么不做"基于 fault_kind 的选择性路由"**：见 ADR-013。历史教训——选路逻辑总会漏掉对的源，"always-fire-all"成本可控（200-400 ms 总耗时），收益更可靠。

## 2. 公共接口

> 类型签名是 Python 参考形式。AI agent 可翻译到其它语言，但契约不变。

```python
def retrieve(query: RetrievalQuery) -> RetrievalResult: ...

@dataclass
class RetrievalQuery:
    raw_question: str
    kernel_version: str | None       # 如 "OLK-6.6"
    fault_domain: str | None         # 如 "oom" / "lockup" / "panic" / "io_hang"
    subsystem_hint: str | None       # 如 "mm" / "net" / "fs/ext4"
    error_keywords: list[str]        # 如 ["ENOMEM", "order=4"]
    cve_ids: list[str]               # 抽出的 CVE-ID
    commit_hashes: list[str]         # 抽出的 SHA (≥ 12 hex)
    keywords: list[str]              # 兜底关键词
    routes: list[RouteTag]           # 默认所有 7 路
    limit_per_route: int = 10

@dataclass
class RetrievalResult:
    items: list[Evidence]            # 合并后按 score 降序
    by_route_count: dict[RouteTag, int]
    used_rerank: bool

@dataclass
class Evidence:
    route: RouteTag                  # 来自哪一路
    score: float                     # 0.0-1.0 归一化相关度
    title: str
    body: str                        # 截断到 500 字符
    commit_hash: str | None
    bug_id: int | None
    cve_id: str | None
    message_id: str | None
    metadata: dict                   # 路由特有字段（author, recall_tier 等）

class RouteTag(str, Enum):
    code   = "code"     # CodeGraph BM25 over kernel source
    docs   = "docs"     # CodeGraph PageIndex（文档树）
    lkml   = "lkml"     # PG body_tsv over lkml_message
    bug    = "bug"      # PG body_tsv over bug
    syzbot = "syzbot"   # PG body_tsv over syzbot_crash
    commit = "commit"   # PG body_tsv over kernel_commit
    cve    = "cve"      # 精确 ID lookup + PG body_tsv over cve
```

## 3. 数据流

```
            ┌─ parse_query (LLM, navigator role) ─→ RetrievalQuery
            │
fault_summary ─┤
            │
            ↓
        ┌─── retrieve() ──┐
        │ 并行（threading）│
        ↓                ↓
   ┌────┴───┬──────┬──────┬──────┬──────┬─────┐
   ↓        ↓      ↓      ↓      ↓      ↓     ↓
 code(CG) docs(CG) lkml  bug  syzbot commit  cve
                  (PG)  (PG)  (PG)   (PG)   (PG)
   └────────┴──────┴──────┴──────┴──────┴─────┘
                       │
                       ↓
            合并 + 去重 + 按 score 排序
                       │
                       ↓
       total > limit_per_route ?
                       │
                       ↓ yes
       reranker.maybe_rerank (LLM, navigator role)
                       │
                       ↓
                    返回
```

## 4. 行为契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **always-fire-all**（ADR-013）：7 路无条件并发 | 选择性路由历史上总漏掉对的源 |
| C2 | **AND-priority 召回**：单路 SQL 先尝试全关键词 AND，不足 3 条则丢最右词重试，OR 是最终兜底 | ts_rank_cd 在 OR 池里把对的结果稀释到第 65+ 名 |
| C3 | **rerank 触发**：仅当 `sum(len_per_route) > limit_per_route` | 候选不够多时 rerank 浪费 token |
| C4 | **rerank 跳过**：navigator rpm 接近耗尽时跳过 rerank，保留 BM25 顺序，**记 warning 不抛错** | 故障路径不能因 LLM 限流挂掉 |
| C5 | **kernel_version 过滤**：当非空时，commit / bug / syzbot 路加 `WHERE :ver = ANY(affected_versions)` | 跨版本误匹配是历史 P1 |
| C6 | **CVE 路精确优先**：`query.cve_ids` 非空时先精确 ID 匹配（score=1.0），再 BM25 | 精确命中淹没在 ts_rank_cd 排序里曾让 cve-001 走偏 |
| C7 | **stub commit 必过滤**：所有 `kernel_commit` SELECT 加 `WHERE subject NOT LIKE '[stub upstream%'` | 见 § 6 #1 |

## 5. 字段映射

> AI agent 重建 SQL 时**严格用这些列名**，写错的话索引失效。

| 表 | 全文索引列（GIN） | 排序列 | 关键字段 |
|---|---|---|---|
| `kernel_commit` | `body_tsv` | `commit_date` | `hash`(PK), `subject`, `body`, `affected_versions[]`, `inclusion_type`, `upstream_commit`, `subsystem`, `short_hash` |
| `lkml_message` | `body_tsv` | `date` | `message_id`(PK), `thread_id`, `subject`, `body` |
| `bug` | `body_tsv` | `updated_at` | `id`(PK), `external_id`, `source`, `title`, `description`, `subsystem`, `status` |
| `syzbot_crash` | `body_tsv` | `last_seen` | `id`(PK), `syzbot_id`, `title`, `stack_signature`, `fix_commits[]` |
| `cve` | `body_tsv` | `published` | `cve_id`(PK), `cvss_v3_score`, `cvss_v3_vector`, `"references"`, `fix_commits[]` |

### ⚠ 易写错的列名

| 表 | ✓ | ✗（不要用） |
|---|---|---|
| `cve` | `cvss_v3_score` / `cvss_v3_vector` | `severity` / `cvss_score` |
| `bug` | `title` | `summary` |
| `kernel_commit` | `subject` / `body` | `message` / `description` |

## 6. 已知陷阱（M5 独有）

### #1 · stub commit 必须过滤

`_link_olk_upstream`（M4 Linker）为找不到的 upstream SHA 插占位行，特征：
- `subject` 形如 `[stub upstream for OLK <hash>]`
- `commit_date` = 1970-01-01
- `body` = NULL

不过滤会让 agent 引用一个**看似 commit 实则不存在**的 SHA。Run 16 cve-001 案曾因此让 agent 把 stub `1d7f1049035b` 当答案。

```sql
WHERE subject NOT LIKE '[stub upstream%'    -- 强制
```

### #2 · BM25 必须走预生成的 `body_tsv`，不可 inline

```sql
-- ✓ 走 GIN 索引，毫秒级
WHERE body_tsv @@ to_tsquery('english', :q)

-- ✗ 全表扫描，1.5M 行秒级
WHERE to_tsvector('english', body) @@ to_tsquery('english', :q)
```

`body_tsv` 是 PG 生成存储列，migration 0001 创建。

### #3 · CodeGraph `search_code` 是 AND 语义

CodeGraph Zoekt 端点**所有 token 必须出现在同一文件**。PG 那 6 路是 OR 语义（带排序）。两套不一致，LLM 风格多词 query 在 CodeGraph 上 0 命中。

`recall.code` 检测多词 query 返回 0 时，**自动拆词重试**，合并去重。详见 `30_TOOL_CONTRACTS.md` § search_code。

> **see also**：`60_GOTCHAS.md` § psycopg3-cast / PG-reserved-words / NVD-quirks（跨模块通用陷阱）

## 7. 验收

### 输入

```python
RetrievalQuery(
    raw_question="OOM kill in memcg /app/java",
    kernel_version="OLK-6.6",
    keywords=["oom_kill_process", "mem_cgroup_out_of_memory", "CONSTRAINT_MEMCG"],
    routes=[RouteTag(t) for t in ("commit","lkml","bug","syzbot","cve","code","docs")],
    limit_per_route=10,
)
```

### 期望

- `result.items` 至少 8 条
- `commit` 路至少 1 条 with `commit_hash` 非空
- `lkml` 路至少 3 条
- 任何 `subject LIKE '[stub upstream%'` 的行**不应**出现
- 当 cve_ids 含 `CVE-2024-26926` 时，CVE 路第 1 条 score = 1.0 且 `cve_id == "CVE-2024-26926"`

### 自动验收

跑 `50_TEST_FIXTURES/run_acceptance.py --module M5`——脚本会从 `cases_v2.json::oom-001` 加载 query，比对上述断言。

## 8. 上下游

| 关系 | 模块 / 节点 |
|---|---|
| **依赖** | M1 LLM Provider（navigator 角色，用于 query_parser + rerank） |
| **依赖** | M2 Storage（16 张表 + `body_tsv` 生成列 + GIN 索引） |
| **依赖**（间接） | M6 MCP（CodeGraph MCP server :8080，提供 code/docs 路） |
| **被调用** | M7 Agent 的 `retrieve` 节点 |
| **被调用** | M7 ReAct 工具 `search_commits` / `search_lkml` / ... 最终落到 `recall/*.py` |
| **配置 keys** | `retrieval.limit_per_route` / `retrieval.rerank_threshold` / `retrieval.codegraph_endpoint` — 完整 schema 见 `70_OPERATIONAL.md` |

---

> **重建校对**：实现完 M5 后，对照 § 7 跑 `run_acceptance.py --module M5` 通过，且 grep 你的代码：C7 (`stub upstream`) / #1 / #2 / #3 都有对应实现或测试覆盖。

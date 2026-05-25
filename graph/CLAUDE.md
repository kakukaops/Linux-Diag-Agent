# graph/ — Cross-Graph Linker

## 职责

把 commit / bug / LKML / CVE 四张子图焊接成可多跳推理的关系网。所有关联都有明确文本证据，**不使用相似度匹配**。

```
graph/
  linker.py            主入口：run_linker() + link_nvd_commits()
  patch_commit_lookup.py  patch-id 反查（WBS 4.1）
  neo4j_rebuild.py     Neo4j 周度全量 rebuild（WBS 4.3）
  reconcile.py         PG link 表 vs Neo4j 关系对账（WBS 4.4）
```

## 运行方式

```python
from graph.linker import run_linker, link_nvd_commits
from storage.pg.engine import get_engine

engine = get_engine()
report = run_linker(engine)          # commit→bug + commit→message + OLK上游桥接
n = link_nvd_commits(engine)         # CVE fix_commits → kernel_commit 关联
```

**必须在 kernel_commit ingestion 完成后运行。** 否则 link 表写入 0 行（引用的 hash 不存在）。

## run_linker() 内部三步

1. `_link_trailer_to_bug` — 扫 `kernel_commit.body` 中 `Fixes: bsc#N` / `bugzilla.kernel.org/...` → 写 `link_commit_bug`
2. `_link_link_trailer_to_message` — 扫 `Link: https://lore.kernel.org/.../<msg-id>` → 写 `link_commit_message`；降级用 subject 模糊匹配
3. `_link_olk_upstream` — OLK commit 的 `upstream_commit` SHA 在 `kernel_commit` 中不存在时，插入 stub mainline commit

## link_nvd_commits()

从 `cve.fix_commits`（JSONB 数组）中取出 commit SHA，用 `LIKE sha[:12]%` 匹配 `kernel_commit.hash`，写入 `link_commit_cve`（link_type = `'nvd_ref'`）。

`fix_commits` 由 `ingest/nvd/extractor.py` 从 NVD references 中的 GitHub/kernel.org commit URL 提取，migration `0003` 添加此列。

## Link 表约束

| 表 | unique constraint | source 值 |
|----|-------------------|----------|
| `link_commit_bug` | `uq_lcb` (commit_hash, bug_id, link_type) | `'trailer'` \| `'llm_inferred'` |
| `link_commit_message` | `uq_lcm` (commit_hash, message_id, link_type) | — |
| `link_commit_cve` | `uq_lcc` (commit_hash, cve_id, link_type) | `'nvd'` \| `'trailer'` |

每条关系都有 `confidence`（0-1）和 `source`（证据来源）字段，查询时可按 confidence 过滤。

## OLK inclusion 类型判定（linker 依赖此）

- `olk_inclusion_type ∈ {mainline, stable}` → backport，有上游 SHA，可链到 kernel.org 生态
- 其他任意值（hulk / driver / urma / 厂商名…）→ openEuler 原生，无上游讨论
- `NULL` → MR merge commit，按原生处理

**不要枚举 openEuler 原生类型**，它是开放集合（40+ 种）。只判断是否属于 `{mainline, stable}`。

## 常见陷阱

- `link_nvd_commits` 依赖 `cve.fix_commits` 列（migration 0003），若报 column 不存在，先跑 `alembic upgrade head`
- `_link_olk_upstream` 插入 stub commit 时，subject 格式固定为 `[stub upstream for OLK <hash[:12]>]`，不要修改（对账脚本依赖此格式识别 stub）
- short SHA 匹配用 `LIKE sha[:12] + '%'`，不是 `=`（OLK 和 mainline SHA 长度不固定）

## 已知数据覆盖缺口（v1.0）

**`link_commit_bug` 始终为 0 的原因：**

OLK commits 的 `bugzilla:` trailer 有两种格式：
- `bugzilla: https://gitee.com/openeuler/kernel/issues/XXXXX`（约 63,000 条）→ gitee issue，无入库路径
- `bugzilla: https://atomgit.com/openeuler/kernel/issues/NNNN`（约 7,900 条）→ atomgit issue，同上
- `Link: https://bugzilla.kernel.org/show_bug.cgi?id=NNNNN`（约 1,600 条）→ bug 表有，但对应 bug 按 `last_change_time` 过旧未入库

Linker 代码本身正确（`_BZ_URL_RE` 可匹配第三类），是数据不在库里导致写入 0 行。

**修复方向（v1.1）**：增加 gitee/atomgit issue ingester，写入 `bug` 表（source = `'gitee'`）；或扩大 Bugzilla 日期窗口全量入库。

**`link_commit_message` 初始为 0 的原因：**

LKML ingester 首次运行尚未完成（180d 全量约 2-4h）。完成后重跑 `run_linker()` 可补充 commit↔LKML 链接。

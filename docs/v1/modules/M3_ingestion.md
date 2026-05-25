# M3 — Ingestion 层 设计文档

> **⚠️ 设计规格文档**：本文档为实施前的原始设计规格（定稿于 2026-05-15）。实际实现以代码为准，两者可能存在偏差。如需了解当前实现状态，请阅读对应目录下的 `CLAUDE.md` 和源代码。


| 字段 | 值 |
|------|---|
| 模块编号 | M3 |
| 状态 | Design Locked（待实施） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [M1](M1_llm_provider.md) · [M2](M2_storage_schema.md) · [Architecture](../Architecture.md) |
| 关联 ADR | [ADR-007](../adr/ADR-007-data-dir-in-repo.md) · [ADR-008](../adr/ADR-008-reuse-codesearch.md) · [ADR-010](../adr/ADR-010-lkml-2y-bootstrap.md) · [ADR-011](../adr/ADR-011-bugzilla-kernel-org-only.md) |
| 最后更新 | 2026-05-14 |

---

## 1. 目标与边界

把 codesearch 不覆盖的内核诊断知识源 ingest 到我方 PG + Neo4j + 文件存储。每周一次增量同步，可断点续传，对外暴露统一的 `ingest_runs` 运行记录。

### 1.1 在范围

| 数据源 | 形态 | 目标存储 | 估算规模（v1.2 末）|
|--------|------|---------|------------------|
| ① **LKML**（lore.kernel.org，2024-05 起 2 年，[ADR-010](../adr/ADR-010-lkml-2y-bootstrap.md)）| mbox + REST API | `lkml_message` / `lkml_thread` / `lkml_patch` / `lkml_review` | ~3M 邮件 / ~35GB mbox |
| ② **Bugzilla**（**仅** bugzilla.kernel.org，[ADR-011](../adr/ADR-011-bugzilla-kernel-org-only.md)）| REST API | `bug` | ~50K bugs |
| ③ **syzbot** | HTML scraping | `syzbot_crash` + `bug` | ~80K crash |
| ④ **Zenodo dataset** | 一次性下载 | `link_commit_bug` + `kernel_commit` 补充 | 90K bug-fix 对 |
| ⑤ **NVD CVE feed** | JSON Feed | `cve` + `bug.cve_ids` 反向更新 | ~30K CVE（Linux 相关）|
| ⑥ **Kernel git commits**（双仓库：OLK 内核仓 `OLK-6.6`/`OLK-5.10` + linux-stable 辅仓，[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md)）| git log + git show | `kernel_commit` | ~2.5M commits |

### 1.2 不在范围

| 项 | 由谁处理 |
|----|---------|
| 内核源码全文索引 / SCIP 符号 | codesearch `scripts/index-repo.sh` |
| kernel-doc / Sphinx JSON 解析 | codesearch `kernel-docs-builder` 容器 |
| man-pages 解析（如 codesearch 接入） | CodeGraph |
| Red Hat Bugzilla（CVE 关联更全）| ❌ v1 暂不接（[ADR-011](../adr/ADR-011-bugzilla-kernel-org-only.md)），v1.1+ 评估 |
| Debian BTS / Launchpad | ❌ v2+ |
| LWN.net 文章 | ❌ v2+（如订阅）|

## 2. 关键决策摘要

| # | 决策 | 出处 |
|---|------|------|
| D1 | LKML 首次 bootstrap 窗口 = 2024-05 起 2 年（~3M 邮件）| [ADR-010](../adr/ADR-010-lkml-2y-bootstrap.md) |
| D2 | Bugzilla 仅接入 bugzilla.kernel.org，**不**接 Red Hat BZ | [ADR-011](../adr/ADR-011-bugzilla-kernel-org-only.md) |
| D3 | Kernel git = **我方独立 clone**（`data/kernel-git/`）；双仓库 = OLK 内核仓（主）+ linux-stable（辅），不复用 CodeGraph checkout | 本文 §3.6 · [ADR-018](../adr/ADR-018-commit-source-olk-kernel.md) |
| D4 | 每周日 03:00 UTC cron，5 个网络源并行，commit ETL 在其后 | 本文 §4 |
| D5 | 错误处理：单条失败 quarantine，连续失败 ≥2 次告警 | 本文 §5 |
| D6 | 长 LKML 线程（>30 封 + 静默 14 天）触发 LLM 摘要 | 本文 §3.1 |

## 3. 各 Ingester 详细设计

### 3.1 LKML Ingester

#### 数据源契约

- **协议**：lore.kernel.org public-inbox REST + mbox download
- **抓取 URL 模板**：`https://lore.kernel.org/<list>/?x=mbox&since=<ISO>`
- **mailing lists**（10 个，覆盖内核诊断主要议题）：
  - `linux-kernel`（主干）
  - `linux-mm`、`linux-fs`、`linux-block`、`linux-net`、`linux-pci`、`linux-arch`
  - `linux-arm-kernel`（架构）
  - `stable`（backport）
  - `syzbot-bugs`（自动崩溃报告）
- **首次窗口**：2024-05-14 起至今（参考 [ADR-010](../adr/ADR-010-lkml-2y-bootstrap.md)，约 2 年）

#### 处理流程

```
incremental(since: datetime per list):
    for each list:
        1. GET lore.kernel.org/<list>/?x=mbox&since=<last_message_iso>
        2. 解析 mbox（Python mailbox.mboxMessage）
        3. 按 Message-ID upsert 到 lkml_message
        4. 落地 mbox 原文到 data/lkml/<list>/<year>/<month>.mbox.gz
        5. 增量更新 thread DAG：
           - 取本批新 message 的 in_reply_to，若 parent 已在库，连边；否则记 orphan
           - orphan 在下一批扫时重试连接
        6. 抽取 patch / trailer：
           - [PATCH vN m/n] 正则 → lkml_patch
           - Fixes:/Reported-by:/Closes:/Link: trailer → 待 M4 Cross-Graph Linker 处理
           - Reviewed-by:/Tested-by:/Acked-by:/NACK trailer → lkml_review
        7. 长线程摘要触发：
           if thread.message_count >= 30 AND thread.last_activity < now - 14d AND thread.summary IS NULL:
               summary = M1.navigator.chat("三段式总结...")
               UPDATE lkml_thread SET summary, summary_model, summary_at
        8. checkpoint: 记录每 list 的 last_message_id 到 ingest_runs.checkpoint_data
```

#### 关键参数

| 参数 | 值 | 备注 |
|------|---|------|
| 并发 | 4（mailing lists 间并行）| 避免 lore.kernel.org 限流 |
| 重试 | 3 次指数退避（初 2s, max 60s） | tenacity |
| 长线程阈值 | ≥30 封 ＋ 静默 14 天 | 平衡覆盖率与 LLM 成本 |
| LLM 模型 | Navigator（claude-haiku） | Pro 订阅下友好 |
| 摘要消息预算 | 单线程一次摘要 = 1 message | Pro 5h 窗口 45 message，最多 ~40 摘要/5h |
| mbox 压缩 | gzip | 减小 80GB→35GB 后再压一半 |

#### 已知挑战

| 挑战 | 应对 |
|------|------|
| 跨 list 同一 Message-ID | 主 list = 第一次见的 list；其他建 cross-reference（PG `lkml_message_alt_list` 简单 1:N 表）|
| In-Reply-To 缺失 | References 头反查；都缺则视为新线程 root |
| LLM 摘要失败 | 失败计入 `summary_model='failed:<reason>'`，下轮重试 |
| Pro 订阅 5h 配额撞墙 | 摘要任务用单独队列，超额延后到下个 cron 窗口 |

### 3.2 Bugzilla Ingester（仅 kernel.org）

#### 数据源契约

- **协议**：Bugzilla REST API v1
- **URL**：`https://bugzilla.kernel.org/rest/bug?...`
- **认证**：无需 API key（公开数据）
- **过滤**：所有 product；exclude `product='Tools'`、`status='UNCONFIRMED'`

#### 处理流程

```
incremental(since: last_change_time):
    1. GET /rest/bug?changed_after=<since>&include_fields=id,summary,status,severity,product,
                                                          component,creation_time,last_change_time,
                                                          resolution,see_also,depends_on,blocks
    2. for each bug:
        - GET /rest/bug/<id>/comment （取 first comment 作为 description）
        - 归一化 status: NEW/IN_PROGRESS/RESOLVED-FIXED/CLOSED-NOTABUG/...
        - 抽取 see_also 中的 commit hash / CVE / Message-ID（供 Cross-Graph Linker 用）
        - 抽取 fix_commit_hashes（从 comments 文本里 grep "Commit:" 或 "Fixed by:"）
        - upsert 到 bug 表（source='bugzilla.kernel.org', source_id=<id>）
    3. 落地原始 JSON 到 data/bugzilla/kernel.org/<year>/<bug_id>.json
    4. checkpoint: 记录本批最大 last_change_time
```

#### 关键参数

| 参数 | 值 |
|------|---|
| 批量大小 | 100 bugs/请求 |
| 并发 | 2（避免触限）|
| 重试 | 3 次指数退避 |
| status 归一化表 | 维护在 `ingest/bugzilla/status_normalize.json` |

#### 已知挑战

| 挑战 | 应对 |
|------|------|
| 部分 bug 缺失关键字段 | raw_metadata 留全；missing 字段记 null |
| CVE 关联覆盖率有限 | 由 NVD ingester 反向补强（CVE → 影响 commit → 影响 bug）|
| Status 字段值差异 | 归一化表保证下游 status 字典固定 |

### 3.3 syzbot Ingester

#### 数据源契约

- **协议**：HTML scraping（`https://syzkaller.appspot.com/upstream`）
- **认证**：无
- **抽取目标**：列表页 → 详情页 → reproducer 文件

#### 处理流程

```
incremental(last_crawl_at, seen_crash_ids):
    1. GET https://syzkaller.appspot.com/upstream → 列表页
    2. BeautifulSoup 解析每行：crash_id, title, first_seen, last_seen
    3. for each new crash_id (not in seen_crash_ids):
        - GET 详情页 /bug?id=<crash_id>
        - 抽取：
          - crash_type（KASAN / OOPS / WARNING / lockdep / ...）
          - crash_signature
          - stack_top_function
          - full_report（按 <pre> 块）
          - reproducer C（如有，下载二进制）
          - reproducer syz（如有，下载）
          - kernel_config（链接到 .config 文件，下载）
        - 落地到 data/syzbot/<crash_id>/{report.txt, reproducer.c, reproducer.syz, config, metadata.json}
        - upsert 到 syzbot_crash + 关联到 bug（source='syzbot', source_id=<crash_id>）
        - 计算 stack 签名：去掉 offset/address → sha256 → 入 syzbot_crash.crash_signature
    4. checkpoint: 更新 seen_crash_ids 集合 + last_crawl_at
```

#### 关键参数

| 参数 | 值 |
|------|---|
| 并发 | 2（避免 503）|
| HTML 兼容 | 多套 selector + fallback；single 失败时 quarantine |
| 重试 | 3 次 |
| Reproducer 下载超时 | 60s/个 |

#### 已知挑战

| 挑战 | 应对 |
|------|------|
| HTML 结构变化 | 多 selector + quarantine + 解析错误告警 |
| 部分 bug 无 reproducer | 字段 NULL，不影响其他记录 |
| crash_signature 不稳定 | 规范化函数名序列（移除偏移），sha256 |

### 3.4 Zenodo Dataset Ingester

#### 数据源契约

- **协议**：HTTPS 直接下载（无 API）
- **DOI**：10.5281/zenodo.10654193
- **文件**：约 200MB（CSV/JSON 混合）
- **频率**：季度（每月第一个周日检查 DOI 是否有新版本）

#### 处理流程

```
check_update():
    1. GET https://zenodo.org/api/records/10654193 → 取 latest_version
    2. if local checkpoint dataset_version != latest_version:
        - 下载 .zip 到 data/zenodo/<version>.zip
        - 解压到 data/zenodo/<version>/
        - 运行 import 子任务
    3. else: no-op
    
import(version):
    1. 读 commits.csv：写入/补充 kernel_commit（hash, subject, author 等）
    2. 读 bug_fix_pairs.json：写入 link_commit_bug（confidence=0.92, source='zenodo_dataset'）
    3. checkpoint: dataset_version + downloaded_at
```

#### 已知挑战

| 挑战 | 应对 |
|------|------|
| 数据集字段与我方 schema 不完全对齐 | mapping 表显式定义 |
| 老旧 commit hash 与我方 git clone 范围不一致 | 仅 link，不强制 commit hash 必须在我方 kernel_commit |

### 3.5 NVD CVE Ingester

#### 数据源契约

- **协议**：NVD JSON Feed v2.0
- **URL**：`https://services.nvd.nist.gov/rest/json/cves/2.0?...`
- **认证**：可选 API key（无 key 限速更紧）
- **过滤**：`virtualMatchString=cpe:2.3:o:linux:linux_kernel:*`

#### 处理流程

```
incremental(last_modified):
    1. GET /rest/json/cves/2.0?lastModStartDate=<since>&virtualMatchString=cpe:2.3:o:linux:linux_kernel:*
    2. 分页（NVD 限制 2000 items/请求）
    3. for each CVE:
        - 解析：cve_id, description, cvss_score, cvss_vector, published, affected_versions
        - 抽取 references 中的 commit hash → fix_commits
        - upsert 到 cve 表
        - 反向更新：UPDATE bug SET cve_ids = array_append(cve_ids, <cve_id>) 
                    WHERE see_also @> ARRAY[<cve_url>]
    4. checkpoint: 更新 lastModStartDate
```

#### 已知挑战

| 挑战 | 应对 |
|------|------|
| 速率限制（无 key 5 请求/30s）| 配置 NVD_API_KEY 后限速放宽 |
| CVE 描述非结构化（commit hash 散在文本）| 多套正则 + 抽取后人工抽检 |
| 跨厂商 product 名差异 | 严格按 cpe 过滤 linux:linux_kernel |

### 3.6 Kernel Commit Ingester（双仓库，[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md)）

诊断目标是 OLK 内核，故 commit 来源采用**双仓库模型**：OLK 内核仓为主、kernel.org `linux-stable.git` 为辅。

#### Git 仓库配置

```
data/kernel-git/
├── olk-kernel.git                 # 主仓库（origin='olk'）：openEuler 内核 bare repo
│   └── worktrees/
│       ├── OLK-6.6/               # checkout OLK-6.6 分支
│       └── OLK-5.10/              # checkout OLK-5.10 分支
└── linux-stable.git               # 辅仓库（origin='mainline'）：含 mainline + 全部 stable 分支
    └── worktrees/
        └── master/                # checkout master（mainline）分支
```

**主仓库远端**：`https://atomgit.com/openeuler/kernel.git`，分支 `OLK-6.6` / `OLK-5.10` —— 已确认与 CodeGraph 索引 `olk-kernel` 同源（[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md) §已确认）；本地 checkout 实例 `/data1/lingqu/codes/OLK-6.6/kernel`。
**辅仓库远端**：`git://git.kernel.org/pub/scm/linux/kernel/git/stable/linux-stable.git` —— 选 `linux-stable.git` 而非纯 mainline，因 OLK `stable inclusion` 的上游锚点指向 stable 树（[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md) D2）。
**磁盘**：olk-kernel bare ~3.5GB + linux-stable bare ~3.5GB + worktree 共享 object store → 实际约 **9GB**。

#### 处理流程

```
incremental(last_sha_per_branch):
    # Phase A: 主仓库 — OLK commit
    for each branch in [OLK-6.6, OLK-5.10]:
        cd data/kernel-git/olk-kernel.git/worktrees/<branch>
        git fetch; new_shas = git log <last_sha>..HEAD --format=%H
        for each sha in new_shas:
            git log <sha> --format=... --raw → parse:
              - hash, short_hash, author, date, subject, body, origin='olk'
              - 若为 MR merge commit（subject 形如 "!NNNNN ..."、body 含
                "See merge request"，实测占 ~7.6%）：跳过 inclusion 解析，
                仅记 merge 元数据（可抽 MR 号），continue
              - 解析 OLK inclusion 头（M4 §3.9）：
                  olk_inclusion_type = inclusion tag（开放集合，40+ 种，见 M4 §3.9）
                  upstream_commit：mainline inclusion 取头部 `commit`、
                                   stable inclusion 取 `[ Upstream commit ]`（缺失则降级）
              - 抽 Fixes:/Reported-by:/Closes:/Link: trailer（inclusion 头下方上游原文区）
              - 抽 changed files → 推断 subsystem
              - affected_versions 追加该 OLK 分支（如 'OLK-6.6'）
            upsert 到 kernel_commit
        checkpoint: last_sha[branch] = HEAD sha

    # Phase B: 辅仓库 — 上游 commit（linux-stable.git 的 master 分支 = mainline）
    for branch = master:
        git fetch; new_shas = git log <last_sha>..HEAD --format=%H
        for each sha in new_shas:
            git log <sha> → parse: origin='mainline'，affected_versions=['mainline']
              抽 Fixes:/Reported-by:/Closes:/Link: trailer
            upsert 到 kernel_commit
        checkpoint
```

**为什么辅仓用 linux-stable.git**：OLK commit ~78% 是 backport，其中 `stable inclusion` 占大头，上游锚点（头部 `commit` SHA、`Reference:` URL）指向 kernel.org **stable 树**——纯 `torvalds/linux.git` 解析不到。`linux-stable.git` 同时含 mainline 历史与全部 stable 分支，可解析两类锚点。Phase B 全量 ingest master（mainline）分支作为 LKML / CVE / 嵌套 Fixes 链（[Spike F3](../spike/Spike_Report.md)）的锚点池；stable 分支 commit 按 OLK `stable inclusion` 引用按需补 ingest。

#### 关键参数

| 参数 | 值 |
|------|---|
| 并发 | 主仓库 2 分支 + 辅仓库 1 分支，串行（避免 git 锁）|
| 单批 commit 上限 | 5000，超过分批入库 |
| subsystem 推断 | 取改动文件最深公共目录（如 `mm/slab.c` 与 `mm/slub.c` → `mm`）|
| 重启策略 | 如断在分支中间，checkpoint 记录到最新成功 sha |

#### 已知挑战

| 挑战 | 应对 |
|------|------|
| `Fixes:` tag 不规范（短 hash、错拼）| 多套正则；短 hash 解析见 [M4 §3.1](../modules/M4_cross_graph_linker.md)；失败记 raw |
| OLK inclusion 头格式变体（缩进、空行、大小写）| M4 §3.9 多 fixture；识别歧义按"原生"保守处理 + quarantine（ADR-018 C1）|
| openEuler 原生 commit（`upstream_commit` 为 NULL）| 正常状态，非错误；诊断时标注「无上游讨论」|
| 同一 commit 跨 OLK 分支（OLK-6.6 与 OLK-5.10 都有）| 同一 hash 仅一条记录；`affected_versions` 追加 |
| git clone ~9GB 占用 | 双 bare + worktree 共享对象 |

## 4. 每周 cron 调度

```bash
# /etc/cron.d/linux-diag-agent
# Weekly sync, Sunday 03:00 UTC
0 3 * * 0  diag-agent  /opt/linux-diag-agent/scripts/weekly_sync.sh
```

`scripts/weekly_sync.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail

RUN_ID=$(date +%Y%m%d-%H%M)
LOG=/var/log/diag-agent/sync-$RUN_ID.log
exec > >(tee -a "$LOG") 2>&1

echo "[$(date)] Starting weekly sync $RUN_ID"

# Phase 1: 网络密集型源 4 路并行（独立可重入）
(python -m ingest.lkml         incremental) &
(python -m ingest.bugzilla     incremental) &
(python -m ingest.syzbot       incremental) &
(python -m ingest.nvd          incremental) &
wait
echo "[$(date)] Phase 1 done"

# Phase 2: Kernel git（依赖 Phase 1 写入 link 表后可关联）
python -m ingest.kernel_commit incremental
echo "[$(date)] Phase 2 done"

# Phase 3: 每月第一个周日：Zenodo 数据集刷新
if [ "$(date +%d)" -le "07" ]; then
    python -m ingest.zenodo check_update
fi

# Phase 4: Cross-Graph Linker（属 M4，本周末统一调度）
python -m graph.linker rebuild
echo "[$(date)] Phase 4 done"

# Phase 5: 健康检查与告警
python -m scripts.sync_audit  # 检查 ingest_runs 状态，超过阈值发告警
echo "[$(date)] Sync $RUN_ID complete"
```

**总耗时预算**：Phase 1 并行 ~2.5h，Phase 2 ~30min，Phase 4 ~30min → **3-4h 完成**。

## 5. 错误处理与告警

### 5.1 错误分类

| 错误 | 重试 | 告警 | 行为 |
|------|------|------|------|
| 网络抖动 / 5xx / timeout | ✓ 3 次指数退避 | 否 | tenacity |
| 单条解析失败 | ✗ | 否 | quarantine 到 `data/quarantine/<source>/<run_id>/`，记入 items_failed，继续 |
| 认证错误（API key 失效）| ✗ | ✓ 立即 | 终止本 ingester；不影响其他 ingester |
| Schema 不匹配 | ✗ | 否 | quarantine + items_failed++ |
| 磁盘 < 10% | ✗ | ✓ 立即 | 终止全部 ingester |
| LLM 调用 rate-limited | ✓ 等到下个 5h 窗口 | 否 | 摘要任务进入 deferred 队列 |

### 5.2 Prometheus metrics

```
ingest_run_duration_seconds{source}              # gauge
ingest_run_status{source,run_id}                 # 0=running 1=success 2=failed
ingest_items_total{source,status=processed|added|updated|failed}  # counter
ingest_last_success_age_seconds{source}          # gauge
ingest_quarantine_files_total{source}            # counter
disk_free_bytes{path}                            # gauge
```

### 5.3 告警阈值

| 指标 | 阈值 | 接收 |
|------|------|------|
| `ingest_last_success_age_seconds` > 14 天 | 严重 | 邮件 + 钉钉/Slack |
| `ingest_items_failed_total` 单轮 > 100 | 警示 | 邮件 |
| `ingest_run_duration_seconds` > 6h | 警示 | 邮件 |
| 连续 2 轮 failed | 严重 | 邮件 + 钉钉/Slack |
| `disk_free_bytes` < 20% | 警示 | 邮件 |
| `disk_free_bytes` < 10% | 严重 | 立即停 ingester |

## 6. Checkpoint Schema 详例

每个 ingester 的 checkpoint 保存到 `ingest_runs.checkpoint_data`（JSONB）。

```json
// lkml
{
  "last_message_id_per_list": {
    "linux-kernel": "<20260514.143@kernel.org>",
    "linux-mm": "<20260514.087@kernel.org>",
    ...
  },
  "summary_deferred_queue": ["thread-id-1", "thread-id-2"]
}

// bugzilla
{
  "last_change_time": "2026-05-13T23:00:00Z"
}

// syzbot
{
  "last_crawl_at": "2026-05-13T23:00:00Z",
  "seen_crash_ids_count": 78342
}

// zenodo
{
  "dataset_version": "10654193.v3",
  "downloaded_at": "2026-04-07T03:00:00Z"
}

// nvd
{
  "last_modified": "2026-05-13T23:30:00Z"
}

// kernel_commit（双仓库，ADR-018）
{
  "last_sha_per_branch": {
    "OLK-6.6": "abc1234567890...",
    "OLK-5.10": "def0987654321...",
    "mainline": "1234567890abc..."
  }
}
```

## 7. 资源估算

### 7.1 磁盘（v1.2 末）

| 项 | 大小 |
|----|------|
| LKML mbox（gzip）| ~35 GB |
| Bugzilla JSON | ~1 GB |
| syzbot 报告 + reproducer | ~10 GB |
| Zenodo 数据集 | ~500 MB |
| NVD JSON | ~200 MB |
| Kernel git（OLK 内核仓 + linux-stable 辅仓，双 bare）| **~9 GB** |
| Quarantine + 日志 | ~5 GB（30 天滚动）|
| **总** | **~61 GB** |

### 7.2 网络（首次 bootstrap）

| 源 | 下载 |
|----|------|
| LKML 2 年 | ~35 GB（mbox + gzip on-the-fly）|
| Bugzilla 50K bugs | ~500 MB |
| syzbot 80K crash | ~5 GB（含 reproducer）|
| Zenodo | ~200 MB |
| NVD | ~100 MB |
| Kernel git fetch（OLK 内核仓 + linux-stable）| ~6 GB |
| **总** | **~47 GB** |

带宽 100Mbps 下，串行约 1 小时；并行约 25 分钟。

### 7.3 时间（首次 bootstrap）

| 阶段 | 时长 |
|------|------|
| Kernel git clone（OLK 内核仓 + linux-stable）+ checkout 3 worktree | ~40 min |
| LKML 2 年抓 + 解析 + 入库 | ~3 天（mbox 大且需逐封解析）|
| Bugzilla bootstrap | ~2 小时 |
| syzbot bootstrap | ~1 天（HTML 限流）|
| Zenodo + NVD | ~30 min |
| LKML 长线程摘要（积压 ~5K）| ~5 天（Pro 5h × 40 摘要）|
| **首次完整跑通** | **~10 天**（可分段验收）|

### 7.4 时间（增量，每周）

| 阶段 | 时长 |
|------|------|
| LKML 周增量 ~30K 邮件 | ~1.5h |
| Bugzilla 周增量 ~200 bugs | ~10 min |
| syzbot 周增量 ~300 crash | ~30 min |
| NVD 周增量 | ~5 min |
| Kernel git 周增量 ~3K commit | ~20 min |
| 长线程摘要新触发 ~50 | ~6h（跨多 5h 窗口） |
| **合计** | **~3-4h 当晚 + 摘要延后**（不阻塞下次 sync）|

## 8. 测试策略

### 8.1 单元测试

每个 ingester 测：
- mbox / JSON / HTML 解析正确性（fixtures 见 `tests/fixtures/<source>/`）
- trailer / signature 抽取正则
- checkpoint 序列化与反序列化
- 错误路径（quarantine）

### 8.2 集成测试

- 小 fixture（10 邮件 / 5 bug / 3 crash）端到端跑通 bootstrap + incremental
- ingest_runs 表行数 + checkpoint 数据正确

### 8.3 Smoke 测试

每次 deploy：
- LKML：抓最近 24h 一封邮件
- BZ：抓 1 个最近变化的 bug
- syzbot：抓最新 1 个 crash 列表项
- NVD：抓最近 1 个 CVE
- Kernel git：fetch + log 1 commit

### 8.4 Bootstrap 演练

v1.0 上线前，单独跑一遍完整 bootstrap，量化首次 ~10 天的真实耗时。

## 9. 文件结构

```
ingest/
├── __init__.py
├── base.py                          # Ingester Protocol + RunReport
├── lkml/
│   ├── __init__.py
│   ├── fetcher.py                   # lore.kernel.org REST + mbox
│   ├── parser.py                    # mailbox + email + trailer
│   ├── thread_builder.py            # Message-ID DAG
│   ├── summarizer.py                # 长线程 LLM 摘要
│   └── tests/
├── bugzilla/
│   ├── client.py                    # python-bugzilla wrapper
│   ├── normalizer.py                # status / severity 归一化
│   ├── status_normalize.json        # 映射表配置
│   └── tests/
├── syzbot/
│   ├── scraper.py                   # BeautifulSoup
│   ├── signature.py                 # stack 签名
│   └── tests/
├── zenodo/
│   ├── downloader.py
│   ├── importer.py
│   └── tests/
├── nvd/
│   ├── fetcher.py
│   ├── extractor.py                 # commit hash 抽取
│   └── tests/
└── kernel_commit/
    ├── git_wrapper.py               # GitPython 或 subprocess
    ├── trailer_parser.py            # Fixes: / Reported-by: / ...
    ├── subsystem_inference.py
    └── tests/

scripts/
├── weekly_sync.sh                   # cron 入口
├── sync_audit.py                    # 健康检查
└── bootstrap_all.sh                 # 一次性 bootstrap
```

## 10. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| LKML 首次 bootstrap 时间超期 | bootstrap 进度日志（每 list 进度）| 分段验收：先 1 个 list 跑通，再扩展 |
| syzbot HTML 大改 | 解析错误率 | quarantine + 解析器多版本 fallback |
| Kernel git fetch 慢 | duration metric | 第一次用 `--depth=1000` 浅克隆，按需 deepen |
| 长线程摘要堆积 > 200 | 摘要 deferred 队列 | 升级 Pro 订阅 / 把 K=1 摘要延后到 K=0（即不摘要，留待查询时 lazy）|
| LKML 同 Message-ID 多 list | 重复 ingest | UNIQUE (message_id) PK + cross-reference 1:N 表 |
| 磁盘满 | disk_free metric | 自动清理 90 天前 mbox（gzip 后保留更久也可）|
| Bugzilla schema 变动 | 解析错误率 | 字段读取容错 + raw_metadata 全留 |

## 11. v1.0 → v1.3 演进

| 阶段 | 主要变化 |
|------|---------|
| v1.0 | 6 个 ingester 全部上线；首次 bootstrap 完成；周度增量稳定运行 4 周 |
| v1.1 | 评估 LKML 是否扩展到 5 年（含早期 syzbot 报告）；评估接入 Red Hat BZ（[ADR-011](../adr/ADR-011-bugzilla-kernel-org-only.md) review）|
| v1.2 | 评测样本回流：30 例 incident 入 eval_cases；ingestion 性能调优 |
| v1.3 | 接入额外源（Debian BTS / Launchpad / LWN）评估；自动化告警接入企业 IM |

## 12. 与外部组件的契约

- **M1 Provider**：LKML 长线程摘要调用 `Navigator` model；遵守 Pro 5h 配额限流
- **M2 存储**：所有 ingester 直写 PG；Neo4j 关系由 M4 Cross-Graph Linker 处理
- **M4 Cross-Graph Linker**：消费 `lkml_message.body` / `kernel_commit.body` / `bug.see_also` 中的 trailer，输出 link_* 表
- **codesearch**：不直接调用其 MCP；但 v1.1+ 如做 stack→function 持久化可改成 runtime 调 `lookup_symbol`

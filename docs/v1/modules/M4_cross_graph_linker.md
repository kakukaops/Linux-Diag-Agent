# M4 — Cross-Graph Linker + CodeGraph 集成 设计文档

| 字段 | 值 |
|------|---|
| 模块编号 | M4 |
| 状态 | Design Locked（待实施） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [M1](M1_llm_provider.md) · [M2](M2_storage_schema.md) · [M3](M3_ingestion.md) · [KnowledgeGraph_Overview](../KnowledgeGraph_Overview.md) |
| 关联 ADR | [ADR-008](../adr/ADR-008-reuse-codesearch.md) · [ADR-009](../adr/ADR-009-multi-version-via-codesearch-repos.md) · [ADR-012](../adr/ADR-012-codesearch-http-transport.md) · [ADR-018](../adr/ADR-018-commit-source-olk-kernel.md) |
| 最后更新 | 2026-05-14 |

---

## 1. 目标与边界

### 1.1 目标

把 M3 ingest 进来的孤立子图（Code 由 codesearch、LKML / Bug / Doc 由本项目）**焊成一张可多跳推理的关联网**。同时承担 Linux-Diag-Agent 与 CodeGraph MCP 服务之间的通信适配。

这是项目的**核心差异化壁垒**。

### 1.2 在范围

| 子能力 | 形态 | 输出 |
|--------|------|------|
| Trailer 全集抽取 | 离线批处理 | `kernel_commit.fixes_refs/reported_by/closes_refs/link_refs/cc_stable` |
| **OLK inclusion 解析 + olk↔upstream 桥接**（[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md)）| 离线批处理 | `kernel_commit.origin/upstream_commit/olk_inclusion_type` + `link_commit_commit(olk_backport_of)` |
| commit ↔ bug 链接 | 离线 + Zenodo 数据集 | `link_commit_bug`（含 confidence + source）|
| **patch ↔ landed_commit 反查** | 离线（git patch-id 主 + subject 兜底，详见 §3.3） | `lkml_patch.landed_commit` + `link_commit_message(link_type='patch_origin')` |
| commit ↔ LKML message 链接 | 离线（Link: trailer + Message-ID 引用）| `link_commit_message` |
| NVD ↔ Bugzilla CVE 桥接 | 离线 | `bug.cve_ids` 反向更新 |
| subsystem 推断 | 离线（v1.0 用 file_path，[ADR-008](../adr/ADR-008-reuse-codesearch.md) 留位 MAINTAINERS）| `kernel_commit.subsystem` |
| **CodeGraph MCP client（HTTP）** | 运行时 | 业务层调用接口 |
| 健康检查 + repo 映射 | 启动时 + 运行时 | CodeGraph 状态 |
| PG link_* ↔ Neo4j 同步 | 周度全量 rebuild + 对账 | Neo4j 关系图 |

### 1.3 不在范围

| 项 | 处理位置 |
|----|---------|
| 函数级 graph 节点 | CodeGraph SCIP（不在 Neo4j）|
| 调用链遍历 | 运行时调 CodeGraph `lookup_symbol(action='references')` |
| MAINTAINERS 完整解析 | v1.1+ |
| Stack frame → function 持久化 | 运行时调 CodeGraph `lookup_symbol`，不持久化 |
| `link_function_subsystem` 表 | v1.0 已删（M2），用 file_path 推断 |

## 2. 关键决策摘要

| # | 决策 | 出处 |
|---|------|------|
| D1 | patch ↔ landed_commit 反查 = **git patch-id 主 + subject 模糊兑底** | 本文 §3.3 |
| D2 | CodeGraph MCP 传输 = **HTTP** | [ADR-012](../adr/ADR-012-codesearch-http-transport.md) |
| D3 | subsystem 推断 = **file_path 最深公共目录 + majority vote** | 本文 §3.6 |
| D4 | PG ↔ Neo4j 同步 = **PG 实时写 + 周度全量 rebuild + 对账** | 本文 §5 |
| D5 | Cross-Graph Linker 触发时机 = **每周日 cron Phase 4 批量** | 本文 §6 |
| D6 | confidence 评分 = **来源驱动（0.5-1.0），查询时按 threshold 过滤** | 本文 §3.2 |
| D7 (F3) | 多层 Fixes 链 = **单跳边平铺写 PG/Neo4j；递归在查询时做（max_depth=5）** | 本文 §3.7 |
| D8 (F4) | short → full SHA 解析 = **按归属仓 git rev-parse（OLK/mainline 双仓）→ 跨仓兜底 → PG LIKE → GitHub API** | 本文 §3.1 |
| D9 (F5) | 无 Fixes trailer 修复 commit 路径 = **stack trace 函数名 + bug.stack_frames 集合包含 + 时间窗** | 本文 §3.8 |
| D10 (ADR-018) | commit 来源 = **OLK 内核仓（主）+ mainline（辅）双仓库**；OLK inclusion 解析 + olk↔upstream 桥接 | 本文 §3.9 |

## 3. Cross-Graph Linker 详细设计

### 3.1 Trailer 抽取

#### 全集 trailer 模式

| Trailer | 正则模式（核心部分） |
|---------|-------------------|
| `Fixes:` | `^Fixes:\s+([0-9a-f]{8,40})\b` |
| `Reported-by:` | `^Reported-by:\s+(.+?)\s+<(.+?)>` |
| `Closes:` | `^Closes:\s+(\S+)` |
| `Link:` | `^Link:\s+(https?://\S+)` |
| `Cc: stable` | `^Cc:\s+stable@vger\.kernel\.org\s*(?:#\s*(.+?))?` |
| `Tested-by:` | `^Tested-by:\s+(.+?)\s+<(.+?)>` |
| `Reviewed-by:` | `^Reviewed-by:\s+(.+?)\s+<(.+?)>` |
| `Acked-by:` | `^Acked-by:\s+(.+?)\s+<(.+?)>` |

**关键规则**：
- **Hash 长度容差**（F4，[Spike_Report](../spike/Spike_Report.md) §5 实证）：Fixes/landed_commit/cherry-pick 等 trailer 中的 hash 从 **7 字符到 40 字符**不等（实证：CVE-2024-35892 的 `Fixes: d636fc5` 是 7 字符）。短 hash 必须解析为 40 字符 full SHA 后才能在 `kernel_commit` 表查找
- **short → full SHA 解析路径**（双仓库，按优先级，[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md)）：
  1. **按 trailer 归属仓优先** `git rev-parse`：
     - 非 backport commit（`olk_inclusion_type ∉ {mainline, stable}`，即 hulk/driver/urma/… 原生）的 trailer → 在 **OLK 内核仓**解析
     - backport commit 上游原文区的 trailer、或 `origin='mainline'` commit 的 trailer → 在 **`linux-stable.git` 辅仓**解析
  2. 另一仓 `git rev-parse` 跨仓兜底
  3. PG `WHERE hash LIKE 'short%'`（commit 已 ingest 时；btree 索引前缀匹配）
  4. GitHub API `GET /repos/torvalds/linux/commits/<short>`（**仅对 mainline SHA**——OLK commit 在 GitHub 上不存在，openEuler 源在 atomgit）
- **歧义处理**：short hash 解析到多个 candidate 时，按时间窗（trailer 所在 commit 时间往前推 365 天）筛选；仍歧义则写 `quarantine` 表手工处理
- **解析失败**：trailer 抽取行保留（`kernel_commit.fixes_refs` 仍含原 short hash），但 `link_commit_commit.target_hash = NULL`，加 `unresolved_short_hash` 标记
- Trailer 必须在 commit body 的**末尾连续块**（避免误抽 example 代码或引用文本）
- 多行 trailer（如 `Cc: stable... # v5.10+`）正则需 multiline
- 解析失败时 commit body 全文留 `raw_metadata`，下游可手工抽样校验

实现：`graph/trailer_parser.py`，~200 行 + fixtures；`graph/short_hash_resolver.py`，~150 行。

### 3.2 commit ↔ bug 链接

#### 多源融合

```
来源                              confidence  link_type
─────────────────────────────────────────────────────────
Closes: bugzilla URL              1.0         closes
Closes: syzkaller URL             1.0         closes
Link: bugzilla URL                0.95        discussion
Link: syzkaller URL               0.95        discussion
bug.see_also 含 commit URL        0.9         (从 bug 侧反向)
Zenodo dataset                    0.92        zenodo
bug.description grep "fixed in"   0.7         body_grep
LKML thread 同时引用 commit+bug   0.5         (低 confidence)
```

#### 实现要点

- 所有候选边都写入 `link_commit_bug`，**不在写入时过滤** confidence
- 查询时按 `WHERE confidence >= $threshold` 过滤（默认 0.7）
- `source` 字段标记来源，便于调试 / 评测
- 同一 (commit, bug, link_type) 多次抽取 → UNIQUE 约束 + UPSERT，最高 confidence 胜出

### 3.3 patch ↔ landed_commit 反查

最难的一块。给定 LKML `[PATCH]` 邮件，找到 maintainer 合入主线/LTS 的 commit hash。

#### 主算法：git patch-id

git 自带 `git patch-id --stable` 能计算 patch 内容的稳定哈希（与文件顺序、行号等无关），相同 patch 内容产生相同 patch-id。

```python
def compute_patch_id(diff_text: str) -> str:
    """git patch-id --stable wrapper."""
    proc = subprocess.run(
        ['git', 'patch-id', '--stable'],
        input=diff_text, capture_output=True, text=True, check=True,
    )
    # 输出格式: "<patch-id-sha1> <commit-id>"
    return proc.stdout.split()[0]
```

**预备工作**：
- `kernel_commit` 表新增列 `patch_id TEXT`（+ btree 索引）
- M3 kernel_commit ingester 在落每个 commit 时一次性算 patch_id
- 全量 1.2M commit 算 patch_id：~3h 一次性

**反查流程**：
```python
def find_landed_commit(patch_msg: LkmlMessage) -> tuple[str, float] | None:
    """主 + 兜底两段式查找。返回 (commit_hash, confidence) 或 None。"""
    
    # Step 1: 从 patch 邮件 body 抽 diff
    diff = extract_diff_from_email(patch_msg.body)
    if not diff:
        return None
    
    # Step 2: 主算法 — git patch-id 精确匹配
    patch_id = compute_patch_id(diff)
    matches = pg.query("""
        SELECT hash, subject FROM kernel_commit
        WHERE patch_id = %s
    """, patch_id)
    
    if len(matches) == 1:
        return (matches[0].hash, 1.0)  # 唯一匹配，最高置信
    elif len(matches) > 1:
        # patch 被 cherry-pick 到多分支：用 subject 进一步筛
        return _filter_by_subject_in_candidates(matches, patch_msg.subject), 0.95
    
    # Step 3: 兜底 — subject 字符串精确匹配 + 时间窗
    clean_subj = re.sub(r'\[[^\]]*\]\s*', '', patch_msg.subject).strip()
    sent = patch_msg.sent_date
    matches = pg.query("""
        SELECT hash FROM kernel_commit
        WHERE subject = %s
          AND commit_date BETWEEN %s AND %s
    """, clean_subj, sent, sent + timedelta(days=90))
    
    if len(matches) == 1:
        return (matches[0].hash, 0.85)
    elif len(matches) > 1:
        return (sorted(matches)[0].hash, 0.7)  # 取最早 commit（最先 land 的那次）
    
    # Step 4: 模糊 subject 匹配（pg_trgm similarity > 0.9）
    fuzzy = pg.query("""
        SELECT hash, similarity(subject, %s) AS sim
        FROM kernel_commit
        WHERE subject %% %s
          AND commit_date BETWEEN %s AND %s
        ORDER BY sim DESC LIMIT 1
    """, clean_subj, clean_subj, sent, sent + timedelta(days=90))
    
    if fuzzy and fuzzy[0].sim > 0.9:
        return (fuzzy[0].hash, 0.6)
    
    return None
```

#### 性能

- patch-id 反查：单次 < 5ms（btree 索引命中）
- subject 兜底：单次 < 50ms（pg_trgm 索引）
- 一周新增 ~3K patch 邮件 → ~30s 全部反查

#### 验收指标

- 反查命中率：≥ 70%（v1.2 评测目标）
- 高置信（confidence ≥ 0.95）占比：≥ 80% 命中
- 误匹配率（评测样本）：≤ 5%

### 3.4 commit ↔ LKML message 链接

```python
def link_commit_to_messages(commit: KernelCommit) -> list[Link]:
    links = []
    
    # 来源 1: Link: trailer 指向 lore.kernel.org
    for url in commit.link_refs:
        if 'lore.kernel.org' in url:
            msg_id = parse_lore_url(url)
            links.append(Link(
                commit_hash=commit.hash, message_id=msg_id,
                link_type='discussion', confidence=1.0, source='link_trailer'
            ))
    
    # 来源 2: patch_origin（由 §3.3 patch 反查产生）
    # 已写入 lkml_patch.landed_commit，此处补强写入 link_commit_message
    
    # 来源 3: commit body grep Message-ID
    msg_ids = re.findall(r'<[^<>@\s]+@[^<>\s]+>', commit.body)
    for msg_id in msg_ids:
        if pg.exists('lkml_message', message_id=msg_id):
            links.append(Link(
                commit_hash=commit.hash, message_id=msg_id,
                link_type='discussion', confidence=0.8, source='body_grep'
            ))
    
    return links
```

### 3.5 NVD ↔ Bugzilla CVE 桥接

```python
def bridge_nvd_to_bug():
    """从 NVD references 抽 Bugzilla URL，回填 bug.cve_ids。"""
    
    for cve in pg.query("SELECT * FROM cve WHERE NOT references_processed"):
        for ref in cve.raw_nvd.get('references', []):
            url = ref.get('url', '')
            
            if 'bugzilla.kernel.org/show_bug.cgi?id=' in url:
                bug_id = parse_bz_url(url)
                pg.execute("""
                    UPDATE bug SET cve_ids = array_append(cve_ids, %s)
                    WHERE source = 'bugzilla.kernel.org' AND source_id = %s
                      AND NOT (%s = ANY(cve_ids))
                """, cve.cve_id, bug_id, cve.cve_id)
            
            elif 'syzkaller.appspot.com/bug?id=' in url:
                crash_id = parse_syzbot_url(url)
                pg.execute("""
                    UPDATE bug SET cve_ids = array_append(cve_ids, %s)
                    WHERE source = 'syzbot' AND source_id = %s
                      AND NOT (%s = ANY(cve_ids))
                """, cve.cve_id, crash_id, cve.cve_id)
            
            # commit URL → 更新 cve.fix_commits
            elif 'git.kernel.org' in url and '/commit/?id=' in url:
                commit_hash = parse_kernel_org_commit_url(url)
                pg.execute("""
                    UPDATE cve SET fix_commits = array_append(fix_commits, %s)
                    WHERE cve_id = %s AND NOT (%s = ANY(fix_commits))
                """, commit_hash, cve.cve_id, commit_hash)
        
        pg.execute("UPDATE cve SET references_processed = true WHERE cve_id = %s", cve.cve_id)
```

### 3.6 subsystem 推断（v1.0 简化）

```python
KNOWN_SUBSYSTEMS = {
    'mm', 'fs', 'net', 'drivers', 'arch', 'kernel', 'security',
    'sound', 'block', 'crypto', 'lib', 'scripts', 'tools',
    'samples', 'init', 'ipc', 'rust', 'virt',
}

def infer_subsystem(changed_files: list[str]) -> str:
    """从 commit 改动文件路径推断 subsystem。"""
    if not changed_files:
        return 'unknown'
    
    top_dirs = [Path(f).parts[0] for f in changed_files]
    valid = [d for d in top_dirs if d in KNOWN_SUBSYSTEMS]
    if not valid:
        return 'unknown'
    
    counter = Counter(valid)
    most_common, count = counter.most_common(1)[0]
    
    # majority < 50% 视为跨子系统
    if count / len(valid) < 0.5:
        return 'multi'
    
    # drivers/ 细分到第二段
    if most_common == 'drivers':
        driver_subs = []
        for f in changed_files:
            parts = Path(f).parts
            if parts[0] == 'drivers' and len(parts) > 1:
                driver_subs.append(parts[1])
        if driver_subs:
            return f"drivers/{Counter(driver_subs).most_common(1)[0][0]}"
    
    return most_common
```

**示例**：
- `mm/oom_kill.c` + `mm/page_alloc.c` → `mm`
- `drivers/net/e1000/main.c` + `drivers/net/e1000/init.c` → `drivers/net`
- `mm/oom_kill.c` + `fs/inode.c` + `kernel/sched.c` → `multi`

**MAINTAINERS 解析延后到 v1.1**（[ADR-008](../adr/ADR-008-reuse-codesearch.md)）。

### 3.7 多层嵌套 Fixes 链支持（F3）

来自 [Spike_Report.md](../spike/Spike_Report.md) §3.1 实证：CVE-2024-35892 的修复链是**嵌套三层**的：

```
fix       b7d1ce2c... "net/sched: fix lockdep splat ..."
  ↓ Fixes:
introduce-1  d636fc5d... "net: sched: add rcu annotations ..."  ← 自己也有 Fixes
  ↓ Fixes:
introduce-2  3a7d0d07... "net: sched: extend Qdisc with rcu"     ← 第二跳引入
```

**约束**：`link_commit_commit` 表必须支持**多跳遍历**；给定一个 fix commit，能查询出完整 Fixes 反向链。

#### Schema 影响

无需变更。M2 现有 `link_commit_commit`（`source_hash`, `target_hash`, `link_type='fixes'`, `confidence`, `source`）已足够：单跳边平铺，递归在查询时做。

#### 查询接口

```python
def trace_fixes_chain(commit_hash: str, max_depth: int = 5) -> list[str]:
    """递归回溯 Fixes 链。返回 [fix → introduce-1 → introduce-2 → ...]。"""
    chain, current, seen = [commit_hash], commit_hash, {commit_hash}
    for _ in range(max_depth):
        row = pg.query_one("""
            SELECT target_hash FROM link_commit_commit
            WHERE source_hash = %s AND link_type = 'fixes' AND target_hash IS NOT NULL
            ORDER BY confidence DESC LIMIT 1
        """, current)
        if not row or row.target_hash in seen:
            break
        current = row.target_hash
        seen.add(current); chain.append(current)
    return chain
```

Neo4j 等价：

```cypher
MATCH path = (fix:Commit {hash: $hash})-[:FIXES*1..5]->(intro:Commit)
RETURN path
```

#### 验收

v1.2 评测：30 例样本中至少 3 例的 fix commit 能展开 **≥ 2 层** Fixes 链（活跃子系统如 net/sched 常见）。

### 3.8 "无 Fixes trailer 修复 commit" 间接关联（F5）

来自 [Spike_Report.md](../spike/Spike_Report.md) §3.3 实证：CVE-2024-40998 fix commit `b4b4fda34e...` **完全没有 `Fixes:` trailer**——这是并发竞态 / 设计性 bug，修复方式是调整初始化顺序，不针对某一引入 commit。

**仅靠 §3.1 trailer 抽取的单一路径会漏掉这类 commit**。M4 必须支持双路径：

| 路径 | 来源 | 已在节 |
|------|------|-------|
| **A** | 从 commit 出发的 trailer（Fixes/Closes/Link/Reported-by）| §3.1 + §3.2 + §3.4 |
| **B**（新增） | 从 bug / CVE 出发的反向关联 | 本节 |

#### 路径 B 的三种证据

| 证据来源 | 落地方式 | confidence |
|---------|---------|-----------|
| **CVE references 含 fix commit URL** | §3.5 NVD 桥接已部分覆盖；补强写 `link_commit_bug(link_type='cve_reference')` | 0.95 |
| **commit message 内置 stack trace 函数名** | 抽 commit body 中 stack trace（spike 案例 C 实证：`__ext4_fill_super` / `ext4_orphan_cleanup` / `__ext4_msg` 等），每个函数调 CodeGraph `lookup_symbol` 取 file_path；再与同 subsystem 近期 bug.stack_frames 做集合包含匹配（时间窗 ±90 天）| 0.7 |
| **commit subject 字面匹配 bug title** | 如 "ext4: fix uninitialized ratelimit_state->lock" 命中 bug "uninitialized ratelimit_state->lock crash" | 0.6 |

#### 抽取算法

```python
async def link_no_fixes_trailer_commits():
    """处理无 Fixes trailer 的修复 commit（F5 路径 B）。"""
    
    # Step 1: 找出"像是修复但没有 Fixes trailer"的 commit
    candidates = pg.query("""
        SELECT hash, subject, body, commit_date, subsystem
        FROM kernel_commit c
        WHERE (c.fixes_refs IS NULL OR cardinality(c.fixes_refs) = 0)
          AND (
            c.subject ~* '\\bfix\\b' OR
            c.body ~* 'syzbot reported|KASAN|use-after-free|null-ptr-deref|deadlock|panic|WARN_ON'
          )
    """)
    
    for commit in candidates:
        # Step 2: 抽 commit body 中的 stack trace 函数名（复用诊断 agent 的 stack 解析器）
        funcs = extract_stack_frame_functions(commit.body)
        if len(funcs) < 2:
            continue  # 单函数证据弱，跳过
        
        # Step 3: CodeGraph 验证 + 取 file_path（注：函数名匹配，不用行号 — ADR-008 F6）
        verified = []
        for fn in funcs:
            hit = await codegraph.lookup_symbol(
                symbol=fn, action='definition',
                repo=resolve_repo('v6.6')
            )
            if hit:
                verified.append(fn)
        
        if len(verified) < 2:
            continue
        
        # Step 4: 反查 bug 表（stack_frames 集合包含 + 时间窗 + 子系统）
        candidate_bugs = pg.query("""
            SELECT id FROM bug
            WHERE reported_date BETWEEN %s - INTERVAL '90 days'
                                     AND %s + INTERVAL '30 days'
              AND stack_frames @> %s::text[]
              AND (subsystem IS NULL OR subsystem = %s)
            LIMIT 10
        """, commit.commit_date, commit.commit_date, verified, commit.subsystem)
        
        # Step 5: 写关联
        for bug in candidate_bugs:
            pg.execute("""
                INSERT INTO link_commit_bug
                  (commit_hash, bug_id, link_type, confidence, source)
                VALUES (%s, %s, 'stack_trace_match', 0.7, 'no_fixes_trailer_path')
                ON CONFLICT DO NOTHING
            """, commit.hash, bug.id)
```

#### 性能与触发

- 候选 commit：每周 ~3K commits × 5-10% 无 trailer 但似修复 → **~150-300 候选/周**
- 每候选 3-10 次 CodeGraph 调用 → 全周累计 ~1500 次调用，端到端 **~5 min**
- 触发：weekly_sync.sh Phase 4 的末尾（在 §3.1-3.6 之后）

#### 验收

- v1.2 评测：30 例样本中**预留 5-8 例**属于"无 Fixes trailer 修复"形态（并发 / 竞态 / 设计性），验证 KG 能找到对应 bug 关联
- false-positive 率：≤ 15%（人工抽样）

### 3.9 OLK commit inclusion 解析 + olk↔upstream linker（[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md)）

诊断目标是 OLK 内核，commit 主仓库 = OLK 内核仓。OLK commit 通过 message 顶部 **inclusion 头**自我标注来源；M3 §3.6 commit ingester 在 Phase A 调用本节解析逻辑。

#### inclusion 头格式（实测 OLK-6.6，2026-05-15）

`stable inclusion` 真实样本（commit `99795c4ef16c`）：

```
stable inclusion
from stable-v6.6.124
commit 7c54d3f5ebbc5982daaa004260242dc07ac943ea         ← stable 树 SHA（非 mainline）
category: bugfix
bugzilla: https://atomgit.com/src-openeuler/kernel/issues/13892
CVE: CVE-2026-23261
Reference: https://git.kernel.org/.../stable/linux.git/commit/?id=7c54d3f5...

--------------------------------                          ← 分隔线（连字符数不定）

[ Upstream commit d1877cc7270302081a315a81a0ee8331f19f95c8 ]   ← 真正的 mainline SHA

<上游原始 commit message 原文，含 Fixes:/Link: trailer>
```

`mainline inclusion` 的头部 `commit <sha>` 直接是 mainline SHA（无 `[ Upstream commit ]`）。

#### inclusion 类型（开放集合）

实测 OLK-6.6 近 8000 个非 merge commit 出现 **40+ 种** inclusion tag，**无需枚举**——按二分规则处理：

| 判定 | inclusion tag | upstream 锚点 | 链回 kernel.org 生态 |
|------|--------------|--------------|---------------------|
| **backport** | `mainline`（实测 1100）| 头部 `commit <sha>` = mainline SHA | ✓ |
| **backport** | `stable`（实测 5129）| 头部 `commit` = stable SHA；`[ Upstream commit ]` = mainline SHA（覆盖 ~67%）| ✓ |
| **原生** | 其它任意 tag（`hulk`/`driver`/`urma`/`sunway`/`kunpeng`/厂商名…）| 无 | ✗ 标「无上游讨论」|

**判定规则**：`tag ∈ {mainline, stable}` → backport；否则 → 原生。

#### parser 算法

```python
OLK_INCLUSION_RE  = re.compile(
    r'^\s*([a-zA-Z][\w -]*?)\s+inclusion\s*$', re.IGNORECASE | re.MULTILINE)
OLK_HEAD_COMMIT_RE = re.compile(
    r'^\s*commit\s+([0-9a-f]{8,40})\b', re.IGNORECASE | re.MULTILINE)
OLK_UPSTREAM_RE   = re.compile(
    r'\[\s*Upstream commit\s+([0-9a-f]{8,40})\s*\]', re.IGNORECASE)
OLK_BUGZILLA_RE   = re.compile(r'^\s*bugzilla:\s*(\S+)', re.IGNORECASE | re.MULTILINE)
OLK_CVE_RE        = re.compile(r'^\s*CVE:\s*(CVE-\d{4}-\d+)', re.IGNORECASE | re.MULTILINE)
MR_MERGE_RE       = re.compile(r'^See merge request:\s', re.MULTILINE)

def parse_olk_inclusion(subject: str, body: str) -> OlkInclusionInfo:
    """解析 OLK commit message。M3 Phase A 调用。"""
    # MR merge commit（实测 ~7.6%）：无 inclusion 头，跳过
    if MR_MERGE_RE.search(body):
        return OlkInclusionInfo(kind='merge')

    parts = re.split(r'^-{3,}\s*$', body, maxsplit=1, flags=re.MULTILINE)
    head, tail = parts[0], (parts[1] if len(parts) > 1 else '')
    m = OLK_INCLUSION_RE.search(head)
    if not m:
        return OlkInclusionInfo(kind='no_header')        # 老 commit / 异常
    tag = m.group(1).strip().lower()

    upstream = None
    if tag == 'mainline':
        hm = OLK_HEAD_COMMIT_RE.search(head)
        upstream = hm.group(1) if hm else None
    elif tag == 'stable':
        # 优先取分隔线下方的 [ Upstream commit ]；缺失则降级用头部 stable SHA
        um = OLK_UPSTREAM_RE.search(tail)
        if um:
            upstream = um.group(1)
        else:
            hm = OLK_HEAD_COMMIT_RE.search(head)
            upstream = hm.group(1) if hm else None       # 降级：stable SHA
    # tag ∉ {mainline, stable} → 原生，upstream 保持 None

    return OlkInclusionInfo(
        kind='backport' if tag in ('mainline', 'stable') else 'native',
        inclusion_tag=tag, upstream=upstream,
        bugzilla=_first(OLK_BUGZILLA_RE, head),
        cve=_first(OLK_CVE_RE, head),
    )
```

#### olk_commit ↔ upstream_commit linker

```python
def link_olk_to_upstream(olk_commit):
    info = parse_olk_inclusion(olk_commit.subject, olk_commit.body)
    if info.kind != 'backport':
        return                                       # C1：原生 / merge / 无头 不建链
    if not info.upstream:
        mark_quarantine(olk_commit, 'backport_missing_upstream_sha')
        return
    # C2：在 linux-stable.git 辅仓验证可解析（mainline + stable 分支都在此仓）
    full = resolve_in_stable_repo(info.upstream)
    if not full:
        olk_commit.metadata['unresolved_upstream'] = info.upstream
        return                                       # 解析不到，不硬链
    pg.execute("""
        INSERT INTO link_commit_commit (source_hash, target_hash, link_type, confidence, source)
        VALUES (%s, %s, 'olk_backport_of', 1.0, 'olk_inclusion_header')
        ON CONFLICT DO NOTHING
    """, olk_commit.hash, full)
    pg.execute("UPDATE kernel_commit SET upstream_commit = %s WHERE hash = %s",
               full, olk_commit.hash)
```

`confidence = 1.0`：inclusion 头是 openEuler 强制规范，显式标注。`stable inclusion` 无 `[ Upstream commit ]` 时（实测 ~33%）upstream 降级为 stable 树 SHA——它在 `linux-stable.git` 可解析，但链到的是 stable commit 而非 mainline commit，M7 据此判定证据等级。

#### 三个准确性强化条件落地（[ADR-018](../adr/ADR-018-commit-source-olk-kernel.md)）

| 条件 | 落地 |
|------|------|
| **C1** 按 tag 二分判定 | `tag ∈ {mainline,stable}` ? backport : 原生；inclusion 头缺失 / merge commit / 无法解析 → 一律按非 backport 处理，不建链；**绝不猜测** |
| **C2** 上游 SHA 必须验证可解析 | `resolve_in_stable_repo` 失败 → 写 `unresolved_upstream` 标记，不建链 |
| **C3** 代码真相以 olk-kernel 为准 | 本 linker 只建 commit↔commit 关系；M7 引用代码 / 行号时强制走 CodeGraph `olk-kernel`，上游仅供讨论脉络 |

#### 诊断证据分级（供 [M7](M7_diagnosis_agent.md)）

| OLK commit 类型 | 证据等级 | M7 报告标注 |
|----------------|---------|------------|
| `mainline inclusion`，或 `stable inclusion` 取到 `[ Upstream commit ]` | **高**（mainline-backed）| 完整链到 LKML / bug / CVE |
| `stable inclusion` 仅 stable SHA（无 `[ Upstream commit ]`）| **中高** | 链到 stable commit；mainline 讨论需再跳一步 |
| 原生 inclusion（hulk/driver/urma/…）| **中** | 「openEuler 特有，无上游讨论」+ atomgit bugzilla 指针 |
| inclusion 头缺失 / MR merge commit | **低** | 「commit 元数据不完整」|

#### 自研工作量

`graph/linkers/olk_inclusion.py` ~280 行 + fixtures（覆盖 backport / 原生 / merge / 无头 + 分隔线变体 + stable 有无 `[ Upstream commit ]` + 大小写）。

## 4. CodeGraph 集成胶水

### 4.1 MCP HTTP 传输

按 [ADR-012](../adr/ADR-012-codesearch-http-transport.md)，diag-agent 与 codesearch 通过 **HTTP MCP 传输**通信。

**部署形态**：

```
┌───────────────────────────────┐         HTTP MCP        ┌──────────────────────────┐
│ Linux-Diag-Agent              │  ◀═════════════════════▶│ CodeGraph MCP server    │
│ - CodeGraphHttpClient        │   localhost:8765         │ FastMCP HTTP transport   │
│ - retry / timeout / 健康检查  │                          │ 已索引 olk-kernel-v6.6   │
│                               │                          │           v5.10          │
└───────────────────────────────┘                          └──────────────────────────┘
```

**与 CodeGraph 协调点**：codesearch 当前架构默认 `FastMCP stdio`。v1 启动前需要 codesearch 提供 HTTP server 模式：
- FastMCP 原生支持 `streamable-http` transport（SSE 或 HTTP/POST）
- codesearch 侧仅需新增一个启动入口：`codesearch-server-http --host 0.0.0.0 --port 8765`
- 工作量预估：codesearch 侧 ~50 行（FastMCP 配置 + 启动脚本）

### 4.2 client 设计

```python
# clients/codegraph/client.py
import httpx
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession

class CodeGraphClient:
    """HTTP MCP client to CodeGraph (implemented by the codesearch project)."""
    
    def __init__(self, base_url: str = "http://localhost:8765"):
        self.base_url = base_url
        self._session: ClientSession | None = None
        self._http_client: httpx.AsyncClient | None = None
    
    async def __aenter__(self):
        # 建立 streamable-http MCP session
        self._streams_ctx = streamablehttp_client(self.base_url)
        read_stream, write_stream, _ = await self._streams_ctx.__aenter__()
        self._session = ClientSession(read_stream, write_stream)
        await self._session.initialize()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self._streams_ctx.__aexit__(exc_type, exc_val, exc_tb)
    
    # ---- 工具调用 wrappers ----
    
    async def list_repos(self) -> list[RepoInfo]:
        result = await self._session.call_tool('list_repos', {})
        return parse_repo_info(result)
    
    async def search_code(self, query: str, repos: list[str], 
                          mode: Literal['literal', 'regex', 'symbol'] = 'literal',
                          limit: int = 10, language: str | None = None) -> list[CodeHit]:
        args = {'query': query, 'repos': repos, 'mode': mode, 'limit': limit}
        if language:
            args['language'] = language
        result = await self._session.call_tool('search_code', args)
        return parse_code_hits(result)
    
    async def lookup_symbol(self, symbol: str, 
                             action: Literal['definition', 'references', 'implementations'],
                             repo: str | None = None) -> list[SymbolHit]:
        args = {'symbol': symbol, 'action': action}
        if repo:
            args['repo'] = repo
        result = await self._session.call_tool('lookup_symbol', args)
        return parse_symbol_hits(result)
    
    async def browse_docs(self, repo: str | None = None) -> str:
        args = {} if repo is None else {'repo': repo}
        result = await self._session.call_tool('browse_docs', args)
        return result.content[0].text  # FastMCP 默认 text content
    
    async def browse_doc_sections(self, repo: str, source_path: str) -> str:
        result = await self._session.call_tool('browse_doc_sections', 
                                                {'repo': repo, 'source_path': source_path})
        return result.content[0].text
    
    async def read_doc_section(self, source_path: str, 
                                section: str | None = None,
                                repo: str | None = None) -> str:
        args = {'source_path': source_path}
        if section:
            args['section'] = section
        if repo:
            args['repo'] = repo
        result = await self._session.call_tool('read_doc_section', args)
        return result.content[0].text
    
    async def search_docs(self, query: str, repo: str | None = None, limit: int = 5) -> list[DocHit]:
        args = {'query': query, 'limit': limit}
        if repo:
            args['repo'] = repo
        result = await self._session.call_tool('search_docs', args)
        return parse_doc_hits(result)
    
    async def read_file(self, repo: str, path: str, 
                        line_start: int = 1, line_end: int | None = None) -> str:
        args = {'repo': repo, 'path': path, 'line_start': line_start}
        if line_end is not None:
            args['line_end'] = line_end
        result = await self._session.call_tool('read_file', args)
        return result.content[0].text
    
    async def get_outline(self, repo: str, path: str) -> str:
        result = await self._session.call_tool('get_outline', {'repo': repo, 'path': path})
        return result.content[0].text
    
    async def search_symbol_docs(self, query: str, limit: int = 5) -> list[SymbolDocHit]:
        result = await self._session.call_tool('search_symbol_docs', 
                                                {'query': query, 'limit': limit})
        return parse_symbol_doc_hits(result)
```

### 4.3 repo 映射

```python
# clients/codegraph/repos.py
KERNEL_VERSION_TO_REPO = {
    'v6.6': 'olk-kernel-v6.6',
    'v5.10': 'olk-kernel-v5.10',
    # 未来: 'v6.12': 'olk-kernel-v6.12',
}

def resolve_repo(kernel_version: str) -> str:
    if kernel_version not in KERNEL_VERSION_TO_REPO:
        raise ValueError(f"Unknown kernel version: {kernel_version}. "
                         f"Known versions: {list(KERNEL_VERSION_TO_REPO)}")
    return KERNEL_VERSION_TO_REPO[kernel_version]

def resolve_repos(kernel_versions: list[str]) -> list[str]:
    return [resolve_repo(v) for v in kernel_versions]
```

### 4.4 健康检查

```python
# clients/codegraph/healthcheck.py
class HealthStatus(Enum):
    HEALTHY = "healthy"
    UNREACHABLE = "unreachable"
    MISSING_REPOS = "missing_repos"
    SCIP_INCOMPLETE = "scip_incomplete"
    SMOKE_FAILED = "smoke_failed"

@dataclass
class HealthReport:
    status: HealthStatus
    error: str | None = None
    repos: dict[str, RepoStatus] = field(default_factory=dict)

async def check_codegraph_health(client: CodeGraphClient) -> HealthReport:
    """启动时调用。"""
    
    # Step 1: HTTP 可达 + MCP session 可初始化
    try:
        async with client:
            repos = await client.list_repos()
    except Exception as e:
        return HealthReport(status=HealthStatus.UNREACHABLE, error=str(e))
    
    # Step 2: 必需 repo 已索引
    required = set(KERNEL_VERSION_TO_REPO.values())
    available = {r.name for r in repos}
    missing = required - available
    if missing:
        return HealthReport(
            status=HealthStatus.MISSING_REPOS,
            error=f"Required repos not indexed: {missing}",
            repos={r.name: RepoStatus.from_repo(r) for r in repos}
        )
    
    # Step 3: SCIP 状态（对必需 repo）
    for repo in repos:
        if repo.name in required and not repo.scip_ready:
            return HealthReport(
                status=HealthStatus.SCIP_INCOMPLETE,
                error=f"{repo.name}: SCIP not ready",
                repos={r.name: RepoStatus.from_repo(r) for r in repos}
            )
    
    # Step 4: smoke test
    try:
        result = await client.lookup_symbol(
            symbol='task_struct', action='definition', 
            repo='olk-kernel-v6.6'
        )
        if not result:
            return HealthReport(status=HealthStatus.SMOKE_FAILED, 
                                error='lookup_symbol returned empty')
    except Exception as e:
        return HealthReport(status=HealthStatus.SMOKE_FAILED, error=str(e))
    
    return HealthReport(status=HealthStatus.HEALTHY)
```

### 4.5 错误处理与降级

| 错误 | 处理 |
|------|------|
| HTTP 5xx | tenacity 3 次指数退避 |
| HTTP 4xx | 立即抛出 `CodeGraphClientError`，不重试 |
| MCP session 断开 | 自动重连 + 重试当前请求（1 次）|
| 工具调用超时（> 30s）| 抛 `CodeGraphTimeout`，业务层降级（如 `search_code` 失败可降到 LKML 关键词检索）|
| `repo not indexed` | 应在启动时被 healthcheck 拦截；运行时遇到属于异常状态 |
| 持续不可用 > 5 分钟 | 进入"降级模式"：agent 拒绝处理需源码/文档检索的查询，告知用户 |

## 5. PG ↔ Neo4j 同步策略

### 5.1 角色分工

| 维度 | PG | Neo4j |
|------|-----|-------|
| 详细字段 | ★ 主存储 | minimal id 引用 |
| 实时写 | ★ Cross-Graph Linker 直接写 link_* | 不实时 |
| 周度 rebuild | — | ★ 从 PG 全量重建 |
| 多跳图遍历 | ❌ 慢 | ★ Cypher 快 |
| 单点详情 | ★ 索引命中 | ❌ |

### 5.2 周度 rebuild 流程

```python
# graph/neo4j_rebuild.py
async def weekly_rebuild():
    """每周日 ingestion + linker 完成后跑。"""
    
    log("Starting Neo4j rebuild...")
    
    # Phase 1: 清空旧关系（节点用 MERGE 幂等，不清）
    neo4j.run("MATCH ()-[r]->() DELETE r")
    
    # Phase 2: 重建节点
    rebuild_commit_nodes()       # ~1.2M
    rebuild_bug_nodes()           # ~250K (kernel.org BZ + syzbot)
    rebuild_message_nodes()       # ~3M
    rebuild_thread_nodes()        # ~600K
    rebuild_cve_nodes()           # ~30K
    rebuild_subsystem_nodes()     # 数十个
    
    # Phase 3: 重建关系
    rebuild_commit_bug_relations()     # FIXES / CLOSES / INTRODUCED_BUG
    rebuild_commit_message_relations() # DISCUSSED_IN / PATCH_FROM
    rebuild_commit_subsystem()         # BELONGS_TO
    rebuild_message_thread()           # IN_THREAD
    rebuild_message_reply()            # IN_REPLY_TO
    rebuild_review_relations()         # REVIEWS
    rebuild_bug_cve()                  # HAS_CVE
    rebuild_bug_message()              # REPORTED_IN
    
    # Phase 4: 对账
    report = audit_pg_neo4j_consistency()
    if report.max_drift > 0.05:
        send_alert(report)
    log(f"Rebuild done. Audit: {report}")
```

### 5.3 节点构建实例

```python
def rebuild_commit_nodes():
    """批量 MERGE Commit 节点。"""
    
    BATCH = 1000
    offset = 0
    while True:
        commits = pg.query("""
            SELECT hash, short_hash, subject, commit_date, subsystem
            FROM kernel_commit
            ORDER BY hash LIMIT %s OFFSET %s
        """, BATCH, offset)
        if not commits:
            break
        
        neo4j.run("""
            UNWIND $rows AS row
            MERGE (c:Commit {hash: row.hash})
            SET c.short_hash = row.short_hash,
                c.subject = row.subject,
                c.date = row.commit_date,
                c.subsystem = row.subsystem
        """, rows=[dict(c) for c in commits])
        
        offset += BATCH
        log(f"  Commits: {offset} / ~1.2M")
```

### 5.4 关系构建实例

```python
def rebuild_commit_bug_relations():
    """按 link_type 分别建关系，保留 confidence + source 属性。"""
    
    BATCH = 5000
    for link_type in ['fixes', 'closes', 'introduced_bug']:
        offset = 0
        while True:
            links = pg.query("""
                SELECT l.commit_hash, b.source as bug_source, b.source_id as bug_source_id,
                       l.confidence, l.source
                FROM link_commit_bug l
                JOIN bug b ON b.id = l.bug_id
                WHERE l.link_type = %s AND l.confidence >= 0.7
                ORDER BY l.id LIMIT %s OFFSET %s
            """, link_type, BATCH, offset)
            if not links:
                break
            
            rel_label = link_type.upper()
            neo4j.run(f"""
                UNWIND $rows AS row
                MATCH (c:Commit {{hash: row.commit_hash}})
                MATCH (b:Bug {{source: row.bug_source, source_id: row.bug_source_id}})
                MERGE (c)-[r:{rel_label}]->(b)
                SET r.confidence = row.confidence, r.source = row.source
            """, rows=[dict(l) for l in links])
            
            offset += BATCH
```

### 5.5 性能预算

| Phase | 耗时（v1.2 末） |
|-------|---------------|
| 清空关系 | <10s |
| 重建节点（commit + message 主要）| ~15min |
| 重建关系（commit-bug-message 主要）| ~20min |
| 对账 | ~2min |
| **总** | **~30-45min** |

### 5.6 对账机制

```python
def audit_pg_neo4j_consistency() -> AuditReport:
    """对照 PG link_* 与 Neo4j 关系数。"""
    
    audits = []
    
    # commit-bug
    pg_count = pg.query("""
        SELECT link_type, COUNT(*) 
        FROM link_commit_bug 
        WHERE confidence >= 0.7 
        GROUP BY link_type
    """)
    for row in pg_count:
        neo4j_count = neo4j.run(f"""
            MATCH ()-[r:{row.link_type.upper()}]->() RETURN COUNT(r)
        """).single()[0]
        drift = abs(row.count - neo4j_count) / max(row.count, 1)
        audits.append(AuditEntry(
            relation=row.link_type, pg=row.count, neo4j=neo4j_count, drift=drift
        ))
    
    # commit-message, bug-cve, message reply, ... 同上
    
    return AuditReport(
        entries=audits,
        max_drift=max(a.drift for a in audits),
        timestamp=now()
    )
```

## 6. 触发时机与调度

Cross-Graph Linker 作为 `scripts/weekly_sync.sh` 的 Phase 4 + 5：

```bash
# Phase 1-3: M3 ingestion （LKML, BZ, syzbot, NVD 并行 + kernel_commit + Zenodo）

# Phase 4: Cross-Graph Linker
python -m graph.linker run_all
#   ├─ trailer 抽取（commit body）
#   ├─ commit-bug 链接（多源融合）
#   ├─ patch ↔ commit 反查（git patch-id + subject）
#   ├─ commit-message 链接
#   ├─ NVD-Bug CVE 桥接
#   └─ subsystem 推断

# Phase 5: Neo4j rebuild + 对账
python -m graph.neo4j_rebuild

# Phase 6: 健康检查 + 告警
python -m scripts.sync_audit
```

总耗时：Phase 4 ~15min + Phase 5 ~45min = **~1h**。

## 7. 文件结构

```
graph/
├── __init__.py
├── linker.py                        # 总入口
├── trailer_parser.py
├── short_hash_resolver.py           # F4: git rev-parse / PG LIKE / GitHub API 三级兜底
├── stack_frame_extractor.py         # F5: commit body 中的 stack trace 函数名抽取
├── linkers/
│   ├── commit_bug.py                # §3.2 + §3.5
│   ├── patch_commit.py              # §3.3 git patch-id + subject 兜底
│   ├── commit_message.py            # §3.4
│   ├── commit_commit.py             # F3: §3.7 Fixes 链单跳边（递归查询）
│   ├── no_fixes_linker.py           # F5: §3.8 路径 B（stack-trace 反查）
│   ├── olk_inclusion.py             # ADR-018: §3.9 OLK inclusion 解析 + olk↔upstream
│   ├── nvd_bug.py
│   └── subsystem.py
├── neo4j_rebuild.py
├── audit.py
└── tests/
    ├── fixtures/                    # commit body / patch mbox / bug json
    └── ...

clients/
└── codegraph/
    ├── __init__.py
    ├── client.py                    # HTTP MCP client
    ├── repos.py                     # version → repo map
    ├── healthcheck.py
    ├── types.py                     # RepoInfo, CodeHit, SymbolHit, DocHit, ...
    └── tests/

scripts/
└── weekly_sync.sh                   # Phase 4 + 5 调用
```

## 8. 测试策略

| 层 | 测什么 |
|----|--------|
| 单元 | trailer 正则、git patch-id 包装、subsystem 推断、CVE URL 解析 |
| 集成 | Fixture 跑端到端 linker；校验 PG link_* 行数 + Neo4j 关系数一致 |
| Smoke | CodeGraph healthcheck 与每个工具一次调用 |
| 对账 | rebuild 后跑 audit；drift < 5% |
| Property-based | hypothesis 生成各种 trailer 变体 |

## 9. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| Trailer 正则误抽 | quarantine 行数 | 人工抽样校验；多 fixture |
| patch-id 反查不命中 | `lkml_patch.landed_commit IS NULL` 比例 | subject 兜底 + 时间窗调整 |
| CodeGraph HTTP server 没启动 | healthcheck failed | 启动脚本依赖 codesearch ready |
| Neo4j rebuild 时间过长（> 1h） | duration metric | v1.1 改增量 rebuild |
| PG ↔ Neo4j drift > 10% | audit metric | 手工排查 + 强制 rebuild |
| codesearch repo 名变更 | startup check 失败 | KERNEL_VERSION_TO_REPO 配置版本化 |
| CodeGraph MCP 协议升级不兼容 | smoke test 失败 | 锁版本 + 测试环境验证 |
| **short hash 解析失败率高**（F4）| `link_commit_commit.target_hash IS NULL` 比例 > 5% | 优先级降到 GitHub API + 增加 quarantine 抽样 |
| **F5 路径 B false-positive 高**（无 trailer 修复 commit 误关联到无关 bug）| 评测人工抽样 fp 率 > 15% | 提高 stack_frames 集合包含的最小函数数（v1.0: 2，可调到 3）+ 缩短时间窗 |
| **Fixes 链循环引用**（A.Fixes:B + B.Fixes:A）| trace_fixes_chain 提前 break | 已实现 seen 集合循环检测；监控 `seen` 命中次数 |

## 10. v1.0 → v1.3 演进

| 阶段 | 主要变化 |
|------|---------|
| v1.0 | 6 类 linker 上线；CodeGraph HTTP 集成稳定；Neo4j 周度 rebuild + 对账 |
| v1.1 | MAINTAINERS 完整解析；Neo4j 增量 rebuild；confidence 评分模型化 |
| v1.2 | 多跳查询编排（M5 + M7 利用 Neo4j）；评测反馈调 linker 规则 |
| v1.3 | 评估 codesearch 是否暴露 commit-history MCP；评估替换 Neo4j 为 NebulaGraph |

## 11. 与外部组件的契约

- **M1**：trailer 抽取规则错误时调 LLM 做兜底解析（可选，v1.0 不启用）
- **M2**：写 `link_commit_bug` / `link_commit_message` / `lkml_patch.landed_commit` / `kernel_commit.subsystem` / `kernel_commit.patch_id` 列；Neo4j 全部节点 + 关系
- **M3**：依赖 ingester 落库的 commit body / message body / bug 数据
- **CodeGraph 服务**（由 codesearch 项目实现）：HTTP MCP 客户端通信；启动前需 CodeGraph HTTP server 启动 + 必需 repo 索引完成（见 §4.4 健康检查）

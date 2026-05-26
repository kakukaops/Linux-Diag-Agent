# FAQ — Linux-Diag-Agent 常见问题

---

## Q11：commit 图谱构建的原理是什么？

### 核心一句话

**Linux 内核 commit 是一种"自带元数据的可执行文档"**。我们不做语义推断，**只把已经存在于 commit body 的纯文本引用，物化成可 JOIN 的关系表**。

### 原理四步走

#### 第 1 步：每个 commit 自我描述

Linus 在 2005 年定下规矩，至今每条 mainline commit 都按这个结构：

```
mm: memcontrol: don't throttle dying tasks on memory.high     ← subject

The OOM killer can deadlock when a memcg under memory.high pressure ...
[多段解释]                                                      ← body

Fixes: abc1234567 ("memcg: introduce memory.high")            ← 修了哪个 commit
Link: https://lore.kernel.org/r/20240115.abc@xyz/             ← 哪里讨论的
Reported-by: Foo Bar <foo@bar.com>                            ← 谁报的
Reviewed-by: Baz Qux <baz@qux.com>
Cc: stable@vger.kernel.org # v6.6+                            ← 标记 backport
CVE: CVE-2024-50022                                           ← 对应漏洞
Signed-off-by: Author <a@a.com>
```

**每行 trailer 都是一条**有向边的文本表达**。** 这是数据原生事实，不是我们设计的。

#### 第 2 步：Ingester 解析 trailer，存进结构化字段

`ingest/kernel_commit/ingester.py` 抓到一条 commit，正则提取：

| 文本 trailer | 存到哪个字段 |
|---|---|
| `Fixes: abc1234` | `kernel_commit.fixes_refs[]` TEXT[] |
| `CVE: CVE-2024-XXXX` | `kernel_commit.body` 里保留 + 后续 linker 用 |
| `bugzilla: gitee.com/.../issues/I3JK` | `kernel_commit.body`（不预解析，linker 用）|
| `Link: lore.kernel.org/.../<msg-id>` | `kernel_commit.body`（不预解析）|
| OLK `[ Upstream commit abc123 ]` 头 | `kernel_commit.upstream_commit` |

**此时仅 commit 表自描述**，没有跨表关系。

#### 第 3 步：等其他源也入库

- **lkml_message**：lore.kernel.org 抓 → 被引用的邮件入库
- **bug**：gitee + atomgit + bugzilla.kernel.org → issue 入库
- **cve**：NVD JSON feed → CVE 入库
- **kernel_commit (mainline)**：linux-stable.git 抓 → 上游 commit

每个源都是**独立摄入**，互不知道。

#### 第 4 步：Linker 跑 SQL，把引用「兑现」成边

`graph/linker.py` 的核心逻辑就是：

```sql
-- 对每条 commit body 里出现过 "Link: lore.kernel.org/.../<msg-id>" 的
-- AND msg-id 在 lkml_message 表也存在
-- → 物化成 link_commit_message 的一行
INSERT INTO link_commit_message (commit_hash, message_id, ...)
SELECT kc.hash, lm.message_id
  FROM kernel_commit kc, regexp_matches(kc.body, 'lore\.kernel\.org/[^\s]+/([^/\s>]+@[^/\s>]+)', 'g') AS ref
  JOIN lkml_message lm ON lm.message_id = ref[1]
```

**两端都必须存在 → 才写一行边**。

每张 link 表对应一种 trailer 模式：

| Link 表 | 来源 trailer | 当前行数 |
|---|---|---|
| `link_commit_message` | `Link: lore.kernel.org/.../<msg-id>` | 33,099 |
| `link_commit_cve` | NVD `references` 字段含 commit URL（反向）| 401 |
| `link_commit_bug` | `bugzilla: gitee.com/.../issues/X` (OLK) + `Closes: bugzilla.kernel.org/...` | 58,388 |
| `link_commit_fixes` | `Fixes: <sha>` trailer | 88,068 |
| `link_commit_revert` | body `This reverts commit <sha>` | 4,511 |

### 查询时变成纯 JOIN

Agent 拿到一个 commit hash，想"按图索骥"找关联，每跳都是 **O(log N) 索引 JOIN**：

```sql
-- 这个 commit 修了哪个 commit？
SELECT fixed_hash FROM link_commit_fixes WHERE fixer_hash = 'abc123';

-- 反向：哪些 commit 修了我？
SELECT fixer_hash FROM link_commit_fixes WHERE fixed_hash = 'abc123';

-- 这个 commit 对应的 patch 讨论邮件？
SELECT lm.subject, lm.body FROM link_commit_message lcm
  JOIN lkml_message lm ON lm.message_id = lcm.message_id
 WHERE lcm.commit_hash = 'abc123';

-- 三跳：这个 CVE 的修复 commit 又修了哪个老 commit？
SELECT lcf.fixed_hash
  FROM link_commit_cve lcc
  JOIN link_commit_fixes lcf ON lcf.fixer_hash = lcc.commit_hash
 WHERE lcc.cve_id = 'CVE-2024-50022';
```

**纯结构化查询，不需 LLM、不需 embedding、不需相似度**。

### 为什么这条路能走通？

| 前提 | 内核生态满足吗？ |
|---|---|
| 引用必须是**显式的**（不靠"猜"）| ✅ 20+ 年 trailer 强制文化 |
| 引用必须**指向稳定标识符** | ✅ commit SHA / msg-id / CVE-ID 都是全局唯一不变 |
| 引用必须**可机器解析** | ✅ 标准格式（"Fixes: <sha>", "Link: <url>"）|
| 数据规模**有边界** | ✅ 内核 1.3-3M commit、几万邮件、几万 issue —— 可全量入库 |

换在很多其他生态行不通：
- **OSS 应用代码** 通常没有 Linus 式的 trailer 文化
- **企业内部代码** commit message 经常空荡荡
- **Web 服务的 PR 模型** 走 GitHub UI，关系散在 PR 评论里，不在 commit body

**内核是恰好满足"图结构隐藏在文本里"的少数项目**。

### 对照传统知识图谱构建

| 传统 KG 构建 | 我们 |
|---|---|
| NER 命名实体识别 | ❌ 不需要 — 实体就是 commit hash / msg-id 这种字面 ID |
| 关系抽取 ML 模型 | ❌ 不需要 — 关系就是 `Fixes:` / `Link:` 这种字面 trailer |
| 实体对齐 / 消歧 | ❌ 不需要 — ID 全局唯一不变 |
| 知识融合 / 冲突解决 | ❌ 不需要 — 每条边都有 100% 文本证据 |
| Embedding + 相似度 | ❌ 不要 — ADR-001 明确拒绝 |
| 统计性置信度 | 简化版本：`confidence=0.95`（有 trailer）vs 0.7（subject 模糊匹配回退）|

**所以严格说我们做的不是"知识图谱"，是"内核 trailer 结构化关系数据库"**。区别在哪：
- KG 强调"从非结构化文本提取关系" —— 我们叫"语义抽取"
- 我们做的是"从已经半结构化的 trailer 物化关系" —— 叫"trailer 抽取"
- 后者**远比 KG 可靠**，因为引用是社区强制的而不是 ML 猜的

### 一个具体例子：oom-001 多跳

用户报 "OOM kill in cgroup"，agent BM25 命中 LKML 邮件，然后顺图谱走：

```
Step 1: LKML BM25
  message_id = "20210913230759.2313-1-daniel@iogearbox.net"
  subject: "[PATCH] bpf, cgroups: Fix cgroup v2 fallback..."

Step 2: link_commit_message JOIN
  → 找到对应 commit: 8520e224f547

Step 3: link_commit_fixes JOIN (查它修了哪个)
  → 老 commit: bd1060a1d671 "cgroup, bpf: support per-cgroup ..."

Step 4: kernel_commit.upstream_commit JOIN (反查 OLK 是否 backport)
  → OLK-6.6 hash: 8520e224 (此 commit 已进 OLK)
  → 但 OLK-5.10 没找到 → 提示用户该 backport 没进 5.10

Step 5: link_commit_cve JOIN (反查有无安全公告)
  → 这个 commit 对应 CVE-2021-XXXX
```

**5 跳走完，全是纯 ID JOIN，毫秒级**。换 embedding 路径，每跳都要算相似度 + 排序 + LLM 选择，5 跳累积几十秒，还可能 hallucinate。

### 一句话总结

**Commit 图谱构建 = 信任 + 解析 + JOIN**：
- **信任**内核社区 20 年 trailer 文化
- **解析**正则把 trailer 抽出来
- **JOIN** 把两端都存在的引用物化成 link 表

技术上简单但工程上稳。这是 ADR-001（无 embedding）的根本依据。

---

*相关问题：Q10（专家 review）· Q6（为什么 commit 是 hub）· Q5（图谱结构）*

---

## Q10：外部专家说要建"commit lineage 图"采多个 distro 仓 + 加 embedding，我们对照下当前差距？

review 一位 Linux 内核专家的建议（2026-05-26）。专家提了 5 个核心点，我们用 DB 数据逐条对照：

### ✅ 已经做了的（专家观点与我们设计一致）

| 专家观点 | 我们的状态 |
|---|---|
| "双层 commit knowledge graph"（distro + upstream） | ✅ OLK 是 distro 层，`kernel_commit.upstream_commit` 字段物化了 **60,824** 个 OLK↔mainline 桥 |
| "LLM 不直接看百万 commit，先经图谱缩上下文" | ✅ ADR-019 Hybrid Agent + ReAct + cross-graph 就是这设计 |
| "commit 不是文本，是因果图" | ✅ ADR-001 拒绝 embedding 的根本理由就是这个 |
| "三层结构：原始索引 / 语义关联 / LLM 推理" | ✅ 跟 ADR-025 L1/L2/L3 + ReAct 同构 |

### ❌ **重大缺口**（专家点出我们没意识到的）

| 缺口 | DB 实测 | 影响 |
|---|---|---|
| **`Fixes:` chain 未物化** | `kernel_commit.fixes_refs` 数组字段已存 **87,750** 行（commit body 87,534 行含 Fixes trailer），**但没建 `link_commit_fixes` 表** | patch lineage 当前要扫文本，应该是 O(1) JOIN |
| **Revert chain 未物化** | **5,971** 个 commit 以 "Revert ..." 开头 | 专家强调的"被 revert 又 redesign 又 backport"链当前查不到 |
| **subsystem 字段 99% NULL** | 仅 22.9% 填充（多在 60K backport 里）| 没法按"mm 子系统 / net 子系统"过滤检索 |
| **kernel.org mainline 仓未真入库** | DB 只有 **958 个 stub** 占位（ADR-018 Phase B 没真跑）| 60K OLK backport 的 `upstream_commit` SHA 是**悬空指针** |
| **stable git 仓未入库** | 0 | `cc stable@vger.kernel.org` 流程的实际 backport 链拿不到 |

**最高 ROI**：`link_commit_fixes` —— 87K 关系**数据已在 `fixes_refs` 数组里**，只差一个 SQL 写入。**30 分钟工作，零抓取成本**。

### ❌ **不适用我们**（专家通用建议 vs 我们的特定 scope）

| 专家观点 | 我们的实际 |
|---|---|
| "Ubuntu / RHEL / Android / Debian 都要采" | OLK 单 distro 产品定位，加进来 70%+ 重复（详见 [Q9](#q9olk-仓库是否包含-kernelorg-官方仓的-commit如果加-debian-仓commit-会重复吗)）|
| "semantic_embedding 字段必须有" | ADR-001 明确拒绝 embedding，用 BM25 + 图谱替代。专家是通用 LLM+RAG 思路，我们立项就走另一条路 |
| "跨 distro patch lineage" | 不在 scope；OLK 内部 lineage 才是 |

### 真正应该立刻做的

**P0a：物化 `link_commit_fixes`**（30 分钟，零成本，87K 新边）—— 这是专家观点里 ROI 最高的一项，本会话确认数据已在手，只差落表。

详细优先级排序见 [docs/v2/ProjectStatus.md §8.2](v2/ProjectStatus.md#82-来自外部专家-review2026-05-26-db-数据审计)。

### 一个具体例子：为什么 Fixes-chain 重要

专家举例："patch 依赖 6.6 锁机制 → backport 到 5.14 漏 memory barrier → RHEL 崩了"。

对应到 OLK 场景**完全一样**：

```
mainline patch A (6.6)
   ↓ OLK 抽过来
OLK-5.10 backport A'      ← upstream_commit 字段已物化
   ↓ 漏 barrier 导致 race
OLK-5.10 fix B            ← 新 commit，body 里写 "Fixes: A'"
```

**第二条边 `Fixes: A' → B` 当前在 `fixes_refs` 里但没建图谱**。建好后 agent 拿到"OLK-5.10 race" → 跳 A' → 顺 `link_commit_fixes` → B → 拿到修复，纯 ID JOIN。

---

*相关问题：Q9（不采 Debian 的原因）· Q5（图谱结构）· Q1（图谱构建机制）*

---

## Q9：OLK 仓库是否包含 kernel.org 官方仓的 commit？如果加 Debian 仓，commit 会重复吗？

三个不同的事，用 DB 真实数据分开说。

### 一、OLK 仓库**不**完全包含上游 commits

OLK 是**分叉（fork）**，不是超集。从 DB 实测：

| 类别 | 数量 | 占比 | 解释 |
|---|---|---|---|
| `origin='olk'` 总计 | **1,299,052** | 100% | 我们抓的 OLK 全部 commit |
| └ `inclusion=mainline` 标记 backport | 14,081 | 1.1% | 从 mainline 选了一个 commit 回移过来 |
| └ `inclusion=stable` 标记 backport | 46,743 | 3.6% | 从 stable 选了一个 commit 回移过来 |
| └ 其他 (`hulk`/`iommu`/...) | ~4,300 | 0.3% | openEuler 自家或厂商专有 |
| └ **NULL（无 inclusion 头）** | **1,239,132** | **95.4%** | openEuler 原生 commit / MR merge commit |
| `origin='mainline'`（stub 行）| 958 | — | linker 自动建的占位行（没有 body）|

**关键事实**：

1. **OLK 1.3M commits 里只有 ~4.7% 是 upstream backport** — 那 60K 个 commit 在 commit body 里写了"从 mainline 哪个 commit 来"（`upstream_commit` 字段），引用了上游 SHA
2. 这 60K **backport commit 自己有新的 OLK hash**，跟 upstream 的 hash 不一样（git fork 的特性）
3. **本地 DB 里没有 mainline kernel.org 仓的 commit metadata** — 60K 引用指向的上游 SHA 不在本地（除了 958 个 stub 占位）

```
┌────────────────────────────────────────────┐
│  OLK 仓 (我们已抓)                          │
│  1.3M commits                              │
│                                            │
│  ┌──────────────────────────────────┐      │
│  │ 60K backport commits             │      │
│  │ (有 upstream_commit SHA 引用)    │──────┼──→  上游 mainline / stable
│  └──────────────────────────────────┘      │     (我们 DB 没有正本)
│                                            │
│  ┌──────────────────────────────────┐      │
│  │ 1.24M openEuler-native commits   │      │     ← 上游永远没有
│  │ (无 upstream，独立 patch)        │      │
│  └──────────────────────────────────┘      │
└────────────────────────────────────────────┘
```

如果要"看到上游 commit 本身的 metadata（mainline 作者 / 日期 / 进入哪个 mainline release tag）"，**得单独抓 kernel.org mainline 仓**。这是 ADR-018 提到但还没真正跑的事（DB 里只有 958 个 stub 占位）。

### 二、Debian 仓加进来 — **绝大部分会重复**

不是 hash 重复，是**信息上的重复**。Linux 发行版内核都做同样的事：

```
                kernel.org mainline (Linus 本尊)
                       │
                ┌──────┴──────┐
                ↓             ↓
            stable v6.6.y   stable v5.10.y
                │             │
       ┌────────┼─────────────┼─────────┐
       ↓        ↓             ↓         ↓
      OLK    Debian        Ubuntu      RHEL
      fork    fork          fork       fork
       │        │             │         │
      OLK    Debian        Ubuntu     RHEL
      自己   自己            自己      自己
      patch   patch         patch     patch
```

每个 distro 都干这三件事：
1. 以 mainline / stable 某个版本为基线
2. 挑一批 stable backport 应用（**大量重叠** — 各 distro 选的 backport 集合 70%+ 一样）
3. 加一些 distro-specific patch（**唯一的** — 这部分才是 Debian 独有）

**假设抓 Debian 入库**：

| 类别 | 估计占比 | 与 OLK 关系 |
|---|---|---|
| Debian 选的 stable backport | ~70-80% | **跟 OLK 的 stable backport 选集大量重叠**（diff 内容一样，hash 不同）|
| Debian 自家 patch（Debian-specific 安全/集成）| ~15% | OLK 没有 |
| 其他（merge / 元数据 commit）| ~5% | OLK 也类似 |

所以 Debian 1M commits 里，可能 70%+ 在内容上跟 OLK 已有 commit 是同一个 upstream 的应用。但**每条都有新的 Debian hash**，所以 DB 不会真"去重"。

**加 Debian 真正的收益**：

| 收益 | 量级 | 评价 |
|---|---|---|
| Debian-specific 安全 patch | ~15% 量 | 只对**诊断 Debian 内核**有用，OLK 用户用不上 |
| Debian 选哪个 backport 优先 | 决策信号 | 看 Debian 选了 OLK 没选的 patch → 提示 OLK 也许该加 |
| Cross-distro CVE 修复对比 | 间接 | 通过 NVD CVE 就能拿到，不一定要 Debian 仓 |
| 邮件/issue 桥接 | 几乎 0 | Debian 用自己的 BTS（debbugs），跟 gitee/atomgit 完全不通 |

**结论**：加 Debian 对 openEuler 故障诊断的**边际价值很低**。除非产品定位扩展到"通用 Linux distro 诊断"，否则不划算。

### 三、要扩仓库的话，按 ROI 排序

| 优先级 | 仓库 | 边际收益 | 工作量 | 评价 |
|---|---|---|---|---|
| **P0** | **kernel.org mainline (linus's tree)** | 高 — 把 60K 上游 SHA 从 stub 升级为真正的 commit metadata，能跨 Link/Fixes/CVE trailer 进一步扩图 | 中（500K+ 上游 commits）| **真正值得做**，对应 ADR-018 Phase B |
| **P0** | **kernel.org stable (linux-stable.git)** | 中高 — `cc stable@vger.kernel.org` 流程的真实 backport 路径在这里 | 中 | 跟 mainline 一起做 |
| P2 | RHEL / CentOS Stream | 中 — Red Hat 内核做的 backport 选择可参照 | 高（需企业渠道）| 法务风险 |
| **P3** | Debian | 低 — 大量内容跟 OLK 重叠 | 中（git 公开）| 除非要诊断 Debian，否则不划算 |
| P3 | Ubuntu / SUSE | 低 | 同上 | |

### 真正的 v2.1 建议

如果要"让图谱更稠密"，**第一步该做的是 kernel.org mainline + stable 入库**，不是 Debian：

1. mainline + stable 一旦入库，OLK 60K backport 的 `upstream_commit` SHA 就能 JOIN 到真实 commit 上（现在是悬空指针）
2. 上游 commit 自己又有 `Link:` / `Fixes:` / `Cc: stable@` / `CVE:` trailer，linker 能继续往外扩
3. 整个图谱从 1.3M 节点 + 92K link 扩到 ~3M 节点 + 几十万 link

这是 **ADR-018 Phase B** 原本规划要做的，目前似乎没真正跑（DB 里只有 958 个 stub）。

### 一句话总结

- **OLK ⊄ mainline**：OLK 是 fork，1.3M commits 里只有 4.7% 是 backport（hash 不同的副本），其余 95% 是 openEuler 原生
- **mainline 真正的 metadata 我们没有**，要扩仓库的话**这是 P0**，不是 Debian
- **加 Debian 性价比低**：内容大量重叠 OLK，桥接也走不通 distro 自己的 bug 系统

---

*相关问题：Q5（图谱结构）· Q2（数据源） · 参考：[ADR-018](v1/adr/ADR-018-commit-source-olk-kernel.md)*

---

## Q8：我们是不是几乎有了全部的内核邮件讨论数据？那 180 天的数据是什么？

**不是**。容易让人误解。精确说：

### 我们有的 ≠ 全部内核邮件

| 维度 | 实际状态 |
|---|---|
| 全部 LKML 历史邮件（粗估）| 24 年 × 各 list 总量 ≈ **几千万条** |
| 我们 DB 里 | **114,923 条**（约万分之一-千分之一）|
| **我们 DB 里**对**图谱构建有用的部分** | **99.1%** ✓ |

**我们不是"几乎有全部邮件"，是"几乎有全部被 commit 引用过的邮件"**。这两者差几个数量级。

### 那 114,923 条究竟是哪些？分两块

```
                          总计 114,923
                              │
            ┌─────────────────┴─────────────────┐
            │                                   │
       ~26K 条                             ~88K 条
       Reference-driven backfill           Bulk 180d
       (ADR-025 v2 主路径)                 (v1 残留)
            │                                   │
       怎么选？                            怎么选？
       扫 commit body 里的                 抓 linux-mm + stable
       Link: trailer，按 msg-id            两个 list 的最近 180 天
       逐封抓                              全部邮件
            │                                   │
       覆盖范围：                          覆盖范围：
       任意年份、任意 list                  只这两个 list、只最近半年
       (只要被任意 commit 引用)            (无论是否被引用)
            │                                   │
       价值：                              价值：
       图谱链接                            BM25 检索备用语料
       (link_commit_message 33K 行)        (找未被引用的相关讨论)
```

### 两块本质区别

**Reference-driven 26K** — **目标性的**，由 commit 决定。一条 commit 里写了 `Link: ...`，我们才去抓这封信。所以它**100% 跟 commit 有桥**，是 cross-graph 的骨架。

**Bulk 88K** — **广播性的**，按 list+时间窗口下载，不管有没有被引用。**绝大多数这 88K 邮件没有任何 commit 引用过它**，所以它**不进** `link_commit_message`，对图谱构建**无贡献**。

### 为什么 bulk 只选 linux-mm + stable 两个 list？

| list | 内容 | 诊断价值 |
|---|---|---|
| `linux-mm` | 内存管理子系统讨论 | OOM / 内存碎片 / cgroup 相关 |
| `stable` | stable 内核 backport 讨论 | CVE 修复 / 关键补丁回移 |

这两个 list 是**故障诊断最常碰到的子系统**。bulk 它们提供"未被 commit 引用但与诊断主题相关的近期讨论"。

### 没有的部分

| 缺什么 | 量级 | 影响 |
|---|---|---|
| 其他 list 的近期邮件（linux-fs / linux-net / kvm / sched / ...）| 几十万条/月 | 这些 list 上的讨论我们本地没有 |
| 任意 list 的历史邮件（不被 commit 引用的） | 几千万条 | 同上 |
| 任何"用户报告但还没人写 patch"的讨论 | 大量 | 同上 |

### 用 L3 lore live search 补救

诊断时如果**用户问的事**我们 L2 里没有，agent 走 `search_lkml` 工具，去 lore.kernel.org 在线搜全归档（这就是 ADR-025 L3 层）。例如：
- 用户问"recent ext4 corruption" → L2 里没有 linux-fs 讨论 → L3 现搜 → 拿到结果 → 缓存回 L2

### 完整链条

| 用途 | 数据来源 |
|---|---|
| Cross-graph 链接（commit ↔ msg） | ✅ L1+L2 本地（26K 覆盖被引用集合 99.1%）|
| BM25 检索"近期讨论"两个核心 list | ✅ L2 bulk 88K（180d） |
| 其他 list / 历史讨论 | ✅ L3 在线 lore search 现搜 |

### 一句话总结

我们**有**：
- 所有 commit 引用过的邮件（99.1%）— 图谱骨架完整
- 两个核心 list 的最近半年（bulk）— 检索备用语料
- 通过 L3 在线检索拿到其他

我们**没有**：
- 全部 LKML 历史（百万级），也**不需要** — 用 L3 现取

ADR-025 设计的核心**就是不下载全部**：只把 commit 桥需要的物化下来，其他靠在线发现。180 天 bulk 那 88K 其实是 v1 时代的过度物化产物，**可以删掉一半**，对图谱无伤。

---

*相关问题：Q7（180 天 LKML 数据足够构建图谱吗）· Q5（图谱结构）*

---

## Q7：我们只下载了 180 天的 LKML，对构建图谱足够吗？是否需要下载更长时间？

**不需要**。先纠正一个前提：**DB 里的 LKML 不止 180 天**。

### "180 天"是配置项，不是 DB 实际内容

| | 你的理解 | DB 实测 |
|---|---|---|
| LKML 数据范围 | 180 天 | **2002-06-11 → 2026-05-21（24 年）** |
| LKML 总量 | ~ 180 天那点 | **114,923 条** |

`configs/local.yaml` 里的 `lookback_days: 180` 只是 **bulk 摄入模式**的窗口（用于 linux-mm + stable 两个 list 的批量抓取）。DB 里实际有两套数据来源：
1. **Bulk 180 天**：抓了 ~80K 条最近邮件
2. **Reference-driven backfill**：扫所有 commit body 里的 `Link: lore.kernel.org/...` trailer，把被引用的 message-id **逐个**抓回 — **无时间限制**，最早抓到 2002 年

按邮件本身日期分布（实测）：
```
2026: 74,842   ← bulk 主要在这
2025: 15,254
2024:  2,761
2023:  5,627
2022:  6,088
2021:  4,975
2020:  4,161
2019:  1,162
2018:    27
...有少量直到 2002
```

### 图谱构建覆盖率：99.1%（实测）

| 维度 | 数据 |
|---|---|
| OLK commits 引用的 distinct lore msg-id | **26,221** |
| 已在 lkml_message | **25,984 (99.1%)** |
| **真实 GAP** | **237** |

**按 commit 年份拆，每一年都 98+%**：
```
2026: 99.4%   2025: 99.7%   2024: 99.5%   2023: 99.4%
2022: 98.7%   2021: 99.0%   2020: 99.1%   2019: 98.3%
```

不存在"老 commit 引用的邮件抓不到"这种问题。

### 那 237 个 GAP 是什么？

不是下载策略问题，**是 lore.kernel.org 自己返回 404**。看 sample：

```
02wrx9Xs@mwanda                                           ← 残缺/伪 msg-id
0G72j@mwanda                                              ← 同上
0000000000000e7156059f751d7b@google.com.                  ← 末尾多 .
02494cb8-2aa5-1769-f28d-d7206f284e5a@digikod.net]         ← 末尾多 ]
```

这是**regex 提取 bug** —— 从 commit body 抓 msg-id 时把后面的标点 `.` `]` `,` 一起抓进去了，导致传给 lore 的 msg-id 不合法。**修 regex 能救回部分，扩下载救不回任何一个**（bulk 用同一个 lore 后端，对错误 ID 同样 404）。

### "扩 LKML 时间窗对图谱的收益" 量化

| 方案 | 能多覆盖几个 GAP | 工作量 |
|---|---|---|
| 扩 bulk 到 730 天 | **0** | 几小时下载 |
| 扩 bulk 到 5 年全量 | **0** | 几天下载 |
| 扩 bulk 到 30 年全量 | **0** | 一周以上 |
| **修 regex 末尾标点**（真正可行）| ~150-200 / 237 | 30 分钟 |

收益是 **0** 因为 GAP 不是"数据没下"，是"那些 msg-id 在 lore 上根本不存在"。

### 结论

**不需要扩 LKML 下载**。99.1% 覆盖已经达到图谱构建的实际上限。剩下 0.9% 的修复路径是**清洗 regex 提取**，不是更多下载。

详细的"全部内核邮件 vs 我们有的"对照见 [Q8](#q8我们是不是几乎有了全部的内核邮件讨论数据那-180-天的数据是什么)。

---

*相关问题：Q8（我们有的 vs 全部 LKML）· Q1（图谱构建机制）*

---

## Q6：为什么 commit 是天然的 hub？

不是数据库设计偏好，是**内核生态的工作流自然产生的**。具体四个原因：

### 1. Commit 是唯一"发货"的东西

```
用户拿到的内核 = 一连串 commit
用户拿不到 = lkml 邮件、bugzilla、CVE 描述
```

当 OLK-6.6 出来时，里面装的就是一系列 commit 的累积。其他东西是**伴随产物**：邮件是讨论它的，bug 是因它而起的，CVE 是描述它漏洞的。**Commit 是被打包发出去的载体本身**。所以引用一个 commit 比引用其他任何东西都更稳定可靠。

### 2. Commit hash 是宇宙唯一的标识

```
"commit 892962a26026"       → 全球唯一，永远不变，可加密验证
"LKML 邮件 5"               → 哪个 list？哪个 thread？
"bug 1234"                  → 哪个 bugzilla？(kernel.org / gitee / atomgit / Red Hat...)
"CVE-2024-50022"            → 唯一但定义晚，commit 早于 CVE
```

SHA-1 哈希是 Linus 选择 git 的根本原因：你说"commit abc123"全世界都指同一份代码。但你说"bug 1234"必须先说**哪个平台的 bug**（我们今天的图谱里就有 3 套不同的 bug ID 空间：gitee / atomgit / bugzilla.kernel.org，全互不通用）。

### 3. 内核开发的工作流自然汇聚到 commit

```
讨论                  实现              后果
─────                ─────             ─────
LKML 邮件   ─→  patch 提交 ─→  COMMIT  ─→  bug 报告（用 Fixes: 引用它）
RFC 设计   ─→  实现尝试   ─→     ↓      ─→  CVE 公告（references 字段引用它）
ML 评审    ─→  v2/v3/... ─→     ↓      ─→  下游 distro backport（OLK 引用上游 SHA）
                                ↓      
                          mainline tag  ─→ stable backport ─→ OLK 入库
```

**commit 是 "before" 和 "after" 的分界线**。在它之前的一切（讨论、RFC、争论）是为了形成这个 commit；之后的一切（用户报告、安全公告、backport）是这个 commit 产生的后果。所有信息流自然以它为中心收敛 + 发散。

### 4. 内核社区 20+ 年强制的 trailer 文化

Linus 在 2005 年开始就要求 commit message 必须遵循特定结构。今天几乎每条 mainline commit body 都有这些字段之一：

```
Fixes: abc123       ← 修复哪个 commit
Link: https://lore.kernel.org/...   ← 在哪讨论的
Reported-by: Foo Bar  ← 谁报告的
Reviewed-by: ...    ← 谁审过
Tested-by: ...      ← 谁测过
Cc: stable@vger.kernel.org   ← 标记 backport 候选
Closes: https://bugzilla.kernel.org/...
CVE: CVE-2024-50022
```

OLK 在此基础上又加了自家约定：

```
mainline inclusion          ← OLK 自定义
from mainline-v6.13-rc1
commit abc1234567...        ← 上游 SHA
category: bugfix
bugzilla: https://gitee.com/openeuler/kernel/issues/XYZ
CVE: CVE-2024-50022
[ Upstream commit abc1234 ]
```

**所有这些 trailer 都把 commit 当作 anchor，把外部世界（邮件、bug、CVE、上游版本）作为它的属性挂在它身上**。

我们的 linker 做的事情，本质上就是**把已经存在于 commit body 里的 trailer 文本，物化成结构化关系表**。链路不是我们设计的，是社区写出来的。

### 反证：如果不用 commit 作 hub 会怎样？

试想以 LKML 邮件为中心建图：
- 邮件没有 trailer 系统（你不会在邮件里写 "Fixes: <某 commit>"）
- 邮件 ID `message_id` 不全局唯一（lore 转发会变）
- 邮件没有"发货"语义，用户拿不到邮件
- 同一个 bug 可能跨多个 list 讨论，没有规范化

或者以 bug 为中心：
- 不同平台 ID 互不通用（gitee `IDCSJV` ≠ atomgit `8929` ≠ kernel.org `12345`）
- bug 状态会变（reopen / dup-of）
- bug 不出现在 commit 里就引用不到

只有 commit 同时满足：
1. ✅ 全球唯一不变 ID
2. ✅ 是被分发的真实产物
3. ✅ 有强制的 trailer 引用其他实体的文化
4. ✅ 在内核工作流里是天然汇聚点

所以 **"commit 作 hub"** 不是架构决策，是承认数据本身的结构。

---

*相关问题：Q5（图谱结构）· Q1（图谱构建）*

---

## Q5：图谱的结构是什么样的？是通过 commit id 把 bug / message / CVE 关联起来吗？

理解基本正确，但更准确说是**多种节点类型 + 多种边类型**，commit 只是**最常见的中心节点**之一，不是唯一桥梁。

### 一、节点（8 种"东西"）

```
Discussion 子图                Commit 子图                  Issue/CVE 子图
──────────────                ──────────────                ──────────────
lkml_thread        ─父─→      kernel_commit                bug
 ↑                              ↑ upstream_commit            (source=gitee/atomgit/
lkml_message                  another kernel_commit         bugzilla_kernel)
 ↑ (一封邮件)                  (OLK ← mainline 桥)
lkml_patch                                                  cve
lkml_review                                                  (fix_commits JSONB)
                                                            syzbot_crash
```

### 二、边（关系类型）

**显式 link 表（3 张，跨子图）：**
| 表 | 起点 | 终点 | 行数（当前）|
|---|---|---|---|
| `link_commit_message` | kernel_commit | lkml_message | 33,099 |
| `link_commit_cve` | kernel_commit | cve | 401 |
| `link_commit_bug` | kernel_commit | bug | **58,388** |

**隐式 FK 边（子图内）：**
| 表 | 字段 | 指向 | 含义 |
|---|---|---|---|
| `kernel_commit` | `upstream_commit` | 另一个 kernel_commit | OLK ↔ mainline 上游桥 |
| `lkml_message` | `thread_id` | lkml_thread | 邮件属于哪个线程 |
| `lkml_patch` | `thread_id`, `message_id` | lkml_thread, lkml_message | patch 邮件 |
| `lkml_review` | `message_id` | lkml_message | Reviewed-by / Acked-by |
| `cve` | `fix_commits` (JSONB SHA list) | kernel_commit | NVD 指明的修复 commit |

### 三、为什么 commit 是中心

不是设计选择，是**内核生态的真实事实**：

| 维度 | 体现 |
|---|---|
| 邮件 → commit | `Link: lore.kernel.org/.../<msg-id>` trailer 在 commit body |
| bug → commit | `bugzilla: gitee.com/.../issues/IDCSJV` trailer 在 commit body |
| CVE → commit | NVD references 字段含 GitHub/kernel.org commit URL |
| 上游 ↔ OLK backport | OLK commit body 顶部的 inclusion 头 |

**所有桥接证据都在 commit body 的 trailer 里**。我们的 linker 只是把这些纯文本 trailer **物化成结构化关系**。详细原理见 Q6。

### 四、可视化（含数量）

```
                                         link_commit_message
                       ┌──────────────────────────────────────────────┐
                       │                  33,099                       │
                       ↓                                                │
                 lkml_message ◄── thread_id ── lkml_thread             │
                 (114,923)                                              │
                                                                        │
                                                                  kernel_commit
                                                                  (1,300,010)
                                                                        │
                                                                        │ link_commit_cve
                                                                        │ 401
                                                                        ↓
                                                                       cve
                                                                       (15,502)
                                                                        │
                                                                  ─────┼─────
                                                                        │ link_commit_bug
                                                                        │ 58,388
                                                                        ↓
                                                                       bug
                                                                       gitee:    3,861
                                                                       atomgit:    467
                                                                       bzkernel: 3,700
                                                                       (= 8,028)
```

### 五、实际多跳遍历的例子

假设用户报 `OOM kill in cgroup`：

```
1. BM25 lkml 路命中：
   message_id = "20210913230759.2313-1-daniel@iogearbox.net"
   subject: "[PATCH] bpf, cgroups: Fix cgroup v2 fallback..."

2. 跳 link_commit_message → 拿到对应 commit:
   8520e224f547 "bpf, cgroups: Fix cgroup v2 fallback on v1/v2 mixed"

3. 顺 kernel_commit.upstream_commit → mainline commit metadata（验证修复进了哪个 mainline）

4. 顺 link_commit_bug → 拿到 gitee/atomgit issue:
   gitee#I3J87Y: "【OLK-5.10】高并发场景下..." (含完整用户报告 + 堆栈)

5. 顺 link_commit_cve → 看是否对应已公开 CVE
```

每一跳都是**纯 ID JOIN**，O(log N)，不需 LLM、不需 BM25、不需相似度。这就是图谱的核心价值。

### 六、跟"通过 commit id 关联"的对照

**95% 正确**。补充两点：
1. 还有 LKML 子图内部的线程 DAG（不通过 commit）
2. 还有 OLK commit ↔ mainline commit 之间的桥（commit ↔ commit）

但核心机制就是：**commit 是 OLK 生态的天然 hub**，所有外部知识源都用 commit body trailer 引用，所以图谱以 commit 为中心是数据决定的，不是设计偏好。

---

*相关问题：Q6（为什么 commit 是 hub）· Q1（图谱构建机制）*

---

## Q4：智能体的诊断思路是否和一个有经验的内核工程师类似？图谱的作用是什么？

是的，**v2 的设计明确是模仿一个有经验的内核工程师**排查故障的思路。让我把"工程师做什么 / agent 怎么对应 / 图谱在哪里发挥作用"对照展开。

### 一、工程师排查 vs Agent 三阶段

| 工程师步骤 | Agent 阶段 | 实现 |
|---|---|---|
| ① 看一眼 log/现象，判断是什么类故障（OOM？oops？硬件？）| **Triage**（确定性）| `parse_input` → `extract_events` → `detect_taint_and_hw` → `classify_fault_and_route` |
| ② 形成假设（"看着像内存碎片"/"像 use-after-free"）| **ReAct loop 启动** | 路由感知 system prompt 告诉 LLM 工具优先级 |
| ③ 查代码：那个 call trace 函数在干啥？相关子系统？| ReAct 工具 D | `search_code`、`get_function_source`、`get_call_graph` |
| ④ 查 commit：有人修过吗？patch 进了哪些版本？| ReAct 工具 A/D | `search_commits` → `get_commit_detail` → `check_backport_status` |
| ⑤ 查 LKML：上游讨论过吗？有什么注意事项？| ReAct 工具 A | `search_lkml`（L2 BM25 + L3 lore live） |
| ⑥ 查 Bug/CVE：已知问题吗？有 workaround 吗？| ReAct 工具 A | `search_bugs`、`search_syzbot`、`search_cve` |
| ⑦ 交叉对照（"这个 commit 修了那个 CVE，对应 LKML 讨论说要加 stable 标"）| ReAct 工具 D + **图谱** | `link_commit_cve` / `link_commit_message` / `link_commit_bug` 的跨表查询 |
| ⑧ 给结论：根因 + 修复建议 + 置信度 | **Report**（确定性）| `bind_claims` + `generate_report` |

22 个 tool 分散在 5 个 route 里，本质就是把工程师**手头那本"资料地图"**显式拆开。

### 二、图谱到底解决什么

**最重要的问题** —— 一个有 10 年经验的内核工程师和一个新人，差距不在他们能查的工具（每个人都能 `git log`、上 lkml），而在他们**脑子里那张关系网**：

> "这个 call trace 里有 `tcp_v4_do_rcv` → 我去年看过一个 CVE 跟它有关 → 那 CVE 修复 commit 是 abc123 → 那 commit 的 patch 讨论里 davem 提过 backport 有坑"

这张网不是文本搜索能搜出来的 —— 它是**显式连接**：

```
   ┌─ Discussion ─┐         ┌─ Code Change ─┐
   │ LKML message │ ←Link:← │  Commit       │
   └──────────────┘         └───────────────┘
                              ↑Fixes:↑   ↑NVD↑
                              │          │
                          ┌── Bug ──┐ ┌── CVE ──┐
                          │ ID 1234 │ │ 2024-… │
                          └─────────┘ └────────┘
```

图谱（`link_commit_bug` / `link_commit_message` / `link_commit_cve`）**就是把那个老工程师脑子里的关系网，物化成数据库行**。这样：

| 没有图谱（纯 BM25）| 有图谱 |
|---|---|
| 每个 route 是孤岛：search_lkml 给你邮件，search_cve 给你 CVE，**它们之间不知道彼此** | 一条 evidence 拿出来可以**多跳遍历**："这个 CVE 的 fix_commits 是哪个" → "那个 commit 引用了哪条 LKML 讨论" → "那讨论里 reviewer 提了什么 NACK" |
| LLM 只能看 keyword 匹配的散落文本，靠 reasoning 把它们串起来（容易瞎编）| LLM 看的是**结构化关系**，"是同一回事"由数据保证，不靠它推 |
| 找"已修没修"靠词频匹配 commit message | 直接 `link_commit_cve` JOIN `kernel_commit.olk_inclusion_type` 给出确定答案 |

### 三、现实差距（诚实说）

| 维度 | 有经验工程师 | 我们的 agent |
|---|---|---|
| 直觉判断"该查什么"| 经验形成的 prior | LLM prompt + 路由 SOP（次一档）|
| 知道"什么时候够了"| 几分钟内决定 | ❌ ReAct 不收敛（eval 显示要靠 force_finalize 强终止）|
| 读代码带子系统理解 | 真的懂 mm/net/fs 设计 | 顺着符号跳，缺整体把握 |
| 多跳关联 | 脑里那张图 | **靠 cross-graph 弥补** ← 图谱的本质价值 |
| 信号噪声过滤 | 立刻忽略 user-language 干扰词 | BM25 把 "java/diagnose" 当 keyword 也算（recall 报告的根因之一）|

### 四、所以图谱的真实作用是什么？

**不是为了"找到资料"** —— 资料 BM25 / lore search 已经能找到。
**是为了"知道资料之间的关系"** —— 把工程师**多年经验形成的直觉关联**变成 LLM 可遍历的边。

具体收益（对应当前 v2 状态）：

1. **`link_commit_cve` (401 行)** — agent 看到一个 CVE-2024-XXXX，能立刻 JOIN 出"修复 commit 是 abc123，subject 是 ..."，不用让 LLM 再去 BM25 猜
2. **`link_commit_message` (33K 行)** — agent 看到一封 LKML patch 讨论邮件，能立刻知道"对应的 commit 是哪个 SHA，落到了哪个 OLK 版本"
3. **`link_commit_bug` (1 行，缺口)** — 因 OLK 大量引用 gitee/atomgit issues 不在 bug 表里。ADR-022 v2.1 修

**一句话总结**：图谱 = **把内核生态里"什么和什么是一回事"这件事固化下来**，让 LLM 拿着图走，比让它读着文本猜要可靠得多。这也是 v2 比 v1 的本质进化 —— v1 是 7 路并行 BM25 + 假设投票（独立证据靠 LLM 串），v2 加 ReAct 让 LLM 沿图自由跳转。

---

*相关问题：Q1（图谱如何构建）· Q3（诊断流程）· 参考：[ADR-019](v2/adr/ADR-019-hybrid-react-deterministic.md)*

---

## Q3：本系统是怎样回答一个故障诊断问题的？详细步骤是什么？

入口函数是 `agent/graph.py:diagnose(raw_input)`，它驱动一条 **10 节点、两阶段**的 LangGraph 流水线，所有节点按有向边顺序执行，状态通过一个共享 dict 传递，关键节点会写入 PostgreSQL checkpoints（可断点续跑）。

```
Triage 阶段（确定"是什么故障"）
  parse_input → extract_events → classify_fault → retrieve
                                                       ↓
Diagnosis 阶段（回答"为什么 + 怎么修"）
  load_sop → generate_hypotheses → verify_hypothesis → self_consistency → bind_claims → generate_report → END
```

---

### 阶段一：Triage（4 个节点）

#### Step 1 · `parse_input` — 输入类型识别

**做什么**：判断用户输入是哪种形式。

| 条件 | 识别为 |
|------|--------|
| 路径存在且是 `.xz/.gz/.bz2` 或含 `sosreport` 字样 | `sosreport` |
| 路径存在且是普通文件 | `dmesg_file` |
| 文本含 `BUG:` / `WARNING:` / `Call Trace:` / `Oops:` 等内核日志标记 | `dmesg` |
| 其他（自然语言问题） | `question` |

**代码位置**：`agent/triage/nodes.py:parse_input()`

---

#### Step 2 · `extract_events` — 结构化事件提取

**做什么**：从原始文本中提取结构化的内核事件，每个事件有 `kind`（故障类型）和 `summary`（一行摘要）。

- **dmesg 输入**：调用 `mcp_servers/dmesg_journal/extractor.py`，用正则解析 OOM kill 记录、Oops、BUG、WARNING、Call Trace、softlockup 检测日志等，输出 `KernelEvent` 列表
- **sosreport 输入**：调用 `mcp_servers/sosreport/parser.py`，从压缩包中提取 `sos_commands/kernel/dmesg`，再走 dmesg 提取路径；同时读取 hostname、kernel version 等系统信息
- **question 输入**：无结构化事件，events 为空列表，由后续节点用 LLM 补偿

**产出**：`kernel_events`（事件列表）、`kernel_version`、`olk_version_tag`

---

#### Step 3 · `classify_fault` — 故障分类 + SOP 选择

**做什么**：从事件中确定主故障类型，并映射到对应的 SOP。

**有事件时**（dmesg/sosreport 路径）：按优先级从事件中选取最严重的 fault_kind：

```
panic > oom > softlockup > hardlockup > rcu_stall > oops > bug > warn > lockdep
```

**无事件时**（question 路径）：调用 Navigator LLM（`cfg.llm.navigator`），用 JSON prompt 分类：
```json
{"fault_kind": "oom|oops|softlockup|...", "fault_summary": "<一行描述>"}
```

**fault_kind → SOP 映射**：

| fault_kind | SOP 文件 |
|-----------|---------|
| oom | `agent/sop/definitions/oom.yaml` |
| softlockup / hardlockup / rcu_stall | `lockup.yaml` |
| panic | `panic.yaml` |
| oops / bug / warn / lockdep | `generic.yaml` |

SOP 文件包含：结构化诊断步骤、假设模板、关键指标列表、报告章节定义。

**产出**：`fault_kind`、`fault_summary`、`sop_name`

---

#### Step 4 · `retrieve` — 7 路并行检索

**做什么**：以 `fault_summary`（或原始问题）为查询，同时从 7 个知识来源召回证据。

1. **query_parser**（非 LLM，基于规则解析）：把 `fault_summary` 解析为 `RetrievalQuery`，提取 kernel_version、keywords、cve_ids 等字段
2. **7 路并行召回**（全部同时触发，`ThreadPoolExecutor`）：

| 路由 | 数据源 | 查询方式 |
|------|--------|---------|
| `commit` | PG `kernel_commit` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `lkml` | PG `lkml_message` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `bug` | PG `bug` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `syzbot` | PG `syzbot_crash` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `cve` | PG `cve` | CVE-ID 精确查找 + BM25 |
| `code` | CodeGraph MCP | `search_code(keywords, repos=['olk-kernel'])` |
| `docs` | CodeGraph MCP | PageIndex 树遍历（LLM 驱动，配额充足时触发）|

3. **分数归一化**：各路由用 `ts_rank_cd` 计分（量纲不同），合并前对每路由内部做 max 归一化到 [0,1]
4. **LLM 重排**（可选）：候选总数 > 10 时，取 BM25 top-20 发给 Navigator LLM 打相关性分数，重新排序

**产出**：`evidence`（Evidence 列表，每条含 `route`、`score`、`title`、`body[:500]`、`commit_hash/bug_id/message_id` 等字段）

---

### 阶段二：Diagnosis（6 个节点）

所有节点均调用 Chat LLM（`cfg.llm.chat`，通常是 claude_code 或 openai_compat）。

#### Step 5 · `load_sop` — 加载 SOP

**做什么**：从 `agent/sop/registry` 读取对应的 YAML 文件，提取 `steps` 列表（结构化诊断步骤）。若 SOP 不存在则 fallback 到 `generic.yaml`。

**产出**：`sop_steps`（字符串列表，5-9 条诊断步骤）

---

#### Step 6 · `generate_hypotheses` — 生成假设

**做什么**：把 `fault_summary` + SOP 步骤 + 前 10 条 Evidence 组合成 prompt，让 LLM 给出 3 条假设，每条含置信度（0-1）。

```
Fault: OOM kill of java with oom_score 999 in cgroup 4GB
SOP steps: 1. Parse OOM kill log... 2. Check cgroup limits...
Evidence: [commit] mm/vmscan: wake up flushers... : body...
→ LLM → [{"id":"h1","text":"cgroup limit too tight","confidence":0.8}, ...]
```

假设按置信度降序排列，最高分的作为 `active_hypothesis`。

**产出**：`hypotheses`（列表）、`active_hypothesis`（当前验证的假设）

---

#### Step 7 · `verify_hypothesis` — 验证假设

**做什么**：把 `active_hypothesis` + 前 8 条 Evidence 发给 LLM，让它判断假设是否被证据支持。

```
Hypothesis: cgroup limit too tight for java workload
Evidence: [commit] mm/memcontrol: don't throttle dying tasks...
→ LLM → {"status": "confirmed", "reasoning": "Evidence shows..."}
```

可能的 status：`confirmed` / `rejected` / `uncertain`

**产出**：更新 `active_hypothesis.status`

---

#### Step 8 · `self_consistency` — 自一致性投票

**做什么**：用相同 prompt 独立调用 LLM **K 次**（默认 K=1，评测时 K=3），得到 K 个技术分析文本，取出现最多的（多数票）作为 `final_analysis`。

- 调用参数：`temperature=0.3`（增加采样多样性）
- 投票方式：字符串规范化后用 `Counter.most_common(1)`
- K 由 `cfg.llm.chat.self_consistency_k` 控制（**Pro 配额紧张时务必保持 K=1**）

**产出**：`candidate_analyses`（K 个候选）、`final_analysis`（获胜分析）

---

#### Step 9 · `bind_claims` — 声明-证据绑定

**做什么**：从 `final_analysis` 中提取可引用的技术声明，验证每条声明是否有可溯源的证据支撑。

```
Analysis: "OOM triggered by cgroup throttling bug. Commit 038ff4e1 fixes..."
→ LLM → [{"text":"OOM caused by throttle","evidence_refs":["038ff4e1"]}]
→ 对比 evidence 中的 commit_hash 集合，判断 verified=True/False
```

**声明标记规则**：
- `verified=True`：`evidence_refs` 中有对应的 commit hash、bug_id 或 message_id
- `verified=False`：无法溯源，**保留在报告中并注明**（不删除）

**产出**：`claims`（Claim 列表，每条含 `text`、`evidence_refs`、`verified`）

---

#### Step 10 · `generate_report` — 生成报告

**做什么**：调用 `agent/report/renderer.py`，把最终状态渲染为两种格式：

**Markdown 报告（`report_md`）** — 面向工程师阅读：
- Executive Summary（根因一句话）
- Evidence（按路由分组，标注 mainline-backed / openEuler-native 置信度）
- Analysis（self_consistency 获胜分析）
- Claims（每条标 ✅verified / ⚠unverified）
- Hypotheses（列出所有假设 + 验证状态）
- Recommended Actions

**JSON 报告（`report_json`）** — 面向程序消费：
```json
{
  "fault_kind": "oom",
  "sop_name": "oom",
  "final_analysis": "...",
  "claims": [...],
  "evidence": [...],
  "report_md": "..."
}
```

---

### 完整数据流图

```
用户输入: "v6.6 OLK 进程 java OOM killed, oom_score 999, cgroup 4GB limit"
    │
    ▼ parse_input
    input_type = "question"
    │
    ▼ extract_events
    kernel_events = []  (question 路径无事件)
    │
    ▼ classify_fault (LLM)
    fault_kind = "oom"
    fault_summary = "OOM kill of java (oom_score=999) under 4GB cgroup"
    sop_name = "oom"
    │
    ▼ retrieve (7路并行 BM25 + CodeGraph)
    evidence = [
      {route:commit, commit_hash:"038ff4e1...", title:"mm/vmscan: wake up flushers...", score:0.85},
      {route:lkml,   message_id:"...", title:"Re: [PATCH] memcg: ...", score:0.72},
      {route:cve,    cve_id:"CVE-2024-50022", title:"...", score:0.61},
      ...共最多 70 条，重排后取 top-10
    ]
    │
    ▼ load_sop
    sop_steps = ["Parse OOM kill log", "Check cgroup limits", ...]
    │
    ▼ generate_hypotheses (Chat LLM)
    hypotheses = [
      {id:"h1", text:"cgroup limit too tight", confidence:0.8, status:"pending"},
      {id:"h2", text:"kernel memcg accounting bug", confidence:0.6, ...},
      {id:"h3", text:"application memory leak", confidence:0.5, ...},
    ]
    active_hypothesis = h1
    │
    ▼ verify_hypothesis (Chat LLM)
    active_hypothesis.status = "confirmed"
    │
    ▼ self_consistency (Chat LLM × K=1)
    final_analysis = "Root cause: cgroup memory.high throttle not bypassed for dying
                      tasks. Commit 038ff4e1 fixes the vmscan flusher wake condition.
                      Recommend upgrading to OLK-6.6 ≥ v6.8-rc3 backport."
    │
    ▼ bind_claims (Chat LLM)
    claims = [
      {text:"cgroup throttle not bypassed for dying tasks",
       evidence_refs:["892962a2"], verified:True},
      {text:"vmscan flusher fix in 038ff4e1",
       evidence_refs:["038ff4e1"], verified:True},
    ]
    │
    ▼ generate_report
    report_md  = "## Executive Summary\nRoot cause: ..."
    report_json = {"fault_kind":"oom", "claims":[...], ...}
```

---

### 关键设计权衡

| 设计 | 原因 |
|------|------|
| 两阶段流水线（triage + diagnosis） | triage 先定类型选 SOP，避免 LLM 对每种故障都泛化推理 |
| 7 路全量触发（不按故障类型路由）| 防止路由误判丢失证据（ADR-013）；代价是多几次 BM25 查询（<1s） |
| 路由内分数 max 归一化 | 不同数据源（LKML 邮件/commit 正文）原始 BM25 分数量纲差异 3-5 倍，不归一化则 commit 结果永远被 LKML 压排 |
| self_consistency K=3 | 三次采样投票提升结论稳定性；配额紧张时设 K=1 跳过（单次采样等价于正常 LLM 调用） |
| bind_claims 保留 unverified | 不删除无法溯源的声明，标注 ⚠ 供工程师判断，避免 LLM 编造难以发现 |
| PG checkpointer | 每个节点完成后写入 PostgreSQL，诊断中途崩溃可从最后一个节点恢复 |

---

*参考代码：`agent/graph.py` · `agent/triage/nodes.py` · `agent/diagnosis/nodes.py` · `agent/report/renderer.py`*

*相关问题：Q2（数据源）· Q1（图谱构建）*

---

## Q2：本系统涉及了哪些数据源？这些数据源分别是什么数据，对故障诊断有什么作用？

系统共有 **6 个数据源**，分两类：自建入库（PG）和外部服务直连（CodeGraph MCP）。

### 数据源一览

| 数据源 | 当前数据量 | 回答的核心问题 |
|--------|-----------|--------------|
| OLK 内核 git | ~130 万 commit | "历史上谁修过类似问题？补丁在哪？" |
| lore.kernel.org（LKML）| ~5792 条邮件（入库中）| "当时为什么这样修？有什么争议？" |
| bugzilla.kernel.org | 3700 条 bug | "这是已知 bug 吗？状态如何？" |
| syzbot | 999 条崩溃 | "是否有崩溃 reproducer？上游修了吗？" |
| NVD CVE | 15502 条 CVE | "是否有 CVE？CVSS 评分和影响面？" |
| CodeGraph（MCP）| olk-kernel 全量源码 | "崩溃点的代码是什么？调用链怎么走？" |

---

### 1. OLK 内核 Git 仓（`atomgit.com/openeuler/kernel`）

**数据内容**：OLK-6.6 + OLK-5.10 全量 commit 历史，包含 commit hash、作者、日期、subject、body（含 OLK inclusion 头、Fixes:/Link:/CVE: 等 trailer）。

**故障诊断作用**：
- **找修复记录**：BM25 匹配故障关键词，命中的 commit 就是历史上修过同类问题的补丁
- **回溯根因**：通过 OLK inclusion 头找到上游 mainline SHA，再桥接到 LKML 讨论和 CVE
- **判断 backport 状态**：确认上游修复是否已回移到用户所在的 OLK 版本

---

### 2. lore.kernel.org（LKML 邮件列表）

**数据内容**：linux-mm、stable 等列表的历史邮件，结构化为线程 DAG（lkml_thread / lkml_message / lkml_patch / lkml_review）。

**故障诊断作用**：
- **理解问题背景**：patch 合入前的讨论往往包含根因分析、失败场景、reviewer 的顾虑
- **找相同症状**：历史上出现过同类崩溃报告的讨论线程
- **多跳推理**：从一个 commit 通过 `link_commit_message` 找到对应审查讨论，再看 NACK/Acked-by 的理由

---

### 3. bugzilla.kernel.org（内核 Bugzilla）

**数据内容**：kernel.org 官方缺陷库，包含标题、描述、状态、严重程度、修复版本。

**故障诊断作用**：
- **找已知缺陷**：症状与已记录 bug 匹配时直接给出"这是已知问题，状态 FIXED/OPEN"
- **提供复现条件**：Bugzilla 报告通常包含触发条件和内核版本范围，比 commit message 更面向运维

---

### 4. syzbot（syzkaller 自动化崩溃检测）

**数据内容**：syzbot 持续运行 syzkaller 发现的内核崩溃，每条含崩溃标题、调用栈、状态（open/fixed）、fix commit。

**故障诊断作用**：
- **崩溃签名匹配**：stack trace 的函数帧序列与 syzbot 历史崩溃做签名对比，快速识别已知崩溃
- **获取 reproducer**：syzbot 崩溃附有 C 或 syz reproducer，可直接用于复现验证
- **确认修复状态**：标注 fix commit 的崩溃说明上游已有补丁，可检查 OLK 是否已 backport

---

### 5. NVD（美国国家漏洞数据库）

**数据内容**：Linux 内核相关 CVE，包含 CVSS 评分、漏洞描述、references 中的 fix commit URL（已提取为 fix_commits 字段）。

**故障诊断作用**：
- **安全属性判定**：确认某个崩溃/异常是否对应已公开 CVE，给出评分和影响范围
- **修复 commit 直达**：从 CVE references 提取 commit SHA → link_commit_cve → 找 OLK 对应 backport

---

### 6. CodeGraph（codesearch MCP 服务，外部直连）

**数据内容**：OLK-6.6 内核源码的两层索引——Zoekt BM25 全文 + SCIP 编译器级符号引用图 + Sphinx 文档章节树。

**故障诊断作用**：
- **定位崩溃代码**：给定 `tcp_v4_do_rcv+0x123` → 精确找到函数定义、调用点、字段布局
- **追踪调用链**：`lookup_symbol(action='references')` 找所有调用者，理解崩溃路径
- **阅读子系统文档**：补充 commit message 没说的设计意图和接口语义

---

### 为什么需要 6 个数据源？

单一数据源只能回答一个维度：Bugzilla 知道"有没有 bug"，但不知道"代码怎么改的"；LKML 知道"讨论过什么"，但不知道"有没有 CVE"。只有把 6 个维度组合起来，才能给出完整的诊断报告：

```
根因（CodeGraph + commit）
  + 修复状态（commit backport 状态）
  + 安全属性（NVD CVE）
  + 已知缺陷（Bugzilla + syzbot）
  + 社区讨论（LKML）
= 可操作的诊断结论
```

这也是 Cross-Graph Linker 存在的原因——把 6 个数据源在 commit 层面焊接起来，使 agent 能跨源做多跳推理。

---

*相关问题：Q1（图谱如何构建） · 参考：[ingest/CLAUDE.md](../ingest/CLAUDE.md)*

---

## Q1：本系统中代码、文档、Bug、邮件列表等信息是怎样通过图结构构建起来的？

### 整体架构：4 张子图 + 1 个 Cross-Graph Linker

系统不使用向量数据库（无 embedding），而是把内核生态的公开知识构建成一张**多层关联图**，由 4 张子图和一个负责"焊接"它们的 Cross-Graph Linker 组成：

```
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐
│  Code Graph      │  │ Discussion Graph │  │   Bug Graph      │  │   Doc Tree      │
│  （源码图谱）     │  │  （邮件讨论）    │  │ （缺陷/崩溃）    │  │  （文档章节树）  │
│                  │  │                  │  │                  │  │                 │
│  Zoekt BM25 文本 │  │  lkml_message    │  │  bug (Bugzilla)  │  │  Sphinx 章节树  │
│  SCIP 符号引用图 │  │  lkml_thread DAG │  │  syzbot_crash    │  │  kernel-doc     │
│  函数定义/调用链 │  │  lkml_patch      │  │  cve (NVD)       │  │  PageIndex 走查 │
│                  │  │  lkml_review     │  │  stack 签名      │  │                 │
│  ★ 复用          │  │                  │  │                  │  │  ★ 复用         │
│  CodeGraph 服务  │  │  我方 PG + Neo4j │  │  我方 PG         │  │  CodeGraph 服务 │
└────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  └────────┬────────┘
         │                     │                     │                     │
         └─────────────────────┴──────────┬──────────┴─────────────────────┘
                                          ▼
                             ┌────────────────────────┐
                             │   Cross-Graph Linker   │
                             │                        │
                             │  commit ↔ bug          │
                             │  commit ↔ LKML message │
                             │  commit ↔ CVE          │
                             │  OLK commit ↔ upstream │
                             └────────────────────────┘
                                          ▼
                              PG link_* 表 + Neo4j 关系边
```

---

### 子图一：Code Graph（源码图谱）

**数据来源**：OLK 内核 git 仓（`atomgit.com/openeuler/kernel`，分支 OLK-6.6 / OLK-5.10），与 CodeGraph 代码索引**同源同版本**。

**构建方式**：由独立的 `CodeGraph`（codesearch）服务承担，本系统通过 MCP HTTP 协议消费：

| 能力 | 实现 |
|------|------|
| 文件全文 + 符号文本搜索 | Zoekt trigram 索引 |
| 精确符号定义 / 引用 / 实现关系 | SCIP（scip-clang 基于 LLVM 编译分析） |
| 函数调用链 | SCIP 跨文件引用图 |
| 文件级大纲 | tree-sitter |

查询代码图谱时，系统通过 MCP 调用 `lookup_symbol(name, action='references', repo='olk-kernel-v6.6')` 即可得到所有调用点，无需自建符号索引。

---

### 子图二：Discussion Graph（邮件讨论）

**数据来源**：lore.kernel.org（公开 mailing list 归档），通过 Atom feed + 逐封 `/raw` 下载拉取。

**构建方式**：

```
Atom feed 分页（?q=d:YYYYMMDD..&x=A）→ 收集消息 URL
  ↓
每封邮件 /{list}/{msg-id}/raw 下载
  ↓
mbox 解析 → 单封邮件元数据 + body
  ↓
存入 PG lkml_message（含 body_tsv BM25 全文索引）
  ↓
基于 In-Reply-To / References header 构建线程 DAG
  → PG lkml_thread + Neo4j (:Message)-[:IN_REPLY_TO]->() 关系
  ↓
[PATCH vN] / Fixes: / Reported-by: trailer 解析 → lkml_patch
  ↓
Reviewed-by / Tested-by / Acked-by 解析 → lkml_review
  ↓
长线程（> 30 封）调用 LLM → 三段摘要存入 lkml_thread.summary
```

结果：每条邮件有上下文（属于哪个线程）、审查信号（Acked-by / NACK）和 patch 关联，形成一个可多跳遍历的讨论 DAG。

---

### 子图三：Bug Graph（缺陷与崩溃）

**数据来源**：bugzilla.kernel.org（v1 仅此源）、syzbot.kernel.org、NVD CVE feed。

**构建方式**：

```
Bugzilla REST API（last_change_time 增量）→ bug 元数据 + description
  ↓ 归一化
PG bug 表（title / severity / status / body_tsv）

syzbot HTML 抓取 → crash 标题 + reproducer + stack trace
  ↓
PG syzbot_crash 表 + 栈帧签名（ingest/syzbot/signature.py）

NVD JSON Feed（lastModStartDate/lastModEndDate 滑动窗口）→ CVE 元数据
  ↓
PG cve 表（cvss_v3_score / references / fix_commits）
```

fix_commits 字段：从 NVD references 的 GitHub/kernel.org commit URL 中提取内核 commit SHA，供 Cross-Graph Linker 做关联。

---

### 子图四：Doc Tree（文档章节树）

**数据来源**：OLK 内核 kernel-doc（Sphinx 构建产物）。

**构建方式**：完全由 CodeGraph 的独立 `kernel-docs-builder` 容器承担（Sphinx JSON backend），本系统通过 MCP 消费三层导航：

```
browse_docs()                → 站点级目录树（toctree）
browse_doc_sections(path)    → 单文档章节树
read_doc_section(path, sec)  → 具体节文内容
search_docs(query)           → BM25 文档命中
```

---

### Cross-Graph Linker：把 4 张子图"焊"在一起

这是系统的核心壁垒（`graph/linker.py`）。所有关联都有明确的文本证据，**不依赖相似度匹配**：

| 关系 | 链接方式 | 证据 | 存储 |
|------|---------|------|------|
| commit → bug | 扫描 commit body 中的 `Fixes: bsc#N`、`Closes: bugzilla.kernel.org/...` | trailer 文本 | PG `link_commit_bug` |
| commit → LKML message | 扫描 commit body 中的 `Link: https://lore.kernel.org/.../<msg-id>` | Link: trailer | PG `link_commit_message` |
| commit → CVE | NVD fix_commits 字段中的 commit SHA ↔ kernel_commit.hash | NVD references 中的 git URL | PG `link_commit_cve` |
| OLK commit → upstream | inclusion 头中的 mainline/stable SHA 锚点 | commit message 嵌入 | kernel_commit.upstream_commit |
| commit → function | git diff 文件 → CodeGraph `lookup_symbol` | 运行时解析，不持久化 | — |
| stack frame → function | 函数名 → CodeGraph `lookup_symbol` | 运行时解析，不持久化 | — |

**OLK commit 的特殊处理**：OLK 内核 ~78% 的 commit 是从 mainline/stable 回移的 backport，commit body 顶部有 inclusion 头记录上游锚点：

```
mainline inclusion          ← 类型
from mainline-v6.12-rc1
commit d1877cc7270302081a   ← 上游 mainline SHA（可直接用）
category: bugfix
CVE: CVE-2024-50022
...
[ Upstream commit d1877cc7 ] ← stable 类型才有此行，是真正的 mainline SHA
```

Cross-Graph Linker 解析这个 inclusion 头，将 OLK commit 桥接到 kernel.org 生态（LKML 讨论、Bugzilla、CVE），让诊断 agent 能从 OLK 的 crash 追溯到原始 mainline 讨论。

---

### 图在检索时如何使用

检索时触发 7 路并行召回（`retrieval/engine.py`），每路独立：

```
用户问题 "v6.6 上 order=4 的 normal zone OOM"
  ↓ LLM 解析 → RetrievalQuery(fault_domain='oom', kernel_version='OLK-6.6', ...)
  ↓
  ├─ code    → CodeGraph search_code('oom_kill_process', repo='olk-kernel-v6.6')
  ├─ docs    → CodeGraph search_docs('OOM killer')
  ├─ lkml   → PG: SELECT FROM lkml_message WHERE body_tsv @@ 'oom & order & normal'
  ├─ bug     → PG: SELECT FROM bug WHERE body_tsv @@ 'oom & zone & normal'
  ├─ syzbot  → PG: SELECT FROM syzbot_crash WHERE body_tsv @@ 'oom & order=4'
  ├─ commit  → PG: SELECT FROM kernel_commit WHERE body_tsv @@ 'oom & zone'
  └─ cve     → PG: SELECT FROM cve WHERE body_tsv @@ 'oom & memory'
  ↓
多路结果合并 → 候选 > 10 时 LLM 重排
  ↓
Evidence 列表（每条带 route 标签 + 可追溯来源）
```

诊断 agent 拿到 Evidence 后，可继续通过 `link_commit_bug` / `link_commit_message` 做多跳：比如找到一个 CVE 相关的 commit，再从 `link_commit_message` 找到对应的 LKML 讨论线程，还原当年的根因分析。

---

### 关键设计原则

1. **无 embedding**（ADR-001）：所有关联基于结构化字段、正则提取、BM25，无向量相似度，结果完全可审计
2. **证据优先**：每条关联都有 `source`（trailer / nvd_ref / subject 匹配）和 `confidence` 字段，诊断报告引用时可追溯
3. **Code Graph 与 commit 图谱同源**（ADR-018）：CodeGraph 索引 `olk-kernel`，commit ingester 也拉 OLK git，行号完全一致，Cross-Graph Linker 不会出现代码/commit 错位

---

*参考文档：[Architecture.md](v1/Architecture.md) · [ADR-018](v1/adr/ADR-018-commit-source-olk-kernel.md) · [graph/linker.py](../graph/linker.py)*

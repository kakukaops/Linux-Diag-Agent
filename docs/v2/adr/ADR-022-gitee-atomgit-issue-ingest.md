# ADR-022 — 引入 gitee / atomgit issue ingester 补齐 `link_commit_bug`

| 字段 | 值 |
|------|---|
| 状态 | Proposed |
| 日期 | 2026-05-20 |
| 决策者 | 用户 + Architect |
| 关联模块 | 新 ingester `ingest/gitee_issue/` · M2 storage · M4 graph |
| 相关 ADR | [ADR-011](../../v1/adr/ADR-011-bugzilla-kernel-org-only.md)（v1 仅 bugzilla.kernel.org） |
| 设计基础 | 本会话实测：`link_commit_bug = 0`；OLK commit 63K+ 引用 gitee issues |

## 上下文

v1 的 Cross-Graph Linker 设计上能从 commit body 的 `bugzilla:` trailer 关联到 `bug` 表。但本会话实测发现 **`link_commit_bug = 0`**——零关联。原因不是 linker bug，是**数据覆盖缺口**：

| OLK commit `bugzilla:` trailer 类型 | 数量 | 在 `bug` 表里吗？ |
|--------------------------------------|------|------------------|
| `gitee.com/openeuler/kernel/issues/XXXXX` | ~63,000 | ❌ 不在 |
| `atomgit.com/openeuler/kernel/issues/NNNN` | ~7,900 | ❌ 不在 |
| `bugzilla.kernel.org/show_bug.cgi?id=N` | ~1,625 | ⚠️ 部分（仅 v1 日期窗口内）|

OLK 用 gitee/atomgit 作为 issue tracker，但 v1 的 `bug` 表只装 kernel.org bugzilla（[ADR-011](../../v1/adr/ADR-011-bugzilla-kernel-org-only.md) 明确 v1 仅此源）。结果 71K 条 commit 的 issue 关联全部丢失。

这条链路丢失意味着诊断时无法回答："这个 commit 对应哪个 issue？issue 描述了什么场景？谁在跟进？"

## 决策

**v2.1 引入 gitee/atomgit issue ingester**，补齐 `bug` 表的 OLK issue 数据，启用 Cross-Graph Linker 对这两个 URL 模式的关联。

### 数据流

```
gitee.com/openeuler/kernel/issues/* （63K commits 引用）
atomgit.com/openeuler/kernel/issues/* （7.9K commits 引用）
   ↓
新 ingester（ingest/gitee_issue/，同 BaseIngester 模式）
   ↓
bug 表 with source ∈ {'gitee', 'atomgit'}
   ↓
Cross-Graph Linker 扫 commit.body 中的 gitee/atomgit URL
   ↓
link_commit_bug 填充
```

### 实施细节

| 项 | 决策 |
|----|------|
| 数据源 | gitee.com / atomgit.com 公开 REST API |
| 请求频率 | 1.5s/req（同 lkml/bugzilla 限速基线，[ingest/CLAUDE.md](../../../ingest/CLAUDE.md)）|
| 增量策略 | `last_change_time` 检查点（同 bugzilla 模式）|
| `bug.source` 取值 | `'gitee'` / `'atomgit'`（与 v1 的 `'bugzilla_kernel'` 并列）|
| `bug.external_id` 格式 | 数字 ID（gitee：纯数字；atomgit：纯数字，与 gitee 不冲突，因 source 区分）|
| Linker 模式扩展 | 在 `graph/linker.py` 加 `_GITEE_URL_RE = r"gitee\.com/openeuler/kernel/issues/(\w+)"` 和 atomgit 同形 |
| 抓取范围 | 仅 `openeuler/kernel`（atomgit）和 `openeuler/kernel` repo（gitee）|

### 数据预估

- gitee 入库估计：基于 OLK commit 引用提取去重后 issue ID，预估 ~10-15K 唯一 issue
- atomgit 入库估计：~3-5K 唯一 issue
- 入库总耗时：10-20K issues × 1.5s = 4-8 小时（首次全量）

## 影响

### 优势

| 维度 | 影响 |
|------|------|
| `link_commit_bug` 从 0 → 万级 | OLK 内部 issue 跟踪与 commit 强关联，诊断时可回答"这个 patch 是什么 issue 推动的" |
| 知识图谱完整性 | v1 缺失的 OLK 内部讨论补齐 |
| 不影响 v1 现有 bug 表 | 通过 `source` 字段区分，向后兼容 |

### 代价

| 维度 | 影响 |
|------|------|
| 新 ingester 工时 | ~7-10 PD（API client + ingester + linker 扩展）|
| 网络依赖 | gitee/atomgit API 可用性；限速 |
| 存储 | ~1-2 GB（issue body）|
| 数据质量 | gitee/atomgit issue 描述质量参差（短描述、中文混杂），需要 BM25 全文索引但不强求结构化字段 |
| 同步频率 | 周度（同其他 ingester），不是实时 |

### v1 ADR-011 的状态

ADR-011 "v1 仅 bugzilla.kernel.org" 在 v1 阶段是正确的（[ADR-011](../../v1/adr/ADR-011-bugzilla-kernel-org-only.md)）。v2 不**否决** ADR-011，而是**扩展**：bugzilla.kernel.org 仍是上游 mainline bug 的权威源；gitee/atomgit 是 OLK 内部 bug 的权威源。两者通过 `bug.source` 区分共存。

## 备选方案（已否决）

### 备选 A：等 OLK 把 issue 全部迁到 bugzilla.kernel.org

否决理由：OLK 选 gitee/atomgit 是社区决定，等不到；71K commit 的链路不能空着。

### 备选 B：通过 commit body 全文 BM25 找"bug X" 关键词，不入 bug 表

否决理由：得不到结构化 issue 元数据（标题、状态、负责人）；BM25 召回不够精确。

### 备选 C：网页爬虫直接抓 gitee issue 页面

否决理由：REST API 更稳定；网页爬虫脆弱（HTML 改版即坏）。

## 安全 & 隐私

| 边界 | 约束 |
|------|------|
| API 凭据 | 公开 issue 不需要凭据；如 gitee 限速更严，部署时可配置 token in `configs/local.yaml`（gitignored）|
| 数据隐私 | 仅抓公开 issue；private issue 不抓 |
| IP 封禁风险 | 限速 1.5s/req；如多次失败自动暂停 |

## 验证

- v2.1 acceptance：`link_commit_bug` ≥ 10,000 条（[PRD §6.2](../PRD.md)）

## 未来回顾

- 如发现 gitee/atomgit issue 体量超出预估（>50K），评估按 issue 状态过滤（只入 closed/fixed）
- 与 OLK 维护团队对齐是否需要双向同步（v2 暂不双向）

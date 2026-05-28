# Knowledge Graph — 设计与验收标准 (v2.3)

**状态**：草案 (2026-05-28)，作为后续 KG 完善工作的参照基准。

**为什么写这个**：v2.3 审计发现"图谱不准确导致前面大量工作都搞错了"。例如
`kernel_commit.subsystem` 列被 trailer 解析 bug 污染了 366K 行（29 % 是
URL 而非真实子系统），又如 v1.0 的 `link_commit_bug` 长期是 0 行因为 OLK 的
bug 引用走 gitee/atomgit 而非 bugzilla.kernel.org（ADR-022 才补上）。这些
错都源于 KG 节点/边的语义、覆盖、保真未被显式定义和验证 — 写一遍 BM25 调优
解决不了根因。本文档把这件事**显式化**：什么是合格的 KG、怎么测、什么算回退。

> 离线优先 / 知识库优先（ADR-001）的核心，是 KG 必须本身可信。这是基础设
> 施层，不是模型层。

---

## 一、设计原则（不可妥协）

1. **每条边必须有文本证据**。无相似度匹配、无 embedding、无 LLM "猜测"
   推导出的边。

2. **每条边可追溯到源**。`source` 字段必须明确取值（`trailer` / `nvd-ref`
   / `git-diff` / `subject-prefix` / `link-url` 等）。下游可按 source
   过滤可信度。

3. **节点不重复 / 不污染**。例如 merge commit 不与 squash 后的 parent
   commit 共存于 link_commit_symbol（merge commit 写 sentinel）。

4. **数据保真度优先于覆盖广度**。宁可 50 % 覆盖率 + 99 % 准确，不要
   95 % 覆盖率 + 70 % 准确。下游 LLM 看到错误结论比 "查不到" 更糟。

5. **任何 backfill 必须 idempotent + 有 sentinel 机制**。教训：
   [feedback-backfill-not-exists-loop] — NOT EXISTS 候选 SELECT 配合
   空结果不写入，导致同一批候选无限重选。9.5 h 0 收益的教训。

6. **数据污染问题 P0 优先**。例如 subsystem 列被污染就要立刻清理 +
   修复源头解析器，不能"后面再说"。

7. **每个表必须有 unique constraint**。防 ingestion 重跑产生重复行。

---

## 二、目标节点 & 边

### 节点（实体）

| 表 | 主键 | 状态 | 备注 |
|---|---|---|---|
| `kernel_commit` | hash | ✅ | OLK + mainline 全量；body / subject / affected_versions / olk_inclusion_type / upstream_commit / commit_date / origin |
| `lkml_thread` | root_message_id | ✅ | LKML 邮件线程 |
| `lkml_message` | message_id | ✅ | 单封邮件，body_tsv GIN |
| `lkml_patch` | patch_id | ✅ | Patch 邮件 + files_changed + series_total |
| `lkml_review` | (msg, reviewer) | ✅ | Reviewed-by / Acked-by / NACK |
| `bug` | (source, external_id) | ✅ | Bugzilla + gitee + atomgit（ADR-022 补） |
| `cve` | cve_id | ✅ | NVD + fix_commits JSONB |
| `syzbot_crash` | syzbot_id | ✅ | crash + stack_signature |
| **`dmesg_event`** | event_id | ❌ 待建 | 用户输入解析后的事件 — fault_kind, kernel_version, stack_signature, raw_text |

### 边（关系）

| 表 | source 取值 | 状态 | 当前规模 |
|---|---|---|---|
| `link_commit_message` | `trailer-Link` / `subject-fuzzy` | ✅ | LKML 入库依赖 |
| `link_commit_bug` | `trailer-bugzilla` / `trailer-gitee` / `trailer-atomgit` | ✅ post-ADR-022 | 待回填 |
| `link_commit_cve` | `nvd-ref` / `trailer` | ✅ | NVD 完整 |
| `link_commit_fixes` | `trailer-Fixes` / `subject-Revert` | ✅ | ~87 K |
| `link_commit_revert` | `subject-Revert` / `body-Reverts` | ✅ | ~6 K |
| **`link_commit_symbol`** | `git-diff-hunkctx` | ✅ v2.3 新建 | ~108 K real + 1.4 K sentinel；OLK 2024-01-01+ |
| **`link_event_signature`** | `stack-hash-top5` | ❌ 待建 | 把 dmesg/bug/lkml-bug-report 都映射到统一 stack signature |
| **`link_commit_fault_domain`** | `body-keyword` / `subject-prefix` | ❌ 待建 | commit → {oom, oops, lockup, panic, ...} 标签 |
| **`link_author_subsystem`** | `aggregate-derived` | ❌ 待建 | 谁改 net/tcp 最多 → 维护者权威度 |
| **`link_message_patch_series`** | `In-Reply-To` / `Subject-PATCH-N/M` | ❌ 待建 | 同系列其它 patch |

### 列（节点上的内嵌属性）

| 表.列 | 来源 | 状态 |
|---|---|---|
| `kernel_commit.subsystem` | `infer_subsystem(changed_files)` | ✅ 已清洗（v2.3） |
| `kernel_commit.upstream_commit` | trailer + OLK inclusion | ✅ |
| `kernel_commit.affected_versions` | git branches | ✅ |
| `kernel_commit.olk_inclusion_type` | trailer parser | ✅ |
| `kernel_commit.fixes_refs[]` | trailer `Fixes:` | ✅（链表已物化到 link_commit_fixes） |
| `cve.fix_commits[]` | NVD references | ✅ |
| `syzbot_crash.stack_signature` | sha256 of normalize_stack | ✅ 但仅 syzbot |
| **`bug.stack_signature`** | 同上 | ❌ 待建 — 跨表推广 |
| **`lkml_message.stack_signature`** | 同上 | ❌ 待建 — 仅对含 trace 的邮件 |
| **`dmesg_event.stack_signature`** | 同上 | ❌ 待建 |

---

## 三、验收 KPI（每个节点/边一组）

每个表需要满足 5 个维度，无验收即视为不可用：

1. **完整性** — populated / total，目标因表而异
2. **保真度** — 抽样 100 行人工/规则核验准确率
3. **可追溯** — 每行的 source 字段非 NULL 且取值在允许集合内
4. **一致性** — 跨表 FK 闭合（无悬挂引用）
5. **新鲜度** — 数据最大年龄 ≤ 7 天（增量 ingestion 频率）

### 具体 KPI 表

| 表 / 列 | 完整性 | 保真度 | 验证方法 |
|---|---|---|---|
| `kernel_commit.hash` | 100 % | 100 % | DB invariant（PK） |
| `kernel_commit.subsystem` | ≥ 80 % | ≥ 95 % | 抽 100 行，subject prefix 与 subsystem 字段一致率 |
| `kernel_commit.upstream_commit` | OLK backport 100 % 有 | ≥ 99 % | `git cat-file -t` 验证 SHA 存在 |
| `kernel_commit.affected_versions` | 100 % | ≥ 99 % | inclusion parser 单测覆盖 |
| `kernel_commit.fixes_refs[]` | trailer 含 `Fixes:` 100 % 抽取 | ≥ 98 % | 抽 100 行人工对照 trailer |
| `link_commit_symbol` | OLK 2 y 90 % | ≥ 95 % | 反向查 100 个 symbol，top-3 commit 合理性 |
| `link_commit_symbol` (sentinel) | merge commit 100 % 有 sentinel | -- | DB invariant |
| `link_commit_message` | LKML 入库覆盖度 80 % | ≥ 99 % | URL trailer 正则准确性 |
| `link_commit_bug` (post-ADR-022) | OLK commit 引用 gitee 的 ≥ 70 % | ≥ 95 % | trailer 抽样 |
| `link_commit_cve` | NVD reference 含 commit URL 100 % | ≥ 99 % | NVD 是权威源 |
| `link_commit_fixes` | trailer `Fixes:` 100 % | ≥ 98 % | SHA 在 DB 存在率 |
| `link_commit_revert` | subject `Revert ` 100 % | ≥ 95 % | 抽样 |
| `link_event_signature`（待建） | trace 含 ≥ 3 帧的 100 % | ≥ 99 % | 哈希稳定性测试 |
| `cve.cvss_v3_score` | ≥ 80 %（部分 CVE 无 v3） | NVD 权威 | -- |

### 端到端集成 KPI（最终用户体感）

- **v2 eval recall@10**（仅看有 GT commit 的 case）≥ **40 %**
- **find_commits_touching_symbol 命中率**（GT 在 top-10 of reverse lookup）≥ **60 %**
- **grounded_rate** ≥ **50 %**（of diagnosed cases）
- **grounded + judge-correct rate** ≥ **25 %**（最严指标）
- **abstention_rate**（主动 `<insufficient_evidence>`）≥ **10 %** 且 < **40 %**

> v2.3 当前：recall@10 ≈ 5.6 %（基线），grounded_rate 20 %，
> grounded+correct 0 %。**全部低于上述目标**。这是后续 v2.4 攻关方向。

---

## 四、当前差距与优先级

| 项 | 状态 | 优先级 | 工时估 |
|---|---|---|---|
| `subsystem` 列污染 | ✅ 366 K 行已清洗 + parser 已修 | -- | 已完成 |
| `link_commit_symbol` 2 y backfill | ✅ 108 K 真实边 | -- | 已完成 |
| `link_commit_symbol` 历史 < 2024 回填 | ❌ pending | P3 | ~4 h（estimated） |
| `link_event_signature` 统一 stack hash | ❌ 仅 syzbot | **P1** | 2-3 天 |
| `dmesg_event` 节点表 + ingest pipeline | ❌ 不存在 | **P1** | 1-2 天 |
| `link_commit_fault_domain` | ❌ 不存在 | P2 | 3-4 天 |
| `link_author_subsystem` | ❌ 不存在 | P3 | 1 天（派生） |
| `link_message_patch_series` | ❌ 不构图 | P3 | 1 天 |
| Neo4j ↔ PG 对账自动化 | 🟡 `reconcile.py` 存在但少跑 | **P1** | 0.5 天（加 cron） |
| KG 保真度 CI 测试 | ❌ 不存在 | **P0** | 1 天 |
| 历史 LKML 全量回填 | 🟡 进行中（180 d） | P2 | 2-4 h（剩余） |
| gitee/atomgit issue ingest | ✅ ADR-022 已 land | -- | 已完成 |

---

## 五、KG 验证 CI（P0 — 必做）

每次 KG-相关代码合入或 ingestion 完成后，必须自动运行：

### 5.1 单元层（毫秒级）
- 全部 `tests/unit/test_*_extractor.py` / `test_trailer_parser.py` / `test_olk_inclusion.py` / `test_subsystem_inference.py` / `test_symbol_extractor.py` / `test_git_wrapper_parser.py` 通过

### 5.2 数据层完整性（秒级）
跑 `scripts/kg_invariants.sql`（待写）：
- 所有 link 表的 commit_hash 都在 kernel_commit 中存在（FK 闭合）
- 所有 `subsystem` 不包含空格
- 所有 `upstream_commit` 是合法 40-char hex 或 NULL
- `kernel_commit.hash` 不重复
- `link_commit_symbol.kind` 在 `{'function','struct','macro','unknown','sentinel'}`
- 等

### 5.3 保真度抽样（分钟级）
跑 `scripts/kg_audit.py`（待写）：
- 随机抽 100 行各表
- 对照源数据（git log / NVD JSON / lore raw）人工或规则验证
- 任一表保真度 < 95 % → 报警

### 5.4 端到端回归（小时级）
跑 v2 eval `eval/runner_v2.py` 全量 → 与上次基线比对：
- recall@10 不允许下降 > 5 pp
- grounded_rate 不允许下降 > 5 pp
- root_cause_correct 不允许下降 > 10 pp

---

## 六、不可妥协的反模式（PR 拒绝合入）

以下任一情况发现于 PR 必须返工：

1. 添加新 link 表但 `source` 字段缺失或允许 NULL
2. backfill 脚本无 sentinel 机制（违反 [feedback-backfill-not-exists-loop]）
3. 用 embedding / 向量 / 相似度建立任何边
4. 用 LLM 推测建立无文本证据的边（除非 confidence < 0.5 + source 显示
   "llm-inferred"，且下游能过滤）
5. 修改 `kernel_commit.subsystem` 写入逻辑而未更新
   `test_subsystem_inference.py`
6. ingestion 写入但**不**用 `ON CONFLICT DO NOTHING` 或 `DO UPDATE SET`
7. 添加新 stack signature 算法但与 `ingest/syzbot/signature.py` 不
   兼容（必须统一）

---

## 七、Next Steps（按优先级）

**v2.4 KG 攻关期目标**：把 grounded+correct 从 0 % 拉到 25 %。

### P0（这周）
- [ ] 写 `scripts/kg_invariants.sql` — 完整性 invariant 检查
- [ ] 写 `scripts/kg_audit.py` — 抽样保真度审计
- [ ] CI 集成（pre-commit + nightly）

### P1（两周内）
- [ ] migration 0008：`bug.stack_signature`, `lkml_message.stack_signature`
- [ ] migration 0009：`dmesg_event` 表 + ReAct 工具 `find_similar_crashes`
- [ ] Neo4j reconcile 自动化（nightly job）

### P2（一个月内）
- [ ] `link_commit_fault_domain` — 把 SOP yaml 转换为 fault_taxonomy 表
- [ ] `subsystem` 列其余 NULL 行的回填（需 git log 重新解析 changed_files）

### P3（季度）
- [ ] `link_author_subsystem`（派生表）
- [ ] `link_message_patch_series`
- [ ] 历史数据回填（< 2024 commit）

---

## 八、相关 ADR / 参考

- ADR-001 — 不用 embedding / 向量
- ADR-013 — 7-route always-fire-all 召回
- ADR-018 — Commit source = OLK 而非 mainline
- ADR-022 — gitee/atomgit issue 入库（补 link_commit_bug 数据缺口）
- ADR-025 — 三层 LKML 模型
- `memory/project_kg_gaps_v23.md` — KG gap 审计
- `memory/feedback_backfill_not_exists_loop.md` — backfill 死循环教训
- `memory/feedback_kernel_commit_hash_bug.md` — hash 污染历史教训

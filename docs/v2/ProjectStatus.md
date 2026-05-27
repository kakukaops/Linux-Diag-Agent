# Linux-Diag-Agent v2 — 项目现状

| 字段 | 值 |
|------|---|
| 最后更新 | 2026-05-20 |
| 状态 | **文档设计阶段（Design Draft, Review Round 1 + Round 2 已应用）** —— 待最终评审，代码开发未启动 |

---

## 1. 当前阶段

| 阶段 | 状态 |
|------|------|
| v1 收尾 | ⏳ LKML stable 已完成（59,715 条）；linux-mm 补跑中；Linker 已重跑 |
| v2 文档（PRD / Architecture / ProjectPlan / ADR-019~025）| ✅ 起稿完成 + ADR-025 同步，待评审 |
| v2 代码开发 | ⏸ 未启动（待文档评审通过 + 用户授权）|

## 2. v2 文档清单

| 文档 | 状态 | 路径 |
|------|------|------|
| PRD | ✅ Draft | [PRD.md](PRD.md) |
| Architecture | ✅ Draft | [Architecture.md](Architecture.md) |
| Project Plan | ✅ Draft | [ProjectPlan.md](ProjectPlan.md) |
| ADR-019 Hybrid ReAct + 确定性骨架 | ✅ Draft | [adr/ADR-019-hybrid-react-deterministic.md](adr/ADR-019-hybrid-react-deterministic.md) |
| ADR-020 drgn over crash | ✅ Draft | [adr/ADR-020-drgn-over-crash.md](adr/ADR-020-drgn-over-crash.md) |
| ADR-021 Observability Adapter Pattern | ✅ Draft | [adr/ADR-021-observability-adapter.md](adr/ADR-021-observability-adapter.md) |
| ADR-022 gitee/atomgit issue 入库 | ✅ Draft | [adr/ADR-022-gitee-atomgit-issue-ingest.md](adr/ADR-022-gitee-atomgit-issue-ingest.md) |
| ADR-023 Hardware-first SOP 路由 | ✅ Draft | [adr/ADR-023-hardware-first-sop-routing.md](adr/ADR-023-hardware-first-sop-routing.md) |
| ADR-024 Insufficient-evidence 出口 | ✅ Draft | [adr/ADR-024-insufficient-evidence-exit.md](adr/ADR-024-insufficient-evidence-exit.md) |
| ADR-025 知识数据 online-default（取代 v1 ADR-010）| ✅ Draft | [adr/ADR-025-knowledge-data-online-default.md](adr/ADR-025-knowledge-data-online-default.md) |
| V2 v1-模块增量 + 新 ingester + 上线策略 | ✅ Draft | [V2_v1_modules_supplements.md](V2_v1_modules_supplements.md) |
| M9 Crash Forensics 详设 | ✅ Draft | [modules/M9_crash_forensics.md](modules/M9_crash_forensics.md) |
| M10 Change Correlator 详设 | ✅ Draft | [modules/M10_change_correlator.md](modules/M10_change_correlator.md) |
| M11 Hardware Layer 详设 | ✅ Draft | [modules/M11_hardware_layer.md](modules/M11_hardware_layer.md) |
| M12 Observability Adapter 详设 | ✅ Draft | [modules/M12_observability_adapter.md](modules/M12_observability_adapter.md) |
| M22 ReAct Loop Engine 详设（原 M13）| ✅ Draft | [modules/M22_react_loop_engine.md](modules/M22_react_loop_engine.md) |
| M23 Eval Feedback Loop 详设（原 M14）| ✅ Draft | [modules/M23_eval_feedback_loop.md](modules/M23_eval_feedback_loop.md) |

## 3. 设计基础

v2 的全部设计源自本会话的两份关键文档：

- [docs/AgentThink.md](../AgentThink.md) —— 业界调研 + L1/L2/L3 三层壁垒模型 + 8 类工具规划
- [docs/v1/ProjectStatus.md](../v1/ProjectStatus.md) —— v1 实测局限（Recall@10 6.7%、link_commit_bug=0 等）

## 4. Review Round 1 修订摘要（2026-05-20）

基于自评 + 用户反馈，本轮已修订以下 12 个问题（详情见各文档对应位置）：

| 严重度 | 修订项 | 受影响文档 |
|--------|--------|-----------|
| 必改 | huatuo 定性确认（eBPF kernel observability，[huatuo.tech](https://huatuo.tech/)），并取消 huatuo 单独拔高；统一 metrics 用 PromQL、logs 用 ES DSL、traces v2 不做 | ADR-021、Architecture、PRD |
| 必改 | ReAct 工具失败语义 + invalid tool call 处理 | ADR-019 |
| 必改 | ProjectPlan 估算修正（T-003 8→10、T-013 8→13、T-021 5→10），v2.0 总 PD 71→90 | ProjectPlan §2.2/§2.5 |
| 必改 | 数据集采集前移到 M13（与开发并行），M15 才有时间用数据集做 acceptance | ProjectPlan §1.1 |
| 应改 | `evidence_strength_score` 校准计划（logistic regression after ≥30 annotated cases）| ADR-024 |
| 应改 | hardware SOP 完整内容延后到 M11 module 详设 | ADR-023 |
| 应改 | vmcore 传输方案（SSH+SCP / sosreport 包内 / 用户提供路径）| Architecture §8.0 |
| 应改 | M9-M11 vs M12 部署形态分类（本地 MCP server vs 远程 HTTP client）| Architecture §1.4 |
| 应改 | `agent_tool_trace` 表 schema | Architecture §6.3 |
| 可改 | "至少 1 例"改为召回率 ≥ 80% / 精确率 ≥ 80% | PRD §6.1 |
| 可改 | 优先扩展 LangGraph prebuilt ReAct，自研为 fallback | ADR-019 |
| 可改 | v2.2 详细规划延后到 v2.1 验收后细化 | PRD §3.3、ProjectPlan §2.5 |

**新增任务**（v2.0）：
- T-025 ReAct prompt 模板设计（3 PD）
- T-026 Token cost 模型 + budget 校准（1 PD）
- T-027 v1→v2 兼容性测试 + 灰度上线策略（3 PD）

## 5. Module 详设完成（2026-05-20）

v2 全部 6 个新模块的详设文档已起草完毕（M9-M12 + M22 + M23，共 ~75KB），覆盖：

- 子组件设计、MCP 工具 schema、内部协议
- 实现要点（含 spike 验证项）
- 与其他模块的集成接口
- 风险表 + ProjectPlan task ID 对照

## 6. Review Round 2 修订摘要（2026-05-20）

针对 v2 全套 16 文档的结构性自评 + 用户授权，本轮已修订 10 个问题：

| 严重度 | 修订项 | 影响 |
|--------|--------|------|
| 严重 | **S-1 命名冲突**：模块 M13/M14 与里程碑 M13/M14 重名 → 重编为 M22 / M23 | 全部 v2 文档 cross-ref 更新 |
| 严重 | **S-2 v1 模块增量缺失** → 新建 [V2_v1_modules_supplements.md](V2_v1_modules_supplements.md)，覆盖 M2/M4/M5/M6/M7 改动 + 新 ingester + 灰度策略 | 新 1 文档 |
| 严重 | **S-3 工具注册协议** → 在 supplements §3 正式规定，M22 详设引用 | M22、supplements |
| 中等 | **M-4 taint_flags 共享位置** → 统一 `mcp_servers/shared/taint_flags.py` | M9、M11 |
| 中等 | **M-5 `eval_human_judgments` schema** → M23 §3.0 给出 DDL | M23 |
| 中等 | **M-6 gitee/atomgit ingester 模块详设** → supplements §2 | supplements |
| 轻 | **L-7 PD 双重归属** → T-001 归 M7 triage，M11 不重复计入 | M11 |
| 轻 | **L-8 Architecture §5.3 M22/M23 详细** → 补充 §5.3.1 + §5.3.2 | Architecture |
| 轻 | **L-9 v1 ancestry 对照表** → supplements §5 | supplements |
| 轻 | **L-10 v1→v2 灰度策略** → supplements §4（影子 → opt-in → 默认 v2） | supplements |

总文档体量（v2 全套）：约 130KB（10 → 17 个文件，新增 supplements 含 4 节内容）。

## 6. 下一步

待用户最终评审 v2 文档后决定：
1. 是否还有需要的修改（Review Round 2）
2. 代码开发启动时机
3. M13 LangGraph prebuilt spike 启动（建议先做,1 周验证）

代码开发暂未启动；v1 的 LKML 摄入收尾和 Linker 重跑独立于 v2 进行。

---

## 7. v2.0 实测 baseline

数据集：`eval/data/cases_v2.json` 15 例；模型：openrouter `deepseek/deepseek-v4-flash`；rate 600 rpm（付费 tier）。

### v2.0 baseline 演进（同一数据集多次跑）

| 跑次 | 配置 | Wall time | ERROR | diagnosed | Recall@10 | Route acc | Root-cause acc | 备注 |
|---|---|---|---|---|---|---|---|---|
| baseline (初次) | serial | 75 min | 6.7% (1) | 12 | 59.5% | 92.9% | 41.7% (5/12) | 初版 |
| post P0-P2 fix | concurrency=3 | 37.5 min | 33% (5) | 6 | 65.0% | 100% | 80% (5/7) | 暴露 catch-all 包装 bug |
| post bug fix (5-25) | concurrency=3 + retry fix | 35.7 min | 6.7% (1) | 9 | 59.5% | 100% | 55.6% (5/9) | 稳定但 link_commit_bug=1 缺口仍在 |
| **final** (2026-05-26) | **+ADR-022 (gitee 51K + atomgit 6.7K links)** | **47.1 min** | **0/15** ✓ | 8 | 53.3% | **100%** | **75.0% (6/8)** ⬆ | cross-graph 全通，3 例 ✗→✓ |

**ADR-022 完成后的关键变化**：
- **Root-cause accuracy 55.6% → 75%** — 3 例从 ✗ 翻 ✓（oom-002 / kasan-001 / panic-001）。原因：bug 表新增 4,328 行 OLK-specific issue（含完整 body/堆栈），agent 能直接命中"同症状已知 issue"。
- **ERROR 0/15** — APIConnectionError pass-through 修复 + retry 完全消化网络瞬态。
- **Recall@10 微降 (59.5 → 53.3%)** 是分母变化（更多 case 走到 diagnosed → 更多被纳入分母），不是质量下降。
- **budget_exhausted 1 → 4** — bug body 3-11KB 注入 prompt 加速 token 累积，**已修：TOKEN_BUDGET 150K → 200K**。

### 按类别 recall（final）
| 类别 | n | recall | 评价 |
|---|---|---|---|
| oom | 3 | **0%** | BM25 keyword 错配（v2.1 P2 攻坚目标）|
| lockup | 3 | 33% | softlockup × 2 低，rcu 100% |
| oops | 3 | 67% | kasan + lockdep |
| panic | 2 | 50% | panic-001 半命中，panic-002 全中 |
| hardware | 2 | 100% | trivially-pass（ground_truth 空）|
| regression | 1 | 100% | change-001 命中 |
| io_hang | 1 | 100% | P1a 路由修复 + cross-graph 双重加成 |

### v2.0 final 验证项达成

| 验证项 | 目标 | 实测 | 状态 |
|---|---|---|---|
| Route accuracy | ≥ 90% | 100% | ✓ |
| Root-cause correctness | ≥ 60% | 75% | ✓ |
| 系统稳定性 (ERROR rate) | ≤ 10% | 0% | ✓ |
| Cross-graph `link_commit_bug` | ≥ 10K | 58,388 | ✓ |
| Wall time 全量 eval | ≤ 60 min | 47.1 min | ✓ |

## 8. v2.0 → v2.1 backlog

### 8.1 来自 P1b judge reasoning 分析

LLM judge 否定的 7 例失败模式归为 3 类：

| 模式 | 案例 | 根因 | v2.1 修复方向 |
|---|---|---|---|
| **A. 硬件归因缺失** | io-hang-001、softlockup-002 | kernel route prompt 没要求考虑硬件/存储备选；agent 把 nvme/scsi/SAN 超时诊断成内核 deadlock | **已补 kernel.md "Non-kernel Root Causes" 段**；后续可加 `nvme_timeout`/`scsi_abort` 模式触发 hardware route |
| **B. 过度具体化** | kasan-001、softlockup-001、rcu-001 | agent 锁定一个具体 commit/fix 当根因，但 GT 描述更宽的可能集；v1 的 generate_hypotheses + self_consistency 在 v2 ReAct 里没了 | 改 ReAct prompt 要求"列举 N 假设带置信度后再选"；或重启 self_consistency K=3 在 budget 允许时 |
| **C. 事实性错误** | panic-001 (major 254 = dm vs virtio_blk) | agent 不知道 `/proc/devices` 的 major 号映射 | 新增 `get_device_major_mapping` 工具（解析 `/proc/devices`） |

### 8.2 来自外部专家 review（2026-05-26）+ DB 数据审计

专家强调 commit graph 的 **patch lineage / Fixes-chain / Revert-chain** 是诊断价值的核心，跨 distro 采集对通用 Linux 诊断重要。结合我们 OLK 单 distro 定位 + 实测 DB，重新排序：

| 优先级 | 工作 | 数据规模 | 工作量 | 收益 |
|---|---|---|---|---|
| **P0a (NEW!)** | **物化 Fixes-chain → `link_commit_fixes` 表**。`kernel_commit.fixes_refs` 数组字段已存 87,750 行 trailer，只差建表 + JOIN 写入 | 87K 个 commit↔commit 边 | **30 min** SQL，**零抓取成本** | patch lineage 直接 O(1) JOIN，专家强调的核心能力上线 |
| **P0b (NEW!)** | **物化 Revert-chain → `link_commit_revert`**。解析 "Revert ..." subject + body 里的 "This reverts commit ..." trailer | ~6K 个 revert 关系 | 1h | 专家强调的"被 revert 又 redesign"链可走 |
| **P0c** | **mainline + stable git 入库**（ADR-018 Phase B，长期搁置）| +500K 节点 | 1-2 天 | 60K 悬空 upstream_commit SHA 升真实 commit；图谱 1.3M → 3M 节点 |
| P1 | **subsystem 自动分类**（从 commit changed files 路径推断 mm/net/fs/...） | 99% → 80%+ 字段填充 | 半天 | 按子系统过滤检索 |
| P1 | 硬件归因扩展：nvme/scsi/SAN timeout 触发 hardware route | — | 1 PD | 修 §8.1 模式 A 余下部分 |
| P1 | 假设枚举模式：ReAct prompt 强制 N 假设带置信度 | — | 1 PD | 修 §8.1 模式 B |
| P2 | `get_device_major_mapping` 工具 | — | 0.5 PD | 修 §8.1 模式 C |
| P2 | BM25 ts_rank_cd 替换（Tantivy BM25Okapi）| 数据迁移 | 1 周 | 当前 BM25 排名差导致 OOM recall 长期 0%，根治需要换引擎 |
| **不做** | Ubuntu / RHEL / Android / Debian 仓 | — | — | 不在 OLK 产品 scope，加进来 70%+ 重复（详见 FAQ Q9）|
| **不做** | semantic embedding | — | — | 违反 ADR-001；我们用 BM25 + graph 替代 |

### 8.3 专家观点 review 总结

外部专家提出的 5 个核心点对照：

| 专家观点 | 我们的状态 |
|---|---|
| "双层 commit graph（upstream + distro）" | ✅ 已做：OLK = distro 层，`upstream_commit` 物化 60,824 桥 |
| "LLM 不直接看 commit，先经图谱缩上下文" | ✅ ADR-019 + ReAct + cross-graph 就是这设计 |
| "commit 是因果图，不是文本知识" | ✅ ADR-001 拒绝 embedding 的根本理由 |
| "patch lineage / Fixes-chain 物化" | ❌ **重大缺口 — 87K 数据在手未建图谱**，P0a 立刻补 |
| "跨 distro 采集 Ubuntu/RHEL/Android" | ⚪ 不适用：OLK 单 distro 定位（FAQ Q9 论证）|

## 8.4 v2.2 路径（Run 7 grounding eval 后的决策）

Run 7 用新 grounding metric 揭示真相：**100% diagnosed cases 都是 speculative**，0 个 grounded。详见 [eval_v2.1_p0p1p2_grounding_report.md](eval_v2.1_p0p1p2_grounding_report.md)。

根本原因不是 metric 太严（overlap 阈值 0.20 校准合理），是两件事被诚实暴露：

1. **BM25 召回质量结构性不足** —— OOM 类 recall 长期 0%，agent 拿到的 evidence 池根本不含 ground truth commit → 引用啥都跟 claim 无语义关系
2. **配置/硬件故障没 commit 可引用** —— panic-001（initramfs 配置）/ hardware-* / io-hang-001 这类，ground truth 不在 commit 域

### v2.2 主 KPI 切换

| 旧 KPI（已废）| 新 KPI（生效）|
|---|---|
| `root_cause_accuracy ≥ 60%` | **`grounded_correct_rate ≥ 30%`** |
| 看似多少 case 答对 | 多少 case 答对且引用真有语义证据 |

旧 metric 是单 judge + verified-by-hash-existence 拼出的虚高数。新 metric 是真实下限。

### v2.2 P0 — 修 BM25 召回

两条路径：

**路径 B（先做）— AND-priority recall + 扩 candidate pool**
- 工作量：2 天
- 修 `retrieval/recall/*.py`：先 AND-match（所有关键词都命中），0 hit 才 OR 降级
- candidate pool 50 → 100 给 reranker
- 预期：oom recall 0% → 15-25%；grounded_correct_rate 0% → 15%+
- 风险：rerank token 成本 ~6×；AND-match 在 query 模糊时直接 0 recall

**路径 A（如 B 不够再上）— Tantivy / Lucene 替换 PG**
- 工作量：1-2 周
- 自建 BM25 服务，替换 `ts_rank_cd` 为 BM25Okapi
- 迁移 commit / lkml / bug / cve / syzbot 5 张表索引
- 预期：oom recall 0% → 30%+；整体 recall 53% → 70%+
- 风险：新依赖、容器化、运维一份

**判定标准**：跑完 B 看 grounded_correct_rate。≥30% 收尾；<15% 上 A。

### v2.2 P1（次要修复）

| | |
|---|---|
| 调查 lockdep-001 / change-001 2-3 iter 早终止 | P2 evidence trace 要求可能让 agent 提前 bailout 但 verdict 未对应 |
| 修 atomgit ~3 个真实可恢复的 gap | 30 min 小补 backfill |
| 扩 cases_v2 → 30+ 例 | 当前 15 例分类后 n=1-3 太弱 |
| 增量 issue 更新机制（gitee/atomgit）| 当前一次性 backfill，新 issue 进不来 |

## 9. 本会话已落地的改进（2026-05-25）

| 改动 | 文件 | 影响 |
|---|---|---|
| query_parser format 转义 + `.provider`→`.backend` | `retrieval/query_parser.py` | 阻止每次查询都掉到 regex 回退；keywords 质量↑ |
| ReAct 终止增强（footer + tool_choice=none + finalize reminder）| `agent/react/loop.py` + `prompts/__init__.py` | budget_exhausted 12→1 (smoke：100% diagnosed) |
| openai_compat max_retries=0 | `llm/provider/openai_compat.py` | 禁 SDK 自动重试，让应用层限流生效 |
| dmesg extractor 增 `page allocation failure` / `hung task` 模式 | `mcp_servers/dmesg_journal/extractor.py` | oom-002 / io-hang-001 不再分到 unknown |
| reranker 默认开启（阈值 10→1）+ 输入扩展 20→60 + 候选池 20→50 | `retrieval/reranker.py` + `retrieval/schema.py` | 召回质量预期↑（待重测验证）|
| Cross-graph commit 注入 | `retrieval/engine.py` `_inject_linked_commits` | LKML / CVE 路命中 → 自动 surface 关联 commit（用 33K + 401 link 数据）|
| kernel.md prompt 加硬件/存储备选 | `agent/react/prompts/kernel.md` | 修 v2.1 backlog A 模式 |
| linker 数据回填 link_commit_message 33,099 行 | DB only | 0 orphan，全部 link_trailer 高置信 |
| eval 优化：case 级并发 + lore 共享限流 + `--case-ids` `--merge-summary` | `eval/runner_v2.py` + `ingest/lkml/fetcher.py` | wall time 75 min → 35.7 min（2×）|
| **关键 bug 修复：openai_compat catch-all 包装 → pass-through** | `llm/provider/openai_compat.py` | ERROR 33% → 6.7%；网络瞬态可重试 |

---

*参考：[PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [ProjectPlan.md](ProjectPlan.md) · [v2_acceptance_report.md](v2_acceptance_report.md)*

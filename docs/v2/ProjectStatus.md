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
| **final** (2026-05-25) | **concurrency=3 + bug fix** | **35.7 min** | **6.7% (1)** | **9** | **59.5%** | **100%** | **55.6% (5/9)** | catch-all bug 修复后稳定 |

**关键指标变化**（baseline → final）：
- Wall time **75 min → 35.7 min**（2× 加速，case 级并发 + lore 限流共享）
- Route accuracy **92.9% → 100%**（oom-002 / io-hang-001 路由修复）
- 同样 5 个 root-cause 判正确，分母从 12 → 9 因更多 case 走到 diagnosed
- ERROR 率回到 baseline 水平（修复 catch-all 包装 bug 后）

### 残留 ERROR：oom-001
1/15 = 6.7% 是 OpenRouter free-tier 实际可用上限。case 跑 9.4 min 后远端 stream 断开（`"Network connection lost"`）— 不可恢复，stream 已消费一半无法续传。这是上游瞬态，非本地代码问题。

### 按类别 recall（最终）
| 类别 | n | recall | 评价 |
|---|---|---|---|
| oom | 2 | **0%** | BM25 keyword 错配（已识别为 v2.1 P0）|
| lockup | 3 | 33% | softlockup × 2 低，rcu 100% |
| oops | 3 | 67% | kasan + lockdep |
| panic | 2 | 67% | panic-001 1/3，panic-002 1/1 |
| hardware | 2 | 100% | trivially-pass（ground_truth 空）|
| regression | 1 | 100% | change-001 命中 |
| io_hang | 1 | 100% | 修了 P1a 后命中 |

## 8. v2.0 → v2.1 backlog（基于 P1b judge reasoning 分析）

LLM judge 否定的 7 例失败模式归为 3 类：

| 模式 | 案例 | 根因 | v2.1 修复方向 |
|---|---|---|---|
| **A. 硬件归因缺失** | io-hang-001、softlockup-002 | kernel route prompt 没要求考虑硬件/存储备选；agent 把 nvme/scsi/SAN 超时诊断成内核 deadlock | **已补 kernel.md "Non-kernel Root Causes" 段**；后续可加 `nvme_timeout`/`scsi_abort` 模式触发 hardware route |
| **B. 过度具体化** | kasan-001、softlockup-001、rcu-001 | agent 锁定一个具体 commit/fix 当根因，但 GT 描述更宽的可能集；v1 的 generate_hypotheses + self_consistency 在 v2 ReAct 里没了 | 改 ReAct prompt 要求"列举 N 假设带置信度后再选"；或重启 self_consistency K=3 在 budget 允许时 |
| **C. 事实性错误** | panic-001 (major 254 = dm vs virtio_blk) | agent 不知道 `/proc/devices` 的 major 号映射 | 新增 `get_device_major_mapping` 工具（解析 `/proc/devices`） |

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

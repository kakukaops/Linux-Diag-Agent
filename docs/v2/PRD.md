# Linux-Diag-Agent v2 — 产品需求文档 (PRD)

| 字段 | 值 |
|------|---|
| 产品名 | Linux-Diag-Agent |
| 版本 | v2 |
| 状态 | Design Draft（2026-05-20，待评审）|
| 关联文档 | [Architecture.md](Architecture.md) · [ProjectPlan.md](ProjectPlan.md) · [adr/](adr/) · [../AgentThink.md](../AgentThink.md) · [../v1/PRD.md](../v1/PRD.md) |
| 设计基础 | [docs/AgentThink.md](../AgentThink.md)（L1/L2/L3 三层壁垒模型 + 8 类工具）|
| 最后更新 | 2026-05-20 |

---

## 1. 背景与目标

### 1.1 v1 已交付（基础)

v1 完成了从零到 MVP 的全部地基（详见 [v1/ProjectStatus.md](../v1/ProjectStatus.md)）：

- 6 个数据源全部入库：OLK kernel commit（130 万）、LKML（摄入中）、Bugzilla（3,700）、syzbot（999）、NVD（15,502）、CodeGraph（OLK-6.6 全量代码索引）
- 7 路并行 BM25 检索引擎 + Cross-Graph Linker 框架
- LangGraph 10 节点诊断 pipeline 可端到端跑通
- PG checkpointer 已接入，诊断中断可恢复

### 1.2 v1 的结构性局限（本会话实测发现）

这些不是 bug，是 v1 设计阶段就需要在 v2 解决的问题：

| 局限 | 实测证据 |
|------|---------|
| LLM 没有工具调用能力，是**固定流水线**而非真正的 agent | `llm/provider/base.py` 定义了 `ToolCall`/`ToolSchema` 但**从未传给 LLM** |
| 检索召回率低 | 合成案例 Recall@10 仅 **6.7%** —— OR 查询命中 74,940 条 commit，目标 commit 排在 top-30 之外 |
| `link_commit_bug = 0` | OLK 6.3 万条 commit 引用 `gitee.com/openeuler/kernel/issues/`，但 `bug` 表只装 kernel.org bugzilla |
| 没有内核崩溃后取证能力 | 无 vmcore/drgn 集成；hardlockup/panic 的诊断脊梁完全缺失 |
| 没有"什么变了"维度 | rpm history / boot history / config drift 全无工具 |
| 硬件层完全缺失 → 系统性误诊 | `hardlockup → lockup SOP → 搜 kernel commit`，但 hardlockup 更可能是坏 DIMM/MCE |
| 没有可观测系统集成 | 无 Prometheus/ES/Huatuo adapter；慢泄漏型 OOM 的时间序列证据无法访问 |
| 没有"无法诊断"出口 | 流水线永远产报告；证据薄弱时硬给一份自信结论的危害大于说"我不知道" |
| 没有 eval 反馈环 | 不知道某次诊断对不对，无法迭代图谱与策略 |

### 1.3 v2 解决什么

基于 [AgentThink.md](../AgentThink.md) 的 **L1/L2/L3 三层壁垒模型**：

```
L1 商品工具   ← 买 / 集成     （vmcore/drgn、监控、硬件、变更采集）
L2 知识图谱   ← 自建·数据壁垒（commit/LKML/bug/CVE + 查询 API）
L3 诊断推理   ← 自建·逻辑壁垒（ReAct 调查策略、SOP、领域启发式、eval）
```

v2 的目标是：
1. **L3:把 pipeline 升级为 hybrid agent**——调查阶段引入 ReAct 工具循环，报告阶段保持确定性骨架
2. **L2:补齐知识图谱的高价值查询接口**——`check_backport_status` / `get_regression_fixes` / `get_commit_diff` + gitee/atomgit issue 数据
3. **L1:接入业界已有工具,不重造**——drgn、mcelog、Prometheus 等都是封装/集成,不自研

### 1.4 产品定位变化（相对 v1）

| 维度 | v1 | v2 |
|------|----|----|
| Agent 形态 | 固定 pipeline，LLM 仅文本生成 | Hybrid：Triage 确定性 + Investigation ReAct + Report 确定性 |
| 数据来源 | 6 个知识库 + dmesg/sosreport | + vmcore + 监控历史 + 变更时间线 + 硬件错误 |
| 诊断输出 | 总是产报告 | 报告 / "无法诊断，请收集 X" / "硬件问题，转硬件路径" 三种出口 |
| 目标用户扩展 | 内核开发者为主 | + 系统级 SRE（监控/变更/硬件接入后真正可用）|

---

## 2. 典型场景（新增 User Stories）

v1 的 US-1 ~ US-7 仍然有效（见 [v1/PRD.md §2](../v1/PRD.md)）。v2 在此基础上新增：

### US-8（P0）— 内核 panic vmcore 取证
**Actor**：内核维护者
**输入**：`/var/crash/2026-05-19-15:00/vmcore` 路径 + 对应 vmlinux
**期望输出**：所有 CPU 栈、谁持锁、最近改过该函数的 commit、对应 LKML 讨论。
**关键能力**：类别 F（drgn / decode_stacktrace / taint）。

### US-9（P0）— hardlockup 自动转向硬件诊断
**Actor**：SRE
**输入**：dmesg 含 `NMI watchdog: Watchdog detected hard LOCKUP` + `Hardware Error` 或 taint=M
**期望输出**：**不再去搜内核 commit**——而是查 mcelog / EDAC / IPMI SEL，输出"疑似硬件故障：DIMM A2，建议 RAS 检查"。
**关键能力**：类别 H + SOP 路由修正（[ADR-023](adr/ADR-023-hardware-first-sop-routing.md)）。

### US-10（P0）— "升级内核后系统不稳定" 变更关联
**Actor**：SRE
**输入**：自然语言 + 故障时间点（"昨天下午 3 点开始 dmesg 有 OOM"）
**期望输出**：rpm 升级时间线、boot 时间、cmdline 漂移，与故障时间对齐，明确指出"内核 3 小时前从 6.6.0 升级到 6.6.2"。
**关键能力**：类别 G（变更关联）。

### US-11（P1）— 慢泄漏型 OOM 的时间趋势
**Actor**：SRE
**输入**：节点 ID + OOM 事件时间
**期望输出**：故障前 2 小时的 cgroup 内存曲线（来自 Prometheus），定位泄漏起点，关联近期重启/部署。
**关键能力**：类别 C（监控适配器）。

### US-12（P1）— 容器级 OOM 归因
**Actor**：SRE / 应用开发
**输入**：被 OOM 杀掉的 PID
**期望输出**：PID → container → pod → deployment → 负责人；该 deployment 的 cgroup 限制设置和最近变更。
**关键能力**：PID→container 映射（[ADR-023 配套](adr/ADR-023-hardware-first-sop-routing.md)）。

### US-13（P1）— 证据不足时主动要求收集
**Actor**：内核维护者
**输入**：只有一段截断的 oops trace，无 vmcore，无 sosreport
**期望输出**：**不强行下结论**，而是返回"证据不足：本次故障无法定位根因。请按以下步骤收集证据：1) 启用 kdump 配置 2) 重现后采集完整 dmesg ..."
**关键能力**：[ADR-024](adr/ADR-024-insufficient-evidence-exit.md) 不确定性出口。

---

## 3. 功能需求

### 3.1 P0（v2.0 必交付）

| ID | 功能 | 来源 |
|----|------|------|
| F-1 | **ReAct 调查循环**：把 `generate_hypotheses` + `verify_hypothesis` + `self_consistency` 三节点合并为 ReAct 子图，LLM 自主选择并调用工具 | AgentThink §三 |
| F-2 | **检索类工具封装为 LLM tool schema**（类别 A 全部 + 类别 B 解析器） | AgentThink P0 |
| F-3 | **`retrieve` 节点启用 LLM query parsing**（当前硬编码 `use_llm=False`） | 本会话 #2 |
| F-4 | **SOP 路由修正**：hardlockup/panic 先检查 taint flags 和 `Hardware Error` 标记 | AgentThink §四 + [ADR-023](adr/ADR-023-hardware-first-sop-routing.md) |
| F-5 | **"无法诊断"出口**：bind_claims 评估总体证据强度，不足时走 insufficient_evidence 路径 | [ADR-024](adr/ADR-024-insufficient-evidence-exit.md) |
| F-6 | **L2 高价值查询工具**：`check_backport_status` / `get_regression_fixes` / `get_commit_diff` | AgentThink 类别 D |
| F-7 | **类别 F 崩溃转储分析**：`check_kdump_available` / `fetch_debuginfo` / `analyze_vmcore`（drgn）/ `decode_stacktrace` / `parse_taint_flags` | AgentThink P0/P1，[ADR-020](adr/ADR-020-drgn-over-crash.md) |
| F-8 | **类别 H 硬件层**：`get_mce_log` / `get_edac_errors` / `get_ipmi_sel` / `get_hardware_inventory` | AgentThink P1 |

### 3.2 P1（v2.1 交付）

| ID | 功能 | 来源 |
|----|------|------|
| F-9 | **类别 G 变更关联**：`get_package_history` / `get_boot_history` / `get_kernel_cmdline_diff` / `get_config_drift` / `correlate_fault_with_changes` | AgentThink P1 |
| F-10 | **类别 C 监控集成**：`ObservabilityAdapter` 接口（统一 PromQL/ES DSL 输入语法，[ADR-021](adr/ADR-021-observability-adapter.md)）+ Prometheus + Elasticsearch 两个 backend；Traces v2 不做 | AgentThink P2 |
| F-11 | **类别 B 补全**：`parse_journal`、带过滤的 `read_live_dmesg`、独立 `extract_call_trace` | AgentThink P1 |
| F-12 | **gitee/atomgit issue ingester**：补齐 `link_commit_bug` 缺口 | [ADR-022](adr/ADR-022-gitee-atomgit-issue-ingest.md) |
| F-12b | **LKML 三层知识数据架构**（[ADR-025](adr/ADR-025-knowledge-data-online-default.md)）：L1 定向抓取 commit 引用的 ~26K message-id 补 `link_commit_message` + L2 懒加载智能缓存 + L3 lore live search 检索路由；取代 v1 的 bulk 摄入 | [supplements §2bis](V2_v1_modules_supplements.md)（2026-05-21 实测发现）|
| F-13 | **PID → container 映射**：cgroup v2 路径解析、podman/docker/k8s 标签查找 | US-12 |
| F-14 | **`weekly_sync.sh` 加 Linker 自动重跑** | 本会话 #21 |
| F-15 | **BM25 同义词/复合词处理**：softlockup ↔ soft_lockup ↔ lockup 别名展开 | 本会话 #13 |

### 3.3 P2（v2.2 交付）

> **注**：v2.2 仅给出方向性条目，详细规划在 v2.1 验收后基于实际数据 / Recall 曲线 / 用户反馈再细化（[ProjectPlan §1.3](ProjectPlan.md) 同步标注）。

| ID | 功能 | 来源 |
|----|------|------|
| F-16 | **类别 C 扩展**：Huatuo / Datadog adapter（与 v2.1 同一份 PromQL/ES 接口语法，adapter 内翻译，[ADR-021](adr/ADR-021-observability-adapter.md)）| AgentThink §C |
| F-17 | **eval 反馈环**：自动质量度量（Recall@10 周报）+ 真实案例数据集（≥30 例） | AgentThink §5.5 |
| F-18 | **诊断结果的诊断**（meta-eval）：把"agent 输出对不对"做成定期 review | 本会话 #6 |
| F-19 | **ReAct 工具调用轨迹的审计 UI**（CLI 或简易 web）| 工程改进 |
| F-20 | **Traces 工具集**（仅 outlook，不在 v2 范围）| 待 OpenTelemetry / Jaeger 生态在 OLK 部署成熟后评估 |

---

## 4. 非功能需求

### 4.1 性能

| 指标 | 目标 |
|------|------|
| Triage P95 | ≤ 2s（同 v1）|
| Investigation 阶段 P95（ReAct 循环）| ≤ 120s（带 budget guard）|
| Report P95 | ≤ 30s |
| 单次诊断 token 上限 | 50K tokens（chat + navigator 合计） |
| ReAct 最大迭代数 | 15（[ADR-019](adr/ADR-019-hybrid-react-deterministic.md)）|

### 4.2 确定性约束

- **报告阶段**（`bind_claims` / `generate_report`）必须确定性：相同输入产相同 RCA
- **调查阶段** ReAct 循环允许多样性，但工具调用轨迹必须完整记录（可审计）

### 4.3 隐私 & 安全

- 同 v1：完全离线可部署，本地 vLLM 路径保留
- 新增：vmcore / sosreport **不上传**外部 LLM；含 vmcore 的诊断默认走本地 vLLM
- drgn 全程**只读**操作 vmcore，不修改任何 live system
- 监控查询走 service account，read-only token

### 4.4 可解释性（硬约束，继承 v1）

每条声明必须有 `evidence_refs`（commit hash / bug_id / message_id / cve_id / vmcore_frame）。v2 新增：

- ReAct 工具调用轨迹保留（每步 tool name + args + result summary）
- 报告里 explicit 标注 "工具调用次数" 和 "信息源覆盖度"
- 不确定性出口 包含"缺什么证据"清单

### 4.5 可扩展性

- 新工具接入：实现 LLM tool schema 即可注册到 ReAct 工具集
- 新监控 backend：实现 `ObservabilityAdapter` 协议即可（[ADR-021](adr/ADR-021-observability-adapter.md)）
- 新 SOP：YAML + 路由规则注册

### 4.6 可运维性

- v2 新增依赖（drgn、mcelog 等）通过容器化 + apt/dnf 标准包提供
- 监控 adapter 配置在 `configs/observability.yaml`（gitignored）

### 4.7 知识数据部署模式（[ADR-025](adr/ADR-025-knowledge-data-online-default.md)）

v2 修正 v1 的 "offline-first 强制" 原则为 **"online-default, offline-capable"**：

- **online 模式（默认）**：知识数据（尤其 LKML）在线为主——定向抓取 + 懒加载缓存 + lore live search。构建时间从"几天 bulk"降到"几小时 + 缓存靠用收敛"。
- **offline 模式（气隙部署）**：出厂预热缓存打包交付；功能不缺，仅失去"在线发现新讨论"的增量召回。
- 由 `configs/` 的 `knowledge.mode` 切换。
- **LLM 本地化（vLLM）与本条正交**——LLM 是否走本地由数据隐私决定（vmcore/sosreport 不发外部 LLM），不受此条影响。
- ADR-001（无 embedding / BM25）不受影响。

---

## 5. 范围与不范围

### 5.1 在范围（v2）

- 所有 P0/P1/P2 功能（§3）
- v1 已有功能的兼容（向后兼容 CLI、报告格式扩展不破坏旧字段）
- v2 新增工具的离线/在线两种部署模式

### 5.2 不在范围（v2）

明确划线，避免 scope creep：

- **自研 Linux 诊断工具**：不重写 `crash`/`drgn`/`mcelog`/Prometheus；只做封装与集成（[AgentThink §5.1](../AgentThink.md)）
- **写操作 / 自动修复**：v2 仍是只读诊断，不做 `sysctl` 修改、cgroup 调整、补丁应用
- **Live system 大规模主动探测**：不并发跑 `bpftrace`/`perf` 等可能加压病机的工具（[AgentThink 设计原则 5](../AgentThink.md)）
- **Web UI / IDE 集成**：CLI 为主，v3+ 考虑
- **多租户 / SaaS 形态**：本地部署 / 单组织内部为主
- **跨发行版**：仅 OLK-6.6 / OLK-5.10；mainline 仅作为参考语料

### 5.3 关键外部依赖

| 依赖 | 用途 | 责任方 |
|------|------|--------|
| `drgn` Python 库 | vmcore 编程化分析（[ADR-020](adr/ADR-020-drgn-over-crash.md)）| 本项目封装 |
| `mcelog` / `ras-mc-ctl` / `ipmitool` | 硬件错误读取 | 系统已装/集成 |
| Prometheus / Elasticsearch / Huatuo | 监控数据源 | 由部署环境提供 |
| OLK `kernel-debuginfo` | 符号解析 | OLK 仓库 |
| gitee / atomgit API | issue 数据 | 网络访问，[ADR-022](adr/ADR-022-gitee-atomgit-issue-ingest.md) |

---

## 6. 验收准则

### 6.1 v2.0（P0 全交付）

| 指标 | 目标 |
|------|------|
| ReAct 调查循环可端到端跑通 | 5 例真实 incident 通过工具调用得出 RCA |
| 真实案例 Recall@10 | ≥ 60%（从 v1 的 6.7%/合成 提升）|
| vmcore 案例端到端 | 5 例：从 vmcore 输入到结构化报告（含持锁 CPU 栈）|
| hardware 误诊修复 | dmesg 含 `Hardware Error` 的案例 100% 不再去搜 kernel commit |
| "无法诊断"出口 | 数据集中应有 ≥ 3 例命中（应 say IDK 的案例至少 3 例正确走出口），且**召回率 ≥ 80%、精确率 ≥ 80%**（避免漏报和过激）|
| `check_backport_status` | 给定 mainline SHA + OLK 版本，返回 backport 状态 准确率 100% |

### 6.2 v2.1（P1 全交付）

| 指标 | 目标 |
|------|------|
| 真实案例 Recall@10 | ≥ 70% |
| 变更关联命中率 | 升级回归类案例中 ≥ 60% 命中"什么变了" |
| 至少 1 个监控 backend 接入 | Prometheus adapter 跑通 5 例 慢泄漏 OOM 趋势分析 |
| `link_commit_bug` | gitee issue 入库后，从 0 提升到 ≥ 10,000 条关联 |
| PID→container 映射 | OOM 案例中 100% 能给出 deployment 归属（如果在 cgroup v2）|

### 6.3 v2.2（P2 全交付）

| 指标 | 目标 |
|------|------|
| 真实案例 Recall@10 | ≥ 80% |
| eval 反馈环 | 自动质量曲线上线，周报输出 |
| 真实案例数据集 | ≥ 30 例，至少 4 类故障覆盖（OOM / lockup / panic / hw error）|
| 多 backend 监控 | Huatuo 或 Datadog 中至少一个接入 |

### 6.4 评测方法

继承 v1（30 例人工 + 2 裁判 + tiebreak），v2 增加：

- 自动 Recall@10 跑批（CI 每周）
- ReAct 工具调用轨迹的 trace-level review
- "无法诊断"出口的 召回准确率（不该 say I don't know 的有没有说，该 say 的有没有说）

---

## 7. 关键术语（v2 新增）

| 术语 | 含义 |
|------|------|
| **L1 / L2 / L3** | 三层壁垒模型：商品工具 / 知识图谱·数据壁垒 / 诊断推理·逻辑壁垒（[AgentThink §5.3](../AgentThink.md)）|
| **Hybrid Agent** | Triage（确定性）+ Investigation（ReAct）+ Report（确定性）的混合模式（[ADR-019](adr/ADR-019-hybrid-react-deterministic.md)）|
| **ReAct** | Reasoning + Acting 循环：LLM 观察 → 推理 → 选择工具 → 工具执行 → 观察 → 重复 |
| **drgn** | Meta 开源的可编程内核调试器（[ADR-020](adr/ADR-020-drgn-over-crash.md)）|
| **vmcore** | 内核 panic 时 kdump 生成的完整内存转储 |
| **taint flags** | `/proc/sys/kernel/tainted`：内核污染位（加载闭源模块、之前 warning、等），收窄故障搜索范围 |
| **MCE** | Machine Check Exception，CPU/内存硬件错误的内核记录 |
| **EDAC** | Error Detection And Correction，内存 ECC 错误监控子系统 |
| **IPMI SEL** | IPMI System Event Log，BMC 记录的硬件事件 |
| **PSI** | Pressure Stall Information，内核 CPU/内存/IO 压力指标 |
| **ObservabilityAdapter** | 统一监控 backend 接口（Prometheus/ES/Huatuo/Datadog 同接口）|
| **Insufficient-Evidence Exit** | 证据不足时的明确输出路径，不强行下结论 |

---

*参考：[Architecture.md](Architecture.md) · [ProjectPlan.md](ProjectPlan.md) · [../AgentThink.md](../AgentThink.md) · [../v1/PRD.md](../v1/PRD.md)*

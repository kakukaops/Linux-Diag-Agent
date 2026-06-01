# Linux-Diag-Agent v2 — 项目计划

| 字段 | 值 |
|------|---|
| 文档类型 | Project Plan |
| 版本 | v2 |
| 状态 | Design Draft（2026-05-20，待评审）|
| 关联文档 | [PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [adr/](adr/) · [../v1/ProjectPlan.md](../v1/ProjectPlan.md) |
| 起始 | M13 = v1 收尾后第 1 个月（M13 = 项目立项后第 13 个月）|
| 总周期 | 9 个月（M13 → M21） |
| 推荐团队 | 双人 dev + 0.5 SRE 顾问 + 1 评测人员（周度） |
| 最后更新 | 2026-05-20 |

---

## 1. 里程碑分解

### 1.1 v2.0 — Hybrid Agent + 知识图谱查询 + 崩溃取证（M13-M15）

| 里程碑 | 月份 | 关键交付 |
|--------|------|---------|
| **M13**：Triage 重构 + ReAct 框架 + L2 查询工具 + **数据集采集启动** | M13 | `classify_fault_and_route`（含 taint 检测）；基于 LangGraph prebuilt 扩展 ReAct Loop（spike 验证后决定 prebuilt vs 自研，详见 [ADR-019](adr/ADR-019-hybrid-react-deterministic.md)）；`check_backport_status` / `get_regression_fixes` / `get_commit_diff` 上线；retrieve 启用 LLM query parse；7 路检索封装为 LLM tool schema；"无法诊断"出口；**真实 incident 数据集采集启动（与开发并行）**|
| **M14**：类别 F 崩溃取证 + 类别 H 硬件层 + **数据集持续累积** | M14 | drgn 集成（含 OLK 兼容 spike）；`analyze_vmcore` / `decode_stacktrace` / `parse_taint_flags`；fetch_debuginfo 自动化；mcelog / EDAC / IPMI SEL / dmidecode 封装；hardware SOP yaml；数据集累积到 ≥10 例 |
| **M15**：数据集就绪 + 真实案例评测 + v2.0 验收 | M15 | 真实 incident 数据集 ≥15 例完成 + ground truth 标注；ReAct 工具调用轨迹审计；基于数据集做 Recall@10 调优（≥ 2 周迭代）；v2.0 acceptance pass |

**v2.0 验收准则**（详见 [PRD §6.1](PRD.md)）：
- 真实案例 Recall@10 ≥ 60%
- ReAct 调查循环端到端跑通,5 例真实 incident 产报告
- vmcore 案例 5 例端到端（含持锁 CPU 栈）
- dmesg 含 `Hardware Error` 的案例 100% 不再去搜 kernel commit
- 至少 1 例证据不足案例返回"请收集 X"而非强行下结论

---

### 1.2 v2.1 — 变更关联 + 监控集成 + 数据完整性（M16-M18）

| 里程碑 | 月份 | 关键交付 |
|--------|------|---------|
| **M16**：类别 G 变更关联 + 类别 B 补全 | M16 | `get_package_history` / `get_boot_history` / `get_kernel_cmdline_diff` / `get_config_drift` / `correlate_fault_with_changes`；`parse_journal`、`read_live_dmesg` 过滤；独立 `extract_call_trace` |
| **M17**：类别 C 监控适配器（Prometheus + ES）| M17 | `ObservabilityAdapter` 协议；Prometheus / Elasticsearch 两个 backend；`query_metric` / `get_memory_trend` / `search_logs` 工具上线 |
| **M18**：gitee/atomgit issue ingester + PID→container + Linker 自动化 | M18 | 新 ingester 入库 ~10K+ gitee issues；`link_commit_bug` 从 0 提升到数千；PID→cgroup→container→deployment 映射；`weekly_sync.sh` 集成 Linker 自动重跑；BM25 同义词表 |

**v2.1 验收准则**（详见 [PRD §6.2](PRD.md)）：
- 真实案例 Recall@10 ≥ 70%
- 变更关联在升级回归类案例中 ≥ 60% 命中
- Prometheus adapter 跑通 5 例慢泄漏 OOM 趋势分析
- `link_commit_bug` ≥ 10,000 条
- OOM 案例 100% 能给出 deployment 归属

---

### 1.3 v2.2 — 适配器扩展 + eval 反馈环 + 长尾打磨（M19-M21）

| 里程碑 | 月份 | 关键交付 |
|--------|------|---------|
| **M19**：Huatuo + Datadog adapter | M19 | Huatuo（openEuler eBPF）/Datadog API 实现；与已有 adapter 接口兼容；至少一个生产化跑通 |
| **M20**：eval 反馈环 + 真实案例数据集（≥30 例）| M20 | 自动 Recall@10 跑批 CI；每周质量曲线；meta-eval（诊断结果对不对的 review） |
| **M21**：audit UI + 长尾优化 + v2.2 验收 | M21 | 工具调用轨迹的 CLI / 简易 web 审计界面；性能调优；v2 完整验收 |

**v2.2 验收准则**（详见 [PRD §6.3](PRD.md)）：
- 真实案例 Recall@10 ≥ 80%
- eval 反馈环上线，周报输出
- 真实数据集 ≥ 30 例覆盖 4 类故障
- Huatuo 或 Datadog 中至少一个 backend 跑通

---

## 2. 任务 WBS + 工时估算

### 2.1 估算口径

- 工时单位：**PD（人日）**，按 8 工时 / 天
- 包含开发 + 单测 + 集成测试 + 代码评审 + 文档
- 不包含 v2 设计阶段（已通过本 PRD / Architecture / ADR 完成）

### 2.2 v2.0（M13-M15）任务清单

| ID | 任务 | 模块 | PD | 备注 |
|----|------|------|----|------|
| T-001 | `detect_taint_and_hw_signals` 节点实现 | M7 triage | 1 | dmesg + taint flag 正则解析 |
| T-002 | `classify_fault_and_route` 重构（含路由表）| M7 triage | 2 | 替换 `_EVENT_KIND_TO_SOP` |
| T-003 | ReAct Loop Engine（**优先基于 LangGraph `create_react_agent` prebuilt 扩展**;若 spike 阻塞再退回自研）| **新 M22** | 10 | iteration/budget/checkpoint/tool dispatch/工具失败处理（[ADR-019](adr/ADR-019-hybrid-react-deterministic.md)）;含 1 周 prebuilt spike |
| T-004 | 7 路检索 + dmesg/sosreport 工具封装为 LLM tool schema | M5 + M6 | 4 | 每个工具一个 JSON schema |
| T-005 | 工具集按路由动态裁剪逻辑 | M13 | 2 | route_to_tools 映射 |
| T-006 | retrieve 节点启用 LLM query parsing | M5 | 1 | 改 `use_llm=False` → `True` + budget guard |
| T-007 | `check_backport_status` 工具实现 | M4 | 2 | SQL 反查 + LLM schema |
| T-008 | `get_regression_fixes` 工具实现 | M4 | 2 | Fixes: 反向索引 |
| T-009 | `get_commit_diff` 工具实现 | M4 | 1 | `git show` wrapper |
| T-010 | "无法诊断"出口（bind_claims + report 分支）| M7 | 3 | 含 `insufficient_evidence_actions` 生成 |
| T-011 | 报告 JSON schema 扩展（tool_call_trace 等）| M7 report | 2 | 含 audit_trail 落库 |
| T-012 | `agent_tool_trace` PG 表 + 迁移 | M2 storage | 1 | Alembic 迁移 |
| T-013 | drgn 集成（vmlinux 加载、bt -a、kmem -i、OLK 兼容 spike 1 周）| **新 M9** | 13 | 含 fallback 到 crash 的可选包装、容器化封装 |
| T-014 | `fetch_debuginfo` 实现（OLK 仓库 + sosreport 包内）| M9 | 3 | |
| T-015 | `decode_stacktrace` 工具 | M9 | 2 | 封装 `decode_stacktrace.sh` |
| T-016 | `parse_taint_flags` 工具 | M9 | 1 | 位映射表 |
| T-017 | `analyze_vmcore` 工具（含 bt -a / 持锁分析）| M9 | 5 | drgn Python 脚本库 |
| T-018 | M9 容器化封装（vmlinux 路径管理）| M9 | 2 | Docker / chroot |
| T-019 | mcelog / EDAC / IPMI / dmidecode 封装 | **新 M11** | 5 | 4 个工具，每个 1-2 PD |
| T-020 | hardware SOP yaml + 路由集成 | M7 + M11 | 1 | |
| T-021 | 真实案例数据集 v2.0（≥15 例，**M13 启动,M15 完成**）| eval | 10 | 数据采集 + ground truth 标注（关联到真实 commit/bug）;含 hw 误诊案例 ≥ 3 例 + 反例 ≥ 2 例（[ADR-023](adr/ADR-023-hardware-first-sop-routing.md)）|
| T-022 | 真实案例 ReAct 跑批 + 调优 | eval + M13 | 5 | 跑通 5 例 vmcore + 5 例 hw + 5 例 kernel |
| T-023 | M9/M11 集成测试 | testing | 3 | |
| T-024 | v2.0 验收 + 报告 | PM | 2 | |
| T-025 | ReAct prompt 模板设计（按路由的 system prompt + tool description 调优）| M13 | 3 | 直接影响工具调用质量 |
| T-026 | Token cost 模型 + budget 校准（基于早期跑批数据校验 50K 上限是否合理）| M13 | 1 | |
| T-027 | v1→v2 兼容性测试套件 + 灰度上线策略（v2 与 v1 并行跑同样 case 对比报告）| testing + PM | 3 | CLI 向后兼容 + JSON schema 字段兼容 |
| **v2.0 合计** | | | **90** | ≈ 3 个月 × 2 dev（含 spike + 数据集前移）|

### 2.3 v2.1（M16-M18）任务清单

| ID | 任务 | 模块 | PD | 备注 |
|----|------|------|----|------|
| T-101 | `get_package_history`（rpm -q --last / dnf history）| **新 M10** | 2 | |
| T-102 | `get_boot_history`（journalctl --list-boots）| M10 | 1 | |
| T-103 | `get_kernel_cmdline_diff` + 基线管理 | M10 | 2 | 需要基线 DB 表 |
| T-104 | `get_config_drift`（sysctl + config 文件）| M10 | 3 | |
| T-105 | `correlate_fault_with_changes`（时间对齐算法）| M10 | 2 | |
| T-106 | M10 集成测试 | testing | 2 | |
| T-107 | `parse_journal` 工具 | M6 | 2 | journalctl 封装 |
| T-108 | `read_live_dmesg` 加过滤参数 | M6 | 1 | |
| T-109 | `extract_call_trace` 独立工具 | M6 | 2 | 已有解析器，包成 tool |
| T-110 | `ObservabilityAdapter` Protocol + 注册机制 | **新 M12** | 3 | |
| T-111 | PrometheusAdapter | M12 | 4 | PromQL 封装 |
| T-112 | ElasticsearchAdapter | M12 | 4 | ES DSL + Loki 兼容 |
| T-113 | `query_metric` / `get_memory_trend` / `get_oom_events` / `search_logs` 工具 | M12 | 4 | 通用查询 + 语义化包装 |
| T-114 | `configs/observability.yaml` schema + loader | configs | 1 | |
| T-115 | M12 集成测试（含 mock backend）| testing | 3 | |
| T-116 | gitee/atomgit issue API client | **新 ingest/gitee_issue** | 4 | REST API + 限速 |
| T-117 | gitee issue ingester（同 BaseIngester 模式）| ingest/gitee_issue | 3 | |
| T-118 | bug 表加 source='gitee'/'atomgit' 数据 | M2 | 1 | 迁移 |
| T-119 | Cross-Graph Linker 扩展（gitee/atomgit URL 模式）| M4 graph | 2 | |
| T-120 | PID → container 映射工具 | M6 | 2 | cgroup v2 + podman/docker label |
| T-121 | `weekly_sync.sh` 集成 Linker 自动重跑 | scripts | 1 | |
| T-122 | BM25 同义词/复合词别名表 + tsquery 展开 | M5 | 3 | |
| T-125 | LKML 定向 message-id 提取 SQL + 去重（ADR-025 L1）| ingest/lkml | 0.5 | 见 [supplements §2bis](V2_v1_modules_supplements.md) |
| T-126 | `python -m ingest.lkml backfill_referenced` 子命令（targeted `/all/<msgid>/raw` 抓取）| ingest/lkml | 2 | 补 `link_commit_message`（实测 0 的真正解法）|
| T-127 | 定向抓取 checkpoint（已抓 message-id 集合）+ 增量逻辑 | ingest/lkml | 0.5 | |
| T-128 | lore search 客户端（`retrieval/recall/lore_search.py`，ADR-025 L3）| retrieval | 3 | 复用 fetcher.py 的 HTTP + Atom 解析 |
| T-129 | LKML 懒加载 + 缓存回填（`ingest/lkml/lazy.py`，ADR-025 L2）| ingest/lkml | 3 | read-through cache,复用 parser/thread_builder |
| T-130 | `recall/lkml.py` 混合化（本地缓存 BM25 + lore search 合并 + 回填）| retrieval | 3 | |
| T-131 | weekly_sync LKML 改造 + summarizer 懒化 + `knowledge.mode` config + lkml_message 缓存列迁移 | ingest/scripts/configs | 3 | |
| T-123 | v2.1 真实案例评测（30 例覆盖范围扩展）| eval | 3 | |
| T-124 | v2.1 验收 + 报告 | PM | 2 | |
| **v2.1 合计** | | | **72** | ≈ 3 个月 × 2 dev（含 ADR-025 LKML 三层化）|

### 2.4 v2.2（M19-M21）任务清单

| ID | 任务 | 模块 | PD | 备注 |
|----|------|------|----|------|
| T-201 | HuatuoAdapter 实现 | M12 | 5 | 需要 Huatuo API 文档对齐 |
| T-202 | DatadogAdapter 实现 | M12 | 4 | |
| T-203 | 多 backend 路由策略（adapter selection by config） | M12 | 2 | |
| T-204 | eval 反馈环：自动 Recall@10 跑批 + CI | **新 M23 eval** | 4 | |
| T-205 | 自动质量曲线 dashboard | M23 | 3 | grafana / 简易 web |
| T-206 | meta-eval 框架（裁判 review 工具） | M23 | 4 | |
| T-207 | 真实案例数据集扩展到 30 例 | eval | 5 | |
| T-208 | 类别 E 假设验证工具（P4）| 新 mcp | 3 | `check_kernel_version_has_fix` 等 |
| T-209 | 工具调用轨迹 audit UI（CLI + simple web）| M23 | 5 | |
| T-210 | 性能调优（reranker、ReAct 路径压缩）| 全模块 | 4 | |
| T-211 | v2.2 完整验收 + 报告 | PM | 3 | |
| T-212 | v2 收尾文档 / postmortem | PM | 2 | |
| **v2.2 合计** | | | **44** | ≈ 2 个月 × 2 dev |

### 2.5 全 v2 总览

| 阶段 | PD | 月份（双人 dev） |
|------|----|-----------------|
| v2.0 | 90 | 3（M13-M15）|
| v2.1 | 72 | 3（M16-M18）|
| v2.2 | 44 | 3（M19-M21，detail TBD）|
| **总计** | **206** | **9 个月** |

> 单人 dev 折算：约 17-19 个月（建议双人）。
> v2.2 PD 估算为占位,详细规划在 v2.1 验收后基于实际数据再细化（[PRD §3.3](PRD.md)）。
> v2.1 含 T-125~T-131（LKML 三层架构改造，[ADR-025](adr/ADR-025-knowledge-data-online-default.md)；2026-05-21 实测 `link_commit_message=0` 后确定）。

---

## 3. 依赖关系图

```
┌────── v1 收尾 ──────┐
│  LKML 摄入完成      │
│  Linker 重跑        │
│  Neo4j rebuild      │
└──────────┬──────────┘
           ↓
┌──── M13 ReAct 框架 + L2 查询工具 ────┐
│  T-003 ReAct Loop ← 全 v2 关键路径   │
│  T-007/008/009 L2 查询 ← 数据已在    │
│  T-006 LLM query parse ← 单点改      │
└──────────┬──────────────────────────┘
           ↓
┌──── M14 类别 F + H ──────────────────┐
│  T-013/017 drgn ← 需要 M13 工具框架  │
│  T-019 hardware ← 独立                │
└──────────┬──────────────────────────┘
           ↓
┌──── M15 v2.0 验收 ───────────────────┐
│  T-021 数据集 ← gating               │
└──────────┬──────────────────────────┘
           ↓
       ┌───┴────┐
       ↓        ↓
┌── M16 ──┐ ┌── M17 ──┐ ┌── M18 ──┐
│ G 变更  │ │ C 监控  │ │ gitee   │
│ B 补全  │ │ adapter │ │ issue   │
└────┬────┘ └────┬────┘ └────┬────┘
     └─────────┬─┴───────────┘
               ↓
        M18 末 v2.1 验收
               ↓
┌──── M19-M21 v2.2 ────────────────────┐
│  T-201 Huatuo/Datadog ← 依赖 M17      │
│  T-204 eval feedback ← 依赖 v2.0 通过 │
│  T-207 数据集 30 例 ← 持续推进        │
└─────────────────────────────────────┘
```

**关键路径**：v1 收尾 → M13 ReAct → M14 vmcore → M15 验收。一旦 M13 延期，整个 v2.0 后移。

**可并行**：M16（变更关联）与 M17（监控 adapter）相互独立，可并行推进。**M18 不完全独立**：内部 T-117（gitee ingester）→ T-119（Linker 扩展）是顺序依赖,且 T-122（同义词表）的效果验证依赖 M17 的真实 metrics 数据落库;M18 排在 M16/M17 之后或末段并行。

**资源约束**：双 dev 同时各负责一个 milestone 时，SRE 顾问（0.5 PD/周）和评测人员（1 PD/周）是共享资源，可能成为新瓶颈;计划上 M16/M17 评审时段错开。

---

## 4. 风险与缓解

| 风险 | 严重度 | 缓解 |
|------|-------|------|
| ReAct 循环 token 消耗不可控 | 高 | `MAX_ITER=15` + `TOKEN_BUDGET=50K` 硬封顶；budget guard 紧时降级到只用 L2 查询；checkpoint 每步落库 |
| drgn 依赖 vmlinux + debuginfo 版本完全匹配 | 高 | `fetch_debuginfo` 自动重试 + sosreport 包内查找；不匹配时降级到 `decode_stacktrace` 文本分析 |
| OLK kernel-debuginfo 缺失或滞后 | 中 | 加 OLK 仓库镜像列表；和 OLK 维护者协调；缺失时 audit_trail 标注 |
| 监控 backend 数据语义差异（PromQL vs ES DSL）| 中 | adapter 内做映射；不提供"通用查询"，只提供 typed 工具；每 backend 独立单测 |
| gitee/atomgit issue API 限速 / 封 IP | 中 | 1.5s/req 延迟（同 lkml）；按时间窗增量同步；缓存 issue body |
| 真实案例数据集采集慢 | 高 | 与 OLK 用户社区合作；从 syzbot 已 fix 的崩溃中提取；不阻塞 M14 开发 |
| hardlockup SOP 修正引入回归（错把 kernel bug 路由到 hardware）| 中 | eval 数据集里加 hw 误诊案例 + 反例（hw 路径不应触发的 case）；A/B 对比 |
| v1 LKML 摄入未完成会阻塞 Linker 重跑 | 低（已在跑）| 当前 83%，~5h 内完成；如果失败，v1 修复机制（gemtokenized fetcher 已上线） |
| ReAct 不收敛（循环工具调用相同 pattern）| 中 | 工具调用轨迹去重检测；连续 3 次同 tool+args 视为 stuck → 强制 final_answer |
| Investigation 阶段 LLM 选错工具，导致路径过长 | 中 | 按路由裁剪工具集（每次最多 ~15 个）；prompt 提示"先用知识图谱查询" |
| 工具间数据格式不一致 | 低 | 强制 LLM tool schema 类型 + Pydantic 验证 |
| 评测主观性高 | 中 | 沿用 v1 双裁判 + tiebreak 机制；ReAct 加 trace-level review |

---

## 5. 人力 & 算力需求

### 5.1 人力（推荐双人 dev 配置）

| 角色 | 投入 | 主要负责 |
|------|------|---------|
| Dev A（高级，agent 主导）| 100% | M13 ReAct / M17 adapter / M19 多 backend |
| Dev B（高级，knowledge graph 主导）| 100% | M14 drgn / M16 变更 / M18 gitee / M20 eval |
| SRE 顾问 | 0.5 PD/周 | hardware / observability 选型评审 |
| 评测人员 | 1 PD/周 | 真实案例标注 + 月度 30 例评测 |

**最小可启动配置**：1 dev（双倍周期 16+ 个月）+ 0.5 评测。

### 5.2 算力 & 存储

| 资源 | 用途 | 估算 |
|------|------|------|
| LLM token | ReAct 调查 + 报告 | 单次诊断 ≤ 50K tokens；月度评测 30 例 ≈ 1.5M tokens |
| drgn 服务器 | vmcore 分析 | 8 GB RAM / vmcore（开发机本地即可）|
| OLK debuginfo 缓存 | fetch_debuginfo | 约 5-10 GB（每个 kernel 版本）|
| gitee/atomgit issue 入库 | bug 表扩展 | 约 1-2 GB（10K+ issue body）|
| 监控测试集群 | adapter 联调 | 可复用现有 Prometheus（或部署 dev 实例）|

### 5.3 Claude / OpenRouter 订阅成本（v2 新增部分）

- v2.0 开发阶段每周 ReAct 实验 ≈ 5K tokens × 100 次/周 = 500K/周 ≈ Pro 配额可覆盖
- v2.1 / v2.2 上量后建议切到 vLLM 本地部署（v1.3 已具备路径）

### 5.4 知识库存储估算（v2.1 末）

| 数据 | v1 末 | v2.1 末 |
|------|-------|---------|
| kernel_commit | ~6 GB | ~6 GB（同） |
| lkml_message | ~3 GB | ~5 GB（+linux-mm 修复后补抓）|
| bug（含 gitee/atomgit）| ~50 MB | ~2-3 GB（+10K issue）|
| agent_tool_trace（新表）| 0 | ~500 MB（90 天保留）|

---

## 6. 度量与汇报机制

### 6.1 持续度量（自动 CI 跑批）

| 指标 | 频次 | 目标 |
|------|------|------|
| 真实案例 Recall@10 | 每周 | v2.0 ≥60% / v2.1 ≥70% / v2.2 ≥80% |
| 单次诊断平均 token | 每次 | ≤ 50K |
| 单次诊断 ReAct 迭代分布 | 每次 | 中位数 ≤ 8，P95 ≤ 15 |
| Insufficient-evidence 出口召回率 | 每月 | 该 say "不知道" 的案例 ≥ 80% 召回 |
| Hardware 误诊率 | 每月 | dmesg 含 `Hardware Error` 案例误指 kernel commit ≤ 5% |
| `link_commit_bug` 行数 | 每周 | v2.1 末 ≥ 10K |

### 6.2 周报 / 月度评审

- **周报**：本周完成 task ID 清单 + 阻塞 + Recall@10 曲线
- **月度评审**：里程碑交付 + 验收准则达成率 + 真实案例 30 例评测（v2.2 起）
- **ADR 评审**：每个新 ADR 提交时双人评审

### 6.3 里程碑评审

| 评审点 | 评审产出 |
|--------|---------|
| M15 末（v2.0 验收）| Pass / Conditional Pass / Fail 报告 + 偏差分析 |
| M18 末（v2.1 验收）| 同上 |
| M21 末（v2.2 验收 + v2 完整收尾）| v2 postmortem |

### 6.4 ADR 记录

v2 计划产出的 ADR（已起草）：

| ADR | 主题 |
|-----|------|
| ADR-019 | Hybrid ReAct + 确定性骨架 |
| ADR-020 | drgn over crash |
| ADR-021 | Observability via adapter pattern |
| ADR-022 | gitee/atomgit issue ingestion |
| ADR-023 | Hardware-first SOP routing |
| ADR-024 | Insufficient-evidence exit path |

实施过程中新增的设计决策会按 v1 模式继续编号（ADR-025+）。

---

## 7. 开始 v2 实施前的最后检查清单

> _审计于 2026-06-01_：实施已超出"开始 v2"阶段，进入 v2.4 收尾。多数 box
> 已实质完成；治理类项（评审通过）保留 `[ ]`，由你最终签发。

### 7.1 文档 & 决策

- [x] PRD 文档已落档 (`docs/v2/PRD.md`) — _评审签发待你确认_
- [x] Architecture 文档已落档 (`docs/v2/Architecture.md`) — _同上_
- [x] ADR-019 ~ ADR-025 全部落档（7 个 ADR） — _同上_
- [x] AgentThink.md 已落档 (`docs/AgentThink.md`)
- [x] v2 vs v1 兼容策略明确：`diagnose()` API 保持向后兼容；`report_json.schema_version` 从 1 → 2（见 `agent/report/renderer.py`）

### 7.2 v1 收尾依赖

- [x] LKML stable 摄入完成（实测 `lkml_message` 115,001 行，2026-05 完成）
- [x] Linker 重跑：`run_linker()` + `link_nvd_commits()` 已多次重跑，`link_commit_message` = 38,211 / `link_commit_cve` = 17,678 / `link_commit_bug` = 58,634
- [x] linux-mm 补抓完成（fetcher 修复 + 增量回填均已上线，2026-05）
- [x] `link_commit_message` 验证非零（38,211）

### 7.3 资源 & 凭据

- [ ] OLK kernel-debuginfo 仓库访问确认 — _vmcore 路由暂未启用，验证延后_
- [x] gitee / atomgit API 凭据已配置（ADR-022 实现，token 在 `configs/local.yaml`）
- [ ] Prometheus / Elasticsearch 测试实例 — _M17 观测层尚未启动_
- [x] LLM API 配额：已切 DeepSeek 官方 API（OpenRouter 已停用），见 `configs/local.yaml`

### 7.4 工程基础

- [ ] CI 跑 v1 全部测试通过 — _本仓库尚未配置 GitHub Actions / 本地 CI；测试可手动跑 (`pytest tests/`)，但无自动门禁_
- [ ] `agent_tool_trace` 表迁移设计评审 — _未做。当前 trace 存在 state 与 eval 输出 JSON 中，未持久化到独立表_
- [ ] drgn 容器化方案 — _T-018 deferred (vmcore 路由暂未启用)_
- [x] ReAct loop 原型 spike：`docs/v2/M22_spike_findings.md` 已记录结论，主实现 (`agent/react/loop.py`) 已 land

---

## 8. 附录：常用命令速查

### 8.1 v2.0 后的诊断 CLI 示例

```bash
# 标准诊断（kernel 路由）
diag-agent diagnose --upload /var/log/dmesg

# 带 vmcore 诊断（kernel+vmcore 路由）
diag-agent diagnose --vmcore /var/crash/2026-05-19-15:00/vmcore --vmlinux /usr/lib/debug/...

# 远程节点诊断（含变更关联）
diag-agent diagnose --node prod-host-42.example.com --since "2 hours ago"

# Hardware 路径（自动检测 taint=M）
diag-agent diagnose --upload /var/log/dmesg  # 若 dmesg 含 Hardware Error 自动走 hw 路由

# 强制走特定路由（debug 用）
diag-agent diagnose --upload ... --force-route hardware
```

### 8.2 v2.1 后的监控查询示例

```bash
# 查 OOM 前 2 小时趋势
diag-agent diagnose --node prod-42 --fault-time "2026-05-19 14:30" --window 2h

# 强制走 change 路由
diag-agent diagnose --upload ... --force-route change
```

### 8.3 ReAct 调试

```bash
# 启用调试模式（输出每步工具调用）
DIAG_AGENT_REACT_DEBUG=1 diag-agent diagnose ...

# 查看历史诊断的 tool trace
diag-agent trace view <diagnosis_id>

# 评测命令（v2.0 起）
diag-agent eval run --dataset eval/data/v2_real_cases.json --output eval/results_v2/
diag-agent eval report --since "30 days ago"
```

### 8.4 数据库 / Linker（v2.1 起）

```bash
# 触发 gitee/atomgit issue 同步
python -m ingest.gitee_issue incremental

# 触发完整 Linker 重跑（含 gitee URL 模式）
python -c "from graph.linker import run_linker; from storage.pg.engine import get_engine; run_linker(get_engine())"
```

### 8.5 监控 adapter 切换（v2.1 起）

```yaml
# configs/observability.yaml
default_adapter: prometheus
backends:
  prometheus:
    url: http://prometheus.local:9090
    timeout_seconds: 30
  elasticsearch:
    url: https://es.local:9200
    index_pattern: "kernel-logs-*"
```

---

*参考：[PRD.md](PRD.md) · [Architecture.md](Architecture.md) · [adr/](adr/) · [../AgentThink.md](../AgentThink.md) · [../v1/ProjectPlan.md](../v1/ProjectPlan.md)*

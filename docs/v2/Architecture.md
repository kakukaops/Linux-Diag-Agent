# Linux-Diag-Agent v2 — 架构

| 字段 | 值 |
|------|---|
| 文档类型 | Architecture |
| 版本 | v2 |
| 状态 | Design Draft（2026-05-20，待评审）|
| 关联文档 | [PRD.md](PRD.md) · [ProjectPlan.md](ProjectPlan.md) · [adr/](adr/) · [../AgentThink.md](../AgentThink.md) · [../v1/Architecture.md](../v1/Architecture.md) |
| 设计基础 | [AgentThink.md](../AgentThink.md) 五个章节（业界调研 → tools 规划 → 架构演进 → Linux 特殊性 → L1/L2/L3 三层壁垒）|
| 最后更新 | 2026-05-20 |

> **⚠️ 设计规格文档**：本文档为 v2 实施前的设计规格，定稿后会与代码并行迭代。当前实现状态见对应目录的 `CLAUDE.md`。

---

## 1. 架构概览

### 1.1 v1 → v2 演进图

**v1（当前）**：固定 10 节点 LangGraph 流水线，LLM 仅做文本生成。

```
parse_input → extract_events → classify_fault → retrieve
  → load_sop → generate_hypotheses → verify_hypothesis
  → self_consistency → bind_claims → generate_report → END
```

**v2（目标）**：三阶段 Hybrid Agent —— Triage 确定性、Investigation ReAct、Report 确定性。

```
┌────────────────── 阶段一：Triage（确定性，3-4 节点）──────────────────┐
│   parse_input → extract_events → detect_taint_and_hw_signals          │
│                                       ↓                                │
│                            classify_fault_and_route                    │
│                          (taint=M → 硬件路径)                          │
│                          (Hardware Error → 硬件路径)                   │
│                          (panic/oops → kernel + vmcore 路径)           │
│                          (others → 标准路径)                            │
└────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌────────────────── 阶段二：Investigation（ReAct 子图）────────────────┐
│                                                                         │
│   ReAct Loop:                                                          │
│     LLM 观察 state → 推理 → 选择并调用工具                              │
│     ↓                                                                  │
│     工具集（按路由动态加载，最多 ~15 个）                                │
│     ├── L2 知识图谱工具：search_*、check_backport_status、…            │
│     ├── L1 vmcore/drgn 工具（若有 vmcore）                              │
│     ├── L1 硬件工具（若 taint=M / HW Error）                            │
│     ├── L1 变更工具（升级类问题）                                       │
│     └── L1 监控工具（慢故障 / 时间序列）                                 │
│     ↓                                                                  │
│     工具返回结果 → 反馈给 LLM → 重复                                    │
│                                                                         │
│   终止条件（任一即停）：                                                 │
│     A. LLM 输出 final_answer                                            │
│     B. 达到 max_iter (默认 15)                                          │
│     C. 达到 token budget (默认 50K)                                     │
│     D. LLM 主动输出 insufficient_evidence                               │
│                                                                         │
└────────────────────────────────────────────────────────────────────────┘
                                       ↓
┌────────────────── 阶段三：Report（确定性，3 节点）───────────────────┐
│   bind_claims → generate_report → save_audit_trail → END               │
│                                                                         │
│   出口分支（基于 Investigation 结果）：                                  │
│     - 正常诊断报告（含工具调用轨迹）                                     │
│     - "无法诊断" 报告（含"请收集 X" 清单）                              │
│     - "硬件问题" 报告（含 RAS / IPMI 证据，不指向 kernel commit）       │
└────────────────────────────────────────────────────────────────────────┘
```

详见 [ADR-019](adr/ADR-019-hybrid-react-deterministic.md)。

### 1.2 L1 / L2 / L3 三层壁垒模型

从 [AgentThink.md §5.3](../AgentThink.md) 提炼，这是 v2 的产品策略核心：

| 层 | 内容 | 策略 | v2 涉及类别 |
|----|------|------|------------|
| **L1 商品工具** | 实时巡检、vmcore 分析、监控、硬件诊断、变更采集 | **买 / 集成**（drgn / mcelog / Prometheus）| C、F、G、H；B 的 live 部分 |
| **L2 知识图谱 + 查询工具** | commit/LKML/bug/CVE/code + 查询 API（`check_backport_status` 等）| **自建 · 数据壁垒** | A、D |
| **L3 诊断推理** | 故障分类、SOP、领域启发式、ReAct 调查策略、证据绑定、eval | **自建 · 逻辑壁垒** | classify_fault、SOP 机制、假设生成、E；B 的解析器 |

### 1.3 核心原则：诊断 = 现场状态 ⋈ 知识图谱

```
现场工具(L1):  tcp_v4_do_rcv panic, OLK-6.6, taint=G
知识图谱(L2):  3 周前有 commit 改过此函数,已 backport 到 OLK-6.6,LKML 有回归报告
诊断结论(L3) = 上述两者的 JOIN
```

知识图谱不是"用到再查"的旁路库,而是 ReAct 每一步推理都会主动 JOIN 的另一半。

### 1.4 系统分层（在 v1 基础上的变化）

```
┌──────────────────────────────────────────────────────────────────────┐
│ L0 — 用户接口层（CLI 不变）                                            │
│   diag-agent diagnose --upload ... [--vmcore /path] [--node hostname]│
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L1 — 诊断 Agent 层 (Hybrid)                                          │
│   Triage(确定性) → Investigation(ReAct) → Report(确定性)              │
│   ★ v2 重构 ★ M7 升级 + 新 M22 ReAct Loop Engine                       │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L2 — 主机 MCP 工具层（大幅扩展）                                      │
│   v1: mcp-dmesg-journal · mcp-sosreport                              │
│   v2 新增（本地 / 节点端运行,MCP server 封装便于沙箱化）:              │
│     M9  Crash Forensics (drgn / decode_stacktrace / taint)            │
│     M10 Change Correlator (rpm/journalctl/cmdline diff)               │
│     M11 Hardware Layer (mcelog/edac/ipmi)                             │
│                                                                       │
│   v2 新增（远程 HTTP 客户端,连外部监控系统）:                          │
│     M12 Observability Adapter (Prometheus/ES/Huatuo/Datadog)          │
│                                                                       │
│   定位差异: M9-M11 在 mcp_servers/（在被诊断节点运行,只读访问硬件     │
│   或系统资源）;M12 在 clients/（agent 机器发起 HTTP 查询到监控系统）  │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L3 — 检索编排层（质量提升 + 工具化）                                  │
│   v2 关键变化:                                                        │
│     - retrieve 节点启用 LLM query parsing                              │
│     - 检索路由作为 LLM 可调用工具暴露                                   │
│     - 同义词/复合词展开                                                │
│     - 跨路由分数归一化（已上线）                                        │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L4 — 知识层（补完 + LKML 改三层）                                     │
│   commit/CVE/Bug/syzbot 子图 + Cross-Graph Linker（本地,不变）         │
│   v2 新增:                                                            │
│     - gitee/atomgit issue ingester → 补 link_commit_bug 缺口          │
│     - Fixes: 反向图索引 → get_regression_fixes                         │
│     - commit diff 接入 (git show wrapper)                              │
│   ★ LKML 改为三层（ADR-025）★:                                        │
│     L1 link 表/线程 DAG（本地）· L2 智能缓存（lkml_message）            │
│     · L3 lore.kernel.org 远程发现服务                                  │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L5 — Ingestion 层（小增量）                                           │
│   v1 + 新 gitee_issue ingester                                        │
│   LKML 不再 bulk:定向抓取(backfill_referenced) + 懒加载缓存（ADR-025）│
│   weekly_sync.sh 加 Linker 自动重跑                                   │
└──────────────────────────────────────────────────────────────────────┘
                                ↓ ↑
┌──────────────────────────────────────────────────────────────────────┐
│ L6 — LLM Provider 层（无变化）                                        │
│   claude_code / openai_compat / vllm 三 backend                       │
│   但 Tool Calling 真正启用（v1 定义未用）                              │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 2. Hybrid Agent 详细流程

### 2.1 阶段一：Triage（确定性，对应 v1 前 4 节点的修正版）

**目的**：从原始输入识别故障类型，决定走哪条诊断路径，避免误诊。

| 节点 | 输入 | 输出 | 变化 |
|------|------|------|------|
| `parse_input` | dmesg / sosreport / 自然语言 | input_type | 无变化（v1 已有）|
| `extract_events` | input_type + 原始内容 | structured KernelEvent[] | 无变化（v1 已有）|
| `detect_taint_and_hw_signals` | events + dmesg 原文 | taint_flags、has_hardware_error 标记 | **★ 新增 ★** 见 [ADR-023](adr/ADR-023-hardware-first-sop-routing.md) |
| `classify_fault_and_route` | 上述全部 | fault_kind + diagnostic_route ∈ {kernel, hardware, change, unknown} | **★ 重构 ★** 路由不再只看 fault_kind |

**路由表（替代 v1 的 `_EVENT_KIND_TO_SOP`）**：

```python
if has_hardware_error or "M" in taint_flags or has_mce_event:
    route = "hardware"          # 走 L1 硬件工具,不搜 kernel commit
elif fault_kind in {"panic", "oops"} and has_vmcore_available:
    route = "kernel+vmcore"     # 走 drgn 分析路径
elif user_question_implies_change():  # "升级后"、"昨天还好"
    route = "change"            # 走变更关联路径
else:
    route = "kernel"            # 标准诊断
```

### 2.2 阶段二：Investigation（ReAct 子图，对应 v1 的 hypotheses/verify/consistency 三节点合并）

**核心：把"假设生成 + 验证 + 自一致性投票"三个固定节点替换为 ReAct 循环。**

```python
state = {"messages": [user_input + triage_summary], "iteration": 0, "tokens_used": 0}

while True:
    # 1. LLM 推理 + 工具选择
    resp = llm.chat(
        messages=state["messages"],
        tools=route_to_tools[route],   # 按 triage 路由动态裁剪工具集
    )
    state["tokens_used"] += resp.usage.total_tokens
    state["iteration"] += 1

    # 2. 终止条件
    if resp.has_final_answer:
        state["verdict"] = "diagnosed"
        break
    if resp.has_insufficient_evidence:
        state["verdict"] = "insufficient_evidence"
        break
    if state["iteration"] >= MAX_ITER:
        state["verdict"] = "max_iter_reached"
        break
    if state["tokens_used"] >= TOKEN_BUDGET:
        state["verdict"] = "budget_exhausted"
        break

    # 3. 执行 LLM 选择的工具调用
    for tool_call in resp.tool_calls:
        result = tool_registry[tool_call.name](**tool_call.args)
        state["messages"].append({"role": "tool", "content": result})

    # 4. checkpoint 落库（每次工具调用后,中断可恢复）
    checkpointer.save(state)
```

**工具集按路由裁剪**（每条路由的 LLM 工具菜单不同）：

| 路由 | 必加工具 | 可选 |
|------|---------|------|
| `kernel` | 类别 A（检索）、D（代码/补丁）、B（解析器）| C（监控）、G（变更）|
| `kernel+vmcore` | 上面全部 + 类别 F（vmcore/drgn）| — |
| `hardware` | 类别 H（mcelog/EDAC/IPMI）+ 类别 A 中"硬件相关 commit"子集 | 类别 G |
| `change` | 类别 G（变更）+ 类别 A、D | 类别 C |
| `unknown` | 全工具 | — |

详见 [ADR-019](adr/ADR-019-hybrid-react-deterministic.md)。

### 2.3 阶段三：Report（确定性，对应 v1 后 2 节点）

| 节点 | 输入 | 输出 | 变化 |
|------|------|------|------|
| `bind_claims` | final_analysis + evidence 全集 | Claim[] with verified/unverified | 同 v1 |
| `generate_report` | state | report_md + report_json | **★ 三种出口 ★** |
| `save_audit_trail` | state | 工具调用轨迹落库 | **★ 新增 ★** |

**`generate_report` 的三种出口**：

```python
if state["verdict"] == "diagnosed":
    return standard_report_md(state)              # 正常 RCA 报告
elif state["verdict"] == "insufficient_evidence":
    return insufficient_evidence_report(state)    # "请收集 X" 清单
elif route == "hardware":
    return hardware_report(state)                 # 不引用 kernel commit
else:
    return inconclusive_report(state)             # max_iter / budget,带降级标记
```

详见 [ADR-024](adr/ADR-024-insufficient-evidence-exit.md)。

---

## 3. 工具规划（来自 AgentThink，按 L1/L2/L3 重新组织）

### 3.1 L1 商品工具（外部集成，不自研）

| 类别 | 模块 | 主要工具 |
|------|------|---------|
| **类别 F** Crash Dump | **M9 新** | `check_kdump_available` / `fetch_debuginfo` / `analyze_vmcore`（drgn）/ `decode_stacktrace` / `parse_taint_flags` |
| **类别 G** Change Correlation | **M10 新** | `get_package_history` / `get_boot_history` / `get_kernel_cmdline_diff` / `get_config_drift` / `correlate_fault_with_changes` |
| **类别 H** Hardware & Firmware | **M11 新** | `get_mce_log` / `get_edac_errors` / `get_ipmi_sel` / `get_hardware_inventory` |
| **类别 C** Observability | **M12 新** | `query_metric` / `get_memory_trend` / `get_oom_events` / `search_logs` / `get_kernel_logs`（通过 adapter，详见 §3.4）|
| **类别 B** Log Parsing（补全）| M6 扩展 | `parse_journal` / `read_live_dmesg(filtered)` / `extract_call_trace` |

### 3.2 L2 知识图谱 + 查询工具（自建数据壁垒）

| 类别 | 模块 | 主要工具 |
|------|------|---------|
| **类别 A** Knowledge Retrieval | M5 扩展（已有，封装为 LLM tool） | `search_commits` / `search_lkml` / `search_bugs` / `search_syzbot` / `search_cve` / `search_code` / `lookup_symbol` |
| **类别 D** Code & Patch | M4 + 新工具 | `get_commit_detail` ✅ / `get_commit_diff` ★新 / `check_backport_status` ★新 / `get_regression_fixes` ★新 / `get_function_source` ✅ / `get_call_graph` ✅ |

### 3.3 L3 诊断推理（自建逻辑壁垒）

不是"工具",而是 ReAct 调度的策略与领域逻辑：

| 组件 | 模块 | 职责 |
|------|------|------|
| 故障分类与路由 | M7 重构 | `classify_fault_and_route`，含 taint/HW 优先规则 |
| SOP 机制 | M7 + 现有 SOP yaml | 9 个 fault_kind 对应 SOP；v2 加 hardware SOP |
| ReAct Loop Engine | **M13 新** | iteration/budget/checkpoint 管理；工具集动态裁剪；终止条件判定 |
| Claim-Evidence Binding | M7 同 v1 | 不变 |
| Insufficient-Evidence 出口 | M7 新分支 | 见 [ADR-024](adr/ADR-024-insufficient-evidence-exit.md) |
| 类别 E 假设验证（P4，留 v2.2）| 待定 | `check_kernel_version_has_fix` / `run_safe_diagnostic_command` / `explain_call_trace` |
| Eval 反馈环 | **M14 新（P2）** | 自动 Recall@10 跑批；真实案例数据集 |

### 3.4 类别 C 适配器架构（关键设计）

**统一输入语法,backend 内部翻译**：metrics 类工具收 PromQL，logs 类工具收 ES Query DSL；traces v2 不做（[ADR-021](adr/ADR-021-observability-adapter.md)）。

```
        LLM Tool Layer
            ↓
   ObservabilityAdapter (Protocol)
        ├── query_metric(promql, time_range) → TimeSeries
        ├── search_logs(es_query, time_range, level) → LogEntries
        └── (traces 留待 v3)
            ↓
   ┌───────────────┬──────────────┬─────────────┬──────────────┐
PrometheusAdapter  ES/Loki    HuatuoAdapter  DatadogAdapter
   直接 PromQL    直接 ES DSL    PromQL→huatuo  PromQL→Datadog
                  (Loki LogQL)   ES DSL→huatuo  ES DSL→Datadog
                                 内部翻译       内部翻译
```

`huatuo` 已确认是 eBPF 内核观测平台（[huatuo.tech](https://huatuo.tech/)），与其他 backend 等价对待——LLM 工具集对所有部署环境是同一份 schema，切 backend 改 yaml 配置即可。详见 [ADR-021](adr/ADR-021-observability-adapter.md)。

---

## 4. 知识图谱演进（L2）

### 4.1 现有结构（v1）

继承 v1 4 子图 + Cross-Graph Linker（[v1/Architecture.md §2](../v1/Architecture.md)）：

- Code Graph（CodeGraph MCP）
- Discussion Graph（lkml_*）
- Bug Graph（bug / syzbot / cve）
- Cross-Graph Linker（link_commit_bug / link_commit_message / link_commit_cve）

### 4.2 v2 新增查询工具（数据已在 DB 内,只需暴露 API）

| 工具 | 实现 | 数据依据 |
|------|------|---------|
| `check_backport_status(upstream_sha, olk_version)` | SQL：根据 `kernel_commit.upstream_commit` 反查 + `olk_inclusion_type ∈ {mainline, stable}` | 已有列 |
| `get_regression_fixes(commit_hash)` | SQL：`kernel_commit.body ~ 'Fixes: <commit_hash>'` + trailer parser | 已有 `body_tsv` GIN |
| `get_commit_diff(commit_hash)` | `git show <hash>` wrapper（OLK 本地 repo） | 本地 git |

### 4.3 v2 新增数据源

**gitee/atomgit issue ingester**（补 `link_commit_bug = 0` 缺口）：

```
gitee.com/openeuler/kernel/issues/* （63K commits 引用）
atomgit.com/openeuler/kernel/issues/* （7.9K commits 引用）
   ↓
新 ingester (M3 扩展)
   ↓
bug 表 with source='gitee' / source='atomgit'
   ↓
Cross-Graph Linker 扫 commit.body 中的 gitee/atomgit URL
   ↓
link_commit_bug 填充
```

详见 [ADR-022](adr/ADR-022-gitee-atomgit-issue-ingest.md)。

### 4.4 检索质量改进

| 改动 | 现状 | 改进 |
|------|------|------|
| LLM query parsing | `retrieve()` 硬编码 `use_llm=False` | 默认启用 + budget guard |
| 关键词来源 | `question.split()[:10]` | LLM 抽取的 `error_keywords` + `subsystem_hint` |
| 跨路由分数 | LKML 13-25 / commit 0.5-5（不可比）| max 归一化到 [0,1]（已上线）|
| 同义词 | `to_tsquery('lockup')` 不匹配 "softlockup" | 别名表 + tsquery 显式展开 `softlockup \| soft_lockup \| lockup` |
| Reranker | LLM 调用频繁失败 + 浪费 | budget 紧时 跳过整个 rerank,不进去再失败 |

### 4.5 LKML 知识数据架构（ADR-025 三层模型）

LKML 从「bulk 摄入全量语料」改为三层（实测依据：bulk 按 list+日期摄入对 `link_commit_message` 无效，34.5h 构建、85% 漏抓）：

| 层 | 内容 | 位置 |
|----|------|------|
| L1 计算结构 | `link_commit_message` / 线程 DAG 元数据 | 本地（小）|
| L2 智能缓存 | `lkml_message` 表 = 缓存（定向抓取 + 懒加载 + 检索回填累积）；本地 BM25 索引在此层 | 本地 |
| L3 远程发现 | lore.kernel.org（Xapian 搜索 + `/raw` + `/t.mbox.gz`）| 远程 |

`lkml` 检索路由变为混合：本地 L2 缓存 BM25 + online 模式 lore live search，结果合并。详见 [ADR-025](adr/ADR-025-knowledge-data-online-default.md) 和 [V2_v1_modules_supplements.md §2bis](V2_v1_modules_supplements.md)。

---

## 5. 关键新组件清单

### 5.1 L1 新模块

| 模块 | 路径（建议） | 主要内容 |
|------|------------|---------|
| **M9 Crash Forensics** | `mcp_servers/crash_forensics/` | drgn Python wrapper；vmcore 分析（CPU 栈、持锁、内存）；taint 解析；decode_stacktrace 集成 |
| **M10 Change Correlator** | `mcp_servers/change_correlator/` | rpm/dnf history 读取；boot 时间线；cmdline/sysctl diff |
| **M11 Hardware Layer** | `mcp_servers/hardware/` | mcelog、ras-mc-ctl、ipmitool、dmidecode 封装 |
| **M12 Observability Adapter** | `clients/observability/` | `ObservabilityAdapter` 协议 + 多 backend 实现 |

### 5.2 L2 模块扩展

| 模块 | 路径 | 改动 |
|------|------|------|
| `graph/linker.py` | 现有 | 加 Fixes 反向索引；扩展 gitee/atomgit URL 模式 |
| `ingest/gitee_issue/` | **新增** | gitee/atomgit issue API ingester |
| `retrieval/recall/code.py` 等 | 现有 | 加同义词展开 |
| `retrieval/engine.py` | 现有 | 已加分数归一化；继续优化 |

### 5.3 L3 模块（核心重构）

| 模块 | 路径 | 改动 |
|------|------|------|
| `agent/triage/nodes.py` | 现有 | 加 `detect_taint_and_hw_signals`；改 `classify_fault` → `classify_fault_and_route` |
| `agent/graph.py` | 现有 | Investigation 阶段替换为 ReAct 子图 |
| **`agent/react/loop.py`** | **新增（M22）** | ReAct loop engine：iteration/budget/checkpoint/tool dispatch |
| `agent/diagnosis/nodes.py` | 现有 | hypotheses/verify/consistency 三节点被 ReAct 替代；`generate_report` 加三出口 |
| `agent/sop/definitions/hardware.yaml` | **新增** | 硬件路径 SOP |
| **`eval/feedback_loop.py`** | **新增（M23, P2）** | 自动质量度量 |

#### 5.3.1 新模块 M22（原 M13）/ M23（原 M14）详细职责

为对齐 §5.1 的格式（M9-M12 都给了"路径 + 主要内容"表）：

| 模块 | 路径（建议）| 主要内容 |
|------|------------|---------|
| **M22 ReAct Loop Engine** | `agent/react/` | LangGraph `create_react_agent` 扩展;iteration/token/timeout 控制;工具异常处理;工具集动态裁剪;per-route prompt 模板。详见 [modules/M22_react_loop_engine.md](modules/M22_react_loop_engine.md) |
| **M23 Eval Feedback Loop** | `eval/` 扩展 | Recall@10 自动跑批 CI;真实案例数据集（≥30 例）;meta-eval 人工 review CLI;质量曲线 dashboard;`evidence_strength_score` retrain。详见 [modules/M23_eval_feedback_loop.md](modules/M23_eval_feedback_loop.md) |

#### 5.3.2 v1 模块的 v2 增量

M2/M4/M5/M6/M7 的 v2 改动统一记录在 [V2_v1_modules_supplements.md](V2_v1_modules_supplements.md)（含工具注册协议 / v1→v2 上线策略 / 新 gitee-atomgit ingester 详设）。

---

## 6. 接口契约

### 6.1 LLM Tool Schema（OpenAI 兼容）

所有 L1/L2 工具作为 `ToolSchema` 注册到 `tool_registry`，由 ReAct Loop 按路由筛选传给 LLM。示例（`check_backport_status`）：

```json
{
  "type": "function",
  "function": {
    "name": "check_backport_status",
    "description": "Check whether an upstream mainline commit has been backported to a specific OLK kernel version.",
    "parameters": {
      "type": "object",
      "properties": {
        "upstream_sha": {"type": "string", "description": "Full mainline commit SHA"},
        "olk_version": {"type": "string", "enum": ["OLK-6.6", "OLK-5.10"]}
      },
      "required": ["upstream_sha", "olk_version"]
    }
  }
}
```

返回：`{"backported": true/false, "olk_commit_hash": "...", "inclusion_type": "mainline|stable|null", "first_seen_in_version": "..."}`

### 6.2 ObservabilityAdapter Protocol

```python
class ObservabilityAdapter(Protocol):
    def query_metric(
        self, promql: str, start: datetime, end: datetime, step: str = "15s"
    ) -> list[TimeSeriesPoint]:
        """统一接收 PromQL；非 Prometheus backend 内部翻译。"""

    def search_logs(
        self, es_query: str, start: datetime, end: datetime, level: str | None = None
    ) -> list[LogEntry]:
        """统一接收 ES Query DSL；非 ES backend 内部翻译。"""

    def health_check(self) -> bool: ...

    # 注：Traces v2 不实现，留待 v3。
```

backend 选择由 `configs/observability.yaml` 决定（gitignored）。

### 6.3 `agent_tool_trace` 表 schema

每次诊断的工具调用轨迹落入此表（PG），用于审计、调试、eval：

```sql
CREATE TABLE agent_tool_trace (
    id              BIGSERIAL PRIMARY KEY,
    diagnosis_id    UUID NOT NULL,
    step            INTEGER NOT NULL,             -- 第几步工具调用
    tool_name       TEXT NOT NULL,
    args_json       JSONB NOT NULL,
    result_summary  TEXT,                          -- 结果摘要（避免存大对象）
    result_size     INTEGER,                       -- 完整结果 byte 数
    latency_ms      INTEGER,
    error           TEXT,                          -- 异常 message,成功时 NULL
    route           TEXT,                          -- triage 路由（kernel/hardware/...）
    started_at      TIMESTAMPTZ NOT NULL,
    finished_at     TIMESTAMPTZ NOT NULL
);

CREATE INDEX idx_att_diag ON agent_tool_trace (diagnosis_id, step);
CREATE INDEX idx_att_tool ON agent_tool_trace (tool_name, started_at);
```

保留期：默认 90 天（与 v1 `ingest_runs` 一致）；详细 result 大对象存对象存储（如有），表里只存摘要。

### 6.4 报告 JSON Schema（v2 扩展）

新增字段（v1 字段保留向后兼容）：

```json
{
  "verdict": "diagnosed | insufficient_evidence | hardware_suspected | inconclusive",
  "route": "kernel | kernel+vmcore | hardware | change | unknown",
  "tool_call_trace": [
    {"step": 1, "tool": "search_commits", "args": {...}, "result_summary": "..."},
    {"step": 2, "tool": "check_backport_status", "args": {...}, "result_summary": "..."},
    ...
  ],
  "iterations": 7,
  "tokens_used": 32100,
  "evidence_coverage": {"commit": 5, "lkml": 2, "vmcore": 1, "monitoring": 0},
  "insufficient_evidence_actions": [   // 仅 verdict=insufficient_evidence 时
    {"action": "enable_kdump", "rationale": "需要 vmcore 才能定位持锁 CPU"},
    {"action": "install_debuginfo", "package": "kernel-debuginfo-6.6.0-...", ...}
  ],
  ...v1 字段
}
```

详见 [ADR-024](adr/ADR-024-insufficient-evidence-exit.md)。

---

## 7. 技术选型（v2 新增）

| 领域 | 选型 | 替代品 | 理由 |
|------|------|--------|------|
| 内核调试器 | `drgn`（Python 库）| `crash`（交互式）| 可编程化调用；契合 agent 架构。详见 [ADR-020](adr/ADR-020-drgn-over-crash.md) |
| ReAct 实现 | 自研 + LangGraph 现有 checkpointer | LangGraph 官方 ReAct prebuilt | 需要 budget guard / 路由化工具集裁剪等定制 |
| 监控查询 | 自研 adapter | 直接读 `/proc/` | 历史数据、多节点、与业界对齐（[ADR-021](adr/ADR-021-observability-adapter.md)）|
| Issue API | gitee / atomgit REST | 网页爬虫 | API 稳定且有限速保护（[ADR-022](adr/ADR-022-gitee-atomgit-issue-ingest.md)）|
| Debuginfo 获取 | `dnf download --source kernel-debuginfo` + sosreport 包内查找 | 手工维护 | OLK 仓库提供 |

---

## 8. 部署拓扑

### 8.0 vmcore 传输

drgn 在 agent 机器跑（不在被诊断节点跑，避免病机加压）。vmcore 几 GB，按优先级：

| 来源 | 传输方式 |
|------|---------|
| 用户上传 sosreport（含 vmcore）| 直接用包内 `/var/crash/.../vmcore`，**不传输** |
| 远程节点已配 kdump 且 agent 有 SSH 凭据 | `scp <node>:/var/crash/<latest>/vmcore /tmp/diag/<diagnosis_id>/` |
| 用户直接提供 vmcore 路径 | 本地直接读 |

诊断完成后 `/tmp/diag/<diagnosis_id>/` 整目录清理（成功或失败都清）。vmcore 不落 PG，不上传外部 LLM（含 vmcore 的诊断默认走本地 vLLM）。

### 8.1 v2.0 拓扑（最小可用）

```
┌────────────────────────────────────────────────────────────────────┐
│ 单节点（与 v1 兼容）                                                │
│   PostgreSQL + Neo4j                                               │
│   diag-agent CLI / Python lib                                      │
│   M5 retrieval / M7 agent                                          │
│   M9 crash_forensics（本地 drgn）                                  │
│   M11 hardware（本地 mcelog/ipmitool）                              │
│   M10 change_correlator（SSH 远程执行）                             │
│                                                                     │
│ 外部依赖                                                            │
│   - CodeGraph MCP (v1 已接,不变)                                   │
│   - OLK debuginfo 仓库（fetch_debuginfo 用）                        │
│   - 待诊断节点（SSH only-read）                                     │
└────────────────────────────────────────────────────────────────────┘
```

### 8.2 v2.1 拓扑（加监控）

```
+ ObservabilityAdapter
   ├─→ Prometheus（同集群或可达）
   └─→ Elasticsearch / Loki（同集群或可达）
```

### 8.3 v2.2 完整拓扑

```
+ HuatuoAdapter / DatadogAdapter
+ gitee/atomgit issue ingestion（独立网络访问）
+ eval feedback loop（CI 周报）
```

---

## 9. 安全 & 隐私

继承 v1（[v1/Architecture.md §6](../v1/Architecture.md)），新约束：

| 边界 | 约束 |
|------|------|
| drgn 操作 vmcore | **只读**，不能 patch 内存；vmcore 文件传输走 SSH（同 sosreport）|
| 远程命令（rpm/journalctl）| 白名单内命令通过 SSH key only-read 用户 |
| 监控查询 | 通过 service account read-only token；查询超时 30s |
| LLM 数据暴露 | 含 vmcore / sosreport 的诊断**默认走本地 vLLM**（v1.3 已具备的路径），不上传 OpenRouter |
| 工具调用轨迹 | 全部落库到 `agent_tool_trace` 表，可审计 |

---

## 10. 演进路径

```
v2.0 → v2.1 → v2.2 → v3 outlook
 │       │       │
 │       │       └─ eval 反馈环 + Huatuo/Datadog + 多 SOP 扩展
 │       │
 │       └─ 变更关联 + Prometheus/ES 监控 + gitee issue + PID→container
 │
 └─ Hybrid Agent + L1 (F/H) + L2 高价值查询工具 + 无法诊断出口
```

v3 outlook（不在 v2 范围）：

- 写操作 / 自动修复（需要审批工作流）
- 多租户 / 跨组织部署
- Web UI / IDE 集成
- 实时诊断（streaming output）

---

## 11. 约束与权衡

| 约束 | 权衡 |
|------|------|
| ReAct 循环带来不可预测的 token 消耗 | 用 `MAX_ITER=15` + `TOKEN_BUDGET=50K` 硬封顶；报告阶段保持确定性 |
| 工具数量增加会让 LLM tool schema 变长 | 按路由动态裁剪，每次调用最多暴露 ~15 个工具 |
| 监控 adapter 多 backend 字段语义不一致 | 统一 schema，adapter 内做映射；不提供"通用查询"，只提供语义明确的 typed 工具 |
| drgn 需要 vmlinux + 对应 debuginfo | `fetch_debuginfo` 自动从 OLK 仓库拉；sosreport 包内若有则优先用本地 |
| gitee/atomgit issue 数据补全是慢功夫 | v2.1 入库；存量 7 万条 commit 引用的链路逐步建立 |
| 数据壁垒（L2）的真壁垒是召回准确率 | v2 把"Recall@10 在真实数据集 ≥ 70%"作为 P1 验收硬指标 |
| 报告阶段确定性 vs Investigation 阶段自由度 | 显式三阶段：自由调查 + 确定报告 |

---

*参考：[PRD.md](PRD.md) · [ProjectPlan.md](ProjectPlan.md) · [../AgentThink.md](../AgentThink.md) · [adr/](adr/) · [../v1/Architecture.md](../v1/Architecture.md)*

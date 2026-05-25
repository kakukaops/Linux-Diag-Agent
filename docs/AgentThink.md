# AgentThink — Linux 故障诊断智能体架构思考

---

## 问题：本系统是故障诊断智能体，从智能体角度看，大部分架构都一样（plan-and-resolve 和 ReAct 模式），对于 Linux 故障智能体，架构部分也一样，可能是 ReAct 部分的提示词有所差异，核心还是 tools。请调研业界做法，评估这个判断是否正确，并给出 Linux 故障诊断智能体的 tools 规划。

---

## 一、用户的判断是否正确？

**结论：基本正确，但需要补充一个关键点。**

### 支持的证据

1. **业界架构确实趋同**：Google SRE Agent（基于 ADK + Remote MCP）、Microsoft Azure SRE Agent、开源 OpenSRE（60+ tools）都选择 ReAct 循环，而不是固定流水线。Plan-and-execute 在离线分析场景有用，但在在线故障诊断（需要实时反馈）中 ReAct 占主导。

2. **工具是真正的差异点**：各家的 LLM 底座越来越像，推理能力差距在收窄。微软的 Agent 和谷歌的 Agent 本质上调用的推理模型差不多，但它们接入的数据源、工具集完全不同——接入 Azure Monitor 还是 Datadog 决定了 Agent 能看到什么，工具质量决定了 Agent 能做什么。业界有一句话："Building agentic AI for production infrastructure is 20% prompt engineering and 80% custom tooling."

3. **工具即提示词**：工具名称、描述、参数定义本身就是 LLM 读到的 prompt。`get_oom_kill_process()` 和 `search_knowledge_base(query)` 暗示了完全不同的推理路径。高质量的工具定义是 domain prompt 的载体，工具设计就是 prompt engineering 的一部分。

4. **学术界验证**：CrashFixer（Linux kernel crash resolution agent）、LinuxFLBench（kernel bug localization benchmark）都表明，在 Linux 内核场景，关键工具是 code browsing、GDB 调试、VM 管理、crash 分析——架构都是 ReAct，工具决定成败。LinuxFLBench 当前文件级定位 top-1 accuracy 约 41.6%，主要瓶颈不是推理能力而是工具覆盖与可观测性。

### 需要补充的关键点

**当前系统不是 ReAct 智能体——是固定流水线。**

代码分析发现：`agent/graph.py` 是 10 节点线性流水线，LLM 在每个节点只做文本生成（假设、分析、报告），**没有工具调用能力**。`llm/provider/base.py` 中虽然定义了 `ToolCall` / `ToolSchema` 数据结构（WBS 7.6），但从未在任何节点中传递给 LLM。

```
当前架构（固定流水线）：
parse_input → extract_events → classify_fault → retrieve
  → load_sop → generate_hypotheses → verify_hypothesis
  → self_consistency → bind_claims → generate_report → END
```

这意味着：
- LLM 不能根据诊断中途的发现自主决定"我需要查这个进程的内存布局"
- 所有工具调用（检索、解析）都是硬编码在节点函数里的直接 Python 调用
- SOP steps 只是字符串列表，不是 LLM 可以执行的可调用步骤
- 检索路由选择（7 路中调哪路、用什么关键词）由规则决定，不由 LLM 决定

**但"pipeline → agent"不是无条件的进步。** 需要分清哪些环节才需要自主性：
- **调查阶段**（查什么证据、下一步看哪里）——需要 autonomy，这是 ReAct 的价值所在
- **报告生成 / 证据绑定阶段**——确定性是优点，不是缺点。同一个故障输入应该产出同一份 RCA，可复现、可审计；这部分不该交给 LLM 自由发挥

所以正确的目标不是"把整条 pipeline 都 agent 化"，而是"在调查阶段引入 ReAct 循环，保留报告阶段的确定性骨架"。

---

## 二、Linux 故障诊断智能体的 Tools 规划

### 设计原则

1. **读优先，写谨慎**：Read-only 工具可以无限制调用；写操作（sysctl 修改、cgroup 调整）必须人工确认
2. **按诊断流程分层**：先"感知"（看现象）→ 再"定位"（分析原因）→ 再"验证"（确认假设）→ 最后"建议"（给方案）
3. **离线与在线分离**：知识库工具（BM25 检索）和系统工具（live 命令）是两个完全不同的维度，都不可或缺
4. **`kernel_version` 是一等公民**：Linux 内核知识是版本化的，同一个 bug 在不同版本的修复状态完全不同
5. **事后取证优先于主动探测**：对一台已经在 softlockup / 内存吃紧的病机跑 `bpftrace`/`perf` 可能直接把它压垮。优先级应是 事后产物（vmcore、已有监控历史）> 被动读取 > 在病机上主动探测
6. **允许输出"无法诊断"**：大量真实工单证据不足。Agent 必须有一条路径输出"证据不足，请先收集 X"（开 kdump、装 debuginfo、设 `panic_on_oops`），而不是基于薄弱证据强行给一份自信的报告

---

### 类别 A：知识库检索（Knowledge Retrieval）

**现状：已实现，7 路并行 BM25，但硬编码调用，LLM 无法自主触发**

| Tool | 描述 | 现状 |
|------|------|------|
| `search_commits(keywords, kernel_version, fault_domain)` | BM25 搜索 OLK kernel commit，找历史修复记录 | ✅ 已有 |
| `search_lkml(keywords)` | 搜索内核邮件列表讨论 | ✅ 已有 |
| `search_bugs(keywords)` | 搜索 Bugzilla 已知 bug | ✅ 已有 |
| `search_syzbot(keywords)` | 搜索 syzbot 崩溃记录，找 reproducer | ✅ 已有 |
| `search_cve(cve_id_or_keywords)` | 精确 CVE 查找 + BM25 | ✅ 已有 |
| `search_code(symbol_or_keyword)` | 搜索 OLK 内核源码（Zoekt BM25）| ✅ 已有（CodeGraph）|
| `lookup_symbol(name, action)` | 查符号定义/调用链（SCIP 编译级）| ✅ 已有（CodeGraph）|

**关键改造**：封装为 LLM tool schema，让 LLM 自己决定何时调用哪个路由、用什么关键词。这是从 pipeline → agent 的最低成本第一步，复用全部现有代码。

---

### 类别 B：日志与事件解析（Log & Event Parsing）

**现状：有 MCP Server 但功能基础，有缺口**

| Tool | 描述 | 现状 |
|------|------|------|
| `parse_dmesg(text_or_path)` | 解析 dmesg 文本，提取结构化事件（OOM/oops/lockup/panic）| ✅ 已有 MCP |
| `read_live_dmesg(since_ts, level)` | 读取当前机器 dmesg，按时间戳/级别过滤 | ⚠️ 有 `read_dmesg` 但无过滤 |
| `parse_sosreport(path)` | 解析 sosreport 包，提取 kernel version、dmesg tail、系统信息 | ✅ 已有 MCP |
| `parse_journal(unit, since, until)` | 查询 systemd journal（`journalctl`）| ❌ 缺失 |
| `extract_call_trace(raw_text)` | 从 dmesg 片段提取 call trace，识别函数帧 | ⚠️ 部分实现，未单独暴露 |

---

### 类别 C：可观测性系统集成（Observability System Integration）

**现状：缺失 — 应优先对接外部监控系统，而非直接读 `/proc/`**

**设计决策**：不应让 Agent 自己去读 `/proc/meminfo`、执行 `ps` 等命令。原因：

1. **历史数据缺失**：`/proc/` 只有当前快照，而 OOM 往往是内存泄漏数小时后触发——"问题发生前 2 小时内存怎么变化"只有时序数据库能回答
2. **多节点盲区**：生产故障常跨节点，`/proc/` 只能看本机；外部监控天然有集群视角
3. **数据已存在**：企业生产环境几乎必有监控系统，数据已按 15s/1min 粒度持续采集，无需 Agent 重复采集
4. **节点宕机后无法访问**：崩溃后 `/proc/` 读不到，监控历史数据反而是唯一来源
5. **与业界对齐**：Google SRE Agent 查 Cloud Logging + Cloud Monitoring，Azure SRE Agent 查 Azure Monitor，Datadog Bits AI 查自家 metrics/logs/traces，无一读 `/proc/`

**重要限制：metrics 对内核瞬时故障基本无效。** 监控系统按 15s/1min 粒度抓取，对不同故障的有效性差异极大：

| 故障类型 | 时间尺度 | metrics 是否有效 |
|---------|---------|----------------|
| 缓慢泄漏型 OOM | 数小时 | ✅ 时序趋势是关键证据 |
| softlockup | 约 20 秒 | ⚠️ 15s 抓取只够 1-2 个数据点 |
| hardlockup / panic | 瞬时，然后节点失联 | ❌ metrics 完全无效，只能靠 vmcore + 日志 |

结论：监控系统擅长回答"慢"故障和"故障前系统在做什么"，但**内核瞬时故障的诊断脊梁是事后产物（vmcore、kernel log），见类别 F**。`huatuo` 若是 eBPF 内核可观测性方案，与 Prometheus 这种通用 metrics 不是一个档次——eBPF 能捕获内核态瞬时事件，应单独拔高对待，而非当作又一个等价 adapter。

**架构：适配器模式**

```
Agent Tool Interface
    ↓
ObservabilityAdapter（统一接口）
    ├── PrometheusAdapter    → PromQL API
    ├── ElasticsearchAdapter → ES Query DSL / Loki LogQL
    ├── HuatuoAdapter        → 华佗 API（openEuler 内部监控）
    └── DatadogAdapter       → Datadog Metrics/Logs/Traces API
```

**Metrics 工具（对接 Prometheus / Huatuo）**：

| Tool | 描述 | PromQL 示例 |
|------|------|------------|
| `query_metric(promql, time_range)` | 执行 PromQL 查询，返回时序数据 | `node_memory_MemAvailable_bytes{node="x"}` |
| `get_memory_trend(node, duration)` | 内存使用趋势（发现缓慢泄漏）| `rate(node_memory_MemFree_bytes[1h])` |
| `get_oom_events(node, time_range)` | 查询 OOM kill 事件记录 | `increase(node_vmstat_oom_kill[1h])` |
| `get_cpu_pressure(node, time_range)` | CPU/IO PSI 压力指标 | `node_pressure_cpu_waiting_seconds_total` |
| `get_cgroup_memory(cgroup, time_range)` | cgroup 内存用量趋势 | `container_memory_usage_bytes{cgroup=...}` |
| `get_disk_latency(device, time_range)` | 磁盘读写延迟 P99 | `histogram_quantile(0.99, node_disk_read_time_seconds_bucket)` |
| `get_network_errors(node, interface)` | 网卡错误/丢包计数 | `node_network_receive_errs_total` |

**Logs 工具（对接 Elasticsearch / Loki）**：

| Tool | 描述 |
|------|------|
| `search_logs(query, node, time_range, level)` | 全文搜索 dmesg/syslog/journal 日志 |
| `get_kernel_logs(node, since, until, grep)` | 查询 kernel 日志（OOM/oops/lockup 等）|
| `get_oom_kill_log(node, time_range)` | 提取 OOM kill 完整记录（含进程名、oom_score、内存分布）|
| `get_crash_context(node, crash_time, window)` | 获取崩溃时间点前后 N 分钟的日志上下文 |

**Traces 工具（对接 Jaeger / SkyWalking，适用于微服务场景）**：

| Tool | 描述 |
|------|------|
| `get_slow_traces(service, threshold_ms, time_range)` | 查找超时 trace |
| `get_error_traces(service, time_range)` | 查找报错 trace |

**补充：内核配置读取（这类可以直接读，因为是静态信息）**：

| Tool | 描述 | 方式 |
|------|------|------|
| `get_sysctl(node, key)` | 读 sysctl 参数值 | 通过 node_exporter 暴露的 sysctl metrics，或 ssh 只读 |
| `get_kernel_config(node, option)` | 读内核编译配置 | sosreport 中已包含，解析即可 |

**注意**：标准 node_exporter 未必导出所有内核级指标（如 `/proc/slabinfo` 详细 slab 统计、`/proc/schedstat` 调度统计）。需确认监控系统的 exporter 覆盖范围，对于确实未被导出的关键指标，可考虑专用 custom exporter，而不是让 Agent 直接读 `/proc/`。

---

### 类别 D：代码与补丁分析（Code & Patch Analysis）

**现状：部分实现（CodeGraph MCP），缺两个高价值工具**

| Tool | 描述 | 现状 |
|------|------|------|
| `get_commit_detail(hash)` | 返回完整 commit message（从 DB）| ✅ 已有 |
| `get_commit_diff(hash)` | 获取 commit 的 patch（实际代码改动，从 git）| ❌ 缺失 |
| `check_backport_status(upstream_sha, olk_version)` | 检查 mainline commit 是否已 backport 到指定 OLK 版本 | ❌ 缺失 |
| `get_regression_fixes(commit_hash)` | 查 `Fixes:` 反向图——该 commit 自己有没有已知回归修复 | ❌ 缺失 |
| `get_function_source(func_name, file_path)` | 读取函数源码 | ✅ 已有（CodeGraph）|
| `get_call_graph(func_name, direction)` | 上下游调用链 | ✅ 已有（CodeGraph SCIP）|

`check_backport_status` 是诊断报告最核心的结论工具——用户问的不是"这是 OOM"，而是"upstream 修了吗，我的内核有这个补丁吗"。`get_regression_fixes` 同样关键：找到嫌疑 commit 后必须确认它自己没有已知回归，否则可能推荐一个自带 bug 的 backport。

---

### 类别 E：诊断假设验证（Hypothesis Testing）

**现状：完全缺失**

SOP steps 从文本变为可执行步骤的关键工具：

| Tool | 描述 |
|------|------|
| `check_kernel_version_has_fix(kernel_ver, commit_hash)` | 对比内核版本与 commit 的合入版本，判断是否已修 |
| `run_safe_diagnostic_command(cmd)` | 白名单内的只读命令（dmesg、free、vmstat 等），执行并返回输出 |
| `explain_call_trace(frames)` | 给定 call trace 函数帧列表，查 CodeGraph 给出每帧功能说明 |

---

### 类别 F：崩溃转储分析（Crash Dump Forensics）

**现状：完全缺失 —— 对一个"内核"故障 agent，这是比检索更核心的能力**

内核 panic / hardlockup 的标准排障动作不是搜日志，而是分析崩溃转储：

```
panic → kdump 抓 vmcore → 分析 vmcore → bt -a / log / kmem
```

当前系统能力上限只是对 dmesg tail 做模式匹配，连不上真正的根因。

| Tool | 描述 |
|------|------|
| `check_kdump_available(node)` | 拿到 panic 后第一件事：这台机器配了 kdump 吗？有没有 vmcore？ |
| `fetch_debuginfo(kernel_version)` | 自动获取匹配版本的 `kernel-debuginfo` / vmlinux 符号表（OLK 需对应版本）|
| `analyze_vmcore(vmcore_path, query)` | 分析 vmcore：所有 CPU 栈、内存状态、持锁情况 |
| `decode_stacktrace(raw_trace, kernel_version)` | 原始地址 → `file:line`（封装 `decode_stacktrace.sh`）|
| `parse_taint_flags(taint_value)` | 解析 taint 位：是否加载过闭源模块、之前是否有 warning——能极大收窄搜索范围 |

**工具选型建议：用 `drgn` 而不是 `crash`。** drgn 是 Meta 开源的可编程内核调试器，本质是 Python 库——Agent 可以编程化调用、结构化拿结果；而 `crash` 是交互式 prompt，很难被 Agent 驱动。这是真正契合 Agent 架构的选择。`bt -a`（所有 CPU 栈）对 lockup 类故障是决定性的——用来定位谁持锁。

---

### 类别 G：变更关联（Change Correlation）

**现状：完全缺失 —— 但"什么变了"是 SRE 排障的第一问**

"昨天还好的，今天坏了"——最高频的根因就是一个变更。没有这一类工具，Agent 会系统性漏掉最常见的根因类别。

| Tool | 描述 |
|------|------|
| `get_package_history(node, package)` | `rpm -q --last` / `dnf history`——内核及关键包的安装/升级时间线 |
| `get_boot_history(node)` | `journalctl --list-boots` / `last reboot` / `uptime`——重启时间点 |
| `get_kernel_cmdline_diff(node)` | `/proc/cmdline` 与基线对比——启动参数漂移 |
| `get_config_drift(node, since)` | sysctl / 关键配置文件相对基线的漂移 |
| `correlate_fault_with_changes(fault_time, node)` | 把故障时间点与上述变更时间线对齐，找时间相关性 |

---

### 类别 H：硬件与固件层（Hardware & Firmware）

**现状：完全缺失 —— 这是一个误诊陷阱**

很多"内核 panic"其实是硬件故障：坏 DIMM、MCE、微码、固件。**hardlockup 更可能是硬件问题，而不是内核 bug。** dmesg 出现 `Hardware Error` / `Machine Check` 时，应立刻停止搜内核 commit，转向硬件诊断。

| Tool | 描述 |
|------|------|
| `get_mce_log(node)` | `mcelog`——CPU/内存 Machine Check 异常记录 |
| `get_edac_errors(node)` | `ras-mc-ctl --summary` / `edac-util`——内存 ECC 纠错/不可纠错错误计数 |
| `get_ipmi_sel(node)` | `ipmitool sel list`——IPMI 系统事件日志，记录 OS 根本看不到的硬件错误 |
| `get_hardware_inventory(node)` | `dmidecode`——DIMM 槽位、固件版本、BMC 版本 |

---

### 优先级建议

| 优先级 | 工具类别 | 理由 |
|--------|---------|------|
| P0 | 把现有**检索类**工具封装为 LLM tool schema | 从 pipeline → agent 的第一步；只 agent 化检索，不要一次全改 |
| P0/P1 | 类别 F：vmcore / drgn / debuginfo / taint | 对内核 agent 这是脊梁，比检索更核心 |
| P1 | 类别 G：变更关联（"什么变了"）| 覆盖最高频的根因类别——一个变更 |
| P1 | 类别 H：硬件层（mcelog / IPMI SEL）+ 修正 hardlockup SOP 路由 | 避免把硬件故障系统性误导向内核代码 |
| P1 | 类别 B：完善日志解析（journal、call trace、taint）| 输入侧质量，直接影响所有后续推理 |
| P1 | 类别 D：`get_commit_diff`、`check_backport_status`、`get_regression_fixes` | 诊断报告最常见结论"已修/未修" |
| P2 | 类别 C：对接 Prometheus/Elasticsearch（metrics + logs）| 历史趋势和跨节点视图，对"慢"故障有效 |
| P3 | 类别 C：对接 Huatuo / Datadog 等企业监控 | 适配器扩展，复用 P2 的统一接口 |
| P4 | 类别 E：假设验证工具 | P0-P2 完成后再加 |

---

## 三、架构演进建议

### 当前：固定 10 节点流水线

```
parse → extract → classify → retrieve → sop → hyp → verify → consistency → claims → report
```

LLM 只做文本生成，工具调用全部硬编码在节点里，无自主性。

### 目标：ReAct 工具调用循环

```
用户输入 → ReAct Loop {
    LLM 观察当前状态 → 推理 → 选择并调用工具
    工具执行 → 结果反馈给 LLM
    重复，直到 LLM 输出 final_answer
} → 报告渲染
```

### 渐进迁移路径（不需要一次重写）

**第一步（P0，低成本）**：把 7 路检索 + dmesg 解析 + CodeGraph 工具封装为 LLM tool schema。不改变流水线结构，但让 LLM 在 `retrieve` 节点内自主决定调用哪个路由、用什么关键词。

**第二步**：把 `generate_hypotheses` + `verify_hypothesis` + `self_consistency` 三节点合并为一个 ReAct 子图，LLM 可以在这个区间内自主发起多轮检索来验证假设。

**第三步**：引入崩溃转储分析（类别 F）、变更关联（类别 G）、监控集成（类别 C），让 Agent 具备真正的内核排障能力。

### ReAct 循环的工程约束

ReAct 循环不能只写"repeat until final_answer"，生产化必须有：
- **最大迭代数上限** + 单工具超时——防止 LLM 在工具调用里打转
- **token / 配额预算**——一次诊断的成本可控
- **保留现有 PG checkpointer**——一次 20 步的调查中途崩溃不能全部丢失，每个工具调用后应 checkpoint
- **报告阶段保持确定性**——调查用 ReAct，但 `bind_claims` / `generate_report` 仍走确定性骨架（见第一部分）

---

## 四、Linux 特殊性：与通用 SRE Agent 的区别

| 特点 | 影响 |
|------|------|
| 知识是版本化的 | `kernel_version` 必须是所有工具的一等公民参数，不能是可选字段 |
| 数据稀疏且非标 | OOM log 有结构化字段，softlockup/oops 的关键信息藏在 call trace 自由格式里，需要专门解析器 |
| 修复溯源比症状分类更重要 | 用户已知"是 OOM"；他们需要的是"upstream 有无修复，我的内核有没有这个补丁" |
| 高权限工具需要严格沙箱 | `bpftrace`、`perf` 需要 CAP_BPF，不当使用可能影响生产内核性能，必须明确 read-only vs might-affect-system 边界 |
| 故障不一定是内核 bug | hardlockup / panic 常是硬件问题（坏 DIMM、MCE）。SOP 路由必须先看 taint flags 和 `Hardware Error` 标记，再决定查内核 commit 还是查硬件——当前 `hardlockup → lockup SOP → 搜内核 commit` 的路由会系统性误诊 |
| 现代故障是容器级的 | OOM 几乎都是 cgroup/容器 OOM。Agent 必须能把 `PID → container → pod → deployment → 负责人`串起来，否则 RCA 对真正要修的人没用 |

---

## 五、产品边界与核心壁垒：买什么、建什么

前四节回答了"需要哪些工具"。但还有一个更上层的问题：**这些工具里，哪些该自己建，哪些该用业界现成方案？** 想清楚这个，才知道工程资源往哪儿投。

### 5.1 判断对的部分

两个判断是对的，且体现了产品纪律：

1. **不重造商品化工具**：`crash`/`drgn`、`mcelog`、`ipmitool`、Prometheus 都是成熟方案，封装它们是集成工作，不是核心 IP。把工程量花在自研 `bpftrace` wrapper 上是浪费。
2. **知识图谱是壁垒**：对一个 openEuler/OLK 离线场景的产品，Cross-Graph Linker 这套"commit ↔ LKML ↔ Bugzilla ↔ CVE"焊接图是单一最可防御的资产——别人有 `crash`，但没人有这张图。

### 5.2 "诊断 + 知识图谱"两部分切法的陷阱

把系统切成「Part 1：调工具诊断」+「Part 2：知识图谱」，问题在于 **Part 1 里塞了两个本质不同的东西**：

- **商品化工具**（vmcore 分析、监控查询、硬件诊断）—— 该买
- **诊断推理**（SOP 路由、故障分类、假设验证、ReAct 里"下一步查什么"的策略、领域启发式如"看到 `Hardware Error` 就停止搜 commit"）—— **是核心 IP，必须自建**

一旦把整个 Part 1 都归到"不是我们的强项、用现成的"，诊断推理就会被当成胶水代码而系统性低估。而它恰恰是第一节的论点——"工具即提示词，domain prompt 是壁垒"——里的那个 domain prompt。

### 5.3 正确的框架：三层模型

应从"两部分"改成"三层"：

| 层 | 内容 | 策略 | 对应类别 |
|----|------|------|---------|
| **L1 商品工具** | 实时巡检、vmcore 分析、监控查询、硬件诊断、变更采集 | **买 / 集成** | 类别 C、F、G、H；类别 B 的 live 读取部分 |
| **L2 知识图谱 + 查询工具** | commit/LKML/bug/CVE/代码图，以及 `check_backport_status`、`get_regression_fixes` 等查询接口 | **自建 · 数据壁垒** | 类别 A、D |
| **L3 诊断推理** | 故障分类、SOP、领域启发式、ReAct 调查策略、证据绑定、eval 反馈环、内核日志/崩溃的领域解析器 | **自建 · 逻辑壁垒** | `classify_fault`、SOP 机制、假设生成/验证、类别 E；类别 B 的解析器部分 |

L1 是别人的强项，集成即可；L2 和 L3 都不可外购，是产品的两道壁垒——一道数据、一道逻辑。原来的"两部分"捕获了 L1 和 L2，但 **L3 被折叠进 Part 1 隐性降级了**。

### 5.4 知识图谱不是"图书馆"，是诊断推理的另一半

把 Part 2 描述为"需要查文档/代码/bug 时去查阅的资料库"，是把图谱当成**被动参考库**。实际上它有两个更主动的角色：

1. **图谱本身是一类工具的来源。** `check_backport_status`、`get_regression_fixes`、`search_commits`——这些**没有任何业界现成工具**能提供，它们就是知识图谱暴露出来的查询 API。所以"工具"和"知识图谱"的边界是模糊的：L2 的一部分工具 = 图谱的查询接口。

2. **诊断 = 现场状态 ⋈ 知识图谱。** 最强的诊断来自实时数据与图谱的 JOIN：

```
现场工具(L1):  tcp_v4_do_rcv panic, OLK-6.6, taint=G
知识图谱(L2):  3 周前有 commit 改过此函数，已 backport 到 OLK-6.6，LKML 有回归报告
诊断结论(L3) = 上述两者的 JOIN
```

如果图谱被建成"用到再查"的旁路库，就会错过这个融合——而融合才是产品。图谱应当 **diagnosis-first** 地建：围绕诊断 agent 实际会问的问题（"给定崩溃函数 + 内核版本，什么变了、有没有 fix"）来索引和结构化，而不是做一个通用内核知识转储。

### 5.5 现实校验

两点必须说清楚：

1. **壁垒不是"有一个知识图谱"，而是"图谱能返回正确的那条 commit"。** 当前合成案例 Recall@10 仅 6.7%（见 `ProjectStatus.md`）。图谱的**结构**（Cross-Graph Linker）已建好，但**检索质量**尚未验证。L2 的真正工作量不在"把图建出来"，而在"让它召回准"——这是持续的、更难的部分。

2. **"用现成方案"不等于零成本。** OLK 既不是 mainline 也不是 RHEL：debuginfo 打包、commit 格式、inclusion 约定都是 OLK 特有的。即使是封装 `crash`/`drgn`、对接 Prometheus/Huatuo，也要做 OLK 适配和适配器层。方向对，但别低估 L1 的集成工作量。

### 一句话总结

> 买 L1 商品工具，建 L2 知识图谱与 L3 诊断推理两道壁垒；知识图谱不是旁路图书馆，而是融进每一步推理的另一半——诊断 = 现场状态 ⋈ 知识图谱。

---

*参考：[agent/graph.py](../agent/graph.py) · [retrieval/engine.py](../retrieval/engine.py) · [mcp_servers/](../mcp_servers/) · [llm/provider/base.py](../llm/provider/base.py)*

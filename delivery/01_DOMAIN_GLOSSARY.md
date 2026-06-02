# 01 · Domain Glossary

> 给重建者补内核诊断 / openEuler 生态术语。**AI agent 读到陌生缩写时翻这里。** 按域分类。

---

## A · openEuler / OLK 内核

| 术语 | 含义 |
|---|---|
| **OLK** | openEuler Linux Kernel — openEuler 项目维护的 Linux 内核分发。当前覆盖两条分支：OLK-6.6（主流）和 OLK-5.10（LTS） |
| **OLK-6.6** | 当前主线，基于 mainline 6.6 + openEuler 厂商补丁 |
| **OLK-5.10** | LTS 分支，基于 mainline 5.10 + openEuler 厂商补丁 |
| **upstream / mainline** | Linus Torvalds 主线 git（`kernel.org/pub/scm/linux/kernel/git/torvalds/linux.git`） |
| **linux-stable** | Greg KH 维护的 stable tree（每个 LTS 系列的修复合并点） |
| **backport** | 把上游 commit 移植回旧版本的过程；OLK 几乎所有 fix 都是 backport |
| **inclusion_type** | `kernel_commit.inclusion_type` 字段，标识这个 commit 怎么进入 OLK：`mainline` / `stable` / NULL / 厂商 tag |
| **mainline-backed commit** | inclusion_type ∈ {mainline, stable}，有可追溯的 upstream SHA，**高置信** |
| **openEuler-native commit** | inclusion_type 不在 {mainline, stable} 内，纯厂商补丁，无 upstream，**中置信**（要看 commit message） |
| **stub commit** | M4 Linker 占位行：`subject='[stub upstream for OLK <hash>]'`, `commit_date='1970-01-01'`, `body=NULL`。代表"某个 OLK backport 引用了一个我们 DB 还没 ingest 的 upstream SHA"。查询时必须显式过滤（见 `60_GOTCHAS.md`） |
| **affected_versions** | `kernel_commit.affected_versions[]` 字段，commit 适用的 OLK 版本（如 `['OLK-6.6']` 或 `['OLK-5.10', 'OLK-6.6']`） |

---

## B · Linux 内核故障类型

| 术语 | 含义 | 典型 dmesg 标志 |
|---|---|---|
| **OOM** | Out Of Memory，内核内存分配器决定杀进程 | `oom-kill:constraint=...` / `Out of memory: Killed/Kill process N` |
| **OOM (cgroup/memcg)** | cgroup memory.max 限额触发的 OOM（vs 全局 OOM） | `constraint=CONSTRAINT_MEMCG` |
| **panic** | 内核致命错误，整机停摆 | `Kernel panic - not syncing: <reason>` |
| **oops** | 非致命异常但内核状态污染（taint），系统多半已不可信 | `Oops: 0000 [#1] PREEMPT SMP NOPTI` |
| **BUG** | `BUG()` / `BUG_ON()` 触发的断言失败 | `kernel BUG at <file>:<line>!` |
| **WARN** | `WARN()` / `WARN_ON()` 触发的警告（不停机） | `WARNING: CPU: N PID: M at <file>:<line> <func>+0xN/0xM` |
| **softlockup** | 单核 RCU/timer 报告"某 CPU 卡死 > N 秒" | `watchdog: BUG: soft lockup - CPU#N stuck for X s!` |
| **hardlockup** | NMI watchdog 检测到 CPU 完全无响应 | `NMI watchdog: Hard LOCKUP detected on CPU N` |
| **rcu_stall** | RCU 子系统报告 grace period 没完成 | `RCU detected stall on CPU N` |
| **lockdep** | 锁依赖循环检测器报警 | `WARNING: possible circular locking dependency detected` |
| **hung task** | task blocked > N 秒（默认 120s）；多半 I/O 挂死 | `task <comm>:<pid> blocked for more than N seconds` |
| **KASAN** | Kernel Address Sanitizer 检测到非法内存访问 | `BUG: KASAN: use-after-free in <func>` |
| **call trace** | 故障点的内核栈帧列表，**诊断核心证据** | `Call Trace:` 段，后面跟 `<func>+0xN/0xM` 帧 |
| **taint** | 内核状态污染标记。一旦污染（loaded proprietary module 等），后续 oops 可信度降低 | `Tainted: G W OE` 等字母组合 |

### Taint 字母速查

| 字母 | 含义 |
|---|---|
| `G` | Proprietary (非 GPL) 模块加载过 |
| `W` | 之前触发过 WARN |
| `O` | Out-of-tree 模块加载过 |
| `E` | 加载了 unsigned 模块 |
| `M` | Machine Check Exception 发生过 |
| `D` | Kernel died recently |
| `P` | Proprietary module loaded |

---

## C · Linux 子系统简称

| 子系统 | 含义 |
|---|---|
| **mm** | Memory Management |
| **memcg** | Memory Cgroup（mm 子模块，cgroup v1/v2 内存限额） |
| **slab/slub** | 内核 slab 分配器 |
| **buddy allocator** | 页面分配器（mm 内部） |
| **net** | 网络栈 |
| **tcp** | TCP 协议层（`net/ipv4/tcp_*`） |
| **fs** | 文件系统通用层 |
| **ext4 / xfs / btrfs** | 具体文件系统 |
| **block** | 块设备层（IO scheduler / blk-mq / multiqueue） |
| **NVMe** | NVMe 驱动子系统 |
| **scsi** | SCSI 子系统 |
| **virtio** | virtio 半虚拟化驱动 |
| **kvm** | KVM 虚拟化 |
| **rcu** | Read-Copy-Update 同步原语 |
| **sched** | 进程调度器 |
| **percpu** | per-CPU 变量管理 |
| **lockdep** | 锁依赖检测（debug 子系统） |
| **kasan** | 内核地址消毒器 |

---

## D · 硬件错误

| 术语 | 含义 |
|---|---|
| **MCE** | Machine Check Exception — CPU 硬件级错误（cache / bus / 内存） |
| **EDAC** | Error Detection And Correction — RAM / cache ECC 子系统 |
| **UE** | Uncorrected Error — EDAC 检测到的不可纠正错误（DIMM 物理失效嫌疑大） |
| **CE** | Corrected Error — EDAC 自动纠正的可纠正错误（ECC 老化预警） |
| **IPMI SEL** | Intelligent Platform Management Interface · System Event Log（BMC 硬件日志） |
| **PCIe AER** | PCIe Advanced Error Reporting（PCIe 链路错误事件） |
| **SAN fabric event** | SAN 网络层事件（光纤通道链路掉，多路径切换） |

---

## E · 知识图谱（KG）

| 术语 | 含义 |
|---|---|
| **KG** | Knowledge Graph — 本项目的内核知识图谱（4 张内容表 + 7 张关系表的合体） |
| **commit graph** | `kernel_commit` 节点 + 派生关系（Fixes / revert / 修哪个 bug 等） |
| **discussion graph** | LKML 邮件层：thread / message / patch / review |
| **bug graph** | Bugzilla / gitee / atomgit issue + CVE + syzbot crash |
| **link 表** | 跨子图的关系表（commit↔bug / commit↔message / commit↔CVE 等 7 张） |
| **stack signature** | 栈签名 —— 把 call trace 归一化为 top-4 distinguishing function names 的 hash。同一 bug 在不同地方上报应得同 signature。算法见 `kg/signature.py` |
| **patch lineage** | 一个 fix 的传承链：`Fixes:` trailer + `revert` 信息 + backport status |

---

## F · 工程术语（项目内独有）

| 术语 | 含义 |
|---|---|
| **vibe code** | 本交付包的理念：用结构化设计文档替代源代码，让 AI 编程智能体能重建等价系统 |
| **Phase 0-6** | kernel.md prompt 里的 6 步调研协议（详见 `40_BEHAVIORAL_CONTRACTS.md § 1`） |
| **ReAct** | Reason + Act —— 智能体范式，LLM 在"推理"和"调工具"间循环 |
| **SOP** | Standard Operating Procedure，按 fault_kind 分类的诊断脚本（M7 选用） |
| **route** | 诊断路由 ∈ {kernel, kernel+vmcore, hardware, change, unknown}，决定 ReAct 暴露哪些工具子集 |
| **TERMINATION_FOOTER** | 所有 5 个 route prompt 末尾追加的强制规则段（详见 `40_BEHAVIORAL_CONTRACTS.md § 4`） |
| **process compliance KPI** | M8 主指标——"agent 是否按 KG 路径调研"（确定性，可复测）；对应"答案是否对"（LLM-noisy）的副指标 |
| **Form A/B/C** | Fix Recommendation 的三种形态：A 干净 backport / B revert 警告 / C 无 commit fix（配置/硬件/by-design） |
| **Path C rescue** | bind_claims 算法：当 LLM 给的 evidence_refs 通不过 overlap 阈值时，**自动扫整个 evidence pool** 找最强 overlap 的 evidence 补 attach |
| **force_finalize** | ReAct loop 在 token > 85% budget 或 step == max_iter 时启动的 last-step 强制收尾机制 |
| **DSML 幻觉** | DeepSeek 在 tool_choice="none" 时会输出 `<｜｜DSML｜｜tool_calls>` 内部 token 串（见 `60_GOTCHAS.md` §4.1） |
| **always-fire-all (ADR-013)** | M5 检索策略：7 路 BM25 无条件全部并发，不做选择性路由 |

---

## G · 外部数据源

| 来源 | URL / 协议 | 入库表 |
|---|---|---|
| **lore.kernel.org** | Atom feed + per-msg raw（**不是** mbox） | `lkml_message` |
| **bugzilla.kernel.org** | REST API `/rest/bug` | `bug` (source='kernel.org') |
| **gitee** | `api/v5/repos/openeuler/kernel/issues` | `bug` (source='gitee') |
| **atomgit** | issue REST | `bug` (source='atomgit') |
| **syzkaller.appspot.com** | HTML scraping（无 REST） | `syzbot_crash` |
| **NVD** | `services.nvd.nist.gov/rest/json/cves/2.0` | `cve` |
| **OLK kernel git** | local clone (`atomgit.com/openeuler/kernel.git`) | `kernel_commit`（Phase A） |
| **linux-stable git** | local clone（清华镜像或 BFSU） | `kernel_commit`（Phase B mainline） |

---

## H · MCP / Tool

| 术语 | 含义 |
|---|---|
| **MCP** | Model Context Protocol —— Anthropic 提出的工具调用标准。本项目用 SSE + JSON-RPC over HTTP |
| **CodeGraph** | 外部 MCP server（`localhost:8080/mcp`），提供 Zoekt BM25 + SCIP 符号索引 |
| **SCIP** | Source Code Intelligence Protocol —— Sourcegraph 提出的精确符号索引格式 |
| **Zoekt** | Google 开源的代码 BM25 索引（CodeGraph 内部用） |
| **`route=kernel`** | 暴露 26 个工具（_K 集合，详见 `30_TOOL_CONTRACTS.md`） |

---

## I · 性能 / 限流术语

| 术语 | 含义 |
|---|---|
| **RPM** | Requests Per Minute（vendor API 限流单位） |
| **TPM** | Tokens Per Minute |
| **sliding window limiter** | 限流算法：滚动 N 秒窗口内的总请求数。M1 用 endpoint 级共享 |
| **PG cache** | LLM 响应的 PostgreSQL 缓存（30 天 TTL，key = `sha256(model + messages)`） |
| **force_finalize threshold** | tokens > TOKEN_BUDGET * 0.85 即触发 |
| **MAX_ITER** | ReAct loop 硬迭代上限 = 15 |
| **TOKEN_BUDGET** | 单次诊断累积 token 上限 = 250,000 |
| **REPEAT_LIMIT** | 同 (tool, args) 重复 / 失败 N 次 → 强制退出 = 3 |

---

## J · 评测术语

| 术语 | 含义 |
|---|---|
| **case** | `eval/data/cases_*.json` 里的一条测试 |
| **expected_kg_paths** | case 字段，列出"理性 agent 应走的工具集"，用于 process compliance 打分 |
| **avg_kg_path_coverage** | 主 KPI：`mean((actual_tools ∩ expected_kg_paths) / |expected_kg_paths|)` |
| **recall@10** | 副指标：`expected_commit_hashes ∩ top10_evidence_hashes / |expected|`（short-SHA prefix 匹配） |
| **groundedness** | bind_claims 输出：`grounded` (>= 50% claims verified) / `speculative` / `n/a` |
| **dual judge** | M8 LLM-as-judge 用两个不同 prime 的 prompt 判同一答案，必须同 YES/NO 才算确定 |

---

> AI agent 读完本表后，应对所有缩写有上下文。如果遇到本表未列的 OLK / 内核 / 项目特有术语，应优先看 `60_GOTCHAS.md` 是否提及，再看具体模块的 `M*.md`。

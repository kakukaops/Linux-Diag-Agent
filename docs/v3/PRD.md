# Linux-Diag-Agent v3 — 产品需求文档 (PRD)

| 字段 | 值 |
|---|---|
| 产品名 | Linux-Diag-Agent |
| 版本 | v3 |
| 状态 | Design Draft（2026-06-02，待客户评审） |
| 关联文档 | [v2/PRD.md](../v2/PRD.md) · [v2/Architecture.md](../v2/Architecture.md) · [v1/PRD.md](../v1/PRD.md) |
| 驱动来源 | 2026-06-02 客户访谈 |
| 主要变更 | 引入"故障定界"作为 P0 能力；强化"未知问题诊断"产品化输出 |

---

## 0. TL;DR

**v3 不是 v2 的重构，是 v2 的产品形态升级**。v2 把"内核工程师诊断助手"做完了；v3 把它**转向 OS SRE 团队的日常作业平台**。

两个客户痛点驱动：
1. **故障定界（"这是不是我的锅"）**——OS SRE 团队 80% 时间消耗在"判定 + 自证 + 移交"上。v2 已有 hardware-first 路由 + Form C "非 commit fix"出口，但**没把"定界"作为头等输出**。v3 升级。
2. **未知问题诊断加速（"没 SOP 的疑难杂症"）**——已被 v2 的 ReAct + Phase 0-6 + KG-silent fallback 覆盖；v3 做产品化包装（不再大改架构）。

---

## 1. 背景与目标

### 1.1 v2 已交付

- 9 模块 + 32 工具 + 5 路由 prompt + 双语 web UI
- KG 数据：1.5M commits / 115K LKML / 16K CVE / 8K bugs / 7K syzbot + 7 张关系表 ~260K 边
- ReAct loop + Phase 0-6 调研协议 + 强制假设枚举 / 自我批驳 / 证据 trace
- M8 process compliance KPI（避免凑指标，保护工程师调研思路）
- 详见 [v2/ProjectStatus.md](../v2/ProjectStatus.md)

### 1.2 客户访谈关键发现（2026-06-02）

| # | 客户痛点 | 客户用工程师视角的描述 |
|---|---|---|
| **P1** | **故障定界** | "我们 OS SRE 每天接 100+ 事件，**60-80% 其实不是 OS 的问题**——可能是应用 OOM、可能是驱动 bug、可能是硬件失效——但用户都报到 OS 团队。我们花大量时间**自证清白 + 给下一棒证据**。" |
| **P2** | **未知问题诊断** | "已知故障我们有 SOP / runbook；**没 SOP 的问题**纯靠人脑 + 谷歌 + 内部 wiki，**单 case 平均 2-4 小时**。我们想让 LLM 介入加速。" |

### 1.3 v2 与客户需求的匹配度

| 痛点 | 匹配度 | 现状 | 差距 |
|---|---|---|---|
| **P2 未知问题诊断** | **95%** | ReAct + Phase 0-6 + KG silent fallback + Form A/B/C 已就绪 | 仅需 UI / 报告**产品化包装**，不动核心 |
| **P1 故障定界** | **60%** | 架构层有：hardware-first 路由（ADR-023）/ KG-silent 出口（ADR-024）/ Form C "非 commit fix" | **缺**第一类输出"定界结论 section" + 应用层信号 + 驱动层信号 + 容器维度 + 非 OS handoff 包 |

### 1.4 v3 解决什么

```
v3 = v2 + 故障定界产品化 + 应用/驱动/容器层取证 + handoff package
```

**不**改动：M1/M2/M5 等基础层；ReAct loop 内核；KG schema。
**新增**：M10 Fault-Boundary Verdict / 增强 Triage 信号 / Form D 报告输出。

### 1.5 产品定位变化（相对 v2）

| 维度 | v2 | v3 |
|---|---|---|
| 主用户 | 内核开发者 + 系统 SRE | **OS SRE 团队（日常作业平台）** |
| 报告头一句话 | "Fault: oom · Quality: grounded · ..." | **"定界：[应用层] · 责任：[业务团队] · 置信度：0.85"** |
| 报告价值 | 给出根因 + 修复 commit | **告诉值班"这是不是我的锅 + 不是的话给下一棒什么"** |
| 关键 KPI | process compliance / route accuracy | + **boundary accuracy（定界对了百分之多少）** |
| 输入侧 | dmesg / sosreport / 文本 | + **告警 webhook（来自客户监控系统）** |
| 输出侧 | Markdown / JSON 报告 | + **handoff package（取证包 PDF / tar.gz）** |

---

## 2. 用户故事

> v1 的 US-1~US-7、v2 的 US-8~US-13 仍然有效。v3 在此基础上**新增 US-14~US-18，全部 P0**。

### US-14 · 应用层 OOM 定界（最常见 case）

**Actor**：OS SRE 值班

**输入**：监控系统转过来的告警 + dmesg 片段
```
告警："Pod billing-service-7f9d8 OOM-killed"
dmesg: [12345.677000] oom-kill:constraint=CONSTRAINT_MEMCG,task=java,pid=8821
       [12345.677500] Out of memory: Killed process 8821 (java) anon-rss:4G ...
```

**当前体验**（v2）：诊断出 Form C，提示"调整 memory.max 或减小 Java 堆"。**没明确说这是应用问题。**

**v3 期望输出**：

```
## 故障定界                                                  [新增 section]

| 维度 | 判定 |
|---|---|
| 责任归属 | **应用层 · JVM 堆配置** |
| 置信度 | high (0.85) |
| 关键证据 | • task=java 用户态进程
           | • anon-rss 4.0G 超 cgroup memory.max 4.0G（限额）
           | • 触发路径 try_charge_memcg → mem_cgroup_out_of_memory 是 OOM killer 按设计执行
           | • 近 6 个月 OLK-6.6 该代码路径无 fix commit（排除 OS 回归） |
| 建议移交 | 业务 SRE / billing-service 应用 owner |
| OS 已附取证 | dmesg 完整片段 / cgroup memory.* 文件快照 / process_memory 详情 |

## 修复建议
**Form D — 非 OS 责任，建议移交**
[handoff package 链接]
```

**关键能力**：M10 定界节点 + 容器维度抽取 + handoff package 渲染

### US-15 · 硬件层定界（PCIe / DIMM / 网卡）

**Actor**：OS SRE 值班

**输入**：

```
dmesg: [pcieport 0000:00:01.0] AER: Corrected error received: id=0008
       [pcieport 0000:00:01.0] PCIe Bus Error: severity=Corrected, ...
       [pcieport 0000:00:01.0] device [8086:1572] error status/mask=0...
```

**v3 期望输出**：

```
## 故障定界
| 维度 | 判定 |
|---|---|
| 责任归属 | **硬件层 · PCIe 链路** |
| 置信度 | high (0.90) |
| 关键证据 | • PCIe AER Corrected Error，device 0000:00:01.0
           | • 同 device 已累计 N 次 AER（dmesg 历史回看）
           | • 内核 PCIe 驱动正常工作（仅 report 错误，未崩溃）
           | • 排除 OS 层：无相关 commit 修过该 AER 处理路径 |
| 建议移交 | 硬件 / 数据中心运维 |
| OS 已附取证 | dmesg AER 片段 / lspci 输出 / IPMI SEL 日志 |
```

### US-16 · 驱动层定界（out-of-tree 模块）

**Actor**：OS SRE 值班

**输入**：

```
dmesg: Tainted: G        W   OE     6.6.0-21.0.0.21.oe2403.x86_64
       Modules linked in: nvidia(OE) nvidia_uvm(OE) ...
       BUG: kernel NULL pointer dereference, address: 0000000000000018
       RIP: 0010:nvidia_drv_release+0x42/0x80 [nvidia]
```

**v3 期望输出**：

```
## 故障定界
| 维度 | 判定 |
|---|---|
| 责任归属 | **驱动层 · 第三方 GPU 驱动 (nvidia)** |
| 置信度 | high (0.92) |
| 关键证据 | • Taint 字母 O + E：out-of-tree + unsigned 模块
           | • 崩溃 RIP 在 nvidia 驱动符号内 (nvidia_drv_release)
           | • Modules linked in: nvidia(OE) 显示当前加载第三方驱动
           | • 排除 OS 层：upstream OLK 内核 6.6.0 无相关 NULL deref fix |
| 建议移交 | GPU 驱动厂商 (NVIDIA) / 业务团队（驱动版本是否兼容） |
| OS 已附取证 | 完整 taint 解析 / 驱动版本 / 加载顺序 / 崩溃栈 |
```

### US-17 · 容器维度归因（PID → Pod → Deployment）

**Actor**：OS SRE 值班，看到 `task_memcg=/kubepods.slice/kubepods-burstable.slice/.../pod0e3...`

**v3 期望输出**：

```
## 故障定界
| 维度 | 判定 |
|---|---|
| 责任归属 | **应用层 · Pod billing-service-7f9d8 / Deployment billing-service** |
| Pod | billing-service-7f9d8 (namespace=billing, node=worker-23) |
| Owner | (按客户 k8s annotation 标准抽，如 owner-team=billing-sre) |
| 关键证据 | • cgroup 路径解析：kubepods.slice/.../pod0e3... → pod billing-service-7f9d8
           | • 该 deployment 近 7 天 OOM 事件 4 次（dmesg_event 表回看）
           | • Pod 内 task=java，进程级 oom_score_adj=0 |
| 建议移交 | billing-sre 团队 |
```

### US-18 · 真 OS 问题（确认是我们的责任）

**Actor**：OS SRE 值班

**输入**：dmesg 含 oops + 栈帧全在 mainline kernel 函数上 + taint 干净

**v3 期望输出**：

```
## 故障定界
| 维度 | 判定 |
|---|---|
| 责任归属 | **OS 层 · 内核 bug** |
| 置信度 | medium (0.72) |
| 关键证据 | • Taint 干净（无 O / G / E / M）
           | • 栈帧全在 mainline 函数符号内
           | • 候选 commit abc123 (mainline 6.6.5) 修复了同栈帧 use-after-free
           | • 该 commit 当前未 backport 到 OLK-6.6 |
| 责任团队 | **OS SRE 自己**（你的锅） |
| 见下方根因分析 + 修复建议 |

## 根本原因 / 修复建议 / 置信度 / 证据来源
(原 v2 Form A/B/C 完整内容，**接在定界结论之后**)
```

---

## 3. 功能需求

### 3.1 P0（v3.0 必交付）

| ID | 功能 | 来源 | 工时估计 |
|---|---|---|---|
| **F-V3-1** | **M10 · 故障定界节点**：在 ReAct 完成后、bind_claims 之前插入。输入 react_final_answer + tool_trace + state，输出 4 维度判定：`responsibility ∈ {os, application, driver, hardware, unknown}` + `confidence` + `evidence_list` + `handoff_target` | P1 | 1.5 PD |
| **F-V3-2** | **报告头部 "故障定界" section**：所有 case 必有，置顶（在 ## 根本原因 之前） | P1 | 0.5 PD |
| **F-V3-3** | **应用层信号抽取**：dmesg task=<comm> + cgroup path + oom_score_adj + anon-rss vs limit；扩展到 Python/Go/Node runtime 关键词 | P1 | 1 PD |
| **F-V3-4** | **驱动层信号抽取**：taint O/E/G + Modules linked in 解析 + 第三方驱动名单 + 栈帧符号归属（在第三方驱动 .ko 内 → 驱动嫌疑） | P1 | 1 PD |
| **F-V3-5** | **容器维度抽取**：`task_memcg=/kubepods.slice/...` 解析出 pod / namespace / deployment | P1 | 0.5 PD |
| **F-V3-6** | **Form D · 非 OS handoff package**：当 responsibility ≠ os 时，渲染 markdown handoff package（含完整证据 + 建议给下一棒说什么 + OS 已排除什么） | P1 | 1 PD |
| **F-V3-7** | **20 个回归 case 标注 boundary GT**：cases_v2 / 新增 cases 都加 `expected_responsibility` 字段；M8 评测加 `boundary_accuracy` 主 KPI | P1 | 1 PD |

P0 合计 **~6.5 PD**。

### 3.2 P1（v3.1 交付）

| ID | 功能 |
|---|---|
| F-V3-8 | **告警 webhook 入口**：HTTP `/api/from-alert`，接客户监控系统标准化告警 schema |
| F-V3-9 | **handoff package 导出为 PDF / tar.gz**：含 dmesg / cgroup / 进程详情 / sosreport 子集 |
| F-V3-10 | **应用层 runtime 取证**（JVM heap dump / Python tracebacks / Go pprof）—— 需要应用 SRE 配合 |
| F-V3-11 | **驱动层"已知问题"数据库**：维护一份第三方驱动 known-issue 列表（nvidia / mlx / 华为 hns 等） |
| F-V3-12 | **历史趋势分析**：同一 pod / cgroup / device 近 N 天事件回看（依赖 dmesg_event 表） |

### 3.3 P2（v3.2+ 方向，不细化）

- 监控指标接入（Prometheus）做更精确 boundary 判定
- 客户 internal wiki / runbook 集成，未知问题 fallback 时查内部知识
- 多人协作 / 审计 / RBAC
- 答疑机器人模式（用户后续追问能记住前一次诊断）

---

## 4. 关键设计：M10 · 故障定界节点

### 4.1 在 LangGraph 拓扑中的位置

```
parse_input → extract_events → detect_taint_and_hw_signals →
   ↓                                                    ↑
   (M10 新增逻辑：识别应用 / 驱动 / 容器信号)            │
   ↓                                                    │
classify_fault_and_route → retrieve → react_investigation
   ↓
M10 · fault_boundary_verdict                          [新增节点]
   ↓
bind_claims → generate_report
```

M10 输入 `state`（已含 react_final_answer / tool_trace / 全部 triage 信号），输出新的 4 字段：

```python
state["boundary_verdict"] = {
    "responsibility": "application",     # os | application | driver | hardware | unknown
    "confidence": 0.85,
    "evidence": [
        "task=java 用户态进程",
        "anon-rss 4.0G 超 cgroup memory.max 4.0G",
        ...
    ],
    "handoff_target": "业务 SRE / billing-service owner",
    "os_already_excluded": [
        "近 6 个月 OLK-6.6 该路径无 fix commit（排除 OS 回归）",
        ...
    ],
}
```

### 4.2 判定算法

**确定性优先**（不依赖 LLM 决策，确保可复测）：

| 触发条件 | responsibility | confidence | 备注 |
|---|---|---|---|
| `has_hardware_signal == True`（MCE / EDAC UE / Hardware Error） | `hardware` | 0.90 | 已有 |
| 崩溃 RIP 在 `[<驱动名>]` 标记的栈帧内 + taint 含 `O/E` | `driver` | 0.92 | 新增 |
| `task_memcg` 含 `kubepods.slice` + task 是用户态 comm（java/python/node...） | `application` | 0.85 | 新增 |
| `task=<comm>` 是已知用户态进程 + anon-rss 超 cgroup limit + 调用栈正常 OOM 路径 + 无内核 regression | `application` | 0.80 | 新增 |
| ReAct 选了 Form C 且 fault summary 含 "by-design" 关键词 | `os` (但属配置/设计问题) | 0.75 | 已有 |
| ReAct 选了 Form A（有 backport 候选）+ taint 干净 | `os` | 0.80 | 已有 |
| ReAct 选了 `<insufficient_evidence>` | `unknown` | 0.30 | 已有 |
| 其它 | LLM fallback 判定 | 0.50 | 兜底 |

**LLM fallback**：上述确定性规则都不命中时，用 navigator LLM 判定一次。prompt 见 § 4.3。

### 4.3 LLM fallback prompt（仅在确定性规则不命中时调用）

```
你是一位 OS SRE 资深工程师。下面是一次故障诊断的完整 trace，请判断
这个故障的**责任归属**：

OS 层（内核 bug / 配置 / 性能 / 设计）
应用层（用户态进程 bug / 配置 / 负载）
驱动层（第三方驱动 / out-of-tree 模块）
硬件层（DIMM / PCIe / NIC / 存储）
不明

诊断 trace:
{react_final_answer 摘要}

dmesg 信号:
- taint = {taint_flags}
- hardware_signals = {hardware_signals}
- io_hang_signals = {io_hang_signals}
- task_memcg = {task_memcg_path}
- 最近改过 fault function 的 OLK commit 数 = {commit_count}

返回 JSON: {
  "responsibility": "application" | "os" | "driver" | "hardware" | "unknown",
  "confidence": 0.0 - 1.0,
  "evidence": ["原因 1", "原因 2", ...],
  "handoff_target": "建议移交团队",
  "os_already_excluded": ["OS 排除依据 1", ...]
}
```

### 4.4 Form D handoff package 模板

```markdown
# 故障移交包 · <fault_kind> · <pod / cgroup / device>

## 1. 定界结论

[M10 boundary_verdict 内容直接渲染]

## 2. OS SRE 已完成的取证

### 2.1 现象证据
- 完整 dmesg 片段（剥时间戳前缀 + 解析事件后）
- task 详情：comm / pid / uid / oom_score_adj / anon-rss / total-vm
- cgroup memory 配置快照（memory.max / memory.high / memory.events）
- 容器维度（如适用）：pod / namespace / deployment / owner

### 2.2 OS 层排除证据
- 近 6 个月 OLK-{kernel_version} 相关代码路径的 commit 历史
- 已知 syzbot / CVE / Bugzilla 中类似栈帧的对比
- 内核版本：{kernel_version}，OLK tag：{olk_version_tag}
- 内核 taint：{taint_flags}（{taint_meaning}）

## 3. 我们需要下一棒（{handoff_target}）反馈

[根据 responsibility 自动生成清单]
- application: -Xmx / GC log / 工作负载变更 / 应用版本
- driver: 驱动版本 / 厂商已知问题列表
- hardware: 同 device 历史告警 / IPMI SEL / 替换计划

## 4. 反馈联系

OS SRE 团队 + 本次诊断的可重现命令
```

---

## 5. 非功能需求

### 5.1 性能

- M10 boundary 节点：< 200 ms（确定性规则路径）；< 5 s（LLM fallback）
- 单次诊断总耗时不增加超过 5 % vs v2
- handoff package 渲染：< 1 s

### 5.2 可观测

- 每次 boundary verdict 写入 `dmesg_event.metadata` JSONB
- 评测时 `boundary_accuracy` 进 summary.json 顶层

### 5.3 兼容性

- v3 报告 schema 是 v2 的**超集**——多一个 `boundary_verdict` 字段；老 v2 客户端忽略此字段不影响
- v2 case JSON 不需要改；`expected_responsibility` 字段是新增可选字段，老 case 没有就跳过此 KPI

---

## 6. 范围与不范围

### 6.1 v3 范围

- 故障定界 4 象限（OS / App / Driver / Hardware）+ Unknown
- 应用层 / 驱动层 / 容器维度 / 硬件层信号抽取
- handoff package 渲染（Markdown，P0；PDF/tar.gz 推到 P1）
- 评测 KPI 新增 boundary_accuracy

### 6.2 v3 **不**范围

- **不**做应用层深度诊断：JVM heap dump 解析 / Python traceback 分析等留给应用 SRE
- **不**做硬件深度诊断：替换决策由数据中心硬件团队做
- **不**做 incident 全生命周期（severity / mitigation / postmortem）—— 我们只到"定界 + 取证移交"为止
- **不**与客户监控系统做深度集成（仅 webhook 入口，P1）
- **不**做多人协作 / RBAC / 审计（P2）

---

## 7. 验收准则

| 维度 | v2 基线 | v3 目标 |
|---|---|---|
| process compliance | ≥ 0.80 | ≥ 0.80（不退步） |
| route accuracy | ≥ 0.85 | ≥ 0.85（不退步） |
| **boundary accuracy（新）** | — | ≥ **0.80** on 20+ 标注 case |
| 报告含 "故障定界" section | 0 / 22 | **22 / 22** |
| Form D 渲染（非 OS case） | — | 当 responsibility ≠ os 时**必有** handoff package |
| 用户接受度（客户内测） | — | 客户值班 SRE 试用 1 周后 NPS ≥ +30 |

### 7.1 测试 case 扩充计划

22 个 v2 case 全部加 `expected_responsibility` 字段，并新增 **15 个 v3 case**：

| 类别 | 数量 | 责任分布 |
|---|---|---|
| 应用层 OOM（含容器） | 5 | application |
| 驱动层（out-of-tree GPU / NIC / 厂商存储） | 4 | driver |
| 硬件层（PCIe AER / DIMM ECC / NIC 链路 / 存储 hangs） | 4 | hardware |
| OS 真问题（kasan / lockdep / panic）混杂干扰信号 | 2 | os |

新 cases 落在 `eval/data/cases_v3.json`。

---

## 8. 实施路径

### 8.1 v3.0（P0 全部）

| 周 | 工作 | 工时 |
|---|---|---|
| 1 | M10 boundary 节点 + 确定性规则 + LLM fallback | 1.5 PD |
| 1 | 应用/驱动/容器维度信号抽取（F-V3-3/4/5） | 2.5 PD |
| 2 | 报告 section + Form D handoff 模板（F-V3-2/6） | 1.5 PD |
| 2 | 15 个新 v3 case + 22 个 v2 case 加 boundary 标注 + boundary_accuracy KPI（F-V3-7） | 1 PD |
| 总计 | | **~6.5 PD** |

### 8.2 v3.1（P1）

时间表待 v3.0 客户内测反馈后再细化。

### 8.3 v3.2+ （P2 方向）

不在本 PRD 细化。

---

## 9. 关键 ADR（新增 / 修订）

| 新 ADR | 内容 |
|---|---|
| ADR-V3-1 | M10 boundary 节点优先用确定性规则，LLM 仅作 fallback —— 确保 boundary verdict 可复测 |
| ADR-V3-2 | Form D handoff package 是 markdown，PDF/tar.gz 推到 P1 —— 控制 P0 范围 |
| ADR-V3-3 | boundary_accuracy 加入 M8 主 KPI（与 process compliance 并列）—— 客户视角的核心指标 |

| 修订 ADR | 变化 |
|---|---|
| ADR-023 修订 | hardware-first 路由保留，但新增 boundary verdict 作为**显式输出**（之前隐含在 Form C） |
| ADR-024 修订 | "无法诊断"出口仍是 `<insufficient_evidence>`；但 v3 这种情况下 boundary = `unknown`，仍给一份**最小取证包**给下一棒 |

---

## 10. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 定界判错（特别是应用 vs OS 边缘 case） | M10 优先用确定性规则（保守判 `unknown` 也比误判好）；评测 case 含 2 个故意"混杂干扰信号" |
| LLM fallback 不稳定 | confidence < 0.6 时 boundary = `unknown` + 用户决策 |
| 客户监控系统接入复杂 | F-V3-8 webhook 推到 v3.1，v3.0 不依赖外部集成 |
| 应用层 / 驱动层取证浅 | 明确 scope：v3 只到"定界 + 取证清单" + handoff，不下到 application internals |
| 二次开发成本 | M10 是 LangGraph 拓扑里的一个新节点，对 v2 是**纯添加**；boundary verdict 是 schema **新增 optional 字段**，老 v2 客户端忽略不影响 |

---

## 11. 与客户访谈映射

| 客户原话（2026-06-02） | v3 对应交付 |
|---|---|
| "60-80% 事件其实不是 OS 的问题" | 报告头一行就明确告诉值班 SRE "这是不是你的锅" |
| "大量时间花在自证清白" | Form D handoff package 里"OS 已排除证据"段，省值班 SRE 自己写 |
| "需要 OS 协助提供取证" | handoff 包含完整 dmesg / cgroup / 进程 / 排除证据 |
| "没 SOP 的疑难问题耗时长" | **v2 已经做了**：ReAct loop + Phase 0-6 + KG silent fallback；v3 不动 |
| "希望 LLM 加速" | v2 LLM 已在 ReAct 里加速；v3 给 LLM **更明确的产品形态**（定界先行） |

---

## 12. 下一步

1. 客户评审本 PRD（建议 1 周窗口）
2. 评审通过 → 启动 v3.0 实施（~2 周）
3. v3.0 完成 → 客户值班 SRE 内测 1 周
4. 收反馈 → 启动 v3.1（P1）

> 联系：[本项目维护团队]
> 客户接口：[OS SRE 团队负责人]

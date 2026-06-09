# Linux-Diag-Agent v4 — 产品需求文档 (PRD)

| 字段 | 值 |
|---|---|
| 产品名 | Linux-Diag-Agent |
| 版本 | v4 |
| 状态 | Design Draft（2026-06-05，待评审） |
| 关联文档 | [v4/Architecture.md](Architecture.md) · [v4/WBS.md](WBS.md) · [v3/PRD.md](../v3/PRD.md) · [v2/PRD.md](../v2/PRD.md) |
| 驱动来源 | 2026-06-04 资深内核专家代码级 review（见本文 §1.2） |
| 主轴 | **延续 SRE 定界平台 + 补强证据可信度** |

---

## 0. TL;DR

**v4 不引入新概念，是一个"兑现 + 接通 + 验证"的版本。**

v1→v3 把架构、ReAct 化、取证层脚手架、定界 PRD 都设计好了；但一次代码级 review 发现：**文档写了、代码也写了，关键能力却没接通、没被执行、没被验证**。v4 的全部工作就是把三件"已设计未兑现"的东西做实，并第一次给它们配上**硬验收门槛**：

| v4 P0 | 一句话 | 当前真实状态 |
|---|---|---|
| **P0-1 证据可信度兑现** | 让"每条结论都能 trace 到本会话工具输出"从契约变成被强制执行的事实 | `bind_claims` 已迭代三轮（v2.1 重叠校验 / v2.2 auto-attach / v2.3 ReAct 证据并入），但 eval 实测 `verified_claim_rate=0`、`traceability=0`、`groundedness=speculative` **全线为零** |
| **P0-2 取证层激活** | 让栈解码/drgn 在真实诊断里被用起来，把"符号名"钉成"源码行→git blame→commit" | `mcp_servers/crash_forensics/` 代码已存在（decode 200 行 / drgn 371 行 / fetch_debuginfo 147 行），但 vmlinux 供给未通、prompt 未强制、drgn 仅 gated 在 `kernel+vmcore` 路由，eval 里**从未被触发** |
| **P0-3 定界 Form D 落地** | 把 v3 的招牌功能"故障定界（这是不是我的锅）"从 PRD 变成报告里真实输出 | v3 PRD 把定界列为 P0，但全仓 `grep boundary\|定界\|Form D\|handoff` 在 `agent/` 下**无任何实现**；`renderer.py` 只有 diagnosed/insufficient verdict |

| v4 P1 | 一句话 |
|---|---|
| **P1-1 上游链闭合** | mainline + linux-stable 入库，补 ~60K 悬空 upstream SHA，让 `Fixes:`/backport 链完整——backport 决策的前提 |
| **P1-2 延迟/成本治理** | stack_signature 命中即短路 ReAct loop；triage 用便宜模型。把单次 ~10min / 192K token 砍下来，匹配 SRE 日常平台定位 |

---

## 1. 背景与目标

### 1.1 v3 已交付（与未交付）

v3 PRD 把产品从"内核工程师助手"升级为"**OS SRE 日常作业平台**"，P0 是"故障定界"。**架构层已就绪**（hardware-first 路由 ADR-023、KG-silent 出口 ADR-024、Form C 非-commit-fix），ReAct 主路径也已落地：

```
parse_input → extract_events → detect_taint_and_hw_signals
  → classify_fault_and_route → retrieve(M5) → react_investigation(ReAct loop)
  → bind_claims → generate_report → write_dmesg_event → END
```
（见 `agent/graph.py`，M22 改造已完成；AgentThink.md 中"当前是固定流水线"是改造**前**的分析。）

**但 v3 的招牌功能"故障定界"在代码里没有落地**（详见 §1.2 发现③）。v3 状态条本身也标注为"Design Draft，待客户评审"。

### 1.2 代码级 review 的三个发现（v4 的驱动）

> 区别于 v3 之前基于文档快照的评审，本次 review 直接读 live 代码 + eval 真实数值。

**发现①：证据 grounding 实际等于零——行为契约是一纸空文。**

`40_BEHAVIORAL_CONTRACTS` 白纸黑字要求"`### 证据来源` 里每个 ID 必须来自本会话工具输出，否则硬性违规"。但 `eval/results_v2/` 真实数值：

```
oom-001 : verdict=diagnosed  recall=0.0  traceability=0.0  verified_claims=0  speculative=3
kasan-001: verdict=diagnosed  recall=0.0  traceability=0.0  verified_claims=0  speculative=14
io-hang-1: verdict=diagnosed  recall=1.0  traceability=0.0  verified_claims=0  speculative=10
summary : grounded_rate=0.0   speculation_rate=1.0   avg_verified_claim_rate=0.0
```

最刺眼的是 io-hang-001：`recall=1.0`（evidence pool 里**有**对的 commit）但 `traceability=0`（最终答案**没扎进**那条证据）。`bind_claims` 已迭代三轮仍全线为零，说明链路里某处真的断了（候选根因见 §4.2）。**当前输出本质是"披着 SOP 外衣的 LLM 猜测"，不是"证据 trace 出来的诊断"。这是信誉根基的塌方。**

**发现②：取证层写了但休眠/不被用。**

`agent/react/tools/vmcore_tools.py` 把 `crash_forensics` 包成了工具，`decode_stacktrace` 的 `routes` 实际含 `{kernel+vmcore, kernel, unknown}`（纯 dmesg 路由本来就能调）。但 eval 的 `react_tools_used` 里它从未触发。原因是组合性的：(a) 要 vmlinux，而 `fetch_debuginfo` 依赖 OLK 源码树 build 出的 `vmlinux`，环境多半没有；(b) prompt 没把栈解码列为必做步；(c) `drgn` 那套 gated 在 `fault_kind∈{panic,oops} 且有 vmcore_path`，eval 不带 vmcore 就永不触发。**最能把"符号名→源码行→commit"的能力目前是死的，这正解释了发现①里 recall 上不去。**

**发现③：v3 招牌功能"故障定界"在代码里基本没落地。**

全仓 `grep -rn "boundary|定界|Form D|handoff|fault_boundary|M10" --include=*.py agent/` 无命中；`agent/report/renderer.py` 只有 diagnosed/insufficient 的 verdict，没有任何定界结论 / 责任归属 / handoff 包输出。v3 设计完成，但 P0 功能的代码没落地。

### 1.3 v4 解决什么

```
v4 = v3（设计） + 证据可信度兑现 + 取证层激活 + 定界落地 + （上游链 + 延迟治理）
```

**不**改动：M1 LLM 抽象、M2 schema 主干、M5 检索主干、ReAct loop 内核拓扑。
**做实**：bind_claims/grounding 链路、crash_forensics 接入、Form D 报告输出。
**补齐**：mainline/stable ingest、signature 快路径。

### 1.4 产品定位（相对 v3）

| 维度 | v3 | v4 |
|---|---|---|
| 主用户 | OS SRE 团队 | 不变 |
| 报告头一句话 | "定界：[应用层] · 责任：[业务团队] · 置信度：0.85"（**设计**） | 同上，但**真实落地**，且每条证据可点开看 trace |
| 核心痛点 | 这是不是我的锅 | 不变 |
| **可信度** | 行为契约要求 grounding，但**未执行** | **grounding 被强制执行 + 设为验收门槛** |
| 关键 KPI | process compliance / route accuracy / boundary accuracy | + **verified_claim_rate ≥ 阈值（首次成为硬门槛）** |

---

## 2. 用户故事（v4 新增 / 强化）

> v1 US-1~7、v2 US-8~13、v3 US-14~18 仍有效。v4 不新增业务场景，而是让既有场景"输出可信 + 证据可查 + 定界真出"。

### US-19 · 工程师对每条结论追问"凭什么"（P0-1）

**Actor**：OS SRE / 内核工程师
**输入**：任意一次诊断报告
**期望**：报告里 `## 证据来源` 的每个 commit/bug/CVE ID，都能在折叠的"工具调用轨迹"里找到产出它的那次工具调用；点开能看到 `result_preview`。报告头标注 `证据可信度：grounded（4/5 条已校验）` 或 `speculative（仅 1/6 条可校验，其余为先验推断）`，**用户一眼知道这份结论敢不敢信**。

### US-20 · 拿到 oops 直接钉到源码行（P0-2）

**Actor**：值班 SRE
**输入**：一段含 `RIP: 0010:ext4_xattr_..+0x4a/0x80` 的 dmesg（无 vmcore）
**期望**：agent 在 Phase 1.5 自动调 `decode_stacktrace`，把 `func+0x4a/0x80` 解到 `fs/ext4/xattr.c:1623`，再用该行去 `find_commits_touching_symbol` / `git blame` 锁定最近改动它的 commit。**偏移信息不再被浪费。**

### US-21 · 定界结论真实出现在报告头（P0-3）

**Actor**：OS SRE 值班
**输入**：`Pod billing-service OOM-killed` 告警 + dmesg
**期望**：报告头直接给 `定界：[应用层] · 责任：[业务团队 billing] · 置信度：0.82`，并附 Form D handoff 包（OS 层已排除的证据 + 需要下一棒反馈什么）。**而不是像现在这样只给一个 kernel-route 的根因分析。**

---

## 3. 功能需求

### 3.1 P0（v4.0 必交付）

| # | 需求 | 验收信号 |
|---|---|---|
| F-01 | **端到端打通证据 grounding 链路**：ReAct 工具输出 → 统一 evidence ledger → bind_claims 校验 → 报告可信度标注 | `verified_claim_rate` 在 22 case 上 ≥ 0.5（从 0 起步） |
| F-02 | **hash 形态归一**：短 hash（12）↔ 全 hash（40）在 ledger 匹配、引用、auto-attach 全程一致 | io-hang-001 这类"证据在池里"的 case，`traceability` > 0 |
| F-03 | **中文 claim 的 token 化修正**：grounding 重叠计算对"中文叙述 + 英文标识符"混排有效 | 中文报告 case 的 verified 率与英文 case 不再有数量级差异 |
| F-04 | **可信度作为验收门槛**：eval runner 把 `verified_claim_rate` / `grounded_rate` 升为**主门槛之一**（不再仅观测） | `eval/runner_v2.py` summary 输出门槛判定；低于阈值 CI/验收 FAIL |
| F-05 | **栈解码进主路径**：`decode_stacktrace` 在纯 `kernel` 路由作为 **Phase 1.5 必做步**（有 vmlinux 时） | coverage case 的 `react_tools_used` 含 `decode_stacktrace` |
| F-06 | **vmlinux/debuginfo 供给打通**：`fetch_debuginfo` 能在标准位置稳定找到对应版本 vmlinux（源码树 build 输出 / debuginfo 缓存 / sosreport 内） | 给定 OLK-6.6 case，`fetch_debuginfo` 返回有效 vmlinux 路径 |
| F-07 | **Form D 定界落地**：新增 M10 定界节点 + renderer 输出"定界结论 + 责任归属 + handoff 包" | US-21 case 报告头出现"定界："行；产出 Form D markdown |
| F-08 | **定界信号采集**：triage 阶段采集 应用层 / 驱动层（out-of-tree 模块）/ 容器维度信号，喂给 M10 | `detect_*` 节点输出 app/driver/container 信号字段 |

### 3.2 P1（v4.1 交付）

| # | 需求 |
|---|---|
| F-09 | mainline（torvalds/linux）+ linux-stable git 入库，补悬空 upstream SHA；`link_commit_*` 重建覆盖上游链 |
| F-10 | stack_signature 命中即短路：`find_similar_crashes` 高置信命中时跳过完整 ReAct loop，直接复用既有分析 |
| F-11 | triage 分级模型：triage/分类用便宜模型，仅综合阶段上贵模型（M1 role 已有 chat/navigator 之分，扩为可配模型） |

### 3.3 P2（v4.2+ 方向，不细化）

- vmcore 全链路产品化（drgn 容器化 + 自动触发，ADR-020 Phase B）
- 回归 bisect 辅助（"X 版本好、Y 版本坏" → 缩 commit 区间）
- 跨版本对比（OLK-6.6 vs 5.10 同一问题处理差异，CodeGraph 双 repo 已具备数据）

---

## 4. 关键设计 P0-1 · 证据可信度兑现

> 这是 v4 的信誉根基，必须最先做。本节定位问题、给出统一 evidence ledger 设计。

### 4.1 现状链路（已存在但断裂）

```
retrieve(M5)            → state["evidence"]（BM25 预检索池）
react_investigation     → tool_trace[{step,tool,args,result_preview}]
                        → _merge_react_evidence(existing, react_hashes)  ← v2.3：把 ReAct 抓到的 hash 查 DB 并回 evidence
bind_claims             → LLM 抽 claim + evidence_refs
                        → (a) ref ∈ evidence_by_hash/bug/msg ?（存在性）
                        → (b) claim 文本与证据 title+body token 重叠 ≥ 0.20 ?（语义）
                        → (c) v2.2 auto-attach：失败则全池扫最佳重叠兜底
                        → verified = (a) AND (b)
generate_report         → 按 groundedness 渲染
```

机制齐全，但 `verified_claim_rate=0`。**三轮加固仍归零 = 链路里有结构性断点，不是阈值调参问题。**

### 4.2 候选断点（v4 必须逐一证伪/修复）

| 断点假设 | 怎么验 | 修法 |
|---|---|---|
| **H1 hash 形态不一致** | `evidence_by_hash` 用全 hash 做 key（`nodes.py:224`），但工具输出/LLM 引用多为短 hash（12）→ `r in evidence_by_hash` 永不命中 | ledger 同时索引短/全 hash；引用归一到统一规范形 |
| **H2 merged 行 body 为空** | `_merge_react_evidence` 只查 subject，重叠计算 (b) 只有 title 无 body → 重叠分天然偏低 | merge 时带回 commit body/title 全文 |
| **H3 中文 claim token 化失效** | `_significant_tokens` 正则 `[a-zA-Z_][a-zA-Z0-9_]{3,}` **只取英文 token**；中文叙述里英文标识符稀少 → 重叠分母虚高、命中虚低 | 对中文 claim 抽取"内核标识符锚点"（函数名/CONFIG_*/CVE/错误码）单独匹配，不靠泛 token 重叠 |
| **H4 ReAct 抓到的 hash 没进 react_evidence_hashes** | loop 的 `seen_hashes` 抽取规则是否覆盖 `get_commit_detail`/`get_commit_diff` 的输出 | 统一在 dispatch 后从 result 抽取所有 commit/bug/cve 标识入 ledger（见 §4.3） |

### 4.3 v4 设计：统一 Evidence Ledger

把"散落在 M5 池 + ReAct trace + DB lookup"的证据收敛成**一个带来源的账本**，bind_claims 只对账本校验，行为契约的"每个 ID 必须来自工具输出"才第一次有据可依。

```
EvidenceLedger（state["evidence_ledger"]）
  entries: list[LedgerEntry]
    - canonical_id   规范 ID（commit→全hash；bug→bug#N；cve→CVE-xxxx；msg→message_id）
    - aliases        别名集合（短hash、URL 尾段、带/不带 c#… 前缀）
    - kind           commit | bug | cve | lkml | syzbot | code_line
    - title, body    全文（commit 用 subject+message；code_line 用 decode 出的 file:line + 源码片段）
    - origin         来源：{stage: retrieve|react, tool: <tool_name>, step: <n>}  ← trace 凭证
  index_by_alias: dict[str, LedgerEntry]   短/全 hash 与各别名统一入口
```

**写入时机**：
- `retrieve(M5)` 产出的池 → 全部入账，origin.stage=retrieve；
- ReAct loop **每次 dispatch 后**，从 `result` 用统一抽取器扫出所有 commit/bug/cve/message 标识 → 入账，origin={react, tool, step}；
- `decode_stacktrace` 命中的 `file:line` → 作为 `code_line` 入账（可被 claim 引用为源码级证据）。

**bind_claims 改造**：
- (a) 存在性：`ref` 经 `index_by_alias` 归一后查账本（覆盖 H1）；
- (b) 语义重叠：对账本条目用**全文 body**（覆盖 H2）+ **标识符锚点匹配**（覆盖 H3）；
- 新增 **trace 校验**：`### 证据来源` 里每个 ID 必须 `origin.stage` 可定位；否则该 claim 直接判 speculative，并在报告里标"⚠ 引用了未在本会话出现的 ID"。

### 4.4 验收（P0-1）

- 在 `cases_v2.json` 上 `avg_verified_claim_rate ≥ 0.5`、`grounded_rate ≥ 0.5`；
- io-hang-001（recall=1.0 的 case）`traceability > 0`；
- 至少 3 个中文 case 的 verified 率与对应英文 case 同量级；
- eval summary 新增 `grounding_gate: PASS/FAIL`。

---

## 5. 关键设计 P0-2 · 取证层激活

### 5.1 目标

让"符号名 → 源码行 → git blame → commit"成为**纯 dmesg 路由的默认动作**，不再依赖用户提供 vmcore。

### 5.2 改动点（均在既有代码上接通，非新建）

| 改动 | 文件 | 说明 |
|---|---|---|
| Phase 1.5 强制栈解码 | `agent/react/prompts/kernel.md` + `kernel.zh.md` | 在 Phase 1（符号反查）前插入："若 dmesg 含 `func+0xNN/0xMM` 形态偏移且能取到 vmlinux，**先**调 `decode_stacktrace` 把帧解到 file:line，再用行号/符号做 Phase 1" |
| vmlinux 供给 | `mcp_servers/crash_forensics/fetch_debuginfo.py` | 扩展查找：OLK 源码树 build 输出（已支持）+ debuginfo 缓存 + sosreport 内 + 配置项显式指定；找不到时返回明确"无 vmlinux，跳过解码"而非报错 |
| 解码结果入账 | bind_claims/ledger（§4.3） | `decode_stacktrace` 的 `file:line` 作为 `code_line` 证据入账，可被 claim 引用 |
| drgn 触发条件放宽 | `agent/triage/nodes.py::_select_route` | `kernel+vmcore` 仍需 vmcore_path；但 vmcore 存在时即便 fault_kind 非 panic/oops 也允许（hung_task/softlockup 最需要 `locks` query） |

### 5.3 验收（P0-2）

- coverage case（带 `func+0xNN` 偏移的 dmesg + 可用 vmlinux）`react_tools_used` 含 `decode_stacktrace`；
- 解出的 `file:line` 出现在报告证据来源；
- 无 vmlinux 时优雅降级（不报错、不空转），日志记 `vmlinux unavailable, skip decode`。

---

## 6. 关键设计 P0-3 · 定界 Form D 落地

> 把 v3 PRD §4 的 M10 设计真正实现到代码 + 报告。

### 6.1 拓扑位置

在 `react_investigation` 之后、`bind_claims` 之前（或与之并行）插入 **M10 fault_boundary** 节点：

```
react_investigation → fault_boundary(M10) → bind_claims → generate_report
```

定界结论需要调研结果（根因/责任层证据）才能下，故放在 ReAct 之后；其结论与 claims 一起进 renderer。

### 6.2 判定算法（确定性优先 + LLM 兜底）

沿用 v3 PRD §4.2/4.3 思路，落到代码：

1. **确定性规则先行**（可测、可复现）：
   - hardware 信号（已有 ADR-023）→ `boundary=硬件层, 责任=硬件/固件团队`；
   - taint 含 `O`/`E`（out-of-tree / 未签名模块）+ 栈帧落在该模块 → `boundary=驱动层, 责任=驱动供应商`；
   - OOM 且 victim 在某 cgroup/Pod 且非全局内存压力 → `boundary=应用层, 责任=该业务团队`；
   - 命中 KG 中 OLK commit 且已修/可 backport → `boundary=OS层, 责任=我们`。
2. **LLM fallback**：规则不命中时，用专用 prompt 给定界 + 置信度（显式标 low/medium）。

### 6.3 报告输出（renderer 改造）

- 报告头新增一行：`定界：[<层>] · 责任：[<handoff_target>] · 置信度：<0-1>`；
- 非 OS 责任时，追加 **Form D handoff 包**（沿用 v3 PRD §4.4 模板）：定界结论 / OS 层已完成取证（现象证据 + 排除证据）/ 需要下一棒反馈什么 / 联系方式；
- OS 责任时，走原有 `## 根本原因 / ## 修复建议（Form A/B/C）/ ## 置信度 / ## 证据来源`。

### 6.4 验收（P0-3）

- US-14~18 对应的 5 个定界 case：`boundary_accuracy ≥ 0.8`（定界层判对的比例）；
- 非 OS case 产出 Form D markdown，含"OS 层排除证据"小节；
- 报告头"定界："行恒存在（含 OS 责任 case）。

---

## 7. 关键设计 P1

### 7.1 P1-1 上游链闭合

- 新增/扩展 ingester：mainline（`torvalds/linux.git`）+ `linux-stable.git` 增量入库（复用 `ingest/kernel_commit/` 的 git_wrapper / trailer_parser）；
- `graph/linker.py` 用 OLK commit 的 `upstream-commit:` trailer 把 OLK↔upstream 桥接，消化 ~60K 悬空 SHA；
- `check_backport_status` / `get_regression_fixes` 工具召回率随之提升。
- **代价**：首次全量 4-12h；增量 weekly。需评估存储（PG 行数 1.5M → 3M+）。

### 7.2 P1-2 延迟/成本治理

- **signature 快路径**：`find_similar_crashes` 返回高置信签名命中（同 stack_signature + 同子系统）时，短路整个 ReAct loop，直接复用历史分析 + 标注"复用已知签名分析"；
- **分级模型**：M1 已有 chat/navigator 角色之分，扩为"triage/分类用便宜模型，综合用贵模型"可配；
- 目标：常见已知签名 case 从 ~10min 降到 < 1min；token 中位数下降 ≥ 40%。

---

## 8. 非功能需求

| 维度 | 要求 |
|---|---|
| 性能 | P0 不得使常规 case 总耗时较 v3 上升 > 10%；P1 快路径 case < 60s |
| 可观测 | evidence ledger 的每条 origin 可在报告"工具调用轨迹"折叠区追溯；eval summary 输出 grounding/boundary 门槛判定 |
| 兼容 | 不破坏 v3 报告 JSON schema；新增字段（boundary / ledger）为增量，老消费者忽略即可 |
| 确定性 | M10 确定性规则部分对同输入必同输出；LLM fallback 部分按既有"答案非确定性可接受"约定 |
| 安全 | 取证层读 vmlinux/sosreport 仅读不写；drgn 仍只跑 `queries/` 下静态脚本（沿用 M9 §4，禁止 LLM 写任意 drgn 脚本） |

---

## 9. 验收准则

> v4 首次把"证据可信度"和"定界准确率"升为**硬门槛**。

| 门槛 | 指标 | v3 实测 | v4.0 目标 |
|---|---|---|---|
| **G1 证据可信度** | `avg_verified_claim_rate` | 0.0 | **≥ 0.5** |
| G1' | `grounded_rate` | 0.0 | ≥ 0.5 |
| G1'' | `traceability`（io-hang-001 等 recall=1 的 case） | 0.0 | > 0 |
| **G2 取证激活** | coverage case `react_tools_used` 含 `decode_stacktrace` | 否 | 是 |
| **G3 定界** | `boundary_accuracy`（US-14~18） | N/A（未实现） | ≥ 0.8 |
| G4 不回退 | `route_accuracy` | 1.0 | ≥ 0.85（不得跌破） |
| G5 不回退 | process compliance（phase_coverage） | — | ≥ 0.80 |

eval runner（`eval/runner_v2.py`）须输出 `gates: {G1:PASS/FAIL, ...}`，任一 P0 门槛 FAIL 即 v4.0 未达标。

### 9.1 测试 case 扩充

- 现 `cases_v2.json` 15-22 例、每类 n=1-3，统计意义弱；
- v4 扩到**每类 fault_kind ≥ 3 例**、定界 case（US-14~18 各 ≥ 2 例）；
- 新增至少 3 个**带原始偏移 `func+0xNN` 的 dmesg** case 用于 P0-2 验收；
- 新增 grounding 专项 case（已知正确 commit 在池中，断言它出现在证据来源）。

---

## 10. 实施路径

### 10.1 v4.0（P0 全部，建议顺序）

1. **P0-1 先行**（信誉根基）：先做 evidence ledger + bind_claims 改造 + grounding 门槛，**因为它是 P0-2/P0-3 证据能不能被信任的前提**；
2. **P0-2 并行可启**：vmlinux 供给 + prompt Phase 1.5 + 解码结果入账（依赖 ledger）；
3. **P0-3**：M10 节点 + renderer Form D + 定界信号采集；
4. case 扩充 + eval 门槛接线，跑全量验收。

### 10.2 v4.1（P1）

5. mainline/stable ingest + linker 上游链；
6. signature 快路径 + 分级模型。

### 10.3 v4.2+（P2 方向）

7. vmcore 产品化 / bisect 辅助 / 跨版本对比。

---

## 11. 关键 ADR（新增 / 修订）

| # | 标题 | 决策 |
|---|---|---|
| ADR-026（新） | **证据可信度为硬门槛** | `verified_claim_rate` 从观测副指标升为验收主门槛之一；低于阈值视为未交付。理由：行为契约要求 grounding，但 v3 实测为零 = 契约未执行 |
| ADR-027（新） | **统一 Evidence Ledger** | 证据收敛为单一带 origin 的账本，bind_claims 只对账本校验；trace 凭 origin 可追。替代 M5 池 + ReAct merge 分裂的现状 |
| ADR-028（新） | **栈解码进默认路径** | `decode_stacktrace` 在纯 kernel 路由作 Phase 1.5 必做（有 vmlinux 时）；修订 ADR-014（M6 v1 范围）"仅文件上传、无 vmcore"的保守边界 |
| ADR-029（新） | **M10 定界节点落地** | 把 v3 PRD §4 的 M10 从设计变实现，置于 react_investigation 之后；确定性规则优先 + LLM 兜底 |
| ADR-018 修订 | **上游链入库** | 由"mainline 仅辅、不入库"修订为 P1 入库 mainline+stable，消化悬空 upstream SHA（v3 因成本搁置，v4 P1 启动） |
| ADR-025 关联 | **KG-silent fallback 与 grounding** | first-principles 回落产出的 claim 必然 unverified；v4 要求这类 claim 在报告中**显式标 speculative + low confidence**，不得计入 verified |

---

## 12. 风险与缓解

| 风险 | 缓解 |
|---|---|
| grounding 修通后 verified 率仍上不去（根因比 §4.2 更深） | P0-1 第一步是**诊断而非修复**：先逐一证伪 H1-H4，确认真实断点再动手；给中间结论留观测埋点 |
| 把 grounding 设硬门槛后，agent 为达标变保守、`<insufficient_evidence>` 漂移上升 | 门槛设 0.5 而非 1.0；KG-silent fallback 仍允许，只是标 speculative；监控 abstention_rate 不得显著上升 |
| 取证层依赖的 vmlinux 在客户环境取不到 | F-06 要求"取不到则优雅降级"，不阻断主流程；把 vmlinux 供给作为部署 checklist 项写入 70_OPERATIONAL |
| 定界判错（把 OS 的锅甩给业务团队，反之亦然） | 确定性规则只覆盖高置信信号；不确定走 LLM + 标 low confidence；boundary_accuracy 门槛 0.8 留容错 |
| 上游链入库使 PG 体积/ingest 时间翻倍 | 列为 P1 而非 P0；先评估存储；可只入 stable 分支而非全 mainline 历史 |

---

## 13. 与 review 结论的映射

| review 发现 | v4 对应 |
|---|---|
| 发现① grounding=0，契约是空文 | P0-1（F-01~04）+ G1 门槛 + ADR-026/027 |
| 发现② 取证层休眠 | P0-2（F-05/06）+ ADR-028 |
| 发现③ 定界未落地 | P0-3（F-07/08）+ ADR-029 |
| review 优化④ 上游链断 | P1-1（F-09）+ ADR-018 修订 |
| review 优化⑤ 延迟 10min | P1-2（F-10/11）|

---

## 14. 下一步

1. 评审本 PRD + `Architecture.md` + `WBS.md`；
2. 按 §10.1 顺序，**P0-1 先做诊断（证伪 H1-H4）**，出一页"grounding 断点根因"小结再编码；
3. case 扩充与 eval 门槛接线同步推进，确保每个 P0 完成即可量化验收。

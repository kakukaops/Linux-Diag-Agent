# FAQ — Linux-Diag-Agent 常见问题

---

## Q4：智能体的诊断思路是否和一个有经验的内核工程师类似？图谱的作用是什么？

是的，**v2 的设计明确是模仿一个有经验的内核工程师**排查故障的思路。让我把"工程师做什么 / agent 怎么对应 / 图谱在哪里发挥作用"对照展开。

### 一、工程师排查 vs Agent 三阶段

| 工程师步骤 | Agent 阶段 | 实现 |
|---|---|---|
| ① 看一眼 log/现象，判断是什么类故障（OOM？oops？硬件？）| **Triage**（确定性）| `parse_input` → `extract_events` → `detect_taint_and_hw` → `classify_fault_and_route` |
| ② 形成假设（"看着像内存碎片"/"像 use-after-free"）| **ReAct loop 启动** | 路由感知 system prompt 告诉 LLM 工具优先级 |
| ③ 查代码：那个 call trace 函数在干啥？相关子系统？| ReAct 工具 D | `search_code`、`get_function_source`、`get_call_graph` |
| ④ 查 commit：有人修过吗？patch 进了哪些版本？| ReAct 工具 A/D | `search_commits` → `get_commit_detail` → `check_backport_status` |
| ⑤ 查 LKML：上游讨论过吗？有什么注意事项？| ReAct 工具 A | `search_lkml`（L2 BM25 + L3 lore live） |
| ⑥ 查 Bug/CVE：已知问题吗？有 workaround 吗？| ReAct 工具 A | `search_bugs`、`search_syzbot`、`search_cve` |
| ⑦ 交叉对照（"这个 commit 修了那个 CVE，对应 LKML 讨论说要加 stable 标"）| ReAct 工具 D + **图谱** | `link_commit_cve` / `link_commit_message` / `link_commit_bug` 的跨表查询 |
| ⑧ 给结论：根因 + 修复建议 + 置信度 | **Report**（确定性）| `bind_claims` + `generate_report` |

22 个 tool 分散在 5 个 route 里，本质就是把工程师**手头那本"资料地图"**显式拆开。

### 二、图谱到底解决什么

**最重要的问题** —— 一个有 10 年经验的内核工程师和一个新人，差距不在他们能查的工具（每个人都能 `git log`、上 lkml），而在他们**脑子里那张关系网**：

> "这个 call trace 里有 `tcp_v4_do_rcv` → 我去年看过一个 CVE 跟它有关 → 那 CVE 修复 commit 是 abc123 → 那 commit 的 patch 讨论里 davem 提过 backport 有坑"

这张网不是文本搜索能搜出来的 —— 它是**显式连接**：

```
   ┌─ Discussion ─┐         ┌─ Code Change ─┐
   │ LKML message │ ←Link:← │  Commit       │
   └──────────────┘         └───────────────┘
                              ↑Fixes:↑   ↑NVD↑
                              │          │
                          ┌── Bug ──┐ ┌── CVE ──┐
                          │ ID 1234 │ │ 2024-… │
                          └─────────┘ └────────┘
```

图谱（`link_commit_bug` / `link_commit_message` / `link_commit_cve`）**就是把那个老工程师脑子里的关系网，物化成数据库行**。这样：

| 没有图谱（纯 BM25）| 有图谱 |
|---|---|
| 每个 route 是孤岛：search_lkml 给你邮件，search_cve 给你 CVE，**它们之间不知道彼此** | 一条 evidence 拿出来可以**多跳遍历**："这个 CVE 的 fix_commits 是哪个" → "那个 commit 引用了哪条 LKML 讨论" → "那讨论里 reviewer 提了什么 NACK" |
| LLM 只能看 keyword 匹配的散落文本，靠 reasoning 把它们串起来（容易瞎编）| LLM 看的是**结构化关系**，"是同一回事"由数据保证，不靠它推 |
| 找"已修没修"靠词频匹配 commit message | 直接 `link_commit_cve` JOIN `kernel_commit.olk_inclusion_type` 给出确定答案 |

### 三、现实差距（诚实说）

| 维度 | 有经验工程师 | 我们的 agent |
|---|---|---|
| 直觉判断"该查什么"| 经验形成的 prior | LLM prompt + 路由 SOP（次一档）|
| 知道"什么时候够了"| 几分钟内决定 | ❌ ReAct 不收敛（eval 显示要靠 force_finalize 强终止）|
| 读代码带子系统理解 | 真的懂 mm/net/fs 设计 | 顺着符号跳，缺整体把握 |
| 多跳关联 | 脑里那张图 | **靠 cross-graph 弥补** ← 图谱的本质价值 |
| 信号噪声过滤 | 立刻忽略 user-language 干扰词 | BM25 把 "java/diagnose" 当 keyword 也算（recall 报告的根因之一）|

### 四、所以图谱的真实作用是什么？

**不是为了"找到资料"** —— 资料 BM25 / lore search 已经能找到。
**是为了"知道资料之间的关系"** —— 把工程师**多年经验形成的直觉关联**变成 LLM 可遍历的边。

具体收益（对应当前 v2 状态）：

1. **`link_commit_cve` (401 行)** — agent 看到一个 CVE-2024-XXXX，能立刻 JOIN 出"修复 commit 是 abc123，subject 是 ..."，不用让 LLM 再去 BM25 猜
2. **`link_commit_message` (33K 行)** — agent 看到一封 LKML patch 讨论邮件，能立刻知道"对应的 commit 是哪个 SHA，落到了哪个 OLK 版本"
3. **`link_commit_bug` (1 行，缺口)** — 因 OLK 大量引用 gitee/atomgit issues 不在 bug 表里。ADR-022 v2.1 修

**一句话总结**：图谱 = **把内核生态里"什么和什么是一回事"这件事固化下来**，让 LLM 拿着图走，比让它读着文本猜要可靠得多。这也是 v2 比 v1 的本质进化 —— v1 是 7 路并行 BM25 + 假设投票（独立证据靠 LLM 串），v2 加 ReAct 让 LLM 沿图自由跳转。

---

*相关问题：Q1（图谱如何构建）· Q3（诊断流程）· 参考：[ADR-019](v2/adr/ADR-019-hybrid-react-deterministic.md)*

---

## Q3：本系统是怎样回答一个故障诊断问题的？详细步骤是什么？

入口函数是 `agent/graph.py:diagnose(raw_input)`，它驱动一条 **10 节点、两阶段**的 LangGraph 流水线，所有节点按有向边顺序执行，状态通过一个共享 dict 传递，关键节点会写入 PostgreSQL checkpoints（可断点续跑）。

```
Triage 阶段（确定"是什么故障"）
  parse_input → extract_events → classify_fault → retrieve
                                                       ↓
Diagnosis 阶段（回答"为什么 + 怎么修"）
  load_sop → generate_hypotheses → verify_hypothesis → self_consistency → bind_claims → generate_report → END
```

---

### 阶段一：Triage（4 个节点）

#### Step 1 · `parse_input` — 输入类型识别

**做什么**：判断用户输入是哪种形式。

| 条件 | 识别为 |
|------|--------|
| 路径存在且是 `.xz/.gz/.bz2` 或含 `sosreport` 字样 | `sosreport` |
| 路径存在且是普通文件 | `dmesg_file` |
| 文本含 `BUG:` / `WARNING:` / `Call Trace:` / `Oops:` 等内核日志标记 | `dmesg` |
| 其他（自然语言问题） | `question` |

**代码位置**：`agent/triage/nodes.py:parse_input()`

---

#### Step 2 · `extract_events` — 结构化事件提取

**做什么**：从原始文本中提取结构化的内核事件，每个事件有 `kind`（故障类型）和 `summary`（一行摘要）。

- **dmesg 输入**：调用 `mcp_servers/dmesg_journal/extractor.py`，用正则解析 OOM kill 记录、Oops、BUG、WARNING、Call Trace、softlockup 检测日志等，输出 `KernelEvent` 列表
- **sosreport 输入**：调用 `mcp_servers/sosreport/parser.py`，从压缩包中提取 `sos_commands/kernel/dmesg`，再走 dmesg 提取路径；同时读取 hostname、kernel version 等系统信息
- **question 输入**：无结构化事件，events 为空列表，由后续节点用 LLM 补偿

**产出**：`kernel_events`（事件列表）、`kernel_version`、`olk_version_tag`

---

#### Step 3 · `classify_fault` — 故障分类 + SOP 选择

**做什么**：从事件中确定主故障类型，并映射到对应的 SOP。

**有事件时**（dmesg/sosreport 路径）：按优先级从事件中选取最严重的 fault_kind：

```
panic > oom > softlockup > hardlockup > rcu_stall > oops > bug > warn > lockdep
```

**无事件时**（question 路径）：调用 Navigator LLM（`cfg.llm.navigator`），用 JSON prompt 分类：
```json
{"fault_kind": "oom|oops|softlockup|...", "fault_summary": "<一行描述>"}
```

**fault_kind → SOP 映射**：

| fault_kind | SOP 文件 |
|-----------|---------|
| oom | `agent/sop/definitions/oom.yaml` |
| softlockup / hardlockup / rcu_stall | `lockup.yaml` |
| panic | `panic.yaml` |
| oops / bug / warn / lockdep | `generic.yaml` |

SOP 文件包含：结构化诊断步骤、假设模板、关键指标列表、报告章节定义。

**产出**：`fault_kind`、`fault_summary`、`sop_name`

---

#### Step 4 · `retrieve` — 7 路并行检索

**做什么**：以 `fault_summary`（或原始问题）为查询，同时从 7 个知识来源召回证据。

1. **query_parser**（非 LLM，基于规则解析）：把 `fault_summary` 解析为 `RetrievalQuery`，提取 kernel_version、keywords、cve_ids 等字段
2. **7 路并行召回**（全部同时触发，`ThreadPoolExecutor`）：

| 路由 | 数据源 | 查询方式 |
|------|--------|---------|
| `commit` | PG `kernel_commit` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `lkml` | PG `lkml_message` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `bug` | PG `bug` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `syzbot` | PG `syzbot_crash` | BM25 `body_tsv @@ websearch_to_tsquery(keywords)` |
| `cve` | PG `cve` | CVE-ID 精确查找 + BM25 |
| `code` | CodeGraph MCP | `search_code(keywords, repos=['olk-kernel'])` |
| `docs` | CodeGraph MCP | PageIndex 树遍历（LLM 驱动，配额充足时触发）|

3. **分数归一化**：各路由用 `ts_rank_cd` 计分（量纲不同），合并前对每路由内部做 max 归一化到 [0,1]
4. **LLM 重排**（可选）：候选总数 > 10 时，取 BM25 top-20 发给 Navigator LLM 打相关性分数，重新排序

**产出**：`evidence`（Evidence 列表，每条含 `route`、`score`、`title`、`body[:500]`、`commit_hash/bug_id/message_id` 等字段）

---

### 阶段二：Diagnosis（6 个节点）

所有节点均调用 Chat LLM（`cfg.llm.chat`，通常是 claude_code 或 openai_compat）。

#### Step 5 · `load_sop` — 加载 SOP

**做什么**：从 `agent/sop/registry` 读取对应的 YAML 文件，提取 `steps` 列表（结构化诊断步骤）。若 SOP 不存在则 fallback 到 `generic.yaml`。

**产出**：`sop_steps`（字符串列表，5-9 条诊断步骤）

---

#### Step 6 · `generate_hypotheses` — 生成假设

**做什么**：把 `fault_summary` + SOP 步骤 + 前 10 条 Evidence 组合成 prompt，让 LLM 给出 3 条假设，每条含置信度（0-1）。

```
Fault: OOM kill of java with oom_score 999 in cgroup 4GB
SOP steps: 1. Parse OOM kill log... 2. Check cgroup limits...
Evidence: [commit] mm/vmscan: wake up flushers... : body...
→ LLM → [{"id":"h1","text":"cgroup limit too tight","confidence":0.8}, ...]
```

假设按置信度降序排列，最高分的作为 `active_hypothesis`。

**产出**：`hypotheses`（列表）、`active_hypothesis`（当前验证的假设）

---

#### Step 7 · `verify_hypothesis` — 验证假设

**做什么**：把 `active_hypothesis` + 前 8 条 Evidence 发给 LLM，让它判断假设是否被证据支持。

```
Hypothesis: cgroup limit too tight for java workload
Evidence: [commit] mm/memcontrol: don't throttle dying tasks...
→ LLM → {"status": "confirmed", "reasoning": "Evidence shows..."}
```

可能的 status：`confirmed` / `rejected` / `uncertain`

**产出**：更新 `active_hypothesis.status`

---

#### Step 8 · `self_consistency` — 自一致性投票

**做什么**：用相同 prompt 独立调用 LLM **K 次**（默认 K=1，评测时 K=3），得到 K 个技术分析文本，取出现最多的（多数票）作为 `final_analysis`。

- 调用参数：`temperature=0.3`（增加采样多样性）
- 投票方式：字符串规范化后用 `Counter.most_common(1)`
- K 由 `cfg.llm.chat.self_consistency_k` 控制（**Pro 配额紧张时务必保持 K=1**）

**产出**：`candidate_analyses`（K 个候选）、`final_analysis`（获胜分析）

---

#### Step 9 · `bind_claims` — 声明-证据绑定

**做什么**：从 `final_analysis` 中提取可引用的技术声明，验证每条声明是否有可溯源的证据支撑。

```
Analysis: "OOM triggered by cgroup throttling bug. Commit 038ff4e1 fixes..."
→ LLM → [{"text":"OOM caused by throttle","evidence_refs":["038ff4e1"]}]
→ 对比 evidence 中的 commit_hash 集合，判断 verified=True/False
```

**声明标记规则**：
- `verified=True`：`evidence_refs` 中有对应的 commit hash、bug_id 或 message_id
- `verified=False`：无法溯源，**保留在报告中并注明**（不删除）

**产出**：`claims`（Claim 列表，每条含 `text`、`evidence_refs`、`verified`）

---

#### Step 10 · `generate_report` — 生成报告

**做什么**：调用 `agent/report/renderer.py`，把最终状态渲染为两种格式：

**Markdown 报告（`report_md`）** — 面向工程师阅读：
- Executive Summary（根因一句话）
- Evidence（按路由分组，标注 mainline-backed / openEuler-native 置信度）
- Analysis（self_consistency 获胜分析）
- Claims（每条标 ✅verified / ⚠unverified）
- Hypotheses（列出所有假设 + 验证状态）
- Recommended Actions

**JSON 报告（`report_json`）** — 面向程序消费：
```json
{
  "fault_kind": "oom",
  "sop_name": "oom",
  "final_analysis": "...",
  "claims": [...],
  "evidence": [...],
  "report_md": "..."
}
```

---

### 完整数据流图

```
用户输入: "v6.6 OLK 进程 java OOM killed, oom_score 999, cgroup 4GB limit"
    │
    ▼ parse_input
    input_type = "question"
    │
    ▼ extract_events
    kernel_events = []  (question 路径无事件)
    │
    ▼ classify_fault (LLM)
    fault_kind = "oom"
    fault_summary = "OOM kill of java (oom_score=999) under 4GB cgroup"
    sop_name = "oom"
    │
    ▼ retrieve (7路并行 BM25 + CodeGraph)
    evidence = [
      {route:commit, commit_hash:"038ff4e1...", title:"mm/vmscan: wake up flushers...", score:0.85},
      {route:lkml,   message_id:"...", title:"Re: [PATCH] memcg: ...", score:0.72},
      {route:cve,    cve_id:"CVE-2024-50022", title:"...", score:0.61},
      ...共最多 70 条，重排后取 top-10
    ]
    │
    ▼ load_sop
    sop_steps = ["Parse OOM kill log", "Check cgroup limits", ...]
    │
    ▼ generate_hypotheses (Chat LLM)
    hypotheses = [
      {id:"h1", text:"cgroup limit too tight", confidence:0.8, status:"pending"},
      {id:"h2", text:"kernel memcg accounting bug", confidence:0.6, ...},
      {id:"h3", text:"application memory leak", confidence:0.5, ...},
    ]
    active_hypothesis = h1
    │
    ▼ verify_hypothesis (Chat LLM)
    active_hypothesis.status = "confirmed"
    │
    ▼ self_consistency (Chat LLM × K=1)
    final_analysis = "Root cause: cgroup memory.high throttle not bypassed for dying
                      tasks. Commit 038ff4e1 fixes the vmscan flusher wake condition.
                      Recommend upgrading to OLK-6.6 ≥ v6.8-rc3 backport."
    │
    ▼ bind_claims (Chat LLM)
    claims = [
      {text:"cgroup throttle not bypassed for dying tasks",
       evidence_refs:["892962a2"], verified:True},
      {text:"vmscan flusher fix in 038ff4e1",
       evidence_refs:["038ff4e1"], verified:True},
    ]
    │
    ▼ generate_report
    report_md  = "## Executive Summary\nRoot cause: ..."
    report_json = {"fault_kind":"oom", "claims":[...], ...}
```

---

### 关键设计权衡

| 设计 | 原因 |
|------|------|
| 两阶段流水线（triage + diagnosis） | triage 先定类型选 SOP，避免 LLM 对每种故障都泛化推理 |
| 7 路全量触发（不按故障类型路由）| 防止路由误判丢失证据（ADR-013）；代价是多几次 BM25 查询（<1s） |
| 路由内分数 max 归一化 | 不同数据源（LKML 邮件/commit 正文）原始 BM25 分数量纲差异 3-5 倍，不归一化则 commit 结果永远被 LKML 压排 |
| self_consistency K=3 | 三次采样投票提升结论稳定性；配额紧张时设 K=1 跳过（单次采样等价于正常 LLM 调用） |
| bind_claims 保留 unverified | 不删除无法溯源的声明，标注 ⚠ 供工程师判断，避免 LLM 编造难以发现 |
| PG checkpointer | 每个节点完成后写入 PostgreSQL，诊断中途崩溃可从最后一个节点恢复 |

---

*参考代码：`agent/graph.py` · `agent/triage/nodes.py` · `agent/diagnosis/nodes.py` · `agent/report/renderer.py`*

*相关问题：Q2（数据源）· Q1（图谱构建）*

---

## Q2：本系统涉及了哪些数据源？这些数据源分别是什么数据，对故障诊断有什么作用？

系统共有 **6 个数据源**，分两类：自建入库（PG）和外部服务直连（CodeGraph MCP）。

### 数据源一览

| 数据源 | 当前数据量 | 回答的核心问题 |
|--------|-----------|--------------|
| OLK 内核 git | ~130 万 commit | "历史上谁修过类似问题？补丁在哪？" |
| lore.kernel.org（LKML）| ~5792 条邮件（入库中）| "当时为什么这样修？有什么争议？" |
| bugzilla.kernel.org | 3700 条 bug | "这是已知 bug 吗？状态如何？" |
| syzbot | 999 条崩溃 | "是否有崩溃 reproducer？上游修了吗？" |
| NVD CVE | 15502 条 CVE | "是否有 CVE？CVSS 评分和影响面？" |
| CodeGraph（MCP）| olk-kernel 全量源码 | "崩溃点的代码是什么？调用链怎么走？" |

---

### 1. OLK 内核 Git 仓（`atomgit.com/openeuler/kernel`）

**数据内容**：OLK-6.6 + OLK-5.10 全量 commit 历史，包含 commit hash、作者、日期、subject、body（含 OLK inclusion 头、Fixes:/Link:/CVE: 等 trailer）。

**故障诊断作用**：
- **找修复记录**：BM25 匹配故障关键词，命中的 commit 就是历史上修过同类问题的补丁
- **回溯根因**：通过 OLK inclusion 头找到上游 mainline SHA，再桥接到 LKML 讨论和 CVE
- **判断 backport 状态**：确认上游修复是否已回移到用户所在的 OLK 版本

---

### 2. lore.kernel.org（LKML 邮件列表）

**数据内容**：linux-mm、stable 等列表的历史邮件，结构化为线程 DAG（lkml_thread / lkml_message / lkml_patch / lkml_review）。

**故障诊断作用**：
- **理解问题背景**：patch 合入前的讨论往往包含根因分析、失败场景、reviewer 的顾虑
- **找相同症状**：历史上出现过同类崩溃报告的讨论线程
- **多跳推理**：从一个 commit 通过 `link_commit_message` 找到对应审查讨论，再看 NACK/Acked-by 的理由

---

### 3. bugzilla.kernel.org（内核 Bugzilla）

**数据内容**：kernel.org 官方缺陷库，包含标题、描述、状态、严重程度、修复版本。

**故障诊断作用**：
- **找已知缺陷**：症状与已记录 bug 匹配时直接给出"这是已知问题，状态 FIXED/OPEN"
- **提供复现条件**：Bugzilla 报告通常包含触发条件和内核版本范围，比 commit message 更面向运维

---

### 4. syzbot（syzkaller 自动化崩溃检测）

**数据内容**：syzbot 持续运行 syzkaller 发现的内核崩溃，每条含崩溃标题、调用栈、状态（open/fixed）、fix commit。

**故障诊断作用**：
- **崩溃签名匹配**：stack trace 的函数帧序列与 syzbot 历史崩溃做签名对比，快速识别已知崩溃
- **获取 reproducer**：syzbot 崩溃附有 C 或 syz reproducer，可直接用于复现验证
- **确认修复状态**：标注 fix commit 的崩溃说明上游已有补丁，可检查 OLK 是否已 backport

---

### 5. NVD（美国国家漏洞数据库）

**数据内容**：Linux 内核相关 CVE，包含 CVSS 评分、漏洞描述、references 中的 fix commit URL（已提取为 fix_commits 字段）。

**故障诊断作用**：
- **安全属性判定**：确认某个崩溃/异常是否对应已公开 CVE，给出评分和影响范围
- **修复 commit 直达**：从 CVE references 提取 commit SHA → link_commit_cve → 找 OLK 对应 backport

---

### 6. CodeGraph（codesearch MCP 服务，外部直连）

**数据内容**：OLK-6.6 内核源码的两层索引——Zoekt BM25 全文 + SCIP 编译器级符号引用图 + Sphinx 文档章节树。

**故障诊断作用**：
- **定位崩溃代码**：给定 `tcp_v4_do_rcv+0x123` → 精确找到函数定义、调用点、字段布局
- **追踪调用链**：`lookup_symbol(action='references')` 找所有调用者，理解崩溃路径
- **阅读子系统文档**：补充 commit message 没说的设计意图和接口语义

---

### 为什么需要 6 个数据源？

单一数据源只能回答一个维度：Bugzilla 知道"有没有 bug"，但不知道"代码怎么改的"；LKML 知道"讨论过什么"，但不知道"有没有 CVE"。只有把 6 个维度组合起来，才能给出完整的诊断报告：

```
根因（CodeGraph + commit）
  + 修复状态（commit backport 状态）
  + 安全属性（NVD CVE）
  + 已知缺陷（Bugzilla + syzbot）
  + 社区讨论（LKML）
= 可操作的诊断结论
```

这也是 Cross-Graph Linker 存在的原因——把 6 个数据源在 commit 层面焊接起来，使 agent 能跨源做多跳推理。

---

*相关问题：Q1（图谱如何构建） · 参考：[ingest/CLAUDE.md](../ingest/CLAUDE.md)*

---

## Q1：本系统中代码、文档、Bug、邮件列表等信息是怎样通过图结构构建起来的？

### 整体架构：4 张子图 + 1 个 Cross-Graph Linker

系统不使用向量数据库（无 embedding），而是把内核生态的公开知识构建成一张**多层关联图**，由 4 张子图和一个负责"焊接"它们的 Cross-Graph Linker 组成：

```
┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐  ┌─────────────────┐
│  Code Graph      │  │ Discussion Graph │  │   Bug Graph      │  │   Doc Tree      │
│  （源码图谱）     │  │  （邮件讨论）    │  │ （缺陷/崩溃）    │  │  （文档章节树）  │
│                  │  │                  │  │                  │  │                 │
│  Zoekt BM25 文本 │  │  lkml_message    │  │  bug (Bugzilla)  │  │  Sphinx 章节树  │
│  SCIP 符号引用图 │  │  lkml_thread DAG │  │  syzbot_crash    │  │  kernel-doc     │
│  函数定义/调用链 │  │  lkml_patch      │  │  cve (NVD)       │  │  PageIndex 走查 │
│                  │  │  lkml_review     │  │  stack 签名      │  │                 │
│  ★ 复用          │  │                  │  │                  │  │  ★ 复用         │
│  CodeGraph 服务  │  │  我方 PG + Neo4j │  │  我方 PG         │  │  CodeGraph 服务 │
└────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘  └────────┬────────┘
         │                     │                     │                     │
         └─────────────────────┴──────────┬──────────┴─────────────────────┘
                                          ▼
                             ┌────────────────────────┐
                             │   Cross-Graph Linker   │
                             │                        │
                             │  commit ↔ bug          │
                             │  commit ↔ LKML message │
                             │  commit ↔ CVE          │
                             │  OLK commit ↔ upstream │
                             └────────────────────────┘
                                          ▼
                              PG link_* 表 + Neo4j 关系边
```

---

### 子图一：Code Graph（源码图谱）

**数据来源**：OLK 内核 git 仓（`atomgit.com/openeuler/kernel`，分支 OLK-6.6 / OLK-5.10），与 CodeGraph 代码索引**同源同版本**。

**构建方式**：由独立的 `CodeGraph`（codesearch）服务承担，本系统通过 MCP HTTP 协议消费：

| 能力 | 实现 |
|------|------|
| 文件全文 + 符号文本搜索 | Zoekt trigram 索引 |
| 精确符号定义 / 引用 / 实现关系 | SCIP（scip-clang 基于 LLVM 编译分析） |
| 函数调用链 | SCIP 跨文件引用图 |
| 文件级大纲 | tree-sitter |

查询代码图谱时，系统通过 MCP 调用 `lookup_symbol(name, action='references', repo='olk-kernel-v6.6')` 即可得到所有调用点，无需自建符号索引。

---

### 子图二：Discussion Graph（邮件讨论）

**数据来源**：lore.kernel.org（公开 mailing list 归档），通过 Atom feed + 逐封 `/raw` 下载拉取。

**构建方式**：

```
Atom feed 分页（?q=d:YYYYMMDD..&x=A）→ 收集消息 URL
  ↓
每封邮件 /{list}/{msg-id}/raw 下载
  ↓
mbox 解析 → 单封邮件元数据 + body
  ↓
存入 PG lkml_message（含 body_tsv BM25 全文索引）
  ↓
基于 In-Reply-To / References header 构建线程 DAG
  → PG lkml_thread + Neo4j (:Message)-[:IN_REPLY_TO]->() 关系
  ↓
[PATCH vN] / Fixes: / Reported-by: trailer 解析 → lkml_patch
  ↓
Reviewed-by / Tested-by / Acked-by 解析 → lkml_review
  ↓
长线程（> 30 封）调用 LLM → 三段摘要存入 lkml_thread.summary
```

结果：每条邮件有上下文（属于哪个线程）、审查信号（Acked-by / NACK）和 patch 关联，形成一个可多跳遍历的讨论 DAG。

---

### 子图三：Bug Graph（缺陷与崩溃）

**数据来源**：bugzilla.kernel.org（v1 仅此源）、syzbot.kernel.org、NVD CVE feed。

**构建方式**：

```
Bugzilla REST API（last_change_time 增量）→ bug 元数据 + description
  ↓ 归一化
PG bug 表（title / severity / status / body_tsv）

syzbot HTML 抓取 → crash 标题 + reproducer + stack trace
  ↓
PG syzbot_crash 表 + 栈帧签名（ingest/syzbot/signature.py）

NVD JSON Feed（lastModStartDate/lastModEndDate 滑动窗口）→ CVE 元数据
  ↓
PG cve 表（cvss_v3_score / references / fix_commits）
```

fix_commits 字段：从 NVD references 的 GitHub/kernel.org commit URL 中提取内核 commit SHA，供 Cross-Graph Linker 做关联。

---

### 子图四：Doc Tree（文档章节树）

**数据来源**：OLK 内核 kernel-doc（Sphinx 构建产物）。

**构建方式**：完全由 CodeGraph 的独立 `kernel-docs-builder` 容器承担（Sphinx JSON backend），本系统通过 MCP 消费三层导航：

```
browse_docs()                → 站点级目录树（toctree）
browse_doc_sections(path)    → 单文档章节树
read_doc_section(path, sec)  → 具体节文内容
search_docs(query)           → BM25 文档命中
```

---

### Cross-Graph Linker：把 4 张子图"焊"在一起

这是系统的核心壁垒（`graph/linker.py`）。所有关联都有明确的文本证据，**不依赖相似度匹配**：

| 关系 | 链接方式 | 证据 | 存储 |
|------|---------|------|------|
| commit → bug | 扫描 commit body 中的 `Fixes: bsc#N`、`Closes: bugzilla.kernel.org/...` | trailer 文本 | PG `link_commit_bug` |
| commit → LKML message | 扫描 commit body 中的 `Link: https://lore.kernel.org/.../<msg-id>` | Link: trailer | PG `link_commit_message` |
| commit → CVE | NVD fix_commits 字段中的 commit SHA ↔ kernel_commit.hash | NVD references 中的 git URL | PG `link_commit_cve` |
| OLK commit → upstream | inclusion 头中的 mainline/stable SHA 锚点 | commit message 嵌入 | kernel_commit.upstream_commit |
| commit → function | git diff 文件 → CodeGraph `lookup_symbol` | 运行时解析，不持久化 | — |
| stack frame → function | 函数名 → CodeGraph `lookup_symbol` | 运行时解析，不持久化 | — |

**OLK commit 的特殊处理**：OLK 内核 ~78% 的 commit 是从 mainline/stable 回移的 backport，commit body 顶部有 inclusion 头记录上游锚点：

```
mainline inclusion          ← 类型
from mainline-v6.12-rc1
commit d1877cc7270302081a   ← 上游 mainline SHA（可直接用）
category: bugfix
CVE: CVE-2024-50022
...
[ Upstream commit d1877cc7 ] ← stable 类型才有此行，是真正的 mainline SHA
```

Cross-Graph Linker 解析这个 inclusion 头，将 OLK commit 桥接到 kernel.org 生态（LKML 讨论、Bugzilla、CVE），让诊断 agent 能从 OLK 的 crash 追溯到原始 mainline 讨论。

---

### 图在检索时如何使用

检索时触发 7 路并行召回（`retrieval/engine.py`），每路独立：

```
用户问题 "v6.6 上 order=4 的 normal zone OOM"
  ↓ LLM 解析 → RetrievalQuery(fault_domain='oom', kernel_version='OLK-6.6', ...)
  ↓
  ├─ code    → CodeGraph search_code('oom_kill_process', repo='olk-kernel-v6.6')
  ├─ docs    → CodeGraph search_docs('OOM killer')
  ├─ lkml   → PG: SELECT FROM lkml_message WHERE body_tsv @@ 'oom & order & normal'
  ├─ bug     → PG: SELECT FROM bug WHERE body_tsv @@ 'oom & zone & normal'
  ├─ syzbot  → PG: SELECT FROM syzbot_crash WHERE body_tsv @@ 'oom & order=4'
  ├─ commit  → PG: SELECT FROM kernel_commit WHERE body_tsv @@ 'oom & zone'
  └─ cve     → PG: SELECT FROM cve WHERE body_tsv @@ 'oom & memory'
  ↓
多路结果合并 → 候选 > 10 时 LLM 重排
  ↓
Evidence 列表（每条带 route 标签 + 可追溯来源）
```

诊断 agent 拿到 Evidence 后，可继续通过 `link_commit_bug` / `link_commit_message` 做多跳：比如找到一个 CVE 相关的 commit，再从 `link_commit_message` 找到对应的 LKML 讨论线程，还原当年的根因分析。

---

### 关键设计原则

1. **无 embedding**（ADR-001）：所有关联基于结构化字段、正则提取、BM25，无向量相似度，结果完全可审计
2. **证据优先**：每条关联都有 `source`（trailer / nvd_ref / subject 匹配）和 `confidence` 字段，诊断报告引用时可追溯
3. **Code Graph 与 commit 图谱同源**（ADR-018）：CodeGraph 索引 `olk-kernel`，commit ingester 也拉 OLK git，行号完全一致，Cross-Graph Linker 不会出现代码/commit 错位

---

*参考文档：[Architecture.md](v1/Architecture.md) · [ADR-018](v1/adr/ADR-018-commit-source-olk-kernel.md) · [graph/linker.py](../graph/linker.py)*

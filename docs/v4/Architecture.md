# Linux-Diag-Agent v4 — Architecture（落地 delta）

> 本文只写 **v4 相对 v3 的代码改动落点**：拓扑变化、新数据结构、每个 P0/P1 改到哪些真实文件。
> 全局架构看 `delivery/02_ARCHITECTURE.md`；本文假设你已读过 v4 `PRD.md`。

---

## 0. 改动总览（一张图）

```
                          ┌──────────────── v4 新增/改造 ────────────────┐
parse_input → extract_events → detect_taint_and_hw_signals → classify_fault_and_route
   → retrieve(M5)
   → react_investigation(ReAct loop)
        │  ┌─ [P0-2] Phase 1.5 强制 decode_stacktrace（kernel 路由，有 vmlinux 时）
        │  └─ [P0-1] 每次 dispatch 后，工具输出抽标识 → Evidence Ledger（带 origin）
   → [P0-3] fault_boundary(M10)   ← 新节点：定界结论 + 责任 + handoff
   → bind_claims                  ← [P0-1] 改造：只对 Ledger 校验 + trace 校验
   → generate_report              ← [P0-3] renderer 出"定界："行 + Form D
   → write_dmesg_event → END
                          └───────────────────────────────────────────────┘
```

拓扑唯一结构变化：**`react_investigation` 与 `bind_claims` 之间插入 `fault_boundary` 节点**（`agent/graph.py`）。其余是节点内部改造，不动边。

---

## 1. P0-1 · Evidence Ledger（数据结构 + 链路）

### 1.1 新数据结构

落点：新建 `agent/evidence/ledger.py`（或并入 `agent/diagnosis/`）。

```python
@dataclass
class LedgerEntry:
    canonical_id: str          # commit→40位全hash; bug→"bug#<N>"; cve→"CVE-YYYY-NNNN"; lkml→message_id; code→"<file>:<line>"
    kind: str                  # commit | bug | cve | lkml | syzbot | code_line
    aliases: set[str]          # 短hash(12)、URL尾段、带前缀变体……全部小写归一
    title: str
    body: str                  # 全文（commit=subject+message；code_line=源码片段）
    origin: dict               # {"stage": "retrieve"|"react", "tool": <name|None>, "step": <int|None>}

class EvidenceLedger:
    entries: list[LedgerEntry]
    _by_alias: dict[str, LedgerEntry]      # 短/全hash 与各别名 → entry
    def add(self, entry): ...              # 去重合并：同 canonical_id 合并 aliases / 补 body / 记最早 origin
    def resolve(self, ref: str) -> LedgerEntry | None:   # 引用归一后查
    def normalize(ref: str) -> str:        # 短/全hash、大小写、前缀统一
```

放进 `state["evidence_ledger"]`。**保留** `state["evidence"]`（M5 池）以兼容现有 renderer/eval 字段，过渡期两者并存，ledger 为 grounding 的唯一真相源。

### 1.2 写入点

| 来源 | 文件 / 函数 | 改动 |
|---|---|---|
| M5 预检索池 | `agent/diagnosis/nodes.py` 或 `retrieve` 节点 | retrieve 产出后，把每行 `add()` 进 ledger，origin.stage="retrieve" |
| ReAct 工具输出 | `agent/react/loop.py` dispatch 后（约 `loop.py:230-247`） | 新增统一抽取器 `extract_identifiers(result_text)`：正则扫 commit hash / `bug#N` / `CVE-…` / message_id → 逐个 `ledger.add(origin={react, tool, step})`。**替代/增强**现有 `seen_hashes`（仅短 hash、仅部分工具） |
| 栈解码命中 | `decode_stacktrace` 工具回调 | 解出的每个 `file:line` 作 `code_line` 入账 |

> 现状 `react_investigation`（`agent/react/nodes.py:59-67`）已有 `_merge_react_evidence(existing, react_hashes)`，v4 用 ledger 取代之；merge 时**带回 commit body 全文**（修 PRD §4.2 H2）。

### 1.3 bind_claims 改造

落点：`agent/diagnosis/nodes.py::bind_claims`（现 192-341 行）。

| 现状 | v4 |
|---|---|
| `evidence_by_hash` 用全 hash 做 key（`:224`），短 hash 引用永不命中（H1） | 改用 `ledger.resolve(ref)`：内部归一短/全 hash（修 H1） |
| 重叠 (b) 用 `title+body`，但 merged 行常无 body（H2） | ledger 条目带全文 body（修 H2） |
| `_significant_tokens` 只取英文 token（`:167`），中文 claim 失效（H3） | 新增"标识符锚点"匹配：从 claim 抽函数名 / `CONFIG_*` / CVE / 错误码，与 ledger 条目锚点比对；泛 token 重叠作为辅助而非唯一判据（修 H3） |
| 无 trace 校验 | 新增：claim 引用的 ID 必须 `ledger.resolve()` 命中且 `origin` 存在；否则判 speculative + 报告标"⚠ 未在本会话出现的 ID" |
| `_OVERLAP_THRESHOLD=0.20` 全局 | 保留 0.20 作辅助阈值；锚点命中可独立判 verified |

### 1.4 诊断先行（实现前必做）

PRD §12 风险要求：**先证伪 H1-H4 再动手**。建议加临时埋点脚本 `scripts/diagnose_grounding.py`：对 1-2 个 case dump（claim 文本、LLM 给的 refs、ledger 全 alias、每步 resolve 命中与否、重叠分），定位真实断点，产出一页小结，再编码。

---

## 2. P0-2 · 取证层激活

### 2.1 现状接线

- `mcp_servers/crash_forensics/decode_stacktrace.py`（包 `scripts/decode_stacktrace.sh vmlinux`）— 已存在；
- `mcp_servers/crash_forensics/fetch_debuginfo.py` — 已存在，查 OLK 源码树 build 输出 / `/boot` / debuginfo 缓存 / sosreport；
- `agent/react/tools/vmcore_tools.py::decode_stacktrace` 工具 `routes=frozenset({"kernel+vmcore","kernel","unknown"})`（`:182`）— **纯 kernel 路由已可调，但无人触发**。

### 2.2 改动落点

| 改动 | 文件 | 细节 |
|---|---|---|
| Phase 1.5 强制 | `agent/react/prompts/kernel.md` + `kernel.zh.md`（现有 Phase 0/1 见 `kernel.zh.md:16-26`） | 在 Phase 1 前插：若 dmesg 含 `\w+\+0x[0-9a-f]+/0x[0-9a-f]+` 形态偏移，**先**调 `fetch_debuginfo` 取 vmlinux，再调 `decode_stacktrace`，用解出的 file:line 驱动 Phase 1 |
| vmlinux 供给扩展 | `fetch_debuginfo.py` | 增加：配置项显式路径（`configs/*.yaml::crash_forensics.vmlinux_paths`）；缓存目录可配；**找不到时返回 `{"status":"unavailable"}` 而非异常**，让 agent 优雅跳过 |
| 解码结果入账 | §1.2 第三行 | file:line → ledger code_line |
| drgn 触发放宽 | `agent/triage/nodes.py::_select_route`（现 `:300-313`） | 现 `kernel+vmcore` 需 `fault_kind∈{panic,oops} 且 vmcore_path`；改为"有 vmcore_path 即允许 kernel+vmcore"，让 hung_task/softlockup 也能跑 `locks` query |

### 2.3 注意（沿用既有 gotcha）

- `decode_stacktrace.sh` 在大 trace 上慢（`_TIMEOUT=60`，见 `decode_stacktrace.py:18`）；
- vmlinux 必须与故障内核**版本一致**，否则偏移解错——`fetch_debuginfo` 要按 `kernel_version` 匹配，错配宁可 unavailable；
- OLK 源码路径硬编码在 `_OLK_SOURCES`（`decode_stacktrace.py:24`）/ `_OLK_BUILD_DIRS`（`fetch_debuginfo.py:24`），v4 改为可配。

---

## 3. P0-3 · M10 定界节点

### 3.1 拓扑插入

`agent/graph.py`（现 `:57-59`）：
```python
graph.add_edge("retrieve", "react_investigation")
graph.add_node("fault_boundary", fault_boundary)          # v4 新增
graph.add_edge("react_investigation", "fault_boundary")   # 改：原 → bind_claims
graph.add_edge("fault_boundary", "bind_claims")           # 新
graph.add_edge("bind_claims", "generate_report")
```

### 3.2 节点实现

落点：新建 `agent/boundary/nodes.py::fault_boundary(state) -> dict`。

读：`state` 中 hardware_signals / taint / events / react_final_answer / evidence_ledger / app·driver·container 信号。
写：
```python
state["boundary"] = {
    "layer": "应用层|驱动层|硬件层|OS层|未定",
    "responsible": "<handoff_target>",
    "confidence": 0.0-1.0,
    "method": "rule|llm",        # 确定性规则命中 or LLM 兜底
    "handoff": {...} | None,     # 非 OS 责任时填 Form D 数据
}
```

算法（PRD §6.2）：确定性规则（hardware / taint O·E + 模块帧 / cgroup-OOM / 命中 OLK 可修）先判；不命中调 LLM fallback（专用 prompt，标 low/medium）。

### 3.3 定界信号采集

落点：扩 `agent/triage/nodes.py`（现有 `detect_taint_and_hw_signals` `:180`）。新增轻量 detector：
- **应用层**：OOM victim 的 cgroup/comm、是否全局内存压力（vs 单 cgroup 触顶）；
- **驱动层**：taint `O`（out-of-tree）/`E`（unsigned）+ 栈帧模块名（非 vmlinux 内置）；
- **容器维度**：PID→cgroup 路径（dmesg 里 `oom-kill:...task_memcg=/...` 可解）。
写入 `state` 供 M10 消费。

### 3.4 renderer 改造

落点：`agent/report/renderer.py`（现 verdict 渲染 `:189/230/353`）。
- 报告头**恒**输出：`定界：[{layer}] · 责任：[{responsible}] · 置信度：{confidence}`（含 OS 责任）；
- `boundary.layer != OS层` 时：渲染 **Form D handoff 包**（模板见 v3 PRD §4.4：定界结论 / OS 层已完成取证（现象 + 排除证据）/ 需要下一棒反馈 / 联系）；
- `== OS层` 时：走原 `## 根本原因 / ## 修复建议(Form A/B/C) / ## 置信度 / ## 证据来源`。
- JSON 输出新增 `boundary` 字段（增量，老消费者忽略）。

---

## 4. P1 落点（提要）

| P1 | 文件 | 改动 |
|---|---|---|
| 上游链 ingest | `ingest/kernel_commit/`（复用 git_wrapper/trailer_parser/ingester）+ 新 `ingest/mainline/`、`ingest/stable/` 入口 | 增量 clone torvalds/linux + linux-stable，入 `kernel_commit` 表（标 source=mainline/stable） |
| 上游桥接 | `graph/linker.py` | 用 OLK `upstream-commit:` trailer 把 OLK↔upstream 连边，消化悬空 SHA |
| signature 快路径 | `agent/react/nodes.py` 或新前置节点 | `find_similar_crashes` 高置信命中 → 短路 ReAct loop，复用历史分析；verdict 标 `reused_signature` |
| 分级模型 | `llm/provider/` + `configs/*.yaml` | triage/分类绑便宜模型，综合绑贵模型（M1 role 已分 chat/navigator，扩为可配 model 映射） |

---

## 5. 数据模型增量（M2）

| 表/列 | 改动 | 用于 |
|---|---|---|
| `kernel_commit.source` | 新增列 `olk|mainline|stable`（若无） | P1 上游链区分来源 |
| `link_commit_upstream` | 确认/补全 OLK↔upstream 边表 | P1 backport 链 |
| `dmesg_event` / eval 结果 | 增列：`boundary_layer` / `boundary_confidence` / `verified_claim_rate` 落库（可选） | 定界/可信度 趋势分析 |

> evidence ledger 当前设计为**进程内 state 结构**，不入库（与 trace 同处理，沿用 `80_KNOWN_LIMITS` "agent_tool_trace 表迁移"搁置策略）。若后续要做 grounding 趋势分析，再做持久化。

---

## 6. eval / 验收接线（M8）

落点：`eval/runner_v2.py` + `eval/data/cases_v2.json`。

- summary 新增 `gates: {G1_verified, G1_grounded, G1_traceability, G2_decode, G3_boundary, G4_route, G5_phase}`，各给 PASS/FAIL + 实测值；
- 新增 `boundary_accuracy`（定界层判对比例，对 US-14~18 case）；
- case 扩充：每类 fault_kind ≥ 3；定界 case 各 ≥ 2；≥ 3 个带 `func+0xNN` 偏移；≥ 若干 grounding 专项（正确 commit 在池中，断言出现在证据来源）。

---

## 7. 不动的东西（防止误改）

- M1 LLM 抽象 / PG cache / rate limit（ADR-019 D-系列）；
- ReAct loop 内核循环、MAX_ITER/REPEAT_LIMIT/TOKEN_BUDGET（`loop.py:31-34`）；
- M5 7 路 always-fire-all（ADR-013）；
- BM25-over-embedding（ADR-001）；
- `20_PROMPT_ASSETS` 的整体结构——**只在 kernel.md/zh.md 插 Phase 1.5，不重写其余**（改 prompt 是高风险动作，见 90_FAQ Q14）。

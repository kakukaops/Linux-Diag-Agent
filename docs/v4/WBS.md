# Linux-Diag-Agent v4 — WBS（任务分解 / 开发清单）

> 按此清单逐项开发。每个任务给：**落点文件 · 依赖 · 验收**。任务号沿用 `T-NNN` 体例。
> 顺序遵循 PRD §10.1：**P0-1 先做诊断 → P0-1 实现 → P0-2 → P0-3 → eval 接线**。

---

## 阶段 A · P0-1 证据可信度（信誉根基，最先做）

### T-401 · grounding 断点诊断（先于一切实现）
- **落点**：新建 `scripts/diagnose_grounding.py`
- **做什么**：对 `cases_v2.json` 取 1-2 个 diagnosed case，跑到 bind_claims，dump：claim 文本 / LLM 返回的 evidence_refs / `state["evidence"]` 每行的 hash 形态 / 每个 ref 的 resolve 命中情况 / 重叠分。逐一证伪 PRD §4.2 的 H1-H4。
- **依赖**：无
- **验收**：产出一页 `docs/v4/grounding_rootcause.md`，明确指出真实断点（H1/H2/H3/H4 哪个为主）。**没有这页不许进 T-402。**

### T-402 · Evidence Ledger 数据结构
- **落点**：新建 `agent/evidence/ledger.py`（`LedgerEntry` / `EvidenceLedger` / `normalize` / `resolve` / `add`）
- **依赖**：T-401
- **验收**：单测 `tests/test_ledger.py`——短 hash 与全 hash 互查命中；同 canonical_id 重复 add 合并 aliases、补 body、保最早 origin；CVE/bug#N/message_id 各 kind resolve 正确。

### T-403 · 统一标识抽取器 + ReAct 写账
- **落点**：`agent/react/loop.py`（dispatch 后，约 `:230-247`）新增 `extract_identifiers(text)`；`agent/react/nodes.py` 用 ledger 取代 `_merge_react_evidence`（`:59-117`），merge 时带回 commit body 全文
- **依赖**：T-402
- **验收**：跑一个 case，断言 ReAct 中 `get_commit_detail` 返回的 commit 出现在 `state["evidence_ledger"]`，且 `origin={stage:react, tool, step}`。

### T-404 · M5 池入账
- **落点**：`retrieve` 节点（`agent/diagnosis/nodes.py` 相关处）
- **依赖**：T-402
- **验收**：retrieve 后 ledger 含全部 M5 池行，origin.stage="retrieve"。

### T-405 · bind_claims 改造（核心）
- **落点**：`agent/diagnosis/nodes.py::bind_claims`（`:192-341`）
- **做什么**：(a) 改用 `ledger.resolve`（修 H1）；(b) 用 ledger 全文 body（修 H2）；(c) 加"标识符锚点"匹配（函数名/CONFIG_*/CVE/错误码），中文 claim 不靠泛英文 token（修 H3）；(d) **新增 trace 校验**：引用 ID 必须 resolve 命中且 origin 存在，否则判 speculative。
- **依赖**：T-402/403/404 + T-401 根因
- **验收**：单测覆盖 (a)-(d)；目标在 §阶段 E 的全量 eval 体现。

### T-406 · KG-silent claim 标注（ADR-025 关联）
- **落点**：bind_claims + renderer
- **依赖**：T-405
- **验收**：Phase-5 first-principles 产出的 claim 一律 speculative + 报告标"KG silent — 先验推断，未校验"，不计入 verified。

---

## 阶段 B · P0-2 取证层激活

### T-411 · vmlinux 供给扩展 + 优雅降级
- **落点**：`mcp_servers/crash_forensics/fetch_debuginfo.py`；OLK 路径外移到 `configs/*.yaml::crash_forensics`
- **做什么**：查找顺序加"配置显式路径 + 可配缓存目录 + sosreport 内"；按 `kernel_version` 严格匹配；**找不到返回 `{"status":"unavailable"}` 不抛异常**。
- **依赖**：无
- **验收**：给定 OLK-6.6 + 可用 vmlinux → 返回有效路径；版本错配或缺失 → `unavailable`，不异常。

### T-412 · Phase 1.5 强制栈解码（prompt）
- **落点**：`agent/react/prompts/kernel.md` + `kernel.zh.md`（Phase 0/1 见 `kernel.zh.md:16-26`）
- **做什么**：Phase 1 前插入 Phase 1.5：dmesg 含 `\w+\+0x[0-9a-f]+/0x[0-9a-f]+` 偏移时，先 `fetch_debuginfo` 再 `decode_stacktrace`，用 file:line 驱动 Phase 1；无 vmlinux 则跳过。
- **依赖**：T-411
- **验收**：coverage case（带偏移 + 可用 vmlinux）`react_tools_used` 含 `decode_stacktrace`。
- **注意**：改 prompt 高风险（90_FAQ Q14），**只插不重写**，改后跑全量对比 phase_coverage 不退化。

### T-413 · 解码结果入账
- **落点**：`decode_stacktrace` 工具回调 / ledger
- **依赖**：T-402 + T-411
- **验收**：解出的 file:line 作 code_line 进 ledger，可被 claim 引用并出现在报告证据来源。

### T-414 · drgn 触发条件放宽
- **落点**：`agent/triage/nodes.py::_select_route`（`:300-313`）
- **做什么**：有 vmcore_path 即允许 `kernel+vmcore`（不再限 panic/oops），让 hung_task/softlockup 能跑 `locks` query。
- **依赖**：无
- **验收**：带 vmcore 的 hung_task case 路由到 kernel+vmcore，且可调 `analyze_vmcore(query="locks")`。

---

## 阶段 C · P0-3 定界 Form D

### T-421 · 定界信号采集
- **落点**：`agent/triage/nodes.py`（扩 `detect_taint_and_hw_signals` 同级新 detector）
- **做什么**：采集 应用层（OOM victim cgroup/comm、全局 vs 单 cgroup 压力）、驱动层（taint O/E + 模块帧）、容器维度（`task_memcg=/...` 解 PID→cgroup）信号写入 state。
- **依赖**：无
- **验收**：US-14（应用层 OOM）case 的 state 含 app_layer 信号；US-16（out-of-tree 模块）含 driver 信号。

### T-422 · M10 fault_boundary 节点
- **落点**：新建 `agent/boundary/nodes.py::fault_boundary`
- **做什么**：确定性规则（PRD §6.2）先判 layer/responsible/confidence；不命中调 LLM fallback prompt（新建 `agent/boundary/prompts/`，标 low/medium）；写 `state["boundary"]`。
- **依赖**：T-421
- **验收**：5 个定界 case 规则命中率合理；不命中走 LLM；确定性部分同输入同输出。

### T-423 · 拓扑插入 M10
- **落点**：`agent/graph.py`（`:57-59`）
- **做什么**：`react_investigation → fault_boundary → bind_claims`。
- **依赖**：T-422
- **验收**：图编译通过；端到端跑通，state 含 boundary。

### T-424 · renderer 出定界 + Form D
- **落点**：`agent/report/renderer.py`（`:189/230/353`）
- **做什么**：报告头恒出 `定界：… · 责任：… · 置信度：…`；非 OS 责任渲 Form D（v3 PRD §4.4 模板）；OS 责任走原 Form A/B/C；JSON 加 `boundary` 字段。
- **依赖**：T-423
- **验收**：US-21 报告头有定界行；非 OS case 产出 Form D markdown 含"OS 层排除证据"；OS case 不回退。

---

## 阶段 D · case 扩充（与 A/B/C 并行）

### T-431 · 扩充测试集
- **落点**：`eval/data/cases_v2.json`（或新 `cases_v4.json`）+ `delivery/50_TEST_FIXTURES/`
- **做什么**：每类 fault_kind ≥ 3；定界 case（US-14~18）各 ≥ 2；≥ 3 个带 `func+0xNN` 偏移；≥ 数个 grounding 专项（正确 commit 在池中，断言出现在证据来源）。
- **依赖**：无（可先行）
- **验收**：case 总数与分布达标；每个新 case 有 expected_route / expected_fault_kind / expected_boundary（定界 case）/ expected_commit_hashes。

---

## 阶段 E · eval 门槛接线 + 验收

### T-441 · eval 门槛 + 新指标
- **落点**：`eval/runner_v2.py`
- **做什么**：summary 输出 `gates`（G1_verified/G1_grounded/G1_traceability/G2_decode/G3_boundary/G4_route/G5_phase）+ `boundary_accuracy`。
- **依赖**：阶段 A/C 完成
- **验收**：summary.json 含 gates；任一 P0 门槛 FAIL 时 runner 退出码非 0（供 CI/验收）。

### T-442 · 全量验收
- **落点**：跑 `eval/runner_v2.py --dataset cases_v4.json`
- **依赖**：T-401~441
- **验收**（PRD §9 门槛）：
  - G1 `avg_verified_claim_rate ≥ 0.5`（从 0）；`grounded_rate ≥ 0.5`；io-hang-001 `traceability > 0`
  - G2 coverage case 含 `decode_stacktrace`
  - G3 `boundary_accuracy ≥ 0.8`
  - G4 `route_accuracy ≥ 0.85` 不回退；G5 phase_coverage ≥ 0.80 不回退

---

## 阶段 F · P1（v4.1，P0 验收通过后）

### T-451 · mainline/stable ingest
- **落点**：`ingest/mainline/`、`ingest/stable/`（复用 `ingest/kernel_commit/` 组件）；`kernel_commit.source` 列
- **验收**：增量入库跑通；悬空 upstream SHA 显著下降；存储/耗时评估记入 `70_OPERATIONAL`。

### T-452 · 上游链 linker
- **落点**：`graph/linker.py`
- **验收**：OLK↔upstream 边建立；`check_backport_status` 召回提升（用 backport case 量化）。

### T-453 · signature 快路径
- **落点**：`agent/react/nodes.py` 或新前置节点
- **验收**：高置信签名命中 case 短路 ReAct，耗时 < 60s，verdict 标 `reused_signature`；不误短路（低置信仍走完整 loop）。

### T-454 · 分级模型
- **落点**：`llm/provider/` + `configs/*.yaml`
- **验收**：triage 用便宜模型、综合用贵模型可配；token 中位数下降 ≥ 40%，质量门槛（G1/G3/G4）不退化。

---

## 依赖速查

```
T-401 ─┬─ T-402 ─┬─ T-403 ─┐
       │         ├─ T-404 ─┤
       │         └─ T-413  │
       └──────────────────► T-405 ─ T-406
T-411 ─ T-412 / T-413                 │
T-414                                 │
T-421 ─ T-422 ─ T-423 ─ T-424         │
T-431 (并行)                          │
                  阶段A/C完成 ─► T-441 ─ T-442 ─► (P1) T-451..454
```

## 完成定义（DoD）

- 每个 T 任务：代码 + 对应单测/断言通过；
- P0 整体：T-442 全量验收 G1/G2/G3 PASS、G4/G5 不回退；
- 文档同步：改动若与 `delivery/` 契约冲突，更新对应 `delivery/*.md`（尤其 40_BEHAVIORAL_CONTRACTS 的 grounding 条、80_KNOWN_LIMITS 的"vmcore 未启用"条、03_ADR 新增 026-029）。

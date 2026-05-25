# agent/ — LangGraph 诊断 Agent

## 两阶段流水线

```
Triage 阶段                    Diagnosis 阶段
─────────────────────          ──────────────────────────────────
parse_input                    load_sop
  ↓                              ↓
extract_events                 generate_hypotheses
  ↓                              ↓
classify_fault                 verify_hypothesis
  ↓                              ↓
retrieve          →            self_consistency (K=3 投票)
                                 ↓
                               bind_claims
                                 ↓
                               generate_report → END
```

入口：`agent/graph.py:diagnose(raw_input)` — 组装 StateGraph 并调用 `app.invoke()`。

## State 结构

两个 TypedDict，LangGraph 将其合并为单一 dict 传递：

**TriageState**（`agent/triage/state.py`）
- `raw_input` / `input_type`（dmesg | sosreport | question）
- `kernel_version` / `olk_version_tag`（OLK-6.6 | OLK-5.10）
- `fault_kind`（oom | oops | softlockup | hardlockup | panic | lockdep | rcu_stall | generic）
- `kernel_events`（KernelEvent dict 列表）
- `evidence`（Evidence dict 列表，来自 retrieval engine）
- `sop_name`（选定的 SOP 名称）

**DiagnosisState**（`agent/diagnosis/state.py`）
- `hypotheses`（Hypothesis 列表，含 confidence 和 status）
- `active_hypothesis`（当前验证中的假设）
- `claims`（Claim 列表，每条含 evidence_refs）
- `candidate_analyses`（K=3 LLM 输出，用于 self_consistency 投票）
- `final_analysis`（多数票获胜的分析）
- `report_md` / `report_json`（最终输出）

## SOP 机制

SOP 文件在 `agent/sop/definitions/` 下，当前 9 个：
`oom` / `lockup` / `panic` / `generic` / `deadlock` / `io_hang` / `network` / `perf_regression` / `sched_anomaly`

```python
from agent.sop.registry import get_sop, list_sops
sop = get_sop("oom")    # KeyError 时自动 fallback 到 generic.yaml
```

故障类型 → SOP 的映射在 `agent/triage/nodes.py:_EVENT_KIND_TO_SOP`：
- oom → oom
- softlockup / hardlockup / rcu_stall → lockup
- panic → panic
- oops / bug / warn / lockdep → generic

## Self-Consistency

`self_consistency` 节点用 K=3 LLM 调用做多数票。K 通过 `cfg.llm.chat.self_consistency_k` 控制，默认 = 1（评测时设为 3）。Pro 配额紧张时不要手动改成 3 后忘记改回。

## Claim-Evidence Binding

`bind_claims` 节点从 `final_analysis` 提取声明，验证每条声明是否有 `evidence_refs`（commit hash / bug_id / message_id）支撑。无法验证的声明标 `verified=False`，**不得删除**，报告中保留并注明。

## 报告输出

`generate_report` 节点同时生成：
- `report_md`：Markdown（人类可读）
- `report_json`：结构化 JSON，schema 见 `agent/report/renderer.py`

commit 证据要分级标注：`mainline-backed`（高置信）vs `openEuler-native`（仅 commit message，中置信）。

## Checkpointing

`agent/checkpointer.py` 提供 PG checkpointer（WBS 7.12）。`build_graph()` 尝试加载，失败时优雅降级（无持久化继续运行）。不要为了调试 import 错误而删除 try/except。

## 调用方式

```python
from agent.graph import diagnose
result = diagnose("kernel dmesg text or free-form question")
# result['report_md']  — Markdown 报告
# result['report_json'] — JSON 报告
```

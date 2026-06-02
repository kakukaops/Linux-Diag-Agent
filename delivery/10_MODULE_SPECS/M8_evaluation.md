# M8 · Evaluation Framework

## 1. 目的

**自动化回归 + 流程合规度量**。让我们能：
- 每次代码改动后跑 22 个 case，秒级看回归
- 给"agent 调研流程是否符合设计"打**确定性**分（process compliance KPI）
- 给"答案是否正确"打**LLM-noisy** 副指标（仅作健康检查，**不作版本验收门槛**）

**WHY 主指标是流程合规度而不是答案准确率**：见 `03_ADR_collection.md::ADR-process-compliance-kpi`（2026-05-30 决定）。简言之：LLM 答案非确定性主导（同一 case 三折 73 % 漂移），追"答案对错"等于拟合一个噪声极大的函数。把"agent 走对了 KG 路径"作主指标——这是设计层面可控的，重跑 trace 完全可复现。

## 2. 公共接口

```python
# CLI
python -m eval.runner_v2 \
    --dataset eval/data/cases_v2.json \
    --output eval/results_v2/run_NN/ \
    [--limit N] \
    [--no-judge] \
    [--category oom] \
    [--case-ids case-1,case-2] \
    [--merge-summary] \
    [--concurrency 2]

# 程序化
def _run_case(case: dict, *, use_judge: bool = True) -> dict:
    """跑一个 case，返回 result 字典（含所有 metrics）。"""
```

## 3. case 文件 schema

```python
{
    "id": "oom-001",                            # 必需
    "category": "oom",                          # 必需
    "kernel_version": "OLK-6.6",                # 可选
    "raw_input": "[ 142.31 ] oom-kill: ...",    # 必需（dmesg 或自由文本）
    "question": "v6.6 OLK kernel: OOM kill ..." # 可选（用户视角问法）
    "expected_route": "kernel",                 # 可选；triage 验收
    "expected_fault_kind": "oom",               # 可选；triage 验收
    "primary_goal": "kg_efficiency",            # 'kg_efficiency' / 'intrinsic_knowledge' / 'llm_synthesis'
    "expected_commit_hashes": ["..."],          # 可选；recall@10 真值
    "expected_bug_ids": [],
    "expected_kg_paths": ["find_similar_crashes",
                          "find_commits_touching_symbol",
                          "search_commits"],    # PROCESS COMPLIANCE 主指标的"应走路径"清单
    "ground_truth_root_cause": "...",           # 可选；LLM judge 用
    "notes": "...",                             # 数据集编辑注释
}
```

3 个 case 文件：
- `cases_smoke.json` — 1 个产品级 smoke（开放问法）
- `cases_coverage.json` — 1 个 QA coverage（命令式 prompt，覆盖所有 KG 路径）
- `cases_v2.json` — 22 个回归 case

## 4. KPI 体系（**两层**）

### 4.1 PRIMARY · 流程合规度（确定性，版本验收门槛）

| 指标 | 公式 | 目标 |
|---|---|---|
| **avg_phase_coverage** | `mean(n_phases_traversed / 5)`，5 = kernel.md Phase 0-4 数 | ≥ **0.80** |
| **per_phase_traversal_rate** | 每 Phase 在所有 case 中的覆盖率 | Phase 2 (BM25) 必 = 1.0；其它 ≥ 0.5 |
| **avg_kg_path_coverage** | 对每个 case，`(actual_tools ∩ expected_kg_paths) / |expected_kg_paths|`，再求均值 | ≥ **0.80** |

#### `_PHASE_TOOL_MAP`（决定一个工具调用算"走过"哪个 Phase）

```python
_PHASE_TOOL_MAP = {
    "phase_0_similar_crashes": {"find_similar_crashes"},
    "phase_1_symbol_lookup":   {"find_commits_touching_symbol", "lookup_symbol"},
    "phase_2_bm25_search":     {"search_commits", "search_lkml",
                                 "search_bugs", "search_syzbot",
                                 "search_cve", "search_code"},
    "phase_3_browse_subsystem":{"browse_subsystem_fixes"},
    "phase_4_regression_check":{"get_regression_fixes"},
}
```

#### `expected_kg_paths` 字段

每个 case 在 JSON 里列出 "这道题理性的 agent 应该走的工具集"。这是**人写的真值**，反映 task 维度上需要的调研深度。例如 oom 类应该包含 `find_commits_touching_symbol` + `browse_subsystem_fixes`；纯 intrinsic_knowledge 题（"What is PREEMPT_NONE?"）则只需要 `get_function_source` + `search_lkml`。

### 4.2 SECONDARY · 输出质量（LLM-noisy，仅诊断参考）

| 指标 | 公式 | 说明 |
|---|---|---|
| `recall_at_10` | `expected_commit_hashes ∩ first_10_evidence_hashes / |expected|` | 只用 short-SHA prefix 匹配（避免 OLK 长 SHA / mainline 短 SHA 不一致） |
| `route_accuracy` | `state.diagnostic_route == case.expected_route` | triage 阶段确定性高，保留 |
| `fault_kind_accuracy` | `state.fault_kind == case.expected_fault_kind` | 同上 |
| `groundedness_rate` | `count(grounded) / count(diagnosed)` | M7 bind_claims 算的 |
| `grounded_correct_rate` | `count(grounded + judge_correct) / count(grounded_judged)` | 最严指标——既扎根又判对 |
| `verified_claim_rate` | 每 case `verified / total claims` 均值 | |
| `abstention_rate` | `count(insufficient_evidence) / count(all)` | 5%-50% 都是健康范围 |

### 4.3 LLM-as-judge（用 navigator 角色）

`_llm_judge(ground_truth, agent_answer)`：用 navigator backend 双 judge：

```
Judge A prompt: "GT: <gt>\nAnswer: <ans>\nDoes the answer correctly identify the root cause? Reply YES / NO / UNCERTAIN."
Judge B prompt: 类似措辞但反向 prime（"忽略表面相似，看是否真在解决同一问题"）
```

两 judge 必须**同时** YES 才算 correct，**同时** NO 才算 incorrect，其它情况 (one YES one NO) 标 `judge_uncertain`。这是反 LLM judge 过度自信的设计。

## 5. `_run_case` 执行流程

```
1. 加载 case JSON
2. 调 diagnose(raw_input) → state（M7 完整跑一遍）
3. 抽取指标:
   - 流程: tool_trace 中走了哪些 Phase / 哪些 expected_kg_paths
   - 召回: state.evidence 前 10 vs expected_commit_hashes
   - 扎根: state.groundedness / verified_claims / speculative_claims
   - 答案: state.react_verdict / state.final_analysis
4. 可选: _llm_judge(GT, answer) → judge_correct
5. 返回 result dict（含 ~30 个字段）
6. _save_case 写 eval/results_v2/run_NN/<case_id>.json
```

最后 `_compute_summary` 聚合所有 result 算 corpus-wide metrics + per_goal 分桶 + per_category 分桶 → `summary.json`。

## 6. 行为契约

| # | 规则 | WHY |
|---|---|---|
| C1 | **process compliance 是确定性的**：同一 tool_trace 重打分必得相同 KPI；LLM 抽奖不影响 | 反"score gaming"——历史教训 |
| C2 | **不要让 prompt 凑流程指标**：曾经加 `_validate_final_answer` retry 推 agent 改 form A/B/C，导致大量 case 退化到 insufficient_evidence；已回滚 | 见 ADR-process-compliance |
| C3 | **judge 是 dual，必须同时表态**：单 judge 误判率 25 %+，dual 把 uncertain 区独立标 | 反 LLM judge 过度自信 |
| C4 | **N-fold 评测**仅用于"测量噪声 vs production reliability"区分：N 折跑同 case，多数票表达"流程稳定 vs 答案漂移"——**不**用于生产 | N-fold 解决评测噪声，不解决生产可靠性 |
| C5 | **per_goal 分桶**：kg_efficiency / intrinsic_knowledge / llm_synthesis 三类用不同 lens 看 | mixed-goal 平均掩盖了真实信号 |

## 7. 已知陷阱（M8 独有）

### #1 · recall@10 必须用 short-SHA prefix

OLK commit 在 DB 里存全长 40-hex，mainline 引用是 short 8/12-hex。直接 string equality 100 % miss。

```python
# ✓
expected_prefixes = {h[:12] for h in case["expected_commit_hashes"]}
actual_prefixes   = {ev["commit_hash"][:12] for ev in evidence[:10] if ev.get("commit_hash")}
recall = len(expected_prefixes & actual_prefixes) / len(expected_prefixes)
```

### #2 · `groundedness == "n/a"` 不要计入 grounded_rate 分母

`react_verdict != "diagnosed"` 的 case 跳过 bind_claims，标 `n/a`。如果分母里算上 n/a，rate 会被压低。

```python
grounded_rate = count(grounded) / count(diagnosed and groundedness != "n/a")
```

### #3 · process compliance 主指标不要回归到答案质量

历史曾经为了"提高 grounded_correct" 在 prompt / loop 里加约束，导致 agent 退缩。Run 17-20 的教训：**只看流程，把答案质量留给副指标**。任何"如果指标不达标就 prompt 强制 X"的改动都要先回到 ADR-process-compliance 重读。

### #4 · case JSON 的 `expected_kg_paths` 是**期望**不是**强制**

设计上 expected 是"理性 agent 应走的路径"，actual 可能多走（额外覆盖更好）也可能少走（流程合规度低）。

- coverage > 1.0 不可能（actual 是 expected 的超集时分子上限 = |expected|）
- coverage ≪ 0.8 → 调查为何这条 case 走丢了路径，**不**调整 expected 凑分数

## 8. 验收

### 输入

```bash
python -m eval.runner_v2 \
    --dataset eval/data/cases_v2.json \
    --output /tmp/eval_acceptance/ \
    --no-judge \
    --concurrency 2
```

### 期望（2026-06 基线）

| 指标 | 实测 | 通过门槛 |
|---|---|---|
| avg_phase_coverage | ~0.85 | ≥ 0.80 |
| avg_kg_path_coverage | ~0.78 | ≥ 0.65 |
| route_accuracy | ~0.95 | ≥ 0.85 |
| fault_kind_accuracy | ~0.75 | ≥ 0.70 |
| recall_at_10 | ~0.15 | 不作门槛（noisy） |
| grounded_rate | ~0.40-0.80 | 不作门槛（noisy） |
| abstention_rate | ~0.30-0.55 | 5%-50% 视为健康 |
| Wall time | ~35-45 min (22 cases × ~120 s avg) | < 60 min |

## 9. 上下游

| 关系 | 模块 |
|---|---|
| **依赖** | M7 Diagnosis Agent (`diagnose()`)/ M1 LLM Provider (judge)/ M2 Storage (`eval_results` 写入) |
| **被调用** | CI / 手动 / 开发者 |
| **下游消费** | `docs/v2/v2_acceptance_report.md` / 报告 |
| **配置 keys** | `eval.judge_model` / `eval.concurrency` |
| **重要 ADRs** | ADR-process-compliance-kpi |

---

> **重建校对**：跑 § 8 完整 eval，验证 process compliance ≥ 0.80；检查 `/tmp/eval_acceptance/summary.json` 顶部输出有 PRIMARY KPI 模块（不是只有 secondary）。

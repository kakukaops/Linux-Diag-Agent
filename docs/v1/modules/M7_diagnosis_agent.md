# M7 — 诊断 Agent 设计文档

> **⚠️ 设计规格文档**：本文档为实施前的原始设计规格（定稿于 2026-05-15）。实际实现以代码为准，两者可能存在偏差。如需了解当前实现状态，请阅读对应目录下的 `CLAUDE.md` 和源代码。


| 字段 | 值 |
|------|---|
| 模块编号 | M7 |
| 状态 | Design Locked（待实施） |
| 关联文档 | [PRD.md](../PRD.md) · [Architecture.md](../Architecture.md) · [M1](M1_llm_provider.md) · [M5](M5_retrieval_orchestration.md) · [M6](M6_host_mcp_tools.md) · [Architecture](../Architecture.md) |
| 关联 ADR | [ADR-001](../adr/ADR-001-no-embedding.md) · [ADR-004](../adr/ADR-004-claude-code-provider.md) · [ADR-015](../adr/ADR-015-m7-diagnosis-strategy.md) |
| 最后更新 | 2026-05-14 |

---

## 1. 目标与边界

### 1.1 目标

接收用户上传的故障证据（dmesg / sosreport / 自然语言描述），通过**双层 agent 协同**（Triage 快速分类 → Diagnosis 深度推理）输出**带证据链的结构化诊断报告**。这是整个项目的"大脑"模块。

### 1.2 关键决策（[ADR-015](../adr/ADR-015-m7-diagnosis-strategy.md)）

| # | 决策 |
|---|------|
| D1 | Agent 编排框架 = **LangGraph** |
| D2 | Claim-Evidence Binding = **Reject + Regenerate ≤ 2 次**，超限标 `speculative` |
| D3 | v1.0 SOP 覆盖 = **3 个故障域**（OOM / lockup / panic）；v1.2 扩 5，v1.3 扩 8 |
| D4 | Self-Consistency 默认 K=1；评测脚本可显式 K=3；Pro budget < 10 messages 强制 K=1 |
| D5 | 报告输出 = **Markdown 默认 + JSON 双输出** |

### 1.3 在范围

| 子能力 | 形态 |
|--------|------|
| Triage Agent（ReAct）| 解析输入、抽结构化字段、简单情况直答 |
| Diagnosis Agent（Hypothesis-Driven）| 生成 3-5 假设 → 并行验证 → 收敛 |
| SOP yaml（v1.0 = 3 个）| OOM / lockup / panic 决策树 + generic fallback |
| Self-Consistency 采样 | K=1 默认 |
| Claim-Evidence Binding | 强制每条结论挂证据 |
| 报告生成 | Markdown + JSON |
| CLI 接口（`diagnose` 子命令）| 用户入口 |
| trace_id 串联 | 可观测性 |
| 状态 checkpointing | LangGraph 原生 |

### 1.4 不在范围

| 项 | 处理位置 |
|----|---------|
| 直接检索调用 | M5 retrieve API |
| MCP 工具调用 | M6 |
| LLM 调用底层 | M1 Provider |
| 评测 / 度量 | M8 |
| Web UI | v2+ |
| 自动修复执行 | 永不做（PRD 锁定） |

## 2. 整体架构

```
┌────────────────────────────────────────────────────────────────────────┐
│                User CLI: diag-agent diagnose                           │
│   --upload dmesg=... --upload sosreport=... --description "..."        │
└───────────────────────────────┬────────────────────────────────────────┘
                                ▼
                    ┌───────────────────────┐
                    │ DiagnosisSession      │
                    │ trace_id = uuid       │
                    └────────┬──────────────┘
                             │
                             ▼
              ┌─────────────────────────────┐
              │ Triage Agent (LangGraph)    │
              │ - ReAct loop                │
              │ - 解析输入                   │
              │ - 抽 kernel_version / fault │
              │ - 已知模式 → 直答           │
              │ - 复杂 → 转 Diagnosis       │
              └──────────────┬──────────────┘
                             │
                  ┌──────────┴──────────┐
                  ▼                     ▼
        [SimpleAnswer]      ┌─────────────────────────────┐
                            │ Diagnosis Agent (LangGraph) │
                            │ Hypothesis-Driven + SOP     │
                            │                             │
                            │ 1. Generate 3-5 hypotheses  │
                            │ 2. Verify in parallel       │
                            │      via M5/M6              │
                            │ 3. Self-Consistency (K=1/3) │
                            │ 4. Claim-Evidence Binding   │
                            │ 5. Report Generation        │
                            └──────────────┬──────────────┘
                                           │
                                           ▼
                            ┌─────────────────────────────┐
                            │ Structured Report           │
                            │ (Markdown + JSON)           │
                            └─────────────────────────────┘

[依赖]
M1 LLM Provider → 所有 LLM 调用（claude_code provider）
M5 Retrieval    → 证据检索
M6 MCP Tools    → dmesg-journal / sosreport 解析
M4 CodeGraph    → 通过 M5 间接访问
```

## 3. Triage Agent 设计

### 3.1 LangGraph 状态机

```python
from langgraph.graph import StateGraph, END

class TriageState(TypedDict):
    trace_id: str
    upload_manifest: dict
    nl_description: str
    
    # 中间产物
    parsed_artifacts: dict          # M6 输出（OopsReport / SystemSummary / ...）
    extracted_fields: dict          # kernel_version, fault_domain, stack_frames
    classification: str             # 'simple' | 'complex' | 'unknown'
    
    # 出口
    direct_answer: str | None
    escalate_to_diagnosis: bool

def build_triage_graph():
    g = StateGraph(TriageState)
    g.add_node("parse_artifacts", parse_artifacts_node)
    g.add_node("extract_fields", extract_fields_node)
    g.add_node("classify", classify_node)
    g.add_node("answer_simple", answer_simple_node)
    g.add_node("prepare_diagnosis", prepare_diagnosis_node)
    
    g.set_entry_point("parse_artifacts")
    g.add_edge("parse_artifacts", "extract_fields")
    g.add_edge("extract_fields", "classify")
    g.add_conditional_edges(
        "classify",
        lambda s: 'simple' if s['classification'] == 'simple' else 'complex',
        {'simple': 'answer_simple', 'complex': 'prepare_diagnosis'}
    )
    g.add_edge("answer_simple", END)
    g.add_edge("prepare_diagnosis", END)
    return g.compile()
```

### 3.2 节点实现

#### parse_artifacts

调 M6 MCP 工具解析上传文件：

```python
async def parse_artifacts_node(state: TriageState) -> dict:
    manifest = state['upload_manifest']
    artifacts = {}
    
    for upload in manifest['uploads']:
        if upload['kind'] == 'dmesg':
            artifacts['oops_reports'] = await mcp_dmesg.parse_dmesg(
                file_path=upload['stored']
            )
            artifacts['oom_reports'] = await mcp_dmesg.extract_oom_kill(
                file_path=upload['stored']
            )
            artifacts['lockup_reports'] = await mcp_dmesg.extract_lockdep_report(
                file_path=upload['stored']
            )
        elif upload['kind'] == 'sosreport':
            artifacts['sos_manifest'] = await mcp_sosreport.extract_sosreport(
                archive_path=upload['stored']
            )
            artifacts['system_summary'] = await mcp_sosreport.get_system_summary(
                extract_dir=artifacts['sos_manifest']['extract_dir']
            )
        elif upload['kind'] == 'journal':
            artifacts['journal_entries'] = await mcp_dmesg.query_journal(
                file_path=upload['stored']
            )
    
    return {'parsed_artifacts': artifacts}
```

#### extract_fields

从 artifacts + NL 抽结构化字段：

```python
async def extract_fields_node(state: TriageState) -> dict:
    fields = {}
    artifacts = state['parsed_artifacts']
    
    # kernel_version: 从 sosreport SystemSummary 优先
    sos = artifacts.get('system_summary')
    if sos:
        fields['kernel_version'] = sos.get('kernel_version_display')
    
    # fault_domain: 从 reports 类型推断
    if artifacts.get('oom_reports'):
        fields['fault_domain'] = 'oom'
    elif artifacts.get('lockup_reports'):
        fields['fault_domain'] = 'soft_lockup' if 'soft' in str(artifacts['lockup_reports']) else 'hard_lockup'
    elif artifacts.get('oops_reports'):
        fields['fault_domain'] = 'panic'
    
    # stack_frames: 从最早 oops 取栈顶 5 帧
    if artifacts.get('oops_reports'):
        first = artifacts['oops_reports'][0]
        fields['stack_frames'] = [f.function for f in first.stack_frames[:5]]
    
    # 补足：调 LLM 做兜底（与 M5 query_parser 类似）
    if not fields.get('kernel_version') or not fields.get('fault_domain'):
        llm_extracted = await navigator_llm.extract_fields(
            nl=state['nl_description'],
            artifacts_summary=summarize(artifacts)
        )
        fields = {**llm_extracted, **fields}  # 已抽字段不被覆盖
    
    return {'extracted_fields': fields}
```

#### classify

判简单 vs 复杂：

```python
CLASSIFY_PROMPT = """\
You are a Linux kernel diagnosis triage system. Given the extracted fields and 
the parsed artifacts summary, classify this case:

- "simple": A direct answer can be given without deep investigation (e.g., 
  known config errors, documentation lookups, command usage questions)
- "complex": Requires hypothesis-driven diagnosis with evidence collection 
  (any oops / panic / lockup / OOM / unknown pattern)

Extracted fields: {fields}
Artifacts summary: {summary}

Return JSON: {{"classification": "simple"|"complex", "reason": "..."}}
"""

async def classify_node(state: TriageState) -> dict:
    response = await navigator_llm.chat(
        messages=[
            {"role": "system", "content": "Triage classifier."},
            {"role": "user", "content": CLASSIFY_PROMPT.format(
                fields=state['extracted_fields'],
                summary=summarize(state['parsed_artifacts'])
            )}
        ],
        response_format={"type": "json_object"},
        temperature=0.0,
    )
    
    parsed = json.loads(response.content)
    return {'classification': parsed['classification']}
```

### 3.3 简单 vs 复杂的边界

| 简单（直答）| 复杂（转 Diagnosis）|
|-----------|------------------|
| 已知配置错误（如 `vm.overcommit_memory=0` 解释）| 任何 oops / panic |
| 单条 journalctl error 解释含义 | 多源证据需要交叉验证 |
| 文档查询（"什么是 PSI"）| 需要时间线推理 |
| 命令使用方法咨询 | 涉及内核内部机制 |

判断成本：1 Navigator message。

### 3.4 Triage 输出

```python
@dataclass
class TriageResult:
    direct_answer: str | None
    escalate: bool
    extracted_fields: dict
    parsed_artifacts: dict
    trace_id: str
```

## 4. Diagnosis Agent 设计

### 4.1 LangGraph 状态机

```python
class DiagnosisState(TypedDict):
    trace_id: str
    extracted_fields: dict
    parsed_artifacts: dict
    
    # SOP 路由
    matched_sop: str | None
    
    # Hypothesis 流程
    hypotheses: list[Hypothesis]
    
    # Self-Consistency
    self_consistency_k: int
    consistency_runs: list[list[Hypothesis]]
    
    # 收敛
    final_winning_hypothesis: Hypothesis | None
    rejected_hypotheses: list[dict]
    
    # 报告
    report_draft: str
    report_markdown: str
    report_json: dict
    
    # Claim-Evidence Binding
    binding_attempts: int
    invalid_claims: list

def build_diagnosis_graph():
    g = StateGraph(DiagnosisState)
    g.add_node("route_sop", route_sop_node)
    g.add_node("generate_hypotheses", generate_hypotheses_node)
    g.add_node("verify_hypotheses", verify_hypotheses_parallel_node)
    g.add_node("self_consistency_loop", self_consistency_loop_node)
    g.add_node("converge", converge_node)
    g.add_node("draft_report", draft_report_node)
    g.add_node("bind_evidence", bind_evidence_node)
    g.add_node("regenerate_report", regenerate_report_node)
    g.add_node("finalize_report", finalize_report_node)
    
    g.set_entry_point("route_sop")
    g.add_edge("route_sop", "generate_hypotheses")
    g.add_edge("generate_hypotheses", "verify_hypotheses")
    
    g.add_conditional_edges(
        "verify_hypotheses",
        lambda s: 'loop' if s['self_consistency_k'] > 1 and 
                            len(s['consistency_runs']) < s['self_consistency_k']
                  else 'converge',
        {'loop': 'self_consistency_loop', 'converge': 'converge'}
    )
    g.add_edge("self_consistency_loop", "verify_hypotheses")
    g.add_edge("converge", "draft_report")
    g.add_edge("draft_report", "bind_evidence")
    
    g.add_conditional_edges(
        "bind_evidence",
        lambda s: 'finalize' if not s['invalid_claims'] or s['binding_attempts'] >= 2
                  else 'regenerate',
        {'finalize': 'finalize_report', 'regenerate': 'regenerate_report'}
    )
    g.add_edge("regenerate_report", "bind_evidence")
    g.add_edge("finalize_report", END)
    return g.compile()
```

### 4.2 类型定义

```python
@dataclass
class Hypothesis:
    id: str                          # 'H1', 'H2', ...
    title: str
    rationale: str
    prior_probability: float
    posterior_probability: float | None
    evidence_needed: list[str]
    evidence_collected: list[EvidenceRef]
    verification_result: Literal['supported', 'refuted', 'inconclusive']
    refute_reason: str | None

@dataclass
class EvidenceRef:
    ref: str                         # M5/M6 规范引用
    snippet: str
    source: str
    score: float
    supports_hypothesis_ids: list[str]
    refutes_hypothesis_ids: list[str]
```

## 5. SOP 路由与覆盖

### 5.1 v1.0 SOP 清单（3 个）

| SOP id | 故障域 | 触发条件 |
|--------|--------|----------|
| `oom_diagnosis` | OOM | dmesg "Out of memory" / "oom_kill_process" / OOMReport 非空 |
| `lockup_diagnosis` | soft / hard lockup | dmesg "soft lockup" / "hard lockup" / "rcu_sched stall" |
| `panic_diagnosis` | kernel panic / oops | dmesg "Unable to handle" / "BUG:" / "kernel BUG at" / OopsReport 非空 |
| `generic_diagnosis` | fallback | 无匹配时使用 |

v1.2 扩 5（+ deadlock + I/O hang）；v1.3 扩 8（+ network + perf regression + sched anomaly）。

### 5.2 SOP yaml 样例（oom_diagnosis）

```yaml
sop_id: oom_diagnosis
version: 1.0
applies_to:
  domains: [oom, memory]
  kernel_versions: [v6.6, v5.10]
  triggers:
    - artifact_field: parsed_artifacts.oom_reports
      condition: not_empty
    - artifact_field: parsed_artifacts.oops_reports
      condition: signature_contains
      value: "oom_kill_process"

default_hypotheses:
  - id: H1_memory_leak_userspace
    title: 用户态进程内存泄漏
    rationale: 长时间 RSS 增长导致系统内存耗尽
    prior_probability: 0.25
    evidence_needed:
      - "RSS 增长趋势（journal + sosreport）"
      - "目标进程 cgroup memory.current 长期增长"
    tools:
      - mcp-sosreport.read_sos_file (path=sos_commands/memory/free_-m)
      - M5.retrieve (query="memory leak", routes=['lkml', 'bug'])
  
  - id: H2_fragmentation
    title: 高阶分配 + 内存碎片化
    rationale: order >= 4 的 alloc 失败，typical buddy 碎片化
    prior_probability: 0.30
    evidence_needed:
      - "/proc/buddyinfo 显示高阶 freelist 空"
      - "OOM 报告 order >= 4"
    tools:
      - mcp-sosreport.read_sos_file (path=proc/buddyinfo)
      - codegraph.lookup_symbol (symbol=alloc_pages_slowpath)
  
  - id: H3_cgroup_misconfig
    title: cgroup memory limit 误配
    rationale: memcg 限制不足或意外触发
    prior_probability: 0.20
    evidence_needed:
      - "OOM 报告 oom_memcg 字段非空"
      - "memcg events 计数"
  
  - id: H4_kernel_regression
    title: 内核 mm 子系统回归
    rationale: 近期 commit 引入 OOM 路径 bug
    prior_probability: 0.25
    evidence_needed:
      - "近 N 周 mm 子系统 commit 含 OOM 相关修改"
      - "syzbot 相似 stack"
    tools:
      - M5.retrieve (query="OOM regression mm", routes=['commit', 'syzbot'])

report_template: standard_v1
```

### 5.3 SOP 路由实现

```python
async def route_sop_node(state: DiagnosisState) -> dict:
    fault = state['extracted_fields'].get('fault_domain')
    
    # 精确匹配
    if fault:
        sop = SOP_REGISTRY.find_by_domain(fault)
        if sop:
            return {'matched_sop': sop.sop_id}
    
    # 触发条件匹配（artifact 驱动）
    for sop in SOP_REGISTRY.list():
        if sop.matches_triggers(state['parsed_artifacts']):
            return {'matched_sop': sop.sop_id}
    
    # LLM 兜底
    sop_id = await navigator_llm.classify_sop(
        oops_text=summarize(state['parsed_artifacts']),
        candidates=[s.sop_id for s in SOP_REGISTRY.list()]
    )
    if sop_id and sop_id in SOP_REGISTRY:
        return {'matched_sop': sop_id}
    
    # 全部失败 → generic
    return {'matched_sop': 'generic_diagnosis'}
```

## 6. Hypothesis 生成与验证

### 6.1 生成 prompt

```python
GENERATE_HYPOTHESES_PROMPT = """\
You are a Linux kernel diagnosis expert. Given the fault evidence below, 
generate {n} competing hypotheses for the root cause.

Each hypothesis should:
- Be specific and falsifiable
- Have a clear evidence_needed list
- Include initial prior_probability (sum to ~1.0 across all)

Fault evidence:
- Kernel version: {kernel_version}
- Fault domain: {fault_domain}
- Oops signature: {oops_signature}
- Top stack: {stack_frames_top5}

SOP default hypotheses (use as starting point, can extend/replace):
{sop_defaults}

Return JSON:
{{
  "hypotheses": [
    {{
      "id": "H1",
      "title": "...",
      "rationale": "...",
      "prior_probability": 0.4,
      "evidence_needed": ["...", "..."]
    }},
    ...
  ]
}}
"""
```

### 6.2 并行验证

```python
async def verify_hypotheses_parallel_node(state):
    hypotheses = state['hypotheses']
    
    # 每个假设独立 collect evidence
    tasks = [_verify_hypothesis(h, state) for h in hypotheses]
    verified = await asyncio.gather(*tasks)
    
    return {'hypotheses': verified}


async def _verify_hypothesis(h: Hypothesis, state: DiagnosisState):
    evidence = []
    
    # 1. 调 M5 retrieve（针对每个 evidence_needed）
    for need in h.evidence_needed:
        query = RetrievalQuery(
            natural_language=need,
            kernel_version=state['extracted_fields'].get('kernel_version'),
            stack_frames=state['extracted_fields'].get('stack_frames'),
            trace_id=state['trace_id'],
            requested_by='diagnosis'
        )
        retrieved = await m5.retrieve(query)
        evidence.extend([EvidenceRef.from_m5(e) for e in retrieved[:3]])
    
    # 2. 调 M6（针对结构化字段验证）
    if 'oom' in h.id.lower() and state['parsed_artifacts'].get('oom_reports'):
        for oom_report in state['parsed_artifacts']['oom_reports']:
            evidence.append(EvidenceRef(
                ref=f"dmesg:{state['trace_id']}:oom@line={oom_report.line}",
                snippet=str(oom_report)[:500],
                source='dmesg',
                score=1.0,
                supports_hypothesis_ids=[h.id],
                refutes_hypothesis_ids=[]
            ))
    
    # 3. LLM 判定：证据支持 / 驳斥 / inconclusive
    verdict = await chat_llm.judge_hypothesis(h, evidence)
    
    h.evidence_collected = evidence
    h.verification_result = verdict['result']
    h.refute_reason = verdict.get('refute_reason')
    h.posterior_probability = verdict.get('posterior', h.prior_probability)
    
    return h
```

### 6.3 收敛

```python
def converge(verified: list[Hypothesis]) -> Hypothesis:
    """挑选 posterior 最高且未被驳斥的假设。"""
    
    supported = [h for h in verified if h.verification_result == 'supported']
    if supported:
        return max(supported, key=lambda h: h.posterior_probability)
    
    # 都未支持 → 取 inconclusive 中后验最高
    inconclusive = [h for h in verified if h.verification_result == 'inconclusive']
    if inconclusive:
        return max(inconclusive, key=lambda h: h.posterior_probability)
    
    # 全被驳斥 → 取第一个（fallback）
    return verified[0]
```

## 7. Self-Consistency（K=3 评测可选）

### 7.1 实现

```python
async def self_consistency_loop_node(state):
    """K > 1 时跑 K 次独立 verify，投票。"""
    
    if state['self_consistency_k'] <= 1:
        return state
    
    # 重新生成 hypotheses（不复用，确保独立性）
    new_hypotheses = await _generate_hypotheses(state, fresh=True)
    new_verified = await _verify_hypotheses(new_hypotheses, state)
    
    state['consistency_runs'].append(new_verified)
    state['hypotheses'] = new_verified
    return state


def majority_vote(runs: list[list[Hypothesis]]) -> Hypothesis:
    winners = [converge(run) for run in runs]
    titles = [w.title for w in winners]
    most_common, count = Counter(titles).most_common(1)[0]
    return next(w for w in winners if w.title == most_common)
```

### 7.2 触发条件

| 场景 | K |
|------|---|
| v1.0 CLI 默认 | 1 |
| 评测脚本 `--self-consistency-k 3` | 3 |
| Pro budget < 10 messages | 强制 1（覆盖用户参数）|
| 高优先级 incident（v1.2+ 评估）| 用户提示后可选 3 |

### 7.3 Pro 配额影响

- K=1：每诊断 ~3-5 messages
- K=3：每诊断 ~9-15 messages
- Pro 45/5h → K=1 时 9-15 诊断；K=3 时 3-5 诊断

## 8. Claim-Evidence Binding（Reject + Regenerate）

### 8.1 流程

```python
async def bind_evidence_node(state):
    """对 draft report 做 claim-evidence 校验。"""
    
    draft = state['report_draft']
    winning = state['final_winning_hypothesis']
    
    # 1. 解析 draft，抽取所有 [REF] 标记的 claim
    claims = parse_claims_from_draft(draft)
    
    # 2. 校验每条 claim 的 REF 存在于 evidence
    invalid = []
    for claim in claims:
        for ref in claim.refs:
            if not _ref_format_valid(ref):
                invalid.append((claim, ref, 'invalid_format'))
                continue
            if not _ref_in_evidence(ref, winning.evidence_collected):
                invalid.append((claim, ref, 'not_in_evidence'))
    
    return {'invalid_claims': invalid}


async def regenerate_report_node(state):
    """有 invalid claims 时重生成。"""
    
    attempts = state.get('binding_attempts', 0) + 1
    
    new_draft = await chat_llm.regenerate_with_feedback(
        old_draft=state['report_draft'],
        invalid_claims=state['invalid_claims'],
        evidence=state['final_winning_hypothesis'].evidence_collected,
        attempt=attempts
    )
    
    return {
        'report_draft': new_draft,
        'binding_attempts': attempts,
    }


async def finalize_report_node(state):
    """生成最终 Markdown + JSON。"""
    
    draft = state['report_draft']
    invalid = state['invalid_claims']
    
    # 仍有 invalid（已 2 次重试失败）→ 标 speculative
    if invalid:
        draft = _mark_speculative(draft, invalid)
        metrics.m7_speculative_claims_total.inc(len(invalid))
    
    md = _to_markdown(draft, state)
    js = _to_json(state)
    
    return {
        'report_markdown': md,
        'report_json': js,
    }
```

### 8.2 ref 格式校验

```python
VALID_REF_PATTERNS = {
    'code': r'^[\w\-/.]+:[\w\-/.]+:\d+(-\d+)?$',
    'doc': r'^[\w\-/.]+:[\w\-/.]+#[\w\-.]+$',
    'lkml': r'^<[^<>@\s]+@[^<>\s]+>$',
    'bug': r'^[\w.]+:[\w\-]+$',
    'syzbot': r'^syzbot:[a-f0-9]{32}$',
    'commit': r'^[a-f0-9]{7,40}$',
    'cve': r'^CVE-\d{4}-\d+$',
    'dmesg': r'^dmesg:[\w\-]+:line=\d+(:.*)?$',
    'sosreport': r'^sosreport:[\w\-]+:[^\s]+$',
}
```

任何 claim 引用的 ref 必须符合 pattern 且能在 `evidence_collected` 中找到对应原始记录。

### 8.3 重生成 prompt

```python
REGENERATE_PROMPT = """\
The previous report contains claims with invalid evidence references:

{invalid_claims_formatted}

Available evidence references:
{evidence_refs_list}

Please regenerate the report. Rules:
1. Every factual claim MUST cite at least one ref from the available list
2. Use the exact ref format (no paraphrasing)
3. If you cannot find evidence for a claim, omit it
4. Preserve the report structure

Previous draft:
---
{old_draft}
---
"""
```

## 9. 报告生成

### 9.1 Markdown 模板（standard_v1）

```markdown
# Diagnosis Report

**Run ID**: {run_id}  
**Generated**: {iso_timestamp}  
**Agent version**: v1.0  
**Kernel version**: {kernel_version_detected} (CodeGraph repo: {codegraph_repo})  
**Fault domain**: {fault_domain}  
**SOP**: {sop_id}  
**Self-Consistency K**: {k}

## 根因诊断

{winning_hypothesis_title}

**置信度**: {posterior_percent}%

## 竞争假设

| ID | 假设 | 结论 | 理由 |
|----|------|------|------|
{hypothesis_table_rows}

## 证据链

{evidence_chain_sections}

## 排查建议

{investigation_steps}

## 修复建议

- **短期**: {short_term_fixes}
- **中期**: {medium_term_fixes}
- **长期**: {long_term_fixes}

---
*Generated by Linux-Diag-Agent. All claims bound to verifiable evidence refs.*
{speculative_notice_if_any}
```

### 9.2 JSON Schema

```json
{
  "run_id": "abc-123-def",
  "generated_at": "ISO8601",
  "agent_version": "v1.0",
  "kernel_version_detected": "5.10.198",
  "codegraph_repo": "olk-kernel-v5.10",
  "fault_domain": "oom",
  "sop_id": "oom_diagnosis",
  "self_consistency_k": 1,
  
  "winning_hypothesis": {
    "id": "H2",
    "title": "...",
    "confidence": 0.85,
    "evidence_refs": [...]
  },
  
  "rejected_hypotheses": [
    {"id": "H1", "title": "...", "refute_reason": "...", "refute_evidence_refs": [...]}
  ],
  
  "evidence_chain": [
    {"ref": "...", "snippet": "...", "supports": [...], "refutes": [...]}
  ],
  
  "investigation_steps": [...],
  "fix_suggestions": {"short_term": [...], "medium_term": [...], "long_term": [...]},
  
  "metadata": {
    "llm_messages_used": 4,
    "retrieval_calls": 6,
    "mcp_tool_calls": 2,
    "duration_seconds": 38.2,
    "invalid_claims_remaining": 0,
    "regenerate_attempts": 0,
    "speculative_claims": []
  }
}
```

## 10. CLI 接口

```bash
diag-agent diagnose \
  --upload dmesg=/path/to/dmesg.txt \
  --upload journal=/path/to/journalctl.json \
  --upload sosreport=/path/to/sosreport.tar.xz \
  --description "kernel panic during boot on production server" \
  --kernel-version v5.10 \              # 可选，若 sosreport 已含则覆盖
  --self-consistency-k 1 \              # 1 默认, 3 评测
  --output report.md \
  --output-json report.json \
  --verbose                             # 实时打印 LangGraph stream events
```

CLI 内部：
1. 创建 run_id（uuid）
2. 拷贝 uploads 到 `data/uploads/<run_id>/`
3. 生成 manifest.json
4. 启动 DiagnosisSession：Triage → (可能转) Diagnosis
5. 流式输出（LangGraph stream mode）
6. 写 report.md + report.json

## 11. 状态管理与 checkpointing

LangGraph 自带 SQLite/PG checkpointer：

```sql
CREATE TABLE diagnosis_checkpoints (
    run_id          TEXT NOT NULL,
    checkpoint_id   BIGSERIAL,
    node_name       TEXT NOT NULL,
    state_blob      JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    PRIMARY KEY (run_id, checkpoint_id)
);
CREATE INDEX idx_dc_run ON diagnosis_checkpoints (run_id, created_at);
```

每个 node 完成后落 checkpoint。失败时可从 last checkpoint 恢复。

## 12. 可观测性

Trace 串联：每次 diagnose = 一个 trace_id，贯穿所有 LLM/M5/M6 调用。

Prometheus metrics：
- `m7_diagnosis_run_total{result=success|failed|degraded}`
- `m7_diagnosis_duration_seconds`
- `m7_diagnosis_llm_messages_total{phase=triage|hypotheses|verify|report}`
- `m7_diagnosis_evidence_per_hypothesis`
- `m7_self_consistency_disagreements_total`
- `m7_invalid_claims_total`（拒绝重生成）
- `m7_speculative_claims_total`（最终未解决）
- `m7_regenerate_attempts_total`

LangGraph 自带 OpenTelemetry tracing，可对接 Jaeger / Zipkin。

## 13. 降级模式

| 场景 | 行为 |
|------|------|
| CodeGraph HTTP 不可达 | 仅依赖 M6 + 我方 PG/Neo4j；报告标注 "code context unavailable" |
| M5 retrieve 全失败 | 仅依赖 M6 解析结果 + 自然语言推理；报告标注 "limited evidence" |
| Pro budget < 10 messages | 强制 K=1；禁 self-consistency；prompt 用户等下个 5h 窗口 |
| Triage classify 失败 | 强制走 Diagnosis（fallback） |
| Diagnosis 超时（> 5min）| 返回最后一轮收敛结果 + warning |
| Claim binding 2 次重试仍失败 | 标 speculative，仍输出（不死锁）|

## 14. 文件结构

```
agent/
├── __init__.py
├── session.py                       # DiagnosisSession 入口
├── triage/
│   ├── __init__.py
│   ├── graph.py                     # LangGraph TriageState
│   ├── nodes.py
│   └── prompts.py
├── diagnosis/
│   ├── __init__.py
│   ├── graph.py
│   ├── nodes.py
│   ├── prompts.py
│   ├── hypothesis.py
│   ├── verification.py
│   └── consistency.py
├── sop/
│   ├── __init__.py
│   ├── registry.py
│   ├── oom_diagnosis.yaml
│   ├── lockup_diagnosis.yaml
│   ├── panic_diagnosis.yaml
│   └── generic_diagnosis.yaml
├── binding/
│   ├── __init__.py
│   ├── claim_parser.py
│   ├── ref_validator.py
│   └── regenerate.py
├── report/
│   ├── __init__.py
│   ├── markdown_template.py
│   ├── json_schema.py
│   └── templates/
│       └── standard_v1.md.j2
└── tests/

ui/
└── cli/
    ├── diagnose.py
    └── tests/
```

## 15. 测试策略

| 层 | 测什么 |
|----|--------|
| 单元 | LangGraph node 逻辑、claim parser、ref validator、SOP yaml schema |
| 集成 | mock M5/M6，跑完整 LangGraph 流程，校验 final report 字段 |
| Fixture | 各 SOP 域有真实 oops + sosreport 样本（v1.0 至少 5 例/SOP）|
| Pro budget | 跑 30 例评测核对 messages_used 与预算 |
| 评测 | 30 例公开案例根因正确率 ≥ 60%（v1.2 验收）|

## 16. 已知风险与监控

| 风险 | 监控 | 缓解 |
|------|------|------|
| Hypothesis 生成偏离 SOP 默认（不收敛）| `m7_hypotheses_outside_sop` | prompt 加严，强制至少 50% 来自 SOP defaults |
| Claim-Evidence binding 失败率高 | `m7_invalid_claims_total / m7_diagnosis_run_total` | prompt 调优 + few-shot binding 示例 |
| Self-Consistency K=3 配额耗尽 | budget remaining | budget < 10 强制 K=1 |
| LangGraph state 过大（>10MB）| state_blob size | 中间产物用 ref 替代（保存路径而非完整 blob）|
| SOP yaml 误配 | startup schema validation | 启动时 validate all SOP yaml 文件 |
| 报告中 speculative 占比高 | `m7_speculative_claims_total` | 评测后 prompt 微调 |

## 17. v1.0 → v1.3 演进

| 阶段 | 主要变化 |
|------|---------|
| v1.0 | LangGraph 双层 + 3 SOP + K=1 + Reject-Regenerate + Markdown/JSON 输出 |
| v1.1 | SOP 扩到 5（+ deadlock + I/O hang）；prompt few-shot 强化；evidence weighting 优化 |
| v1.2 | 30 例完整人工评测；根因正确率优化；SOP yaml 反馈调整；Self-Consistency K=3 仅评测启用 |
| v1.3 | SOP 扩到 8（+ network + perf + sched）；本地 vLLM 后 K=3 永久启用（无配额限制）；评估 weighted edges in Neo4j 反哺 verification |

## 18. 与外部组件的契约

- **M1 Provider**：所有 LLM 调用（triage / classify / generate_hypotheses / verify / regenerate / draft_report）走 chat_llm 或 navigator_llm；遵守 Pro 5h budget
- **M5 Retrieval**：每个 hypothesis verify 调 retrieve(query)；同时 Triage 阶段也可调
- **M6 MCP Tools**：Triage 调 mcp-dmesg-journal / mcp-sosreport 解析输入
- **M2 存储**：写 `diagnosis_checkpoints` 表；读 `kernel_commit` / `bug` 等做 fallback 查询
- **M8 评测**（待写）：通过 JSON 输出对接评测脚本，写 `eval_results` 表

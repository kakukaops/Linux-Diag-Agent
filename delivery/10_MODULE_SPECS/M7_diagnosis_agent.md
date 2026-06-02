# M7 · Diagnosis Agent

## 1. 目的

把 raw 输入（dmesg / sosreport / 文本）转成结构化诊断报告。M7 是**整个系统的指挥中枢**，编排 M5 retrieval + M6 MCP tools + 5 个 prompt 路由 + 32 个 ReAct 工具 + bind_claims + render report。

由两层组成：
1. **LangGraph 拓扑**——固定 9 节点 DAG，顺序执行 triage / 调度
2. **ReAct loop**（嵌在拓扑中的 `react_investigation` 节点）——可变步数的"推理 + 工具调用"循环，最多 15 iter / 250K token

## 2. 公共接口

```python
def diagnose(raw_input: str, lang: str = "en") -> dict:
    """
    入口函数。一次性走完整个拓扑，返回 state dict。
    state 含 keys（按拓扑先后填入）：
      raw_input, input_type, kernel_events, taint_flags, hardware_signals,
      io_hang_signals, fault_kind, fault_summary, sop_name, diagnostic_route,
      kernel_version, olk_version_tag, hostname,
      retrieval_query, evidence,
      react_verdict, react_final_answer, react_tool_trace, react_iterations,
      react_tokens_used,
      claims, verified_claims, speculative_claims, groundedness,
      report_md, report_json
    """
```

## 3. LangGraph 拓扑

```
                       diagnose(raw_input, lang)
                                │
                                ↓
   ┌────────────────  StateGraph  ────────────────┐
   │                                              │
   │  parse_input        ① 探测 input_type        │
   │       │                (dmesg/sosreport/    │
   │       │                 dmesg_file/question)│
   │       ↓                                      │
   │  extract_events     ② 提取 KernelEvent       │
   │       │                + 抽取 kernel_version │
   │       ↓                                      │
   │  detect_taint_and_hw_signals                 │
   │       │                ③ taint + MCE/EDAC + │
   │       │                  I/O hang signals    │
   │       ↓                                      │
   │  classify_fault_and_route                    │
   │       │                ④ 决定 fault_kind +   │
   │       │                  sop_name +          │
   │       │                  diagnostic_route    │
   │       ↓                                      │
   │  retrieve           ⑤ M5 7-route BM25 召回   │
   │       │                                      │
   │       ↓                                      │
   │  react_investigation                         │
   │       │                ⑥ ReAct loop          │
   │       │                  (见 § 5)            │
   │       ↓                                      │
   │  bind_claims        ⑦ 从 final_answer 提取   │
   │       │                claims + 语义重叠校   │
   │       │                验 + Path C auto-     │
   │       │                attach rescue         │
   │       ↓                                      │
   │  generate_report    ⑧ render_md + render_   │
   │       │                json                  │
   │       ↓                                      │
   │     END                                      │
   └──────────────────────────────────────────────┘
```

## 4. 节点契约

### ① parse_input

| 输入 | 输出 |
|---|---|
| `raw_input: str` | `input_type ∈ {dmesg, dmesg_file, sosreport, question}` |

判定规则：
- 含 `[ ... ]` / `BUG:` / `WARNING:` / `Call Trace:` / `Oops:` → `dmesg`
- 像路径（≤ 260 字符、无空格、无换行、无 `?`、`/` ≤ 12）且 `Path().is_file()` 且后缀 `.xz/.gz/.bz2` 或含 `sosreport` → `sosreport`
- 像路径且 `Path().is_file()` → `dmesg_file`
- 像路径且 `Path().is_dir()` → `sosreport`（解压后的目录）
- 其它 → `question`

**关键 gotcha**：必须先做"像路径"形状检查，否则把 260+ 字符 prose 喂给 `Path().exists()` 会抛 `ENAMETOOLONG`。见 `60_GOTCHAS.md` §5.x。

### ② extract_events

- `dmesg` / `dmesg_file` → 跑 M6 `dmesg_journal.extract_events()`
- `sosreport` → 跑 M6 `sosreport.parser`，从中取 hostname + kernel_version + dmesg_tail 再 extract
- **额外职责（2026-06-01 新增）**：从 raw_input / dmesg_text 抽取 `kernel_version` + `olk_version_tag`（regex 见 § 4.5）

### ③ detect_taint_and_hw_signals

正则扫 `dmesg_text`：

| 信号 | 模式 | 用途 |
|---|---|---|
| taint 字母 | `extract_taint_letters(text)` → `["G","W","O","E",...]` | 提示给 prompt + agent 判 LKM 嫌疑 |
| hardware_error | `\bHardware Error\b` | → hardware route |
| mce | `\bMachine Check\b` / `^mce:` | → hardware route |
| edac_uncorrected | `EDAC.{0,40}(Uncorrected\|\bUE\b)` | → hardware route |
| taint_machine_check | taint 含 `M` | → hardware route |
| nvme_timeout | `nvme\d+:.*(timeout\|reset\|abort)` | → io_hang signal（**不**强制 hardware 路由，只 surface 给 prompt） |
| scsi_timeout | `sd \w+:.*(timing out\|aborting cmd)` | → io_hang signal |
| buffer_io_error | `Buffer I/O error on dev` | → io_hang signal |
| blk_io_error | `blk_update_request:.*I/O error` | → io_hang signal |
| san_fabric_event | `(scsi target\|qla\|lpfc\|bnx2fc).*(link offline\|Fabric)` | → io_hang signal |
| pcie_link_event | `pcieport.*AER\|PCIe Bus Error\|Link.*(retrain\|degraded)` | → io_hang signal |

### ④ classify_fault_and_route

#### fault_kind 决策

如果 ② 提取出 events：按优先级 `panic > oom > softlockup > hardlockup > rcu_stall > oops > bug > warn > lockdep` 取第一个出现的 kind。

如果 events 空：调 navigator LLM 分类，prompt 见 `agent/triage/nodes.py:_llm_classify`，返回 `{"fault_kind": ..., "fault_summary": ...}`。

#### route 决策（ADR-023）

```python
if has_hardware_signal:                    return "hardware"
if fault_kind in {panic, oops} and vmcore: return "kernel+vmcore"
if user_text implies_recent_change:        return "change"
if user_text contains CVE-ID:              return "kernel"
if fault_kind == "generic" and no events:  return "unknown"
                                           return "kernel"
```

`implies_recent_change` 中英文正则：`升级|内核更新|换内核|upgrad|update.*kernel|worked.*yesterday|after.*update` 等。

#### sop_name 决策

route=hardware → `"hardware"`，否则按 fault_kind 映射：

```python
{oom: "oom", softlockup: "lockup", hardlockup: "lockup", rcu_stall: "lockup",
 panic: "panic", oops: "generic", bug: "generic", warn: "generic",
 lockdep: "generic"}.get(fault_kind, "generic")
```

### 4.5 kernel_version 抽取（2026-06-01 加）

正则匹配 dmesg CPU 行 / `Linux version` 行的内核 release：

```python
_KVER_RE = re.compile(
    r"\b(\d+\.\d+\.\d+[-.][\w.+~+-]+?\."
    r"(?:x86_64|aarch64|ppc64le|s390x|i686|riscv64))\b"
)
_OE_TAG_RE = re.compile(r"\boe\d+(?:sp\d+)?\b", re.IGNORECASE)

# 抽取规则
full_version = _KVER_RE.search(text).group(1)        # 如 "6.6.0-21.0.0.21.oe2403.x86_64"
if _OE_TAG_RE.search(full_version):
    # 6.6.0-...oe2403.x86_64  →  OLK-6.6
    parts = full_version.split(".")
    olk_tag = f"OLK-{parts[0]}.{parts[1]}"            # "OLK-6.6"
```

**只有 state.kernel_version 已为空时才填**，不覆盖显式设置。

### ⑤ retrieve

调 M5（详见 `M5_retrieval.md`）。

### ⑥ react_investigation

调 ReAct loop（详见 § 5）。

### ⑦ bind_claims

把 `final_answer` 拆成原子 claim，每条带 `evidence_refs`，再做语义重叠校验。详见 § 6。

### ⑧ generate_report

调 `render_md(state, lang)` + `render_json(state)`。详见 `M8` 模块 + `agent/report/renderer.py`。

## 5. ReAct Loop 协议

### 5.1 入口

```python
def run_react_loop(*, provider, registry, route, system_prompt, user_prompt,
                   max_iter=15, on_event=None) -> ReactResult: ...
```

`on_event`（optional）让 web UI 实时拿到每步事件：`step_start` / `tool_call` / `terminate`。批 eval / CLI 路径不传。

### 5.2 不变量

| 常量 | 值 | 含义 |
|---|---|---|
| `MAX_ITER` | 15 | 硬迭代上限 |
| `REPEAT_LIMIT` | 3 | 同 `(tool, args)` 重复 / 失败 N 次 → 强制退出 |
| `TOKEN_BUDGET` | 250_000 | 累积 token 上限 |
| force_finalize 触发条件 | `step == MAX_ITER` 或 `tokens_used >= TOKEN_BUDGET * 0.85` | 此时 `tool_choice=none` + 追加 reminder，强制出最终答案 |

### 5.3 循环流程

```python
for step in 1..MAX_ITER:
    force_finalize = (step == MAX_ITER) or (tokens_used >= TOKEN_BUDGET * 0.85)
    if force_finalize and not finalize_reminder_added:
        messages.append(HARD_FINALIZE_REMINDER)  # 见 § 5.5
        finalize_reminder_added = True
    tool_choice = "none" if force_finalize else "auto"

    resp = provider.chat(ChatRequest(messages, tools=schemas, tool_choice=...))
    tokens_used += resp.usage.in + resp.usage.out
    emit(step_start)

    # 终止判定
    if finish_reason != "tool_calls" OR resp.tool_calls is empty
       OR "<final_answer>" in content OR "<insufficient_evidence>" in content:
        verdict = decide_verdict(content)        # diagnosed | insufficient_evidence
        # 5.4 fallback hallucination retry
        if force_finalize and has_DSML_pattern(content) and not hallucination_retry_used:
            messages.append(HARD_RESET_REMINDER)  # 见 § 5.6
            hallucination_retry_used = True
            continue
        emit(terminate)
        return ReactResult(verdict, content, step, ...)

    # 工具调用
    for tc in resp.tool_calls:
        result = registry.dispatch(tc.function.name, json.loads(tc.function.arguments))
        messages.append(Message(role=tool, tool_call_id=tc.id, content=result))
        trace.append({step, tool, args, result_preview[:300]})
        emit(tool_call)
        # 从 result 文本里抽 commit hashes（12-40 hex），加入 seen_hashes

    # ADR-019 D4 早退
    if any tool failed 3 times: emit(terminate insufficient_evidence); return
    if any (tool, args) called 3 times: emit(terminate max_iter); return
    if tokens_used >= TOKEN_BUDGET: emit(terminate budget_exhausted); return

# 自然到达 MAX_ITER
emit(terminate max_iter_reached); return
```

### 5.4 verdict 集合

| verdict | 触发条件 | 含义 |
|---|---|---|
| `diagnosed` | content 含 `<final_answer>` 且 stripped 非空 | 正常完成，有可执行答案 |
| `insufficient_evidence` | content 含 `<insufficient_evidence>` 或 REPEAT_LIMIT 失败 | agent 主动放弃，证据不够 |
| `max_iter_reached` | 自然到达 15 步 或 REPEAT_LIMIT 同 call | 步数耗尽 |
| `budget_exhausted` | tokens > 250K | 预算耗尽 |

### 5.5 `HARD_FINALIZE_REMINDER`（force_finalize 触发时追加）

完整文本见 `40_BEHAVIORAL_CONTRACTS.md` § force-finalize-reminder。要点：
- 明确告诉 LLM tool calling 已禁
- 列出 forbidden 模式（任何 tool-call syntax / DSML / `<invoke>` / 内部 special token）
- 唯一合法输出：`<final_answer>...</final_answer>` 或 `<insufficient_evidence>...</insufficient_evidence>`

### 5.6 `HARD_RESET_REMINDER`（DSML 幻觉 retry）

详见 `60_GOTCHAS.md` §4.1。一次性 retry，文本要求 agent 用纯文本回答，不要任何 tool-call 语法。

## 6. bind_claims 协议

### 输入
- `state.react_final_answer` — LLM 出的 `<final_answer>` body
- `state.evidence` — M5 + ReAct harvest 合并的 evidence pool

### 输出
- `state.claims` — 每条 `{text, evidence_refs[], verified, evidence_strength}`
- `state.verified_claims` / `state.speculative_claims` — 二分
- `state.groundedness ∈ {grounded, speculative, n/a}`

### 算法

```
1. evidence_by_hash = {ev.commit_hash: ev for ev in evidence}
   evidence_by_bug  = {...}
   evidence_by_msg  = {...}

2. prompt LLM 抽取 claims：
   "Analysis: <final_answer>
    Retrieved evidence pool (cite ONLY from this list):
      - commit_hash_12: <subject>
      - bug#123: <title>
      ...
    Extract factual claims; each claim's evidence_refs must contain IDs from above.
    Return JSON: [{text, evidence_refs}]"

3. 对每条 claim:
   (a) 把 refs 解析到 cited_evidence: list[dict]
   (b) evidence_strength = max(_claim_evidence_overlap(text, ev) for ev in cited_evidence)
        - overlap = significant_token_overlap_ratio (≥ 3 char + non-stopword) 见 _claim_evidence_overlap
   (c) Path C 救援：若 strength < 0.20 → 全 evidence 池里扫最强重叠，若 ≥ 0.20 自动 attach
       attached ref 标 "AUTO:<id>"
   (d) verified = bool(cited_evidence) and strength >= 0.20

4. groundedness:
   - 0 claims → "speculative"
   - >= 50% verified → "grounded"
   - else "speculative"
   - react_verdict != "diagnosed" → "n/a" (跳过整个 bind)
```

**关键常量**：`_OVERLAP_THRESHOLD = 0.20`，全局唯一阈值。

## 7. 已知陷阱（M7 独有）

### #1 · `parse_input` path-shape guard 不可省

直接 `Path(raw_input).exists()` 在长 prose 上会 `OSError: [Errno 36] File name too long`。必须先做形状检查（260 字符内 / 无空格换行问号 / `/` ≤ 12）。

### #2 · `extract_events` 的 OOM 头格式有 3 种

OOM 头有 3 种历史格式，全要识别：
```
oom-kill:constraint=CONSTRAINT_MEMCG,...,task=java,pid=N        ← 现代标准 head
Out of memory: Killed process N (java) total-vm:NkB...           ← 6.x 标准
Out of memory: Kill process N (java) score N or sacrifice child  ← 5.x legacy
```
任一个都视为 OOM event。

### #3 · `extract_call_trace` 必须先剥 timestamp

```
[12345.681001]  dump_stack_lvl+0x4d/0x6c
```

生产 dmesg 都有 `[<float>]` 前缀。剥前缀再匹配栈帧。`60_GOTCHAS.md` §5.2。

### #4 · ReAct loop 5 个 return 路径都必须 emit `terminate`

历史 bug：只有"自然 final_answer"路径 emit terminate，其它 4 个 fallback（REPEAT_LIMIT failures / REPEAT_LIMIT calls / TOKEN_BUDGET / MAX_ITER 自然）静默 return。导致 web UI spinner 永转。修复：每个 return 前都 emit。

### #5 · `tool_choice="none"` + DeepSeek = DSML 幻觉

LLM 不肯放弃 tool calling 时会幻觉 DSML token。检测 + 1 次 hard-reset retry。`60_GOTCHAS.md` §4.1。

### #6 · Backward-compat：`claims` 字段可能没分箱

老 state（来自 v2.0 的 bind_claims）只有 `claims` 数组、没 `verified_claims` / `speculative_claims` 分箱。`render_md` 要 fallback：

```python
if verified_claims is None or speculative_claims is None:
    legacy = state.get("claims", [])
    verified_claims = [c for c in legacy if c.get("verified")]
    speculative_claims = [c for c in legacy if not c.get("verified")]
```

> **see also**：`60_GOTCHAS.md` §5（Agent / Diagnosis 整章）

## 8. 验收

### 集成测试：smoke case

```bash
python 50_TEST_FIXTURES/run_acceptance.py --module M7 \
       --case 50_TEST_FIXTURES/cases_smoke.json
```

通过条件：
- 走完拓扑 8 个节点不抛错
- 至少调用 5 个不同的 ReAct 工具
- verdict ∈ {diagnosed, insufficient_evidence}
- final_answer 含 `## 修复建议` / `## Fix Recommendation`（按 lang）
- report_md 字符数 ≥ 1000
- 完整 trace 至少 8 步

### 单元测试

- `_extract_kernel_version("...6.6.0-21.0.0.21.oe2403.x86_64...")` → `("6.6.0-...x86_64", "OLK-6.6")`
- `parse_input(raw=<260+ char prose>)` 不抛 `ENAMETOOLONG`
- `extract_call_trace(<dmesg with [ts] prefix>)` 返回非空 frame list

## 9. 上下游

| 关系 | 模块 |
|---|---|
| **依赖** | M1 (`get_provider`) / M2 (`get_engine`) / M5 (`retrieve`) / M6 (dmesg / sosreport parser) |
| **依赖** | `20_PROMPT_ASSETS/*.md`（system prompt 模板，按 lang 选 `.md` 或 `.zh.md`） |
| **依赖** | `30_TOOL_CONTRACTS.md` 列出的 32 个 ReAct 工具实现 |
| **依赖** | `40_BEHAVIORAL_CONTRACTS.md` 的 TERMINATION_FOOTER（追加在所有 system_prompt 末尾） |
| **被调用** | M9 web UI（`stream_diagnose(raw_input, lang)` 流式包装） / CLI / M8 eval runner |
| **配置 keys** | `llm.chat.*` / `llm.navigator.*` / `app.langgraph_checkpointer` — 详见 `70_OPERATIONAL.md` |
| **重要 ADRs** | ADR-019 (hybrid pipeline) / ADR-023 (hardware-first routing) / ADR-024 (insufficient_evidence exit) |

---

> **重建校对**：实现完 M7 后跑 `run_acceptance.py --module M7`，验证 § 8 集成通过；grep 你的 ReAct loop 实现，确认 § 7 #4 (5 处 terminate emit) 和 #5 (DSML detection + retry) 都有代码体现。

# 40 · Behavioral Contracts（智能体"该怎么思考"）

> 这份文档定义 **ReAct agent 在调研期间必须遵守的行为协议**。它独立于工具实现（M5/M7）和 prompt 资产（20_PROMPT_ASSETS）。
>
> 重建时：你的 `system_prompt = route_template(填占位符) + io_hang_alert + TERMINATION_FOOTER + (optional fallback directive)`。完整 footer 文本见 § 4。

---

## 1. Phase 0-6 调研协议

| Phase | 名称 | 工具 | WHY |
|---|---|---|---|
| 0 | 同栈帧检索 | `find_similar_crashes` | "我以前见过没？" 优先且最便宜 |
| 1 | 符号反查 | `find_commits_touching_symbol` × 每个栈帧 | KG-edge reverse lookup，比 BM25 精准 |
| 2 | BM25 跨源搜索 | `search_commits` / `_lkml` / `_bugs` / `_syzbot` / `_cve` / `_code` | 多种说法表达同一症状 |
| 3 | 子系统浏览 | `browse_subsystem_fixes` | 1+2 找不到时的兜底 |
| 4 | 候选验证 | `get_commit_detail` / `_diff` / `check_backport_status` / `get_regression_fixes` / `find_syzbot_fixed_by_commit` / `lookup_subsystem_owner` | 找到候选 commit 后**必做**：读全文 + 读 diff + 检 revert chain + 查维护者 |
| 5 | KG-silent fallback | 自身内核知识 + first-principles | 当 Phase 1-4 都没结果，**不要立刻** `<insufficient_evidence>`：基于训练时知识给"低置信假设"，但**必须显式标注**"KG silent — prior knowledge" |
| 6 | 结构化输出 | `<final_answer>` 含 `## 根本原因` / `## 修复建议` (Form A/B/C) / `## 置信度` / `### 证据来源` | 把 Phase 0-5 成果按 SRE 友好格式呈现 |

详见 `20_PROMPT_ASSETS/kernel.zh.md`（或 `kernel.md`）正文。

---

## 2. 工具调用反模式（禁止）

### 2.1 死锚栈顶帧

```
栈帧:  tcp_v4_do_rcv ← _raw_spin_lock ← queued_spin_lock_slowpath  (栈顶)
        ↓
        tcp_v4_rcv     (真正的关键帧)
        ↓
        ip_protocol_deliver_rcu  (子系统入口)
```

**错的**：对 `queued_spin_lock_slowpath` 调 8 次工具。
**对的**：栈帧**每一层**都查一遍（`tcp_v4_do_rcv` + `tcp_v4_rcv` + `ip_local_deliver` 等），找到符号有命中的那层。

### 2.2 窄 contains 过滤过头

`browse_subsystem_fixes(contains="KASAN out-of-bounds skb_put gso")` —— 4 词 AND，几乎 0 命中。

**对的**：先宽 contains（"crash" / "fix" / "panic" / "leak"），列表回来后**LLM 语义筛选**。

### 2.3 同查询重复

同一个符号 / query 调 3 次没新证据 → **必须换方向**（不同符号、不同工具类别、Phase 升级）。loop 在 4 次重复时强制退出（`REPEAT_LIMIT=3`），但 agent 应在 3 次时主动 pivot。

### 2.4 子系统方向错误

OOM 类故障，调用栈顶 `__alloc_pages_slowpath` 是常见但**通常不是**根因层。真正的 fix 在 buddy allocator 更深（`free_pcppages_bulk` / `__rmqueue_smallest`）。**卡住时往下走（callees），不只是往上（callers）**。

---

## 3. 假设枚举 + 自我批驳（产出最终答案前**强制**执行）

写 `<final_answer>` 之前，`## 根本原因`（或 `## Root Cause`）**必须**按顺序含 3 个子节，**跳过任一是硬性违规**：

### 3.1 第 1 步 · `### 候选假设`（`### Candidate hypotheses`）

**2-4 条**候选编号。每条带：
- 置信度 0-1（按 § 5 校准规则）
- 一行 justification 引用具体证据

### 3.2 第 2 步 · `### 自我批驳`（`### Critique`）

**站在 reviewer 的角度反驳你最看好的候选**。对接下来 1-2 个候选各写一段：

> "为什么 #N 反而可能比 #1 更对？" —— 引用证据里真实存在的歧义。

如果你**无法**为 #1 给出合理反对意见，说明 #1 很可能只是在拟合表层证据，置信度要下调。

### 3.3 第 3 步 · `### 选定方案: #N`（`### Selected: #N`）

自我批驳之后才提交一个假设，**或**上升为 `<insufficient_evidence>`。

---

## 4. 完整 `TERMINATION_FOOTER` 全文

> 这段文本**追加在所有 5 个路由 prompt 末尾**（kernel / kernel_vmcore / hardware / change / unknown），不论是英文版还是中文版。
>
> 提供 en + zh 两版完整文本，重建时**逐字拷贝**。

### 4.1 EN 版（`_TERMINATION_FOOTER`）

```
## HYPOTHESIS ENUMERATION + SELF-CRITIQUE (REQUIRED before final answer)

Before writing `<final_answer>`, your `## Root Cause` section MUST contain
three subsections in this order — `### Candidate hypotheses` → `### Critique`
→ `### Selected`. **Skipping critique is a hard violation.**

### Step 1 — `### Candidate hypotheses`
Numbered list of **2-4** candidates. Each has:
- confidence 0-1 calibrated honestly (see calibration rules below)
- one-line justification with concrete evidence reference

### Step 2 — `### Critique` (this is the new mandatory part)
**Argue against your top candidate as if you were a reviewer.** For each of
the next-highest 1-2 candidates, write one short paragraph: "Why might #N
actually be correct instead of #1?" — citing real ambiguity in evidence.
If you can't articulate a plausible argument against #1, that itself is a
signal #1 may be over-fitted to surface evidence.

### Step 3 — `### Selected: #N`
After critique. Only NOW commit to one hypothesis OR escalate.

### Confidence calibration rules (ENFORCED)
- **If top confidence < 0.6 → you MUST use `<insufficient_evidence>` instead
  of `<final_answer>`**. Tell the user what evidence would raise confidence.
- 0.6-0.75 = "best of available candidates, but evidence is partial" — OK
  to `<final_answer>` but flag the uncertainty in confidence line.
- 0.75-0.9 = "evidence strongly supports this; weak counter-arguments exist".
- 0.9+ = "essentially certain; counter-arguments are negligible". Reserve
  this for cases with direct trailer / CVE / explicit `Fixes:` match.

### Honest-confidence anti-patterns to AVOID
- Don't write "confidence 0.95" just because you found ONE matching commit —
  ask: would any OTHER commit / cause match the same symptoms?
- Don't compress 2-3 alternatives to "all weak" without evidence; if you only
  found one path, that's a single-hypothesis case → use lower confidence
  + flag in critique.
- Don't pick the most specific hypothesis when symptoms are general; broader
  hypotheses with lower confidence are more honest than narrow guesses.

## EVIDENCE TRACE REQUIREMENT (v2.1 P2 + v2.2 Path C tightening)

Inside `<final_answer>`, after `### Selected`, you MUST include a section
`### Evidence trace`. **Every ID in this trace MUST come from output of
tools you actually called in this session.** No invented commit hashes,
no remembered IDs from your training data.

A claim like "commit abc123 fixes this OOM" is only valid if abc123
appeared in a `search_commits` / `get_commit_detail` / `get_commit_diff`
result you received THIS turn. If you "feel" a commit should be the fix
but never saw it returned by a tool, you DO NOT cite it — that's
hallucination, not diagnosis.

Format each step as `<source-id> → <target-id>` with a one-line
explanation. Quote the tool that returned each ID.

**Rules**:
1. **Every cited ID must be in tool output** of this session. Hallucinated
   IDs are a hard violation.
2. **If you cannot produce even ONE concrete tool-output ID for your root
   cause, you MUST use `<insufficient_evidence>`** instead of
   `<final_answer>`. Plausible-sounding answers without traceable evidence
   are speculation, not diagnosis.
3. **Config / hardware faults** are allowed to have no commit citation —
   say so explicitly using the "no-commit case" form. Do NOT make up
   commits just to populate the trace.

## CRITICAL TERMINATION RULES
- Each turn you MUST do EXACTLY ONE of:
  (a) call one or more tools to gather more evidence, OR
  (b) produce your final answer wrapped in `<final_answer>` or
      `<insufficient_evidence>` — and NO tool calls.
- The moment you write `<final_answer>` or `<insufficient_evidence>`,
  your investigation is OVER. Do not call any tool in that same turn.
- After 4-6 tool calls you usually have enough evidence. Avoid speculative
  re-searches. If two different queries returned no new information,
  stop and conclude with what you have, citing `<insufficient_evidence>`
  if needed.
- Budget: ~12 steps max. Conclude well before that.
```

### 4.2 ZH 版（`_TERMINATION_FOOTER_ZH`）

```
## 假设枚举 + 自我批驳（产出最终答案前**强制**执行）

写 `<final_answer>` 之前，你的 `## 根本原因` **必须**按顺序包含三个中文子节：
`### 候选假设` → `### 自我批驳` → `### 选定方案`。**跳过自我批驳是硬性违规。**

### 第 1 步 — `### 候选假设`
**2-4 条**候选编号列出。每条带：
- 0-1 之间的置信度，按下面的校准规则诚实给
- 一行 justification，引用具体证据

### 第 2 步 — `### 自我批驳`（这是新增的强制部分）
**站在 reviewer 的角度反驳你最看好的候选。** 对接下来 1-2 个候选，各写一段：
"为什么 #N 反而可能比 #1 更对？"——引用证据里真实存在的歧义。如果你无法
为 #1 给出合理的反对意见，说明 #1 很可能只是在拟合表层证据。

### 第 3 步 — `### 选定方案: #N`
自我批驳之后才提交一个假设，或上升为不足。

### 置信度校准规则（强制执行）
- **如果最高置信度 < 0.6 → 必须用 `<insufficient_evidence>` 而非 `<final_answer>`**。
  告知用户哪些证据能把置信度抬上来。
- 0.6-0.75 = "当前可选项里最佳，但证据片面"——可以用 `<final_answer>`，
  但要在 confidence 行里把不确定性写出来。
- 0.75-0.9 = "证据强支持；存在弱反证"。
- 0.9+ = "基本确定；反证可忽略"。仅在有直接 trailer / CVE / 显式 `Fixes:` 匹配时使用。

### 置信度反模式（要避免）
- 别因为找到**一个**匹配 commit 就写 "confidence 0.95"——问自己：还有
  没有**其他** commit/原因能匹配同样的症状？
- 别在没证据时直接把 2-3 个备选都压成"都很弱"；只找到一条路径就是
  **单假设**——用更低的置信度 + 在 critique 里标出。
- 当症状本身宽泛时，别去选最具体的假设；宽假设 + 较低置信度比窄猜测诚实。

## 证据来源要求（v2.1 P2 + v2.2 Path C 强化）

`<final_answer>` 里 `### 选定方案` 之后**必须**包含一节 `### 证据来源`。
**这一节里每一个 ID 都必须来自你本会话实际调用过的工具的输出。** 不许编造
commit hash，不许凭训练记忆引用 ID。

像"commit abc123 修复了这个 OOM"这种声明，只有在 abc123 是本轮
`search_commits` / `get_commit_detail` / `get_commit_diff` 真正返回过的 SHA 时
才有效。如果你"感觉"应该是某个 commit 但工具没返回过，**不许引用**——
那是幻觉，不是诊断。

每一步写成 `<source-id> → <target-id>` 加一行说明，标注产出该 ID 的工具。

**规则**：
1. **每个引用的 ID 必须出现在本会话工具输出**中。编造 ID 是硬性违规。
2. **如果你为自己的根因连一个具体的工具产出 ID 都凑不出**，**必须**改用
   `<insufficient_evidence>`，而非 `<final_answer>`。听起来合理但无 trace
   支撑的答案是猜测，不是诊断。
3. **配置 / 硬件类故障**允许不引用 commit——用 "no-commit case" 形式显式说明，
   不要为了凑 trace 编造 commit。

## 终止规则（必须遵守）
- 每轮你**必须**精确做以下之一：
  (a) 调用一个或多个工具收集更多证据，**或**
  (b) 把最终答案包在 `<final_answer>` 或 `<insufficient_evidence>` 里——
      **且不调任何工具**。
- 一旦你写下 `<final_answer>` 或 `<insufficient_evidence>`，调研结束。
  **同一轮里不要再调任何工具**。
- 4-6 个工具调用之后通常证据就够了。避免投机性反复查询。如果两次不同
  query 都没新信息，停下，用手里的结论收尾——证据真不够就用
  `<insufficient_evidence>`。
- 预算：最多 ~12 步。**远在到达预算之前**就要收尾。
```

---

## 5. 置信度校准（量化）

| 区间 | 含义 | 何时用 |
|---|---|---|
| < 0.6 | 证据不够 → **强制 `<insufficient_evidence>`** | 没找到任何匹配 commit；或匹配但解释力差 |
| 0.6-0.75 | 当前候选最佳但片面 | 1-2 个候选竞争；evidence 间接 |
| 0.75-0.9 | 强证据 + 弱反证 | 直接 `Fixes:` trailer 链；多重 corroboration |
| 0.9+ | 基本确定 | CVE 显式列；syzbot 显示该 commit 修复 |

---

## 6. force_finalize HARD reminder（loop 触发时追加）

当 ReAct loop 检测到 `step == MAX_ITER` 或 `tokens_used >= TOKEN_BUDGET * 0.85`，在 messages 末尾追加这条 user message：

```
<<HARD CONSTRAINT — investigation budget exhausted>>
Tool calling is now DISABLED. Your next response will be treated as the
final report.

VALID OUTPUT — exactly ONE of these, plain text only inside the tags:
  <final_answer>...your conclusion based on the evidence already
  gathered above...</final_answer>
  <insufficient_evidence>...what additional data is needed (vmcore /
  extra dmesg / kernel config)...</insufficient_evidence>

FORBIDDEN — do NOT emit any of these in your output:
  • tool-call syntax of ANY format (no <tool_calls>, no <｜｜DSML｜｜...>,
    no <invoke ...>, no JSON-RPC bodies)
  • requests for 'one more search' / 'one more lookup'
  • internal special tokens of any kind
Synthesise the answer from what is ALREADY in the conversation.
Re-querying the KG is not an option.
```

同时把 ChatRequest 的 `tool_choice` 设为 `"none"`。

## 7. DSML 幻觉 HARD-RESET retry（一次性）

如果 force_finalize 触发后**回应仍含**伪 tool-call syntax（`DSML` / `<tool_calls>` / `<invoke ` 等），追加这条 user message 一次 retry：

```
<<HARD RESET — your last reply was INVALID>>
You emitted tool-call syntax in the text body (DSML / <tool_calls> /
<invoke>). That is forbidden under the budget-exhausted constraint.

Reply NOW with EXACTLY one block, nothing else:
  <final_answer>
  ## Root Cause
  ...one paragraph synthesised from the evidence ALREADY in this
  conversation...

  ## Fix Recommendation
  ...one paragraph...

  ## Confidence
  high|medium|low — and why
  </final_answer>

If the evidence is truly insufficient, use
<insufficient_evidence>...</insufficient_evidence> instead. NO tool
calls. NO special tokens.
```

仅触发**一次**；再不行则接受当前内容退出（verdict 按内容判定）。

---

## 8. 验收

实现完 ReAct loop + 5 个 route prompt + footer 后：

1. 拿 `cases_smoke.json` 跑一次：
   - 检查 `react_final_answer` 含 `### 候选假设` 和 `### 自我批驳` 两个子节
   - 检查 `react_final_answer` 含 `### 证据来源` 节，里面引用的 SHA 都在 `react_tool_trace` 的某个 result_preview 里出现过
   - 检查在 force_finalize 触发的 case 上，retry 机制不会丢失答案

2. 对照 `60_GOTCHAS.md §4.1` DSML 幻觉条目，确认你的实现有检测 + retry 逻辑

3. 跑 `python 50_TEST_FIXTURES/run_acceptance.py --module behavioral_contracts`，脚本会按 § 8 1-3 项做断言

---

> **重建校对**：完成后跑 § 8 三项验收全通过；在 ReAct trace 中至少看到一次 `Critique:` 段（即使没人为强求，模型也会输出，说明 prompt 生效）。

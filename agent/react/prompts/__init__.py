"""Per-route ReAct system prompt templates (M22 T-025).

Each route maps to a Markdown template in this directory that is rendered
with triage-state variables before being passed to run_react_loop as the
system_prompt argument.
"""

from __future__ import annotations

from pathlib import Path

_HERE = Path(__file__).parent

_ROUTE_FILES: dict[str, str] = {
    "kernel":        "kernel.md",
    "kernel+vmcore": "kernel_vmcore.md",
    "hardware":      "hardware.md",
    "change":        "change.md",
    "unknown":       "unknown.md",
}

# Per-route Chinese variant. All 5 routes now have a .zh.md translation
# (2026-06-01). Routes without a .zh.md would fall back to the English
# template + a directive in _ZH_FALLBACK_DIRECTIVE below.
_ROUTE_FILES_ZH: dict[str, str] = {
    "kernel":        "kernel.zh.md",
    "kernel+vmcore": "kernel_vmcore.zh.md",
    "hardware":      "hardware.zh.md",
    "change":        "change.zh.md",
    "unknown":       "unknown.zh.md",
}

# Fallback directive appended when lang='zh' but no .zh.md variant exists
# for the route. Keeps code identifiers verbatim per Chinese-translation
# rules.
_ZH_FALLBACK_DIRECTIVE = (
    "\n\n## 输出语言\n"
    "本次诊断的所有人类可读叙述与 **Markdown 标题**（## 根本原因 /\n"
    "## 修复建议 / ## 置信度 / ## 证据来源 等）一律使用**简体中文**。\n"
    "但函数名、commit hash、CVE-ID、文件路径、CONFIG_* 宏、工具名、\n"
    "错误码（ENOMEM / EINVAL …）、以及 memcg / OOM / KASAN /\n"
    "use-after-free 等无固定中译的技术术语**保留英文原文**。\n"
)


_TERMINATION_FOOTER = """

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

### Example

```
## Root Cause

### Candidate hypotheses
1. (confidence 0.7) memcg accounting race during reclaim — supported by
   commit f9c645621a28 ("memcg, oom: don't require __GFP_FS") and matching
   call trace frame `mem_cgroup_out_of_memory`.
2. (confidence 0.5) misconfigured cgroup memory.high limit — anon-rss
   = 99% of limit, dying tasks might be throttled (cf. 892962a26026).
3. (confidence 0.2) global memory pressure unrelated to cgroup — order=0
   alloc, but failcnt=47 makes this unlikely.

### Critique
Why might #2 be correct instead of #1? The user's report shows
memory.high pressure, and 892962a26026 specifically addresses dying-task
throttling on memory.high — if the workload had short-lived processes,
this would directly explain the symptoms. The lack of explicit "throttled"
log line is the main weakness of #2.

### Selected: #1 (confidence 0.7)
[detailed explanation...]
```

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

Valid trace examples:

```
### Evidence trace
- search_commits returned commit `892962a26026` (subject: "memcontrol:
  don't throttle dying tasks on memory.high") on call #3
- get_commit_detail of 892962a26026 confirmed `Fixes:` trailer pointing
  to <upstream-sha>
- check_backport_status: present in OLK-6.6, missing from OLK-5.10
```

or, for config / hardware faults where no commit-fix exists:

```
### Evidence trace (no-commit case)
- search_bugs returned gitee#I3J87Y reporting identical kernfs symptom
- bug body confirms this is a userspace systemd / initramfs config issue
  (not a kernel bug)
- No commit citation; root cause is configuration, not code.
```

**Rules**:
1. **Every cited ID must be in tool output** of this session. Hallucinated
   IDs are a hard violation.
2. **If you cannot produce even ONE concrete tool-output ID for your root
   cause, you MUST use `<insufficient_evidence>`** instead of
   `<final_answer>`. Plausible-sounding answers without traceable evidence
   are speculation, not diagnosis.
3. **Config / hardware faults** are allowed to have no commit citation —
   say so explicitly using the "no-commit case" form above. Do NOT make up
   commits just to populate the trace.

## CRITICAL TERMINATION RULES
- Each turn you MUST do EXACTLY ONE of:
  (a) call one or more tools to gather more evidence, OR
  (b) produce your final answer wrapped in `<final_answer>` or `<insufficient_evidence>` — and NO tool calls.
- The moment you write `<final_answer>` or `<insufficient_evidence>`, your investigation is OVER. Do not call any tool in that same turn.
- After 4-6 tool calls you usually have enough evidence. Avoid speculative re-searches. If two different queries returned no new information, stop and conclude with what you have, citing `<insufficient_evidence>` if needed.
- Budget: ~12 steps max. Conclude well before that.
"""


_TERMINATION_FOOTER_ZH = """

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
- 0.9+ = "基本确定；反证可忽略"。仅在有直接 trailer / CVE / 显式
  `Fixes:` 匹配时使用。

### 置信度反模式（要避免）
- 别因为找到**一个**匹配 commit 就写 "confidence 0.95"——问自己：还有
  没有**其他** commit/原因能匹配同样的症状？
- 别在没证据时直接把 2-3 个备选都压成"都很弱"；只找到一条路径就是
  **单假设**——用更低的置信度 + 在 critique 里标出。
- 当症状本身宽泛时，别去选最具体的假设；宽假设 + 较低置信度比窄猜测
  诚实。

### 示例

```
## 根本原因

### 候选假设
1. (confidence 0.7) memcg accounting race during reclaim — 由 commit
   f9c645621a28（"memcg, oom: don't require __GFP_FS"）以及 call trace
   中匹配帧 `mem_cgroup_out_of_memory` 支持。
2. (confidence 0.5) misconfigured cgroup memory.high limit — anon-rss
   是 limit 的 99%；垂死任务可能被 throttle（参考 892962a26026）。
3. (confidence 0.2) 与 cgroup 无关的全局内存压力 — order=0 alloc，
   但 failcnt=47 让这条不太可能。

### 自我批驳
为什么 #2 反而可能更对？用户报告显示了 memory.high 压力，而
892962a26026 正是针对垂死任务在 memory.high 上的 throttle 问题——
如果业务存在大量短命进程，能直接解释症状。#2 的主要弱点是
日志里没有显式的 "throttled" 行。

### 选定方案: #1 (confidence 0.7)
[详细说明……]
```

## 证据来源要求（v2.1 P2 + v2.2 Path C 强化）

`<final_answer>` 里 `### 选定方案` 之后**必须**包含一节 `### 证据来源`。
**这一节里每一个 ID 都必须来自你本会话实际调用过的工具的输出。** 不许编造
commit hash，不许凭训练记忆引用 ID。

像"commit abc123 修复了这个 OOM"这种声明，只有在 abc123 是本轮
`search_commits` / `get_commit_detail` / `get_commit_diff` 真正返回过的 SHA 时
才有效。如果你"感觉"应该是某个 commit 但工具没返回过，**不许引用**——
那是幻觉，不是诊断。

每一步写成 `<source-id> → <target-id>` 加一行说明，标注产出该 ID 的工具。

合法 trace 示例：

```
### 证据来源
- search_commits 在第 3 次调用返回了 commit `892962a26026`
  （subject: "memcontrol: don't throttle dying tasks on memory.high"）
- get_commit_detail(892962a26026) 确认 `Fixes:` trailer 指向 <upstream-sha>
- check_backport_status: 在 OLK-6.6 已 backport，OLK-5.10 缺失
```

或者，对于无 commit-fix 的配置 / 硬件类故障：

```
### 证据来源（无 commit 情形）
- search_bugs 返回 gitee#I3J87Y，报告完全相同的 kernfs 症状
- bug body 确认这是 userspace systemd / initramfs 配置问题（非内核 bug）
- 不引用 commit；根因是配置，不是代码。
```

**规则**：
1. **每个引用的 ID 必须出现在本会话工具输出**中。编造 ID 是硬性违规。
2. **如果你为自己的根因连一个具体的工具产出 ID 都凑不出**，**必须**改用
   `<insufficient_evidence>`，而非 `<final_answer>`。听起来合理但无 trace
   支撑的答案是猜测，不是诊断。
3. **配置 / 硬件类故障**允许不引用 commit——用上面的 "no-commit case" 形式
   显式说明。不要为了凑 trace 编造 commit。

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
"""


def _io_hang_alert(triage_state: dict, lang: str = "en") -> str:
    """Render data-driven alert when triage detected I/O timeout signals.

    Surfaces them as a banner above the system prompt so the kernel-route
    LLM doesn't miss the "this could be hardware/storage, not a kernel bug"
    angle (P1a v2.1 fix for FAQ Q4 model A: io-hang-001 / softlockup-002).
    """
    sigs = triage_state.get("io_hang_signals") or []
    if not sigs:
        return ""
    if lang == "zh":
        return (
            f"\n## ⚠️  在 dmesg 中检测到 I/O 挂起信号\n"
            f"Triage 检出以下模式：{', '.join(sigs)}。\n"
            f"这强烈暗示根因属于硬件 / 固件 / SAN / PCIe / 磁盘，**不是**内核\n"
            f"软件 bug。你的 `<final_answer>` **必须**把硬件/存储假设作为\n"
            f"首要根因，内核 patch 最多只是辅助。建议先做磁盘 / SMART / \n"
            f"固件 / IPMI 检查。\n"
        )
    return (
        f"\n## ⚠️  I/O HANG SIGNAL DETECTED IN DMESG\n"
        f"Triage found these patterns: {', '.join(sigs)}.\n"
        f"This STRONGLY suggests the root cause is hardware / firmware / SAN /\n"
        f"PCIe / disk, NOT a kernel software bug. Your `<final_answer>` MUST\n"
        f"present the hardware/storage hypothesis as the PRIMARY root cause.\n"
        f"Kernel-side patches are at most contributory. Recommend disk/SMART/\n"
        f"firmware/IPMI checks first.\n"
    )


def render_system_prompt(route: str, triage_state: dict,
                         lang: str = "en") -> str:
    """Return the system prompt for *route*, filled with *triage_state* vars.

    Falls back to the 'unknown' template for unrecognised routes.
    ``lang`` ∈ {'en', 'zh'} selects the localized template + termination
    footer. When lang='zh' and no .zh.md exists for the route, we use the
    English template + a fallback directive instructing Chinese narrative
    output (so untranslated routes still produce zh-leaning answers).
    """
    lang = (lang or "en").lower()
    if lang == "zh":
        fname = _ROUTE_FILES_ZH.get(route) or _ROUTE_FILES.get(route) \
                or _ROUTE_FILES["unknown"]
        has_zh_template = route in _ROUTE_FILES_ZH
    else:
        fname = _ROUTE_FILES.get(route, _ROUTE_FILES["unknown"])
        has_zh_template = False

    template = (_HERE / fname).read_text(encoding="utf-8")
    body = template.format(
        kernel_version=triage_state.get("kernel_version") or "unknown",
        olk_version_tag=triage_state.get("olk_version_tag") or "unknown",
        fault_kind=triage_state.get("fault_kind") or "unknown",
    )

    footer = _TERMINATION_FOOTER_ZH if lang == "zh" else _TERMINATION_FOOTER
    out = body + _io_hang_alert(triage_state, lang=lang) + footer
    # If lang=zh but the route prompt itself is still English, append a
    # short directive so the LLM still narrates in Chinese.
    if lang == "zh" and not has_zh_template:
        out += _ZH_FALLBACK_DIRECTIVE
    return out

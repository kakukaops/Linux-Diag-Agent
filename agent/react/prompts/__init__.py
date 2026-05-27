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


def _io_hang_alert(triage_state: dict) -> str:
    """Render data-driven alert when triage detected I/O timeout signals.

    Surfaces them as a banner above the system prompt so the kernel-route
    LLM doesn't miss the "this could be hardware/storage, not a kernel bug"
    angle (P1a v2.1 fix for FAQ Q4 model A: io-hang-001 / softlockup-002).
    """
    sigs = triage_state.get("io_hang_signals") or []
    if not sigs:
        return ""
    return (
        f"\n## ⚠️  I/O HANG SIGNAL DETECTED IN DMESG\n"
        f"Triage found these patterns: {', '.join(sigs)}.\n"
        f"This STRONGLY suggests the root cause is hardware / firmware / SAN /\n"
        f"PCIe / disk, NOT a kernel software bug. Your `<final_answer>` MUST\n"
        f"present the hardware/storage hypothesis as the PRIMARY root cause.\n"
        f"Kernel-side patches are at most contributory. Recommend disk/SMART/\n"
        f"firmware/IPMI checks first.\n"
    )


def render_system_prompt(route: str, triage_state: dict) -> str:
    """Return the system prompt for *route*, filled with *triage_state* vars.

    Falls back to the 'unknown' template for unrecognised routes.
    """
    fname = _ROUTE_FILES.get(route, _ROUTE_FILES["unknown"])
    template = (_HERE / fname).read_text(encoding="utf-8")
    body = template.format(
        kernel_version=triage_state.get("kernel_version") or "unknown",
        olk_version_tag=triage_state.get("olk_version_tag") or "unknown",
        fault_kind=triage_state.get("fault_kind") or "unknown",
    )
    return body + _io_hang_alert(triage_state) + _TERMINATION_FOOTER

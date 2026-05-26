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

## HYPOTHESIS ENUMERATION (REQUIRED before final answer)
Before writing `<final_answer>`, your `## Root Cause` section MUST start with
**a numbered list of 2-4 candidate hypotheses** with confidence (0-1) and
one-line justification each. Only after listing them do you pick the highest
and explain in detail. Example:

```
## Root Cause

### Candidate hypotheses
1. (confidence 0.7) memcg accounting race during reclaim — supported by
   commit f9c645621a28 and matching call trace.
2. (confidence 0.2) misconfigured cgroup limit (user error) — possible
   given anon-rss = 99% of limit, but failcnt=47 suggests kernel issue.
3. (confidence 0.1) NUMA imbalance — weak signal only.

### Selected: #1
[detailed explanation of hypothesis 1, evidence, commits, etc.]
```

This avoids over-specificity (locking onto one commit when the ground truth
is a broader pattern). If only ONE hypothesis fits, still list it as #1
confidence 0.9+ and explain why alternatives were ruled out.

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

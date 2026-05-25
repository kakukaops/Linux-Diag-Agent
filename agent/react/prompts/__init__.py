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

## CRITICAL TERMINATION RULES
- Each turn you MUST do EXACTLY ONE of:
  (a) call one or more tools to gather more evidence, OR
  (b) produce your final answer wrapped in `<final_answer>` or `<insufficient_evidence>` — and NO tool calls.
- The moment you write `<final_answer>` or `<insufficient_evidence>`, your investigation is OVER. Do not call any tool in that same turn.
- After 4-6 tool calls you usually have enough evidence. Avoid speculative re-searches. If two different queries returned no new information, stop and conclude with what you have, citing `<insufficient_evidence>` if needed.
- Budget: ~12 steps max. Conclude well before that.
"""


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
    return body + _TERMINATION_FOOTER

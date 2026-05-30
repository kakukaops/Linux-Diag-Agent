"""dmesg / journald log extractor (WBS 4.5).

Extracts structured kernel events from raw dmesg or journald output:
  - oops / BUG / WARN
  - OOM kill events
  - lockup (soft/hard) and RCU stall
  - lockdep warnings

Each event is returned as a typed dict so the MCP server can expose it
as a tool result, and the agent can route it to the right SOP.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    oops = "oops"
    bug = "bug"
    warn = "warn"
    oom = "oom"
    softlockup = "softlockup"
    hardlockup = "hardlockup"
    rcu_stall = "rcu_stall"
    lockdep = "lockdep"
    panic = "panic"
    unknown = "unknown"


@dataclass
class KernelEvent:
    kind: EventKind
    summary: str
    raw: str
    call_trace: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "summary": self.summary,
            "raw": self.raw,
            "call_trace": self.call_trace,
            "metadata": self.metadata,
        }


# ── Patterns ──────────────────────────────────────────────────────────────────

_OOPS_RE = re.compile(r"Oops:\s*(\S+)", re.IGNORECASE)
_BUG_RE = re.compile(r"BUG:?\s+(.+?)(?:\n|$)", re.IGNORECASE)
_WARN_RE = re.compile(r"WARNING:?\s+(.+?)(?:\n|$)", re.IGNORECASE)
_PANIC_RE = re.compile(r"Kernel panic[- ]+not syncing:\s*(.+?)(?:\n|$)", re.IGNORECASE)
# Bug #84 fix: kernel 5.x emitted "Out of memory: Kill process N (comm) score N",
# but 6.x emits "Out of memory: Killed process N (comm) total-vm:NkB...". The
# canonical OOM head line `oom-kill:constraint=...,task=comm,pid=N` is the most
# reliable signal across versions (always present on memcg / global OOM). Try
# all three in order; "score" is optional metadata.
_OOM_CONSTRAINT_RE = re.compile(
    r"oom-kill:constraint=(\S+?),.*?task=(\S+),\s*pid=(\d+)",
    re.IGNORECASE,
)
_OOM_KILLED_RE = re.compile(
    r"Out of memory:\s+Killed\s+process\s+(\d+)\s+\((\S+)\)",
    re.IGNORECASE,
)
_OOM_RE = re.compile(
    r"(?:Out of memory:\s+)?Kill process\s+(\d+)\s+\((\S+)\)\s+score\s+(\d+)",
    re.IGNORECASE,
)
# Page allocation failure — memory pressure event in same family as OOM.
# Form: "<comm>: page allocation failure: order:N, mode:0x..."
_ALLOC_FAIL_RE = re.compile(
    r"(\S+):\s+page allocation failure:\s*order:(\d+)",
    re.IGNORECASE,
)
# Hung task — "task <comm>:<pid> blocked for more than <N> seconds"
_HUNG_TASK_RE = re.compile(
    r"task\s+(\S+):(\d+)\s+blocked for more than (\d+) seconds",
    re.IGNORECASE,
)
_SOFT_LOCKUP_RE = re.compile(r"soft lockup.*?CPU#(\d+)", re.IGNORECASE)
_HARD_LOCKUP_RE = re.compile(r"NMI watchdog.*?Hard LOCKUP.*?CPU (\d+)", re.IGNORECASE)
_RCU_STALL_RE = re.compile(r"RCU.*?stall detected.*?CPU (\d+)", re.IGNORECASE)
_LOCKDEP_RE = re.compile(r"WARNING: possible (circular locking dependency|deadlock)", re.IGNORECASE)
_CALL_TRACE_START = re.compile(r"Call Trace:", re.IGNORECASE)
_CALL_FRAME_RE = re.compile(r"^\s+[<\[]?[0-9a-f]+[>\]]?\s+(\S+\+0x[0-9a-f]+/0x[0-9a-f]+)")
_TIMESTAMP_RE = re.compile(r"^\[\s*[\d.]+\]\s*")


def extract_events(log_text: str) -> list[KernelEvent]:
    """Parse raw dmesg/journald text and return all detected kernel events."""
    events: list[KernelEvent] = []
    lines = log_text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = _TIMESTAMP_RE.sub("", line).strip()

        event = _match_event(stripped, lines, i)
        if event:
            events.append(event)
            i += max(1, len(event.raw.splitlines()))
        else:
            i += 1
    return events


def _match_event(
    stripped: str, lines: list[str], idx: int
) -> KernelEvent | None:
    # Panic takes precedence over oops/bug
    m = _PANIC_RE.match(stripped)
    if m:
        raw, trace = _collect_block(lines, idx)
        return KernelEvent(EventKind.panic, f"Kernel panic: {m.group(1)}", raw, trace)

    # OOM detection (Bug #84): try the canonical constraint line first, then
    # the modern "Killed process" form, then the legacy "Kill process ... score".
    m = _OOM_CONSTRAINT_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx, max_lines=30)
        return KernelEvent(
            EventKind.oom,
            f"OOM kill: constraint={m.group(1)} task={m.group(2)} pid={m.group(3)}",
            raw, trace,
            metadata={"constraint": m.group(1), "comm": m.group(2),
                      "pid": m.group(3)},
        )
    m = _OOM_KILLED_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx, max_lines=30)
        return KernelEvent(
            EventKind.oom,
            f"OOM kill: pid={m.group(1)} ({m.group(2)})",
            raw, trace,
            metadata={"pid": m.group(1), "comm": m.group(2)},
        )
    m = _OOM_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx, max_lines=30)
        return KernelEvent(
            EventKind.oom,
            f"OOM kill: pid={m.group(1)} ({m.group(2)}) score={m.group(3)}",
            raw, trace,
            metadata={"pid": m.group(1), "comm": m.group(2),
                      "score": int(m.group(3))},
        )

    m = _ALLOC_FAIL_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx, max_lines=30)
        return KernelEvent(
            EventKind.oom,
            f"Page allocation failure: {m.group(1)} order={m.group(2)}",
            raw, trace,
            metadata={"comm": m.group(1), "order": int(m.group(2))},
        )

    m = _HUNG_TASK_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx, max_lines=30)
        return KernelEvent(
            EventKind.bug,
            f"Hung task: {m.group(1)}:{m.group(2)} blocked {m.group(3)}s",
            raw, trace,
            metadata={"comm": m.group(1), "pid": m.group(2), "seconds": int(m.group(3))},
        )

    m = _BUG_RE.match(stripped)
    if m:
        raw, trace = _collect_block(lines, idx)
        kind = EventKind.oops if _OOPS_RE.search(raw) else EventKind.bug
        return KernelEvent(kind, f"BUG: {m.group(1).strip()}", raw, trace)

    m = _WARN_RE.match(stripped)
    if m:
        raw, trace = _collect_block(lines, idx)
        kind = EventKind.lockdep if _LOCKDEP_RE.search(raw) else EventKind.warn
        return KernelEvent(kind, f"WARN: {m.group(1).strip()}", raw, trace)

    m = _SOFT_LOCKUP_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx)
        return KernelEvent(
            EventKind.softlockup,
            f"Soft lockup on CPU#{m.group(1)}",
            raw,
            trace,
            metadata={"cpu": int(m.group(1))},
        )

    m = _HARD_LOCKUP_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx)
        return KernelEvent(
            EventKind.hardlockup,
            f"Hard lockup on CPU {m.group(1)}",
            raw,
            trace,
            metadata={"cpu": int(m.group(1))},
        )

    m = _RCU_STALL_RE.search(stripped)
    if m:
        raw, trace = _collect_block(lines, idx)
        return KernelEvent(
            EventKind.rcu_stall,
            f"RCU stall on CPU {m.group(1)}",
            raw,
            trace,
            metadata={"cpu": int(m.group(1))},
        )

    return None


def _collect_block(
    lines: list[str], start: int, max_lines: int = 60
) -> tuple[str, list[str]]:
    """Collect lines from `start` until a blank line or max_lines.

    Returns (raw_block, call_trace_frames).
    """
    block: list[str] = []
    trace: list[str] = []
    in_trace = False

    for i in range(start, min(start + max_lines, len(lines))):
        line = lines[i]
        stripped = _TIMESTAMP_RE.sub("", line).strip()
        if not stripped and i > start:
            break
        block.append(line)
        if _CALL_TRACE_START.match(stripped):
            in_trace = True
            continue
        if in_trace:
            fm = _CALL_FRAME_RE.match(line)
            if fm:
                trace.append(fm.group(1))
            elif stripped and not stripped.startswith("["):
                in_trace = False

    return "\n".join(block), trace

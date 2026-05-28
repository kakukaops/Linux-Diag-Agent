"""Stack signature — shared module for cross-table similar-crash lookup.

Closes v2.3 KG gap #2: the symptom-signature edge. Previously this was
embedded in `ingest/syzbot/signature.py` and used only on syzbot_crash.
v2.3 promotes it to a shared module so the same algorithm can hash
trace text out of bug, lkml_message, and dmesg_event (user input)
rows — enabling "find similar past crashes" reverse lookup across
sub-graphs.

Algorithm (stable across kernel versions):
  1. For each line in the trace: strip the printk timestamp prefix,
     "by task X/Y/Z" / "CPU: N" suffixes, hex addresses (0x... and
     bare 8+ hex chars), `+0xOFFSET/0xSIZE` pairs, [module] suffixes,
     and long standalone numbers (≥4 digits).
  2. Take the top 20 normalized lines.
  3. SHA-256 → first 32 hex chars.

Two same crashes from different builds will collapse to the same
signature because volatile bits (addresses, offsets, line numbers, PID)
are normalized out, while symbol names + nesting structure survive.
"""

from __future__ import annotations

import hashlib
import re

# Kernel printk timestamp prefix: `[   12.345678]`
_TIMESTAMP_RE = re.compile(r"^\[\s*[\d.]+\]\s*")
# Hex addresses (bare 8+ hex chars, 0x-prefixed, or +offset/size pairs)
_ADDR_RE = re.compile(
    r"(?:\+0x[0-9a-f]+/0x[0-9a-f]+|0x[0-9a-f]+|\b[0-9a-f]{8}[0-9a-f]*\b)",
    re.IGNORECASE,
)
_MODULE_RE = re.compile(r"\s+\[[\w.]+\]")   # `[module_name]` suffixes
_NUMBERS_RE = re.compile(r"\b\d{4,}\b")     # long standalone numbers
# "by task X/Y/Z" and "CPU: N" suffixes vary per run
_TASK_RE = re.compile(r"\bby task \S+|\bCPU:\s*\d+")

# Heuristic: a kernel call-trace frame typically looks like
#   "function_name+0xOFF/0xSIZE"  or  " ? function_name+..."  or
#   "[<ffffffff...>] function_name+..."
# A line is considered "trace-like" if it contains a function-name
# token followed by `+0x` or has the dump_stack-style `<TASK>` marker.
_FRAME_RE = re.compile(r"\b[a-z_][a-z0-9_]+\s*\+0x[0-9a-f]+", re.IGNORECASE)


def normalize_stack(raw_trace: str) -> str:
    """Remove volatile parts from a kernel stack trace."""
    lines = []
    for line in raw_trace.splitlines():
        line = _TIMESTAMP_RE.sub("", line)
        line = _TASK_RE.sub("", line)
        line = _ADDR_RE.sub("ADDR", line)
        line = _MODULE_RE.sub("", line)
        line = _NUMBERS_RE.sub("NUM", line)
        lines.append(line.strip())
    return "\n".join(lines)


def stack_signature(raw_trace: str) -> str:
    """Return a hex SHA-256 (32-char prefix) of the normalized stack.

    Returns "" when the input is empty / whitespace-only (so callers can
    skip the row instead of storing a junk signature).
    """
    if not raw_trace or not raw_trace.strip():
        return ""
    norm = normalize_stack(raw_trace)
    top = "\n".join(norm.splitlines()[:20])
    return hashlib.sha256(top.encode("utf-8", errors="replace")).hexdigest()[:32]


def extract_trace_from_text(text: str) -> str | None:
    """Pull a kernel stack trace block out of free-form text.

    Used to derive a signature from bug-report body / lkml-email body,
    where the trace is embedded inside descriptive prose.

    Heuristic: find a contiguous block of lines where ≥ 30 % look like
    kernel call-trace frames (`name+0xHEX/0xHEX`), bounded by lines
    that look like neither code nor frames. Returns the trace block or
    None if no plausible block is found.
    """
    if not text:
        return None
    lines = text.splitlines()
    n = len(lines)

    # Find the longest contiguous block of "trace-like" lines, allowing
    # short non-trace gaps (e.g. "<TASK>", "Call Trace:" headers).
    best_start, best_end, best_score = None, None, 0
    cur_start = None
    cur_frames = 0
    cur_lines = 0
    for i, line in enumerate(lines):
        is_frame = bool(_FRAME_RE.search(line))
        if is_frame:
            if cur_start is None:
                cur_start = i
            cur_frames += 1
            cur_lines += 1
        elif cur_start is not None:
            # Allow gaps of up to 2 non-frame lines (Call Trace:, <TASK>, ...)
            cur_lines += 1
            if cur_lines - cur_frames > 3:
                # gap too long — close current block and evaluate
                score = cur_frames
                if score > best_score:
                    best_score = score
                    best_start, best_end = cur_start, i - (cur_lines - cur_frames)
                cur_start = None
                cur_frames = 0
                cur_lines = 0
    if cur_start is not None and cur_frames > best_score:
        best_start, best_end = cur_start, n
        best_score = cur_frames

    if best_start is None or best_score < 3:
        return None    # need at least 3 frame-like lines to be a trace

    return "\n".join(lines[best_start:best_end])

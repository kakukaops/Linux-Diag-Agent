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
# v2.3 fix: syzbot traces carry `kernel/sched/core.c:6745`-style file:line
# annotations after each frame. They shift across kernel versions and
# differ from user-pasted traces (which often lack them) — strip so the
# signature collapses across the variants.
_FILE_LINE_RE = re.compile(r"\s+[\w./\-]+\.[ch]:\d+\b")

# Heuristic: a kernel call-trace frame typically looks like
#   "function_name+0xOFF/0xSIZE"  or  " ? function_name+..."  or
#   "[<ffffffff...>] function_name+..."
# A line is considered "trace-like" if it contains a function-name
# token followed by `+0x` or has the dump_stack-style `<TASK>` marker.
_FRAME_RE = re.compile(r"\b[a-z_][a-z0-9_]+\s*\+0x[0-9a-f]+", re.IGNORECASE)


def normalize_stack(raw_trace: str) -> str:
    """Remove volatile parts from a kernel stack trace.

    Strips: timestamps, task/CPU labels, hex addresses, [module] suffixes,
    long numbers, and source-file:line annotations. The output collapses
    across kernel versions AND across "user-pasted free-form trace" vs
    "syzbot's structured HTML trace" — both routes through this function
    yield the same hash for the same crash.
    """
    lines = []
    for line in raw_trace.splitlines():
        # Drop frame-irrelevant header/preamble lines that vary per source.
        # syzbot HTML traces include "INFO: task ...", "Not tainted ...",
        # "task:... state:D ...", "Call Trace:", "<TASK>" before the
        # actual frames. User-pasted dmesg sometimes omits them. The
        # signature must be robust to either being present. Strip
        # leading whitespace first — syzbot indents these lines.
        stripped = line.strip()
        if (stripped.startswith(("INFO: task ", "task:", "Not tainted",
                                  '"echo 0 > /proc/sys/kernel/hung_task',
                                  "RIP:", "RSP:", "Code:", "RAX:", "RDX:",
                                  "RBP:", "R10:", "R13:", "FS:", "CS:", "CR2:")) or
            stripped in ("Call Trace:", "<TASK>", "</TASK>", "")):
            continue
        line = _TIMESTAMP_RE.sub("", line)
        line = _TASK_RE.sub("", line)
        # v2.4: substitute addresses with "" (was "ADDR"). The "ADDR" form
        # broke word-boundary detection for the next pass — patterns like
        # `devinet_ioctl+0x123/0x456` became `devinet_ioctlADDR`, a single
        # contiguous \w+ run, and the function-name extractor couldn't
        # bound the identifier.
        line = _ADDR_RE.sub("", line)
        line = _MODULE_RE.sub("", line)
        line = _FILE_LINE_RE.sub("", line)
        line = _NUMBERS_RE.sub("", line)
        lines.append(line.strip())
    return "\n".join(lines)


_FRAME_FUNC_RE = re.compile(r"\b([a-z_][a-z0-9_]{3,})\b")

# Kernel-internal frames that get included in syzbot's HTML traces (via
# inlining annotations) but are routinely absent from user-pasted dmesg.
# These DON'T distinguish the crash — they're plumbing through the
# scheduler, the locking primitives, the ioctl path, and the syscall
# entry. Strip them so both source paths converge to the same hash.
_INTERNAL_FRAMES = frozenset({
    # scheduling
    "context_switch", "__schedule", "__schedule_loop", "schedule",
    "schedule_preempt_disabled", "schedule_timeout", "io_schedule",
    "preempt_schedule", "preempt_schedule_common",
    # locking primitives
    "__mutex_lock", "__mutex_lock_common", "__mutex_lock_slowpath",
    "mutex_lock", "mutex_lock_nested",
    "down_read", "down_write", "rwsem_down_read_slowpath",
    "rwsem_down_write_slowpath",
    "spin_lock", "raw_spin_lock", "queued_spin_lock_slowpath",
    # generic syscall / ioctl plumbing
    "vfs_ioctl", "__do_sys_ioctl", "__se_sys_ioctl", "do_syscall_64",
    "do_syscall_x64", "entry_SYSCALL_64", "entry_SYSCALL_64_after_hwframe",
    # fork / kthread / asm entry
    "ret_from_fork", "ret_from_fork_asm", "kthread", "kernel_init",
})

# Non-function tokens that the regex would otherwise pick up.
_BANNED_TOKENS = frozenset({
    "task", "info", "tainted", "call", "trace", "stack",
    "addr", "num", "tgid", "pid", "ppid", "flags",
    "rip", "rsp", "eflags", "orig_rax", "regs", "code",
})


def stack_signature(raw_trace: str) -> str:
    """Return a hex SHA-256 (32-char prefix) over the top-N **distinguishing**
    function names in the trace.

    v2.4 redesign: previously hashed the top-20 normalized lines, which made
    user-pasted dmesg and syzbot's HTML-scraped trace produce different
    signatures for the SAME crash. The HTML form includes inlined frames
    (context_switch, __schedule_loop, ...), file:line annotations
    (kernel/sched/core.c:6745), and RIP/RSP register dumps that user input
    lacks.

    The fix: extract function names in trace order, drop the kernel-internal
    plumbing frames + non-function regex artefacts, and hash the first 5
    distinguishing functions. Two captures of the same crash from different
    sources (or different kernel versions, since file:line is also gone)
    collapse to one signature.

    Empty / not-enough-signal inputs return "" so callers can skip.
    """
    if not raw_trace or not raw_trace.strip():
        return ""
    norm = normalize_stack(raw_trace)

    funcs: list[str] = []
    seen: set[str] = set()
    # Top 4 chosen empirically: user-pasted dmesg often has just 4-5
    # distinguishing frames once internals are stripped, while syzbot's
    # HTML may have 5-8 (more inlining/visibility). Capping at 4 ensures
    # both sources collapse to the same signature for the same crash.
    _MAX_FUNCS = 4
    for line in norm.splitlines():
        for m in _FRAME_FUNC_RE.finditer(line):
            name = m.group(1).lower()
            if name in seen or name in _INTERNAL_FRAMES or name in _BANNED_TOKENS:
                continue
            seen.add(name)
            funcs.append(name)
            if len(funcs) >= _MAX_FUNCS:
                break
        if len(funcs) >= _MAX_FUNCS:
            break

    if len(funcs) < 3:
        return ""    # too little signal for a meaningful signature

    return hashlib.sha256(
        "|".join(funcs).encode("utf-8", errors="replace")
    ).hexdigest()[:32]


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

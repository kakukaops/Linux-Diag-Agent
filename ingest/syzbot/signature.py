"""Stack signature normalization — strip offsets/addresses → sha256.

Stable signature allows deduplication across kernel versions.
"""

from __future__ import annotations

import hashlib
import re

# Kernel printk timestamp prefix: [   12.345678]
_TIMESTAMP_RE = re.compile(r"^\[\s*[\d.]+\]\s*")
# Hex addresses (bare 8+ hex chars, 0x-prefixed, or +offset/size pairs)
_ADDR_RE = re.compile(
    r"(?:\+0x[0-9a-f]+/0x[0-9a-f]+|0x[0-9a-f]+|\b[0-9a-f]{8}[0-9a-f]*\b)",
    re.IGNORECASE,
)
_MODULE_RE = re.compile(r"\s+\[[\w.]+\]")   # [module_name] suffixes
_NUMBERS_RE = re.compile(r"\b\d{4,}\b")      # long standalone numbers
# "by task X/Y/Z" and "CPU: N" suffixes vary per run
_TASK_RE = re.compile(r"\bby task \S+|\bCPU:\s*\d+")


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
    """Return a hex SHA-256 of the normalized stack (top ~20 frames)."""
    norm = normalize_stack(raw_trace)
    top = "\n".join(norm.splitlines()[:20])
    return hashlib.sha256(top.encode("utf-8", errors="replace")).hexdigest()[:32]

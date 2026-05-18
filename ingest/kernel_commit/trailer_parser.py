"""Kernel commit trailer parser — Fixes:, Reported-by:, Closes:, Link:."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Fixes: <hash> (<title>)   —  hash is 7-40 hex chars
_FIXES_RE = re.compile(
    r"^Fixes:\s+([0-9a-f]{7,40})(?:\s+\(.*?\))?",
    re.MULTILINE | re.IGNORECASE,
)
_REPORTED_RE = re.compile(r"^Reported-by:\s*(.+)", re.MULTILINE | re.IGNORECASE)
_CLOSES_RE = re.compile(r"^Closes:\s*(https?://\S+)", re.MULTILINE | re.IGNORECASE)
_LINK_RE = re.compile(r"^Link:\s*(https?://\S+)", re.MULTILINE | re.IGNORECASE)


@dataclass
class CommitTrailers:
    fixes_refs: list[str] = field(default_factory=list)
    reported_by: list[str] = field(default_factory=list)
    closes_refs: list[str] = field(default_factory=list)
    link_urls: list[str] = field(default_factory=list)


def parse_trailers(body: str) -> CommitTrailers:
    """Extract structured trailers from a commit message body."""
    return CommitTrailers(
        fixes_refs=[m.group(1).lower() for m in _FIXES_RE.finditer(body)],
        reported_by=[m.group(1).strip() for m in _REPORTED_RE.finditer(body)],
        closes_refs=_CLOSES_RE.findall(body),
        link_urls=_LINK_RE.findall(body),
    )

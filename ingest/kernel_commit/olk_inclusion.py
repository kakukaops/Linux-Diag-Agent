"""OLK inclusion header parser (ADR-018 C1).

Parses the structured inclusion header at the top of OLK commit messages.
Implements the two-class rule: tag ∈ {mainline, stable} → backport, else → native.

Real data verified 2026-05-15 against OLK-6.6 (1,251,433 commits):
- ~7.6% are MR merge commits (skip inclusion parsing)
- inclusion types: open set 40+ kinds; mainline(1100), stable(5129), hulk(435), driver(414), …
- stable inclusion: commit <sha> = stable-tree SHA; mainline SHA in [Upstream commit ...] below ----
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# MR merge commit detection (body contains "See merge request")
_MR_MERGE_RE = re.compile(r"See merge request.*openeuler/kernel!", re.DOTALL)

# Inclusion header: "<tag> inclusion" at the very top of the body (before ---)
_OLK_INCLUSION_RE = re.compile(
    r"^([\w\-]+)\s+inclusion\s*\n"
    r"(?:from\s+\S+\s*\n)?"
    r"commit\s+([0-9a-f]{7,40})\b",
    re.IGNORECASE | re.MULTILINE,
)

# mainline SHA from [Upstream commit ...] block (stable inclusion only)
_UPSTREAM_COMMIT_RE = re.compile(
    r"\[\s*[Uu]pstream\s+commit\s+([0-9a-f]{7,40})\s*\]",
)

# Separator between OLK header and upstream commit message body
_SEPARATOR_RE = re.compile(r"^-{3,}\s*$", re.MULTILINE)

BACKPORT_TAGS = frozenset({"mainline", "stable"})


@dataclass
class OlkInclusionInfo:
    """Parsed OLK inclusion header result."""
    kind: Literal["backport", "native", "no_header", "merge"]
    tag: str | None = None                # raw inclusion tag (lower-cased)
    head_commit: str | None = None        # commit SHA in inclusion header (mainline or stable sha)
    upstream_commit: str | None = None    # true mainline SHA (for stable inclusion)

    @property
    def is_backport(self) -> bool:
        return self.kind == "backport"

    @property
    def best_upstream_sha(self) -> str | None:
        """Return the best mainline SHA for cross-linking.

        mainline inclusion: head_commit IS the mainline sha.
        stable inclusion:   upstream_commit is mainline (if present), else head_commit is stable sha.
        """
        if self.tag == "mainline":
            return self.head_commit
        if self.tag == "stable":
            return self.upstream_commit or self.head_commit
        return None


def parse_olk_inclusion(subject: str, body: str) -> OlkInclusionInfo:
    """Parse OLK inclusion header from commit subject + body.

    ADR-018 C1 compliance:
    - MR merge commits → kind='merge'
    - No inclusion header → kind='no_header'
    - tag ∈ {mainline, stable} → kind='backport'
    - other tag → kind='native'
    """
    if not body:
        return OlkInclusionInfo(kind="no_header")

    # MR merge commit check
    if _MR_MERGE_RE.search(body):
        return OlkInclusionInfo(kind="merge")

    # Split body at the --- separator
    parts = _SEPARATOR_RE.split(body, maxsplit=1)
    header_zone = parts[0]
    tail_zone = parts[1] if len(parts) > 1 else ""

    # Try to match inclusion header in the header zone
    m = _OLK_INCLUSION_RE.search(header_zone)
    if not m:
        return OlkInclusionInfo(kind="no_header")

    tag = m.group(1).strip().lower()
    head_commit = m.group(2).lower()

    if tag not in BACKPORT_TAGS:
        # openEuler native commit (hulk/driver/urma/kunpeng/…)
        return OlkInclusionInfo(kind="native", tag=tag, head_commit=head_commit)

    # Backport: find upstream_commit for stable inclusion
    upstream: str | None = None
    if tag == "stable":
        um = _UPSTREAM_COMMIT_RE.search(tail_zone)
        if um:
            upstream = um.group(1).lower()

    return OlkInclusionInfo(
        kind="backport",
        tag=tag,
        head_commit=head_commit,
        upstream_commit=upstream,
    )

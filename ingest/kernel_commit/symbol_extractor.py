"""Extract C symbols touched by a commit from `git show` hunk headers.

Closes v2.3 KG gap #1: the symbol → commit edge.

Approach
--------
`git show --format= --unified=0 <hash>` outputs each hunk as:

    diff --git a/path/to/file.c b/path/to/file.c
    @@ -L,N +L,N @@ <function_context_line>
    -    deleted line
    +    added line

git auto-detects the enclosing function (or struct / macro) for C-like
files using its xfuncname regex. We parse:

  1. The file path from the `diff --git` line.
  2. The function-context line after the second `@@` marker.
  3. From the context, extract the C identifier (regex-based; handles
     function definitions, struct decls, K&R-style, etc.).

A single commit yields N rows: one per (file, symbol) pair touched.

Limitations
-----------
- Pure-data changes outside any function (e.g. Kconfig edits, top-level
  globals) get kind='unknown' — we skip those.
- C++ / Rust / Python diffs in the kernel tree (rare) may not parse
  cleanly; we drop rows where no identifier was extracted.
- Merge commits with no diff produce zero rows (correct).
"""

from __future__ import annotations

import logging
import re
import subprocess
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)


# git auto-detected function context is freeform — typical patterns:
#   "static int tcp_send_mss(struct sock *sk, ...)"
#   "void __ext4_writepages_complete(struct inode *inode)"
#   "struct nla_policy ifla_policy[IFLA_MAX+1] = {"
#   "#define WRITE_INT(...)"
#
# We aim for the FIRST C identifier that looks like a definition target.
# Strategy: skip type keywords (static, const, void, int, ...) and take
# the first identifier followed by '(' (function) or '[' / '=' (struct
# instance) or end (#define X).
_C_IDENT = r"[A-Za-z_][A-Za-z0-9_]*"
_TYPE_KEYWORDS = frozenset({
    "static", "const", "extern", "inline", "void", "char", "int",
    "long", "short", "unsigned", "signed", "u8", "u16", "u32", "u64",
    "s8", "s16", "s32", "s64", "size_t", "ssize_t", "bool", "struct",
    "union", "enum", "typedef",
})

_HUNK_FUNCNAME_RE = re.compile(
    r"@@\s+-\d+(?:,\d+)?\s+\+\d+(?:,\d+)?\s+@@\s*(.*)$"
)
_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/.+?$")


def extract_symbols_from_context(context: str) -> tuple[str | None, str]:
    """From a git hunk function-context line, return (symbol, kind).

    Returns (None, 'unknown') if no plausible C identifier is found.
    """
    if not context or not context.strip():
        return (None, "unknown")
    s = context.strip()

    # Macro definition: #define FOO(...)  or  #define FOO ...
    m = re.match(r"^#\s*define\s+(" + _C_IDENT + r")", s)
    if m:
        return (m.group(1), "macro")

    # Find identifier followed by `(` — most likely a function definition.
    for m in re.finditer(_C_IDENT + r"\s*\(", s):
        word = m.group(0).rstrip(" (")
        if word not in _TYPE_KEYWORDS:
            return (word, "function")

    # Find identifier followed by `[` or `=` — struct/array instance.
    for m in re.finditer(_C_IDENT + r"\s*[\[=]", s):
        word = re.match(_C_IDENT, m.group(0)).group(0)
        if word not in _TYPE_KEYWORDS:
            return (word, "struct")

    # Fallback: first non-keyword identifier in the line.
    for token in re.findall(_C_IDENT, s):
        if token not in _TYPE_KEYWORDS:
            return (token, "unknown")

    return (None, "unknown")


def extract_symbols_for_commit(
    repo_path: str | Path,
    commit_hash: str,
    timeout_s: float = 30.0,
) -> list[dict]:
    """Run `git show` on a commit and return touched-symbol rows.

    Each row: {'commit_hash', 'symbol', 'file_path', 'kind'}.
    Deduplicated by (file_path, symbol) within a commit.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "show",
             "--format=", "--unified=0", "--no-color", "-M", "-C", commit_hash],
            capture_output=True, timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired:
        logger.warning("git show timeout for %s", commit_hash[:12])
        return []
    if result.returncode != 0:
        logger.warning("git show failed for %s: %s",
                       commit_hash[:12], result.stderr[:200])
        return []

    text = result.stdout.decode("utf-8", errors="replace")
    return _parse_diff_for_symbols(text, commit_hash)


def _parse_diff_for_symbols(diff_text: str, commit_hash: str) -> list[dict]:
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    current_file: str | None = None

    for line in diff_text.splitlines():
        m = _DIFF_HEADER_RE.match(line)
        if m:
            current_file = m.group(1)
            continue
        if current_file is None:
            continue
        m = _HUNK_FUNCNAME_RE.match(line)
        if not m:
            continue
        symbol, kind = extract_symbols_from_context(m.group(1))
        if not symbol:
            continue
        key = (current_file, symbol)
        if key in seen:
            continue
        seen.add(key)
        rows.append({
            "commit_hash": commit_hash,
            "file_path": current_file,
            "symbol": symbol,
            "kind": kind,
        })
    return rows


def backfill_batch(
    engine,
    repos: dict[str, str | Path],
    limit: int = 1000,
    since_date: str | None = None,
) -> tuple[int, int, int]:
    """Process up to `limit` commits not yet in link_commit_symbol.

    `repos` maps OLK-version tag → on-disk git path, e.g.:
        {"OLK-6.6": "/data1/.../OLK-6.6/kernel",
         "OLK-5.10": "/data1/.../OLK-5.10/kernel"}

    For each candidate commit, picks the first repo whose tag is in the
    commit's `affected_versions` and runs `git show` there. Mainline-only
    commits without a matching repo are skipped (not failures, just
    out-of-scope).

    Returns (processed, rows_inserted, skipped).
    """
    from sqlalchemy import text

    repo_tags = list(repos.keys())     # search order
    with engine.connect() as conn:
        where_clauses = [
            "NOT EXISTS (SELECT 1 FROM link_commit_symbol lcs "
            "             WHERE lcs.commit_hash = kc.hash)",
            # Only commits whose affected_versions intersects available repos.
            f"kc.affected_versions && :versions",
        ]
        params: dict = {"limit": limit, "versions": repo_tags}
        if since_date:
            where_clauses.append("kc.commit_date >= :since")
            params["since"] = since_date
        sql = f"""
            SELECT kc.hash, kc.affected_versions
              FROM kernel_commit kc
             WHERE {' AND '.join(where_clauses)}
             ORDER BY kc.commit_date DESC NULLS LAST
             LIMIT :limit
        """
        candidates = conn.execute(text(sql), params).fetchall()

    if not candidates:
        return (0, 0, 0)

    rows_total = 0
    processed = 0
    skipped = 0
    with engine.begin() as conn:
        for row in candidates:
            # Pick the first repo whose tag appears in the commit's versions.
            repo_path = None
            for tag in repo_tags:
                if tag in (row.affected_versions or []):
                    repo_path = repos[tag]
                    break
            if repo_path is None:
                skipped += 1
                continue

            extracted = extract_symbols_for_commit(repo_path, row.hash)
            processed += 1
            if not extracted:
                continue
            conn.execute(
                text("""
                    INSERT INTO link_commit_symbol
                        (commit_hash, file_path, symbol, kind)
                    VALUES (:commit_hash, :file_path, :symbol, :kind)
                    ON CONFLICT (commit_hash, symbol, file_path) DO NOTHING
                """),
                extracted,
            )
            rows_total += len(extracted)

    return (processed, rows_total, skipped)


def _iter_hashes_to_backfill(conn, limit: int) -> Iterable[str]:
    """Used by tests."""
    from sqlalchemy import text
    rs = conn.execute(text("""
        SELECT hash FROM kernel_commit kc
         WHERE NOT EXISTS (
            SELECT 1 FROM link_commit_symbol lcs
             WHERE lcs.commit_hash = kc.hash
         )
         LIMIT :n
    """), {"n": limit})
    for r in rs:
        yield r.hash

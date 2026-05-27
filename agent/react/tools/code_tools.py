"""Category D — Code & Patch Analysis tools (M22).

Wraps graph queries (check_backport_status, get_regression_fixes), git diff
retrieval, kernel_commit DB queries, and CodeGraph symbol lookups as Tool
objects for the ReAct ToolRegistry.
"""

from __future__ import annotations

from agent.react.tool_registry import Tool

_K = frozenset({"kernel", "kernel+vmcore", "unknown"})
_KC = frozenset({"kernel", "kernel+vmcore", "change", "unknown"})


# ── tool functions ────────────────────────────────────────────────────────────

def _get_commit_detail(*, commit_hash: str, **_: object) -> str:
    """Fetch commit subject, author, date, and body from kernel_commit."""
    from sqlalchemy import text
    from storage.pg.engine import get_engine

    h = (commit_hash or "").strip()
    if not h:
        return "error: commit_hash is required"
    try:
        with get_engine().connect() as conn:
            row = conn.execute(
                text("""
                    SELECT hash, subject, body, author_name, commit_date,
                           olk_inclusion_type, upstream_commit, affected_versions
                      FROM kernel_commit WHERE hash LIKE :p LIMIT 1
                """),
                {"p": h[:12] + "%"},
            ).fetchone()
    except Exception as exc:
        return f"error: DB query failed: {exc}"

    if row is None:
        return f"Commit {h!r} not found in kernel_commit."
    lines = [
        f"hash: {row.hash}",
        f"subject: {row.subject}",
        f"author: {row.author_name}",
        f"date: {row.commit_date}",
        f"inclusion_type: {row.olk_inclusion_type}",
        f"upstream_commit: {row.upstream_commit or 'N/A'}",
        f"affected_versions: {row.affected_versions}",
        "",
        (row.body or "")[:1000],
    ]
    return "\n".join(lines)


def _get_commit_diff(*, commit_hash: str, max_chars: int = 5000,
                     **_: object) -> str:
    """Return the patch (git show output) for a commit from a local OLK repo."""
    from graph.git_diff import get_commit_diff
    result = get_commit_diff(commit_hash, max_chars=max_chars)
    if not result.get("found"):
        return f"Diff not found for {commit_hash!r}: {result.get('error', 'unknown')}"
    diff = result["diff"]
    suffix = "\n[truncated]" if result.get("truncated") else ""
    return f"repo: {result['repo']}\n\n{diff}{suffix}"


def _check_backport_status(*, upstream_sha: str, olk_version: str,
                           **_: object) -> str:
    """Check whether a mainline commit has been backported to an OLK version."""
    from graph.queries import check_backport_status
    r = check_backport_status(upstream_sha, olk_version)
    if r.get("error"):
        return f"error: {r['error']}"
    if r["backported"]:
        return (
            f"BACKPORTED: upstream {upstream_sha!r} is present in {olk_version}\n"
            f"  olk_commit: {r['olk_commit_hash']}\n"
            f"  subject: {r['olk_commit_subject']}\n"
            f"  inclusion_type: {r['inclusion_type']}"
        )
    return f"NOT backported: {upstream_sha!r} not found in {olk_version}"


def _get_regression_fixes(*, commit_hash: str, **_: object) -> str:
    """Find commits that fix a regression introduced by commit_hash."""
    from graph.queries import get_regression_fixes
    r = get_regression_fixes(commit_hash)
    if r.get("error"):
        return f"error: {r['error']}"
    if not r["has_regression_fix"]:
        return f"No known regression introduced by {commit_hash!r}."
    lines = [f"REGRESSION: {len(r['fixing_commits'])} commit(s) fix a regression from {r['commit_hash'][:12]}:"]
    for fc in r["fixing_commits"]:
        lines.append(f"  {fc['hash'][:12]} [{fc['inclusion_type']}] {fc['subject']}")
    return "\n".join(lines)


def _get_function_source(*, func_name: str, kernel_version: str | None = None,
                         **_: object) -> str:
    """Retrieve kernel function source code via CodeGraph."""
    from clients.codegraph.client import get_codegraph_client, CodeGraphError
    try:
        client = get_codegraph_client()
        hits = client.search_code(
            f"func {func_name}",
            version_hint=kernel_version,
            limit=5,
        )
    except CodeGraphError as exc:
        return f"CodeGraph unavailable: {exc}"
    if not hits:
        return f"Function {func_name!r} not found in CodeGraph."
    parts = []
    for h in hits:
        parts.append(f"{h.get('file', '')}:")
        if h.get("snippet"):
            parts.append(h["snippet"][:500])
    return "\n".join(parts)


def _expand_query_from_symbol(*, symbol: str,
                              kernel_version: str | None = None,
                              **_: object) -> str:
    """Pull candidate fix-side keywords out of a function's source.

    Used when BM25 with the crash-site symbol returns nothing useful — the
    fix commit typically touches a related symbol (struct field, callee,
    upstream variable) that doesn't appear in the dmesg trace. This tool
    reads the source via CodeGraph and extracts the identifiers most likely
    to be in the fix commit body.

    Returns a newline-separated list "<token>: <count>  -- <hint>".
    """
    import re
    from collections import Counter
    from clients.codegraph.client import get_codegraph_client, CodeGraphError

    try:
        client = get_codegraph_client()
        # Use lookup_symbol — SCIP-precision definition lookup. Falls back
        # to search_code if the symbol isn't indexed.
        repo = client.resolve_repo(kernel_version)
        args: dict = {"symbol": symbol, "action": "definition"}
        if repo:
            args["repos"] = [repo]
        result = client.passthrough("lookup_symbol", args)
        from clients.codegraph.client import _get_text_content
        sources = _get_text_content(result) or ""
        if not sources.strip():
            hits = client.search_code(symbol, version_hint=kernel_version, limit=3)
            sources = " ".join((h.get("snippet") or "")[:4000] for h in hits)
    except CodeGraphError as exc:
        return f"CodeGraph unavailable: {exc}"
    if not sources.strip():
        return f"No source returned for {symbol!r}."

    # Kernel identifiers: snake_case (≥2 chars, has '_') or ALL_CAPS macros.
    snake = re.findall(r"\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b", sources)
    caps  = re.findall(r"\b[A-Z][A-Z0-9_]{2,}\b", sources)

    # Drop the symbol itself plus C / kernel stopwords that BM25 won't help on.
    stop = {symbol, "if", "else", "for", "while", "return", "goto",
            "struct", "static", "const", "void", "char", "int", "long",
            "u8", "u16", "u32", "u64", "size_t", "bool", "true", "false",
            "null", "unlikely", "likely", "WARN", "BUG", "EXPORT_SYMBOL",
            "GFP_KERNEL", "GFP_ATOMIC", "EINVAL", "ENOMEM", "EFAULT",
            "READ_ONCE", "WRITE_ONCE", "BUILD_BUG_ON",
            # MCP / response-metadata leakage
            "SCIP", "scip", "Zoekt", "zoekt"}
    toks = [t for t in snake + caps if t not in stop and len(t) > 3]
    counts = Counter(toks).most_common(20)

    if not counts:
        return f"No expandable identifiers extracted from {symbol!r}."

    lines = [f"Candidate keywords from {symbol} source "
             "(feed top items into search_commits / search_lkml):"]
    for tok, n in counts:
        kind = "macro" if tok.isupper() else "ident"
        lines.append(f"  {tok}  ({n}×, {kind})")
    return "\n".join(lines)


def _find_commits_touching_symbol(*, symbol: str,
                                  kernel_version: str | None = None,
                                  since_months: int = 36,
                                  limit: int = 20,
                                  **_: object) -> str:
    """Reverse-lookup `link_commit_symbol`: which commits modified this symbol?

    Closes the v2.3 symptom-symbol → fix-symbol gap. When the crash trace
    names a function (e.g. `ext4_writepages`), this returns recent commits
    that actually changed that function's source — a high-precision way to
    surface candidate fixes that BM25 on commit bodies cannot find.
    """
    import datetime
    from sqlalchemy import text
    from storage.pg.engine import get_engine

    sym = (symbol or "").strip()
    if not sym:
        return "error: symbol is required"
    cutoff = datetime.date.today() - datetime.timedelta(days=since_months * 30)

    version_clause = ""
    params: dict = {"sym": sym, "cutoff": cutoff, "limit": min(max(limit, 5), 50)}
    if kernel_version:
        version_clause = " AND kc.affected_versions && ARRAY[:kv]::text[]"
        params["kv"] = kernel_version

    sql = f"""
        SELECT DISTINCT ON (substring(lcs.commit_hash, 1, 12))
               lcs.commit_hash, lcs.file_path, lcs.kind,
               kc.subject, kc.commit_date, kc.origin
          FROM link_commit_symbol lcs
          JOIN kernel_commit kc ON kc.hash = lcs.commit_hash
         WHERE lcs.symbol = :sym
           AND kc.commit_date >= :cutoff
           {version_clause}
         ORDER BY substring(lcs.commit_hash, 1, 12), kc.commit_date DESC
         LIMIT :limit
    """
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
    except Exception as exc:
        return f"DB error: {exc}"

    if not rows:
        kv_part = f" affecting {kernel_version}" if kernel_version else ""
        return (f"No commits found touching symbol {sym!r}{kv_part} "
                f"since {cutoff}. (Note: only ~0.5 % of OLK commits are "
                f"indexed in v2.3 PoC; coverage will grow as the symbol "
                f"backfill completes.)")

    # Sort by date desc for display
    rows = sorted(rows, key=lambda r: r.commit_date or datetime.date.min,
                  reverse=True)
    lines = [f"Commits touching symbol {sym!r} since {cutoff} (newest first):"]
    for r in rows:
        d = r.commit_date.date() if r.commit_date else "?"
        lines.append(f"  {r.commit_hash[:12]} {d!s:<11} [{r.origin or '?':<8}] "
                     f"({r.kind}@{r.file_path[:35]}) {(r.subject or '')[:80]}")
    return "\n".join(lines)


def _browse_subsystem_fixes(*, subsystem_prefixes: str,
                            contains: str | None = None,
                            since_months: int = 18,
                            limit: int = 25,
                            **_: object) -> str:
    """Browse recent 'fix' commits in a subsystem by subject prefix.

    Kernel commits follow the convention `<subsystem>: <change>`, e.g.
    `net: fix crash when ...` or `ext4: fix race in ...`. When BM25 with
    specific symbols misses (because the fix subject uses different
    vocabulary), browse-by-subsystem surfaces candidates a human engineer
    would scan via `git log --oneline net/`.
    """
    import datetime
    from sqlalchemy import text
    from storage.pg.engine import get_engine

    prefixes = [p.strip().lower() for p in subsystem_prefixes.split(",") if p.strip()]
    if not prefixes:
        return "error: subsystem_prefixes is required (comma-separated, e.g. 'net,tcp')"
    if len(prefixes) > 8:
        prefixes = prefixes[:8]
    cutoff = datetime.date.today() - datetime.timedelta(days=since_months * 30)

    prefix_clauses = " OR ".join(
        f"LOWER(subject) LIKE :pfx{i}" for i in range(len(prefixes))
    )
    params: dict = {f"pfx{i}": f"{p}:%" for i, p in enumerate(prefixes)}
    params["cutoff"] = cutoff
    params["limit"] = min(max(limit, 5), 50)

    contains_clause = ""
    if contains:
        contains_clause = " AND LOWER(subject) LIKE :contains"
        params["contains"] = f"%{contains.lower()}%"

    sql = f"""
        SELECT hash, subject, commit_date, origin
          FROM kernel_commit
         WHERE ({prefix_clauses})
           AND LOWER(subject) LIKE '%fix%'
           AND commit_date > :cutoff
           {contains_clause}
         ORDER BY commit_date DESC
         LIMIT :limit
    """
    try:
        with get_engine().connect() as conn:
            rows = conn.execute(text(sql), params).fetchall()
    except Exception as exc:
        return f"DB error: {exc}"
    if not rows:
        contains_part = f" containing '{contains}'" if contains else ""
        return (f"No fix commits in subsystems {prefixes}{contains_part} "
                f"since {cutoff}.")

    lines = [f"Recent fix commits in {prefixes}"
             + (f" containing '{contains}'" if contains else "")
             + f" since {cutoff} (newest first):"]
    for r in rows:
        d = r.commit_date.date() if r.commit_date else "?"
        origin = r.origin or "?"
        lines.append(f"  {r.hash[:12]} {d!s:<11} [{origin:<8}] "
                     f"{(r.subject or '')[:120]}")
    return "\n".join(lines)


def _get_call_graph(*, func_name: str,
                    direction: str = "callers",
                    kernel_version: str | None = None,
                    **_: object) -> str:
    """Get callers or callees of a kernel function via CodeGraph PageIndex."""
    from clients.codegraph.client import get_codegraph_client, CodeGraphError
    try:
        client = get_codegraph_client()
        result = client.page_index_walk(func_name, symbol=func_name,
                                        version_hint=kernel_version)
        raw = result.get("raw", "")
        if not raw:
            return f"No call graph data for {func_name!r}."
        return raw[:2000]
    except CodeGraphError as exc:
        return f"CodeGraph unavailable: {exc}"


def _get_device_major_mapping(*, major: int | str, kind: str = "block",
                              **_: object) -> str:
    """Look up which Linux driver/subsystem owns a device major number.

    Static LANANA table; covers static majors precisely and notes typical
    dynamic-range assignments. (P1c v2.1 fix for FAQ Q4 model C: agent
    confused major 254 with virtio_blk in panic-001; actually = dm-*.)
    """
    from mcp_servers.shared.device_majors import lookup_major
    try:
        m = int(major)
    except (TypeError, ValueError):
        return f"error: 'major' must be an integer (got {major!r})"
    if kind not in ("block", "char"):
        return f"error: 'kind' must be 'block' or 'char' (got {kind!r})"
    r = lookup_major(m, kind)
    out = [f"major={r['major']}  kind={r['kind']}",
           f"driver: {r['driver']}"]
    if r["notes"]:
        out.append(f"notes:  {r['notes']}")
    if r["dynamic"]:
        out.append("⚠ Dynamic allocation — value depends on module load order; "
                   "check /proc/devices on the affected host for definitive answer.")
    return "\n".join(out)


# ── Tool objects ──────────────────────────────────────────────────────────────

GET_COMMIT_DETAIL = Tool(
    name="get_commit_detail",
    description=(
        "Fetch commit metadata and body from the local kernel_commit database. "
        "Returns subject, author, date, inclusion type (mainline/stable/native), "
        "and upstream SHA if it is a backport."
    ),
    parameters={
        "type": "object",
        "properties": {
            "commit_hash": {
                "type": "string",
                "description": "Full or short (≥12 char) commit SHA",
            },
        },
        "required": ["commit_hash"],
    },
    fn=_get_commit_detail,
    routes=_KC,
)

GET_COMMIT_DIFF = Tool(
    name="get_commit_diff",
    description=(
        "Return the actual code diff (patch) of a commit from the local OLK "
        "git repository. Use to inspect what code changed and assess risk."
    ),
    parameters={
        "type": "object",
        "properties": {
            "commit_hash": {"type": "string"},
            "max_chars": {
                "type": "integer",
                "default": 5000,
                "description": "Truncate diff at this many characters",
            },
        },
        "required": ["commit_hash"],
    },
    fn=_get_commit_diff,
    routes=_KC,
)

CHECK_BACKPORT_STATUS = Tool(
    name="check_backport_status",
    description=(
        "Check whether a mainline upstream commit has been backported into "
        "OLK-6.6 or OLK-5.10. Critical for confirming whether a known fix "
        "is present in the customer's kernel."
    ),
    parameters={
        "type": "object",
        "properties": {
            "upstream_sha": {
                "type": "string",
                "description": "Mainline commit SHA (short or full)",
            },
            "olk_version": {
                "type": "string",
                "description": "OLK-6.6 or OLK-5.10",
            },
        },
        "required": ["upstream_sha", "olk_version"],
    },
    fn=_check_backport_status,
    routes=_K,
)

GET_REGRESSION_FIXES = Tool(
    name="get_regression_fixes",
    description=(
        "Find commits that fix a regression introduced by the given commit. "
        "Must check before recommending a backport — a fix that itself caused "
        "a regression should not be recommended alone."
    ),
    parameters={
        "type": "object",
        "properties": {
            "commit_hash": {
                "type": "string",
                "description": "SHA of the commit to check for regressions",
            },
        },
        "required": ["commit_hash"],
    },
    fn=_get_regression_fixes,
    routes=_K,
)

GET_FUNCTION_SOURCE = Tool(
    name="get_function_source",
    description=(
        "Retrieve kernel function source code from CodeGraph. Use when you "
        "need to inspect the implementation of a function mentioned in a "
        "call trace or commit message."
    ),
    parameters={
        "type": "object",
        "properties": {
            "func_name": {"type": "string", "description": "Kernel function name"},
            "kernel_version": {"type": "string", "description": "OLK-6.6 or OLK-5.10"},
        },
        "required": ["func_name"],
    },
    fn=_get_function_source,
    routes=_K,
)

GET_CALL_GRAPH = Tool(
    name="get_call_graph",
    description=(
        "Get the callers or callees of a kernel function via CodeGraph. "
        "Useful for understanding the call chain around a crash site."
    ),
    parameters={
        "type": "object",
        "properties": {
            "func_name": {"type": "string", "description": "Kernel function name"},
            "direction": {
                "type": "string",
                "enum": ["callers", "callees"],
                "default": "callers",
            },
            "kernel_version": {"type": "string", "description": "OLK-6.6 or OLK-5.10"},
        },
        "required": ["func_name"],
    },
    fn=_get_call_graph,
    routes=_K,
)

GET_DEVICE_MAJOR_MAPPING = Tool(
    name="get_device_major_mapping",
    description=(
        "Look up which Linux driver/subsystem owns a device major number "
        "(e.g. 254 → device-mapper / dm-*; 8 → sd; 259 → nvme). Uses the "
        "static LANANA table plus typical dynamic-range assignments. "
        "Use when dmesg shows references like 'major:minor 254:0' and you "
        "need to know what device that is — agents often misidentify "
        "dynamic majors (240-254) without this lookup."
    ),
    parameters={
        "type": "object",
        "properties": {
            "major": {
                "type": "integer",
                "description": "The major device number (0-511)",
            },
            "kind": {
                "type": "string",
                "enum": ["block", "char"],
                "default": "block",
                "description": "Device kind (default: block)",
            },
        },
        "required": ["major"],
    },
    routes=frozenset({"kernel", "kernel+vmcore", "hardware", "change", "unknown"}),
    fn=_get_device_major_mapping,
)

EXPAND_QUERY_FROM_SYMBOL = Tool(
    name="expand_query_from_symbol",
    description=(
        "When BM25 search using a crashing-site symbol returns nothing "
        "relevant, call this to extract candidate fix-side keywords from "
        "that function's source. Returns struct fields, callees, and "
        "ALL_CAPS macros — the vocabulary that's likely in the actual "
        "fix commit body. Example: symbol='tcp_send_mss' may surface "
        "'sk_gso_max_size', 'tcp_mss_split_point', 'tcp_skb_pcount' — "
        "feed those into search_commits next."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "Kernel function symbol from the crash trace"},
            "kernel_version": {"type": "string",
                               "description": "OLK-6.6 or OLK-5.10"},
        },
        "required": ["symbol"],
    },
    fn=_expand_query_from_symbol,
    routes=_K,
)

BROWSE_SUBSYSTEM_FIXES = Tool(
    name="browse_subsystem_fixes",
    description=(
        "Browse recent 'fix' commits in a kernel subsystem by subject "
        "prefix. Use this when BM25 search with specific symbols misses "
        "the fix commit because the patch subject uses different "
        "vocabulary than the crash trace. This is the tool equivalent of "
        "a kernel engineer running `git log --oneline --grep=fix net/` "
        "to scan recent patches. "
        "Example: for a TCP/skb crash, call with "
        "subsystem_prefixes='net,tcp,ipv4', contains='crash' to surface "
        "subjects like 'net: fix crash when config small gso_max_size'."
    ),
    parameters={
        "type": "object",
        "properties": {
            "subsystem_prefixes": {
                "type": "string",
                "description": ("Comma-separated subject prefixes to match "
                                "(without colon), e.g. 'net,tcp,ipv4' or "
                                "'ext4,jbd2' or 'mm,memcg'."),
            },
            "contains": {
                "type": "string",
                "description": ("Optional extra substring filter on the "
                                "subject, e.g. 'crash', 'overflow', 'leak'."),
            },
            "since_months": {
                "type": "integer",
                "default": 18,
                "description": "How far back to look (default 18 months).",
            },
            "limit": {
                "type": "integer",
                "default": 25,
                "description": "Max results (5-50, default 25).",
            },
        },
        "required": ["subsystem_prefixes"],
    },
    fn=_browse_subsystem_fixes,
    routes=_K,
)

FIND_COMMITS_TOUCHING_SYMBOL = Tool(
    name="find_commits_touching_symbol",
    description=(
        "Reverse-lookup of the link_commit_symbol KG edge: given a C "
        "symbol from the crash trace (function / struct / macro name), "
        "return recent commits that actually modified that symbol's "
        "source. Highest-precision way to find candidate fixes when the "
        "crash-site symbol differs from the fix-side vocabulary — BM25 "
        "on commit bodies can't bridge such gaps but this edge can. "
        "Example: symbol='ext4_writepages' returns commits that changed "
        "the function definition in fs/ext4/inode.c."
    ),
    parameters={
        "type": "object",
        "properties": {
            "symbol": {"type": "string",
                       "description": "C symbol name (function/struct/macro) "
                                      "from the crash trace or hypothesis"},
            "kernel_version": {"type": "string",
                               "description": "Filter to commits affecting "
                                              "OLK-6.6 or OLK-5.10"},
            "since_months": {"type": "integer", "default": 36,
                             "description": "Lookback window (months)"},
            "limit": {"type": "integer", "default": 20,
                      "description": "Max results (5-50)"},
        },
        "required": ["symbol"],
    },
    fn=_find_commits_touching_symbol,
    routes=_K,
)

ALL_CODE_TOOLS = [
    GET_COMMIT_DETAIL, GET_COMMIT_DIFF, CHECK_BACKPORT_STATUS,
    GET_REGRESSION_FIXES, GET_FUNCTION_SOURCE, GET_CALL_GRAPH,
    EXPAND_QUERY_FROM_SYMBOL, BROWSE_SUBSYSTEM_FIXES,
    FIND_COMMITS_TOUCHING_SYMBOL, GET_DEVICE_MAJOR_MAPPING,
]

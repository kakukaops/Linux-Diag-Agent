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

ALL_CODE_TOOLS = [
    GET_COMMIT_DETAIL, GET_COMMIT_DIFF, CHECK_BACKPORT_STATUS,
    GET_REGRESSION_FIXES, GET_FUNCTION_SOURCE, GET_CALL_GRAPH,
    GET_DEVICE_MAJOR_MAPPING,
]

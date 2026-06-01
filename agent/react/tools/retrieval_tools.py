"""Category A — Knowledge Retrieval tools (M22).

Wraps the existing 7 BM25 recall routes and CodeGraph client as Tool objects
for the ReAct ToolRegistry. Each fn accepts keyword args matching its JSON
Schema and returns a human-readable string for LLM consumption.
"""

from __future__ import annotations

import json

from agent.react.tool_registry import Tool

# Route membership constants
_K = frozenset({"kernel", "kernel+vmcore", "unknown"})
_KC = frozenset({"kernel", "kernel+vmcore", "change", "unknown"})
_KH = frozenset({"kernel", "kernel+vmcore", "hardware", "unknown"})
_ALL = frozenset({"kernel", "kernel+vmcore", "hardware", "change", "unknown"})


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_evidence(items: list) -> str:
    if not items:
        return "No results found."
    parts = []
    for i, e in enumerate(items, 1):
        header = f"[{i}] score={e.score:.2f} | {e.title}"
        if e.commit_hash:
            header += f" | hash={e.commit_hash[:12]}"
        if e.cve_id:
            header += f" | cve={e.cve_id}"
        if e.bug_id:
            header += f" | bug_id={e.bug_id}"
        parts.append(header)
        if e.body:
            parts.append("    " + e.body[:300].replace("\n", " "))
    return "\n".join(parts)


def _bm25_query(route_module: str, keywords: str,
                kernel_version: str | None = None,
                cve_ids: list[str] | None = None,
                limit: int = 10) -> str:
    import importlib
    from retrieval.schema import RetrievalQuery, RouteTag
    route_tag = getattr(RouteTag, route_module.split(".")[-1].split("_")[0], None)
    mod = importlib.import_module(f"retrieval.recall.{route_module.split('.')[-1]}")
    q = RetrievalQuery(
        raw_question=keywords,
        keywords=keywords.split(),
        kernel_version=kernel_version or None,
        cve_ids=cve_ids or [],
        routes=[route_tag] if route_tag else [],
        limit_per_route=limit,
    )
    return _fmt_evidence(mod.recall(q))


# ── Category A tools ──────────────────────────────────────────────────────────

def _search_commits(*, keywords: str, kernel_version: str | None = None,
                    limit: int = 10, **_: object) -> str:
    return _bm25_query("commit", keywords, kernel_version=kernel_version, limit=limit)


def _search_lkml(*, keywords: str, limit: int = 10, **_: object) -> str:
    return _bm25_query("lkml", keywords, limit=limit)


def _search_bugs(*, keywords: str, limit: int = 10, **_: object) -> str:
    return _bm25_query("bug", keywords, limit=limit)


def _search_syzbot(*, keywords: str, limit: int = 10, **_: object) -> str:
    return _bm25_query("syzbot", keywords, limit=limit)


def _search_cve(*, cve_id: str | None = None, keywords: str = "",
                limit: int = 10, **_: object) -> str:
    from retrieval.schema import RetrievalQuery, RouteTag
    from retrieval.recall import cve as cve_mod
    q = RetrievalQuery(
        raw_question=cve_id or keywords,
        keywords=(keywords or "").split(),
        cve_ids=[cve_id] if cve_id else [],
        routes=[RouteTag.cve],
        limit_per_route=limit,
    )
    return _fmt_evidence(cve_mod.recall(q))


def _search_code(*, query: str, kernel_version: str | None = None,
                 limit: int = 10, **_: object) -> str:
    """CodeGraph BM25 search, with automatic single-token fallback.

    CodeGraph's BM25 endpoint is AND-matched: every term must appear in the
    same file. When the LLM passes a multi-word query like
    "try_charge_memcg mem_cgroup_out_of_memory" or
    "CONFIG_MEMCG_QOS fine grained stall memcontrol OLK", the AND
    intersection is almost always empty even though each token individually
    has many hits. Without a fallback we'd return "No results" and the LLM
    would never see the useful per-token hits.

    Strategy:
      1. Try the full query as-is (preserves precision for users who know
         the AND semantics or pass a single token).
      2. If hits=0 AND the query has >1 alphanumeric tokens, retry each
         token alone, take the top hits, merge unique by file path.
    """
    from clients.codegraph.client import get_codegraph_client, CodeGraphError
    import re
    try:
        client = get_codegraph_client()
        hits = client.search_code(query, version_hint=kernel_version, limit=limit)
        fallback_used = False
        if not hits:
            tokens = [t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query)
                      if t.lower() not in {"the", "and", "for", "with", "from",
                                            "into", "this", "that", "have",
                                            "kernel", "olk"}]
            if len(tokens) > 1:
                seen: set[str] = set()
                merged: list[dict] = []
                per_tok = max(2, limit // max(len(tokens), 1))
                for tok in tokens[:6]:  # cap to 6 tokens to bound cost
                    for h in client.search_code(tok, version_hint=kernel_version,
                                                 limit=per_tok):
                        key = (h.get("file") or "") + "::" + (h.get("snippet") or "")[:50]
                        if key in seen:
                            continue
                        seen.add(key)
                        merged.append({**h, "_matched_token": tok})
                        if len(merged) >= limit:
                            break
                    if len(merged) >= limit:
                        break
                if merged:
                    hits = merged
                    fallback_used = True
    except CodeGraphError as exc:
        return f"CodeGraph unavailable: {exc}"
    if not hits:
        return ("No code results found. Note: search_code requires ALL "
                "terms to appear in the same file. Try a single keyword "
                "(function name, macro, struct) instead of a phrase.")
    parts = []
    if fallback_used:
        parts.append("(Multi-word query had 0 AND-match hits; falling back "
                     "to per-token search and merging top results.)")
    for i, h in enumerate(hits, 1):
        tag = f" via '{h['_matched_token']}'" if h.get("_matched_token") else ""
        parts.append(f"[{i}] {h.get('file', '')} (score={h.get('score', 0):.0f}){tag}")
        if h.get("snippet"):
            parts.append("    " + h["snippet"][:200].replace("\n", " "))
    return "\n".join(parts)


def _lookup_symbol(*, name: str, kernel_version: str | None = None,
                   **_: object) -> str:
    from clients.codegraph.client import get_codegraph_client, CodeGraphError
    try:
        client = get_codegraph_client()
        result = client.page_index_walk(name, symbol=name, version_hint=kernel_version)
        raw = result.get("raw", "")
        return raw[:2000] if raw else f"Symbol '{name}' not found in CodeGraph."
    except CodeGraphError as exc:
        return f"CodeGraph unavailable: {exc}"


# ── Tool objects ──────────────────────────────────────────────────────────────

SEARCH_COMMITS = Tool(
    name="search_commits",
    description=(
        "BM25 search over OLK kernel commits. Use to find historical fixes, "
        "regressions, or backports by symptom keywords or subsystem. "
        "Returns commit hashes, subjects, and body excerpts."
    ),
    parameters={
        "type": "object",
        "properties": {
            "keywords": {"type": "string", "description": "Space-separated search terms"},
            "kernel_version": {"type": "string", "description": "OLK-6.6 or OLK-5.10"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["keywords"],
    },
    fn=_search_commits,
    routes=_ALL,
)

SEARCH_LKML = Tool(
    name="search_lkml",
    description=(
        "BM25 search over the LKML message cache (L2) plus live lore.kernel.org "
        "search (L3). Use to find patch discussions, review threads, and design "
        "rationale for kernel subsystems."
    ),
    parameters={
        "type": "object",
        "properties": {
            "keywords": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["keywords"],
    },
    fn=_search_lkml,
    routes=_KC,
)

SEARCH_BUGS = Tool(
    name="search_bugs",
    description=(
        "BM25 search over kernel.org Bugzilla reports. Use to find known bugs "
        "matching the observed symptom."
    ),
    parameters={
        "type": "object",
        "properties": {
            "keywords": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["keywords"],
    },
    fn=_search_bugs,
    routes=_KC,
)

SEARCH_SYZBOT = Tool(
    name="search_syzbot",
    description=(
        "BM25 search over syzbot crash records. Use to find fuzzer-triggered "
        "crashes that match the observed call trace or panic type."
    ),
    parameters={
        "type": "object",
        "properties": {
            "keywords": {"type": "string"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["keywords"],
    },
    fn=_search_syzbot,
    routes=_K,
)

SEARCH_CVE = Tool(
    name="search_cve",
    description=(
        "Exact CVE ID lookup or BM25 search over NVD CVE descriptions. "
        "Returns CVSS score, description, and associated fix commit hashes."
    ),
    parameters={
        "type": "object",
        "properties": {
            "cve_id": {"type": "string", "description": "e.g. CVE-2024-50022"},
            "keywords": {"type": "string", "description": "Fallback BM25 if no cve_id"},
            "limit": {"type": "integer", "default": 10},
        },
    },
    fn=_search_cve,
    routes=_KH,
)

SEARCH_CODE = Tool(
    name="search_code",
    description=(
        "BM25 keyword search over the OLK kernel source via CodeGraph. "
        "Returns file paths and code snippets. Use to find where a "
        "function, macro, or data structure is defined / referenced.\n\n"
        "QUERY SEMANTICS — IMPORTANT: terms are AND-matched (every "
        "token must appear in the same file). Best results come from a "
        "SINGLE identifier ('try_charge_memcg', 'CONFIG_MEMCG_QOS', "
        "'mem_cgroup_charge'). Multi-word natural-language queries "
        "like 'memcg OOM during reclaim' will usually return 0 hits "
        "directly — the tool falls back to per-token search and merges "
        "results, but a single-token query is faster and more precise."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {"type": "string",
                       "description": "ONE identifier or exact phrase. "
                                       "Multi-word queries are AND-matched "
                                       "and rarely useful without fallback."},
            "kernel_version": {"type": "string", "description": "OLK-6.6 or OLK-5.10"},
            "limit": {"type": "integer", "default": 10},
        },
        "required": ["query"],
    },
    fn=_search_code,
    routes=_K,
)

LOOKUP_SYMBOL = Tool(
    name="lookup_symbol",
    description=(
        "Look up a kernel symbol (function, struct, macro) in the CodeGraph "
        "PageIndex. Returns definition location and surrounding context."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Symbol name"},
            "kernel_version": {"type": "string", "description": "OLK-6.6 or OLK-5.10"},
        },
        "required": ["name"],
    },
    fn=_lookup_symbol,
    routes=_K,
)

ALL_RETRIEVAL_TOOLS = [
    SEARCH_COMMITS, SEARCH_LKML, SEARCH_BUGS, SEARCH_SYZBOT,
    SEARCH_CVE, SEARCH_CODE, LOOKUP_SYMBOL,
]

# retrieval/ — 7-Route Recall + Rerank

## Architecture

Always-fire-all strategy (ADR-013): every query fires all 7 routes in parallel threads, results merged and optionally reranked by LLM.

```
retrieval/
  engine.py          Entry point: retrieve(RetrievalQuery) → RetrievalResult
  query_parser.py    NL → RetrievalQuery (full LLM parse via navigator backend)
  schema.py          RetrievalQuery, Evidence, RetrievalResult, RouteTag
  reranker.py        LLM rerank when candidates > limit_per_route
  recall/
    code.py          CodeGraph MCP HTTP client (Zoekt BM25 + SCIP graph)
    docs.py          CodeGraph PageIndex walk
    lkml.py          PG tsvector BM25 on lkml_message
    bug.py           PG tsvector BM25 on bug
    syzbot.py        PG tsvector BM25 on syzbot_crash
    commit.py        PG tsvector BM25 on kernel_commit
    cve.py           Exact CVE-ID lookup + PG tsvector BM25 on cve
    page_index.py    LLM-driven tree traversal (for docs/code concept queries)
```

## Column names — common mistakes

All recall SQL must use the actual DB column names:

| Table | Correct | Wrong (do not use) |
|-------|---------|-------------------|
| `cve` | `cvss_v3_score`, `cvss_v3_vector` | `severity`, `cvss_score` |
| `bug` | `title` | `summary` |
| `bug` | `body_tsv` | inline `to_tsvector(...)` |
| `cve` | `body_tsv` | inline `to_tsvector(...)` |

## BM25 query pattern

All text search goes through the pre-built `body_tsv` GIN index — do not rebuild tsvector inline:

```python
# CORRECT
text("SELECT ... FROM cve, to_tsquery('english', :q) AS query WHERE body_tsv @@ query")

# WRONG — skips GIN index, slow on large tables
text("WHERE to_tsvector('english', description) @@ to_tsquery('english', :q)")
```

## RetrievalQuery fields

```python
@dataclass
class RetrievalQuery:
    raw_question: str
    kernel_version: str | None       # e.g. "OLK-6.6", "v5.10"
    fault_domain: str | None         # "oom", "lockup", "panic", "io_hang", ...
    subsystem_hint: str | None       # "mm", "net", "fs/ext4", ...
    error_keywords: list[str]        # ["ENOMEM", "order=4", "zone Normal"]
    cve_ids: list[str]               # ["CVE-2024-50022"]
    commit_hashes: list[str]         # short or full SHAs
    keywords: list[str]              # fallback keyword list
    routes: list[RouteTag]           # default: all 7
    limit_per_route: int             # default: 10
```

## Evidence schema

```python
@dataclass
class Evidence:
    route: RouteTag                  # code/docs/lkml/bug/syzbot/commit/cve
    score: float                     # 0.0–1.0 relevance
    title: str
    body: str                        # truncated to 500 chars for display
    # Optional fields (set per route):
    cve_id: str | None
    bug_id: int | None
    commit_hash: str | None
    message_id: str | None
    metadata: dict                   # route-specific extra fields
```

## Reranker trigger

`reranker.maybe_rerank()` fires when total candidates across all routes > `limit_per_route`. Uses the `navigator` LLM backend. LLM budget guard: if navigator rate limit nearly exhausted, reranker is skipped gracefully.

## CodeGraph routes (code, docs)

Routes `code` and `docs` go through `clients/codegraph/client.py` → CodeGraph MCP HTTP at `http://localhost:8080/mcp`. Health checked via MCP `initialize` call (not GET /health — that endpoint returns 404). Response format is SSE (`data: <json>`).

Repo mapping (from `configs/default.yaml`):
- `"OLK-6.6"` / `"v6.6"` → `olk-kernel` (verified via `list_repos`)
- `"OLK-5.10"` / `"v5.10"` → `olk-kernel`

`search_code` tool uses `repos: [<name>]` (array), not `repo: <name>` (string).

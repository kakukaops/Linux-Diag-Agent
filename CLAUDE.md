# CLAUDE.md

Behavioral guidelines to reduce common LLM coding mistakes. Merge with project-specific instructions as needed.

**Tradeoff:** These guidelines bias toward caution over speed. For trivial tasks, use judgment.

## 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

## 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

## 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

## 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.

---

**These guidelines are working if:** fewer unnecessary changes in diffs, fewer rewrites due to overcomplication, and clarifying questions come before implementation rather than after mistakes.

---

## Project Context

### What this is

Offline-first Linux kernel fault diagnosis agent for **OLK (openEuler) v6.6 + v5.10**. Knowledge-base first, no embeddings, no vectors anywhere (ADR-001).

### Module map

```
llm/           LLM Provider — 3 backends: claude_code / openai_compat / vllm (ADR-004)
storage/       PostgreSQL 16 tables + Neo4j graph
ingest/        6 pipelines: lkml / bugzilla / syzbot / nvd / zenodo / kernel_commit
graph/         Cross-Graph Linker: commit ↔ bug ↔ LKML ↔ CVE (ADR-018)
retrieval/     7-route always-fire-all BM25 recall + LLM rerank (ADR-013)
mcp_servers/   Host tools: dmesg/journal parser + sosreport parser
agent/         LangGraph triage + diagnosis + SOP registry
clients/       CodeGraph MCP HTTP client (Zoekt/SCIP over HTTP)
cli/           diag-agent CLI: search / diagnose / feedback / logs
configs/       YAML config + Pydantic loader; local overrides in configs/local.yaml (gitignored)
```

### Key architectural decisions (non-negotiable)

- **No embeddings, no vectors** — BM25 (PG tsvector GIN) + graph traversal + LLM rerank only (ADR-001, ADR-006)
- **Commit source = OLK kernel** (`atomgit.com/openeuler/kernel`), not kernel.org mainline (ADR-018)
  - Local repos: `/data1/lingqu/codes/OLK-6.6/kernel` and `/data1/lingqu/codes/OLK-5.10/kernel`
  - CodeGraph indexes `olk-kernel-v6.6` / `olk-kernel-v5.10` — same source, confirmed same line numbers
- **Bugzilla = kernel.org only** in v1 (ADR-011); Red Hat BZ deferred to v1.1+
- **7-route always-fire-all** retrieval, not selective routing (ADR-013)
- **BM25 = PG tsvector + GIN** (`body_tsv` generated stored column); upgrade to Tantivy in v1.1 if recall < 70%

### LLM backends

Three backends: `claude_code` (default, Claude Code OAuth) / `openai_compat` (OpenRouter etc.) / `vllm` (self-hosted). Switch per-role in `configs/local.yaml`. API key goes in `llm.endpoints.api_key` (gitignored). Details: @llm/CLAUDE.md

### Hard-won API constraints — DO NOT change without re-testing

**PostgreSQL / psycopg3:**
- Named param + PG cast: use `CAST(:name AS jsonb)`, never `:name::jsonb` (psycopg3 parser chokes on `::`)
- `references` is a PG reserved word — always double-quote: `"references"`
- `ingest_runs` is written only at run END, not start — no mid-run DB record

**NVD API (services.nvd.nist.gov/rest/json/cves/2.0):**
- Use `keywordSearch=linux kernel`, NOT `virtualMatchString` with CPE (returns 404)
- Max date window = 120 days; implemented as 119-day sliding chunks
- Rate limit: 1 req/6s without API key → `_DELAY_S = 6.0` in ingester; do not reduce

**lore.kernel.org:**
- `?x=mbox&since=` returns HTML, not mbox — do not use
- Correct approach: Atom feed `?q=d:YYYYMMDD..&x=A&o=N` (200 entries/page) + per-message `/raw`
- Rate limit: 1.5s between ALL requests — `_REQUEST_DELAY_S = 1.5` in fetcher; do not reduce

**git log / kernel_commit ingester:**
- OLK repo has non-UTF-8 bytes in commit messages — use `result.stdout.decode("utf-8", errors="replace")`, not `text=True`
- Ingests OLK first (Phase A), then linux-stable (Phase B)

**Bugzilla REST API:**
- Date filter param = `last_change_time`, format `YYYY-MM-DD` (not `changed_after`, not ISO datetime)

### Config system

```
configs/default.yaml    # Checked in; production defaults
configs/local.yaml      # Gitignored; local/test overrides (deep-merged over default)
```

`get_config()` deep-merges default → local. Tests that check default values must patch `_load_yaml` to skip local.yaml — see `tests/unit/test_config.py` `default_only` fixture.

### BM25 recall

All tables have `body_tsv` GIN index. Always query via `body_tsv @@ query`, never inline `to_tsvector()`. Full column name reference: @storage/CLAUDE.md · @retrieval/CLAUDE.md

### Running the ingestion pipelines

```bash
# Individual ingesters
python -m ingest.nvd incremental
python -m ingest.bugzilla incremental
python -m ingest.syzbot incremental
python -m ingest.lkml incremental
python -m ingest.kernel_commit incremental

# Full sync (runs all in parallel phases)
./scripts/weekly_sync.sh

# DB migrations
alembic -c storage/pg/alembic.ini upgrade head
```

### OLK commit inclusion parsing

`tag ∈ {mainline, stable}` → backport (has upstream SHA); anything else → openEuler-native. Never enumerate native types (40+ kinds). Missing/unparseable header → treat as native. Details: @ingest/CLAUDE.md · @graph/CLAUDE.md

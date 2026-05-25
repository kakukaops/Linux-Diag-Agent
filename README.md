# Linux-Diag-Agent

Offline-first Linux kernel fault diagnosis agent for **OLK (openEuler Linux Kernel) v6.6 + v5.10**.

- **Knowledge-base first**: BM25 (PG tsvector) + Graph (Neo4j) + CodeGraph (Zoekt/SCIP) — no embedding vectors.
- **Always-fire-all 7-route retrieval**: code / docs / LKML / bug / syzbot / commit / CVE.
- **LangGraph Hypothesis-Driven diagnosis** with Claim-Evidence Binding.
- **Fully offline v1.3**: switch to local vLLM, zero data leaves the network.

## Quick start (development)

```bash
# 1. Install
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,ingest,agent]"

# 2. Infra
docker compose up -d

# 3. Database migrations
alembic -c storage/pg/alembic.ini upgrade head

# 4. Config (required — default.yaml alone won't work without LLM credentials)
cp configs/default.yaml configs/local.yaml
# Edit configs/local.yaml: set llm.chat.backend and credentials.
# Options: claude_code (default, needs Claude Code CLI login),
#          openai_compat (OpenRouter/DeepSeek, set llm.endpoints.api_key),
#          vllm (self-hosted, set llm.endpoints.vllm)

# 5. Smoke test
diag-agent search "v6.6 上 order=4 的 normal zone OOM 怎么诊断"
```

## Project layout

```
llm/           # M1 — LLM Provider abstraction (claude_code / openai_compat / ollama / vllm)
storage/       # M2 — PostgreSQL schema (16 tables) + Neo4j graph model
ingest/        # M3 — Ingestion pipelines (lkml / bugzilla / syzbot / nvd / zenodo / kernel_commit)
graph/         # M4 — Cross-Graph Linker (commit ↔ bug ↔ LKML ↔ CVE)
retrieval/     # M5 — 7-route always-fire-all retrieval + LLM rerank
mcp_servers/   # M6 — Host MCP tools (dmesg/journal + sosreport parsers)
agent/         # M7 — LangGraph Triage + Diagnosis agent + SOP registry
clients/       # CodeGraph MCP HTTP client
cli/           # diag-agent CLI (search / diagnose / eval / logs)
configs/       # YAML config + Pydantic loader
scripts/       # weekly_sync.sh, etc.
docs/v1/       # Architecture, ADRs, module specs
data/          # Runtime data (gitignored except .gitkeep)
```

## Documentation

- [FAQ](docs/FAQ.md) — common questions (graph construction, architecture decisions)
- [Architecture](docs/v1/Architecture.md) — system layers, knowledge graph construction, interface contracts
- [Project Status](docs/v1/ProjectStatus.md) — current state, technical debt, next steps
- [Project Plan](docs/v1/ProjectPlan.md)
- [ADRs](docs/v1/adr/)

For Claude Code context, see `CLAUDE.md` (root) and per-module `CLAUDE.md` files in `agent/`, `ingest/`, `retrieval/`, `llm/`, `storage/`, `graph/`.

## Requirements

- Python 3.11+
- Docker (PostgreSQL 15 + Neo4j 5)
- [Claude Code CLI](https://claude.ai/code) (logged in, Pro subscription) — MVP LLM backend
- CodeGraph MCP service (codesearch, port 8080) — code/doc retrieval

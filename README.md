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
alembic upgrade head

# 4. Config
cp configs/default.yaml configs/local.yaml
# edit configs/local.yaml as needed

# 5. Smoke test (requires Claude Code CLI logged in)
diag-agent search "v6.6 上 order=4 的 normal zone OOM 怎么诊断"
```

## Project layout

```
llm/           # M1 — LLM Provider abstraction (claude_code / openai_compat / ollama / vllm)
storage/       # M2 — PostgreSQL schema (14 tables) + Neo4j graph model
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

- [Architecture](docs/v1/Architecture.md)
- [Project Plan](docs/v1/ProjectPlan.md)
- [Knowledge Graph Overview](docs/v1/KnowledgeGraph_Overview.md)
- [ADRs](docs/v1/adr/)

## Requirements

- Python 3.11+
- Docker (PostgreSQL 15 + Neo4j 5)
- [Claude Code CLI](https://claude.ai/code) (logged in, Pro subscription) — MVP LLM backend
- CodeGraph MCP service (codesearch, port 8080) — code/doc retrieval

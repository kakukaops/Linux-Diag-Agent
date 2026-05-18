.PHONY: install install-dev lint typecheck test test-unit test-integration \
        migrate db-up db-down db-reset clean

PYTHON   := python
ALEMBIC  := alembic
PYTEST   := pytest
RUFF     := ruff
MYPY     := mypy

# ── Setup ──────────────────────────────────────────────────────────────────

install:
	pip install -e .

install-dev:
	pip install -e ".[dev,ingest,agent]"

# ── Code quality ───────────────────────────────────────────────────────────

lint:
	$(RUFF) check .
	$(RUFF) format --check .

lint-fix:
	$(RUFF) check --fix .
	$(RUFF) format .

typecheck:
	$(MYPY) llm/ storage/ ingest/ retrieval/ graph/ clients/ cli/ configs/

# ── Tests ──────────────────────────────────────────────────────────────────

test:
	$(PYTEST) tests/

test-unit:
	$(PYTEST) tests/unit/ -m "not integration"

test-integration:
	$(PYTEST) tests/integration/

test-cov:
	$(PYTEST) tests/unit/ --cov=. --cov-report=html --cov-report=term-missing

# ── Database ───────────────────────────────────────────────────────────────

db-up:
	docker compose up -d postgres neo4j
	@echo "Waiting for Postgres..."
	@until docker compose exec postgres pg_isready -U diag >/dev/null 2>&1; do sleep 1; done
	@echo "Postgres ready."

db-down:
	docker compose down

db-reset:
	docker compose down -v
	docker compose up -d postgres neo4j
	@until docker compose exec postgres pg_isready -U diag >/dev/null 2>&1; do sleep 1; done
	$(ALEMBIC) upgrade head

migrate:
	$(ALEMBIC) upgrade head

migrate-new:
	$(ALEMBIC) revision --autogenerate -m "$(MSG)"

# ── Smoke tests ────────────────────────────────────────────────────────────

smoke-ollama:
	$(PYTEST) tests/smoke/test_ollama.py -v

smoke-codegraph:
	$(PYTEST) tests/smoke/test_codegraph.py -v

# ── Cleanup ────────────────────────────────────────────────────────────────

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
	find . -name "*.pyc" -delete
	rm -rf .pytest_cache htmlcov .coverage dist build *.egg-info

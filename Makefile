.PHONY: setup test baseline run eval clean help serve \
        db-up db-down db-migrate db-status test-postgres test-all

# ─── Default PostgreSQL connection URL ──────────────────────────────────────
# Override by exporting SENTINEL_DATABASE_URL before running any db-* target.
PG_URL ?= postgresql://sentinel:sentinel_dev_password@localhost:5432/sentinel_db

help:
	@echo "Sentinel Incident Investigator — Make Targets"
	@echo ""
	@echo "  Development:"
	@echo "    setup          Install Python dependencies (pip install -r requirements.txt)"
	@echo "    test           Run offline unit/mock suite (no PostgreSQL required)"
	@echo "    serve          Start the Sentinel API server (localhost:8000)"
	@echo "    baseline       Run baseline evaluation"
	@echo "    run            Run advanced Sentinel investigator on inc_01"
	@echo "    eval           Run comparative evaluation benchmark"
	@echo "    clean          Remove __pycache__ and *.pyc files"
	@echo ""
	@echo "  PostgreSQL / Database (requires Docker):"
	@echo "    db-up          Start PostgreSQL 16 container (detached)"
	@echo "    db-down        Stop PostgreSQL container (data volume preserved)"
	@echo "    db-migrate     Apply pending schema migrations and verify schema"
	@echo "    db-status      Show which schema migrations have been applied"
	@echo "    test-postgres  Run live PostgreSQL integration tests only"
	@echo "    test-all       Run complete suite (offline + live PostgreSQL)"
	@echo ""
	@echo "  Requirements for db-* targets:"
	@echo "    - Docker Desktop (or Docker Engine) running"
	@echo "    - PG_URL env var (default: $(PG_URL))"

# ─── Standard targets ───────────────────────────────────────────────────────

setup:
	pip install -r requirements.txt

test:
	python -m pytest tests/ -v

baseline:
	python -m eval.run_eval --mode baseline

run:
	python -m agents.orchestrator incidents/inc_01_n_plus_one_query

serve:
	python -m api

eval:
	python -m eval.run_sentinel_eval

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null; \
	find . -type f -name "*.pyc" -delete 2>/dev/null; true

# ─── PostgreSQL / Database targets ──────────────────────────────────────────

db-up:
	@echo "Starting PostgreSQL 16 container..."
	docker compose up -d postgres
	@echo "Waiting for PostgreSQL to be healthy..."
	@for i in 1 2 3 4 5 6 7 8 9 10; do \
	  docker compose exec postgres pg_isready -U sentinel -d sentinel_db && break; \
	  echo "  Waiting... (attempt $$i)"; sleep 3; \
	done
	@echo "PostgreSQL is ready."

db-down:
	@echo "Stopping PostgreSQL container (data volume preserved)..."
	docker compose stop postgres
	@echo "Container stopped. Run 'docker compose rm postgres' to remove it,"
	@echo "or 'docker volume rm sentinel-incident-investigator_sentinel_pgdata' to wipe data."

db-migrate:
	@echo "Applying schema migrations via authoritative migration runner..."
	SENTINEL_DATABASE_URL=$(PG_URL) python -m core.persistence.schema apply --url "$(PG_URL)"
	@echo "Verifying schema completeness..."
	SENTINEL_DATABASE_URL=$(PG_URL) python -m core.persistence.schema verify --url "$(PG_URL)"

db-status:
	SENTINEL_DATABASE_URL=$(PG_URL) python -m core.persistence.schema status --url "$(PG_URL)"

test-postgres:
	@echo "Running live PostgreSQL integration tests..."
	@echo "  Using: $(PG_URL)"
	SENTINEL_DATABASE_URL=$(PG_URL) python -m pytest \
	  tests/test_postgres_persistence.py \
	  tests/test_postgres_concurrency.py \
	  tests/test_schema_migration.py \
	  tests/test_outbox.py \
	  tests/test_persistence_contract.py \
	  -v --tb=short

test-all:
	@echo "Running complete test suite with live PostgreSQL..."
	@echo "  Using: $(PG_URL)"
	SENTINEL_DATABASE_URL=$(PG_URL) python -m pytest tests/ -v --tb=short
	@echo ""
	@echo "Running incident dataset invariant validation..."
	python tests/validate_incidents.py

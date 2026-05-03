.PHONY: install run test lint fmt migrate migrate-rev docker-up docker-down

VENV    ?= .venv
PY      := $(VENV)/bin/python
PYTEST  := $(VENV)/bin/pytest
RUFF    := $(VENV)/bin/ruff
MYPY    := $(VENV)/bin/mypy
ALEMBIC := $(VENV)/bin/alembic
UV      ?= uv

install:
	$(UV) sync --frozen
	@if [ -d .git ]; then $(VENV)/bin/pre-commit install; else echo "(skipping pre-commit install — not a git checkout)"; fi

run:
	$(VENV)/bin/uvicorn webhook_ai_router.main:app --reload --host 0.0.0.0 --port 8000

test:
	$(PYTEST) --cov=webhook_ai_router --cov-report=term-missing

lint:
	$(RUFF) check src tests
	$(MYPY) src

fmt:
	$(RUFF) format src tests
	$(RUFF) check --fix src tests

# Apply all pending migrations against the configured DATABASE_URL.
migrate:
	$(ALEMBIC) upgrade head

# Generate a new migration: `make migrate-rev MSG="add foo column"`.
migrate-rev:
	$(ALEMBIC) revision --autogenerate -m "$(MSG)"

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

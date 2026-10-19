API := services/api
COMPOSE_PROD := docker compose -f infra/compose.yaml --env-file .env
COMPOSE_DEV := docker compose -f infra/compose.dev.yaml
DEV_DB := postgresql+asyncpg://celius:celius@localhost:5432/celius

.PHONY: install dev-deps dev-api migrate migration test test-api-pg lint format up down logs

install:            ## Install API dependencies
	cd $(API) && uv sync

dev-deps:           ## Start local Postgres and Redis
	$(COMPOSE_DEV) up -d

dev-api:            ## Run the API with hot reload against the dev database
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run uvicorn app.main:app --reload --port 8000

migrate:            ## Apply migrations to the dev database
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run alembic upgrade head

migration:          ## Autogenerate a migration: make migration m="add files"
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run alembic revision --autogenerate -m "$(m)"

test:               ## Unit tests (SQLite, mocked model server)
	cd $(API) && uv run pytest -q

test-api-pg:        ## Unit tests plus migration round-trip on the dev Postgres
	cd $(API) && TEST_POSTGRES_URL=$(DEV_DB) uv run pytest -q

lint:
	cd $(API) && uv run ruff check . && uv run ruff format --check .

format:
	cd $(API) && uv run ruff check --fix . && uv run ruff format .

up:                 ## Build and start the production stack
	$(COMPOSE_PROD) up -d --build

down:
	$(COMPOSE_PROD) down

logs:
	$(COMPOSE_PROD) logs -f api

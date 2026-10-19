API := services/api
COMPOSE_PROD := docker compose -f infra/compose.yaml --env-file .env
COMPOSE_DEV := docker compose -f infra/compose.dev.yaml
DEV_DB := postgresql+asyncpg://celius:celius@localhost:5432/celius

.PHONY: install dev-deps dev-api dev-worker dev-web fake-llama migrate migration test test-api-pg \
	test-web lint format e2e up down logs

install:            ## Install API and web dependencies
	cd $(API) && uv sync
	pnpm install

dev-deps:           ## Start local Postgres and Redis
	$(COMPOSE_DEV) up -d

dev-api:            ## Run the API with hot reload against the dev database
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run uvicorn app.main:app --reload --port 8000

dev-worker:         ## Run the generation worker against the dev database
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run python -m app.worker

dev-web:            ## Vite dev server on :5173, proxying /api to :8000
	pnpm dev

fake-llama:         ## Fake llama-server on :8080 for development without a GPU
	cd $(API) && uv run python -m devtools.fake_llama_server --port 8080

migrate:            ## Apply migrations to the dev database
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run alembic upgrade head

migration:          ## Autogenerate a migration: make migration m="add files"
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run alembic revision --autogenerate -m "$(m)"

test:               ## Unit tests (SQLite, mocked model server)
	cd $(API) && uv run pytest -q

test-api-pg:        ## API tests on PostgreSQL (uses a celius_test database) + migration round-trip
	-docker compose -f infra/compose.dev.yaml exec -T postgres createdb -U celius celius_test
	cd $(API) && TEST_POSTGRES_URL=$(DEV_DB) \
		TEST_DATABASE_URL=postgresql+asyncpg://celius:celius@localhost:5432/celius_test uv run pytest -q

test-web:
	pnpm test && pnpm typecheck

e2e:                ## Browser tests against a full local stack (fake model server)
	pnpm build
	cd $(API) && DATABASE_URL=$(DEV_DB) uv run alembic upgrade head
	DATABASE_URL=$(DEV_DB) infra/scripts/e2e-stack.sh
	cd apps/web && npx playwright test; status=$$?; kill $$(cat ../../.e2e/pids); exit $$status

lint:
	cd $(API) && uv run ruff check . && uv run ruff format --check .
	pnpm typecheck

format:
	cd $(API) && uv run ruff check --fix . && uv run ruff format .

up:                 ## Build and start the production stack
	$(COMPOSE_PROD) up -d --build

down:
	$(COMPOSE_PROD) down

logs:
	$(COMPOSE_PROD) logs -f api

API := services/api
COMPOSE_PROD := docker compose -f infra/compose.yaml --env-file .env
COMPOSE_DEV := docker compose -f infra/compose.dev.yaml
DEV_DB := postgresql+asyncpg://celius:celius@localhost:5432/celius

.PHONY: gen-api desktop install dev-deps dev-api dev-worker dev-web fake-llama migrate migration test test-api-pg \
	test-web test-sandbox lint format e2e up down logs \
	backup restore restore-drill upgrade rollback status

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

test-sandbox:       ## Sandbox service tests
	cd services/sandbox && uv sync --quiet && uv run pytest -q

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

gen-api:            ## Regenerate packages/api-client from the API's OpenAPI document
	pnpm gen:api

desktop:            ## Build the desktop app for this OS (Windows installers on Windows)
	pnpm --filter @celius/desktop build

lint:
	cd $(API) && uv run ruff check . && uv run ruff format --check .
	cd services/sandbox && uv run ruff check . && uv run ruff format --check .
	pnpm typecheck

format:
	cd $(API) && uv run ruff check --fix . && uv run ruff format .
	cd services/sandbox && uv run ruff check --fix . && uv run ruff format .

up:                 ## Build and start the production stack
	$(COMPOSE_PROD) up -d --build

down:
	$(COMPOSE_PROD) down

logs:
	$(COMPOSE_PROD) logs -f api

# --- Operations (docs/operations/) ---

backup:             ## Encrypted backup of the database, files and settings
	infra/scripts/backup.sh

restore:            ## Restore a backup: make restore ARCHIVE=backups/celius-....tar.enc
	infra/scripts/restore.sh "$(ARCHIVE)"

restore-drill:      ## Prove the newest backup restores, in a throwaway copy of the stack
	infra/scripts/restore-drill.sh $(ARCHIVE)

upgrade:            ## Backup, then upgrade: make upgrade [REV=v1.2.0]
	infra/scripts/upgrade.sh $(REV)

rollback:           ## Undo the last upgrade
	infra/scripts/rollback.sh

status:             ## Health checks, as on the status page
	$(COMPOSE_PROD) exec api python -m app.ops.status

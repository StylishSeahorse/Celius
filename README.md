# Celius

A self-hosted, Claude-style AI workspace. It syncs conversations across Windows (Tauri),
Android (PWA) and web clients, and talks to a remote llama.cpp inference server.

**Status:** Phase 1 (foundation) plus the model gateway from Phase 2. There is no
chat UI yet. See [`docs/backlog.md`](docs/backlog.md) for the full plan and what is done.

## What exists today

- `services/api`: FastAPI service with typed settings, a PostgreSQL schema
  (users, sessions, projects, conversations, branches, messages, revisions, runs,
  run events, sync log, audit log) and Alembic migrations.
- Single-user auth: argon2id passwords, revocable server-side sessions, cookie or bearer token.
- Model gateway for llama.cpp / OpenAI-compatible servers: streaming, error mapping,
  safe retries, capability detection via `/props`, live tool-calling probe.
- Health endpoints, a non-root Docker image and a production Compose stack.
- `packages/contracts`: the run-event JSON Schema.

## Quick start (development)

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Docker.

```sh
make install          # API dependencies
make dev-deps         # local Postgres + Redis
make migrate
BOOTSTRAP_USERNAME=admin BOOTSTRAP_PASSWORD='choose-a-long-password' \
  MODEL_BASE_URL=http://YOUR_LLAMA_HOST:8080/v1 make dev-api
```

API docs: http://localhost:8000/api/docs (disabled in production).

```sh
make test             # unit tests (SQLite, mocked model server)
make test-api-pg      # + migration round-trip on the dev Postgres
make lint
```

## Deployment

See [`docs/deployment/reverse-proxy.md`](docs/deployment/reverse-proxy.md).

## Layout

```
services/api/         FastAPI app, migrations, tests
packages/contracts/   JSON Schemas shared with clients
infra/                Dockerfiles and Compose files
docs/                 Backlog, ADRs, deployment guides
```

Planned but not created yet: `apps/web` (React/Vite PWA), `apps/desktop` (Tauri),
`services/worker`, `services/tool-runner`, `packages/api-client`.

## Architecture decisions

- [0001: Separate app, inference and execution](docs/adr/0001-separate-app-inference-execution.md)
- [0002: Python + FastAPI backend](docs/adr/0002-python-fastapi-backend.md)
- [0003: SSE with persisted run events](docs/adr/0003-sse-with-persisted-run-events.md)
- [0004: Server-side sessions](docs/adr/0004-sessions-and-auth.md)

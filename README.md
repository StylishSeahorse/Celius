# Celius

A self-hosted, Claude-style AI workspace. It syncs conversations across Windows (Tauri),
Android (PWA) and web clients, and talks to a remote llama.cpp inference server.

**Status:** Phases 1–3 are implemented. You can:
- chat with streaming replies
- edit messages, regenerate replies and switch between versions
- group chats into projects with their own instructions
- keep working offline (messages are queued and sent exactly once)
- see changes from your other devices live
- install the app on Android (PWA) or Windows (Tauri)

See [`docs/backlog.md`](docs/backlog.md) for the full plan and what is done.

## What exists today

- `services/api`: FastAPI service. It contains:
  - PostgreSQL schema and Alembic migrations
  - Single-user auth (argon2id, revocable sessions)
  - Conversations, idempotent message submission and runs
  - Resumable SSE run streams (`Last-Event-ID`)
  - The llama.cpp / OpenAI-compatible model gateway
- `python -m app.worker`: the generation worker. It claims runs from PostgreSQL with
  leases, so a crashed worker's run is picked up again. It also handles cancellation
  and context budgeting.
- `apps/web`: React + Vite + Tailwind chat UI, served by the API in production. It is an
  installable PWA with an IndexedDB cache and an offline outbox.
- `apps/desktop`: Tauri v2 Windows app that connects to your server.
- `packages/api-client`: TypeScript types generated from the API (`pnpm gen:api`).
- `packages/contracts`: run-event JSON Schema and payload reference.
- `infra/`: one Docker image (API, worker and web) and Compose files.
- `devtools/fake_llama_server.py`: a stand-in llama-server for development and e2e tests without a GPU.

## Quick start (development)

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22 + pnpm, Docker.

```sh
make install          # API + web dependencies
make dev-deps         # local Postgres + Redis
make migrate
make fake-llama       # or point MODEL_BASE_URL at your real llama-server

# in separate terminals:
BOOTSTRAP_USERNAME=admin BOOTSTRAP_PASSWORD='choose-a-long-password' \
  MODEL_BASE_URL=http://127.0.0.1:8080/v1 make dev-api
MODEL_BASE_URL=http://127.0.0.1:8080/v1 make dev-worker
make dev-web          # http://localhost:5173
```

API docs: http://localhost:8000/api/docs (disabled in production).

## Tests

```sh
make test             # API tests (SQLite, mocked model server)
make test-api-pg      # same suite on PostgreSQL + migration round-trip
make test-web         # web unit tests + typecheck
make e2e              # Playwright against a full local stack, desktop + mobile viewports
make lint
```

See [`docs/acceptance-tests/phase-2.md`](docs/acceptance-tests/phase-2.md) for the
Phase 2 exit-gate checks, including manual worker-restart and real-GPU checks.

## Deployment

- Server: [`docs/deployment/proxmox-lxc.md`](docs/deployment/proxmox-lxc.md) and
  [`docs/deployment/reverse-proxy.md`](docs/deployment/reverse-proxy.md)
- Phone and desktop apps: [`docs/deployment/clients.md`](docs/deployment/clients.md)

## Layout

```
apps/web/             React chat UI and PWA
apps/desktop/         Tauri v2 Windows app
packages/api-client/  Generated TypeScript API types
services/api/         API, worker, migrations, tests, dev tools
packages/contracts/   JSON Schemas shared with clients
infra/                Dockerfile, Compose files, scripts
docs/                 Backlog, ADRs, deployment, acceptance tests
```

Planned but not created yet: `services/tool-runner`.

## Architecture decisions

- [0001: Separate app, inference and execution](docs/adr/0001-separate-app-inference-execution.md)
- [0002: Python + FastAPI backend](docs/adr/0002-python-fastapi-backend.md)
- [0003: SSE with persisted run events](docs/adr/0003-sse-with-persisted-run-events.md)
- [0004: Server-side sessions](docs/adr/0004-sessions-and-auth.md)
- [0005: PostgreSQL run queue; API serves the web app](docs/adr/0005-postgres-run-queue.md)
- [0006: Change feed, outbox and branches for multi-device use](docs/adr/0006-sync-and-offline.md)

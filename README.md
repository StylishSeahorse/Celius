# Celius

A self-hosted, Claude-style AI workspace. It syncs conversations across Windows (Tauri),
Android (PWA) and web clients, and talks to a remote llama.cpp inference server.

**Status:** Phases 1–6 are implemented. You can:
- chat with streaming replies
- edit messages, regenerate replies and switch between versions
- group chats into projects with their own instructions
- keep working offline (messages are queued and sent exactly once)
- see changes from your other devices live
- install the app on Android (PWA) or Windows (Tauri)
- attach files, folders and images (text, code, PDF, Word, Excel, PowerPoint, ZIP), with
  documents read for the model and images sent to vision models
- give projects knowledge files, and export conversations (Markdown/JSON/ZIP) or projects (ZIP)
- switch the model's thinking on, off or to its default
- attach files from approved folders on your PC in the Windows app
- let the model use tools: run Python and shell commands in an isolated sandbox, read
  and write workspace files, use Git, fetch web pages, edit images and make Word, Excel,
  PowerPoint and PDF files, and call MCP servers you register
- decide what tools may do: permission profiles, approvals for exact arguments, and an
  approvals inbox
- schedule prompts to run every N minutes, daily or weekly, and follow all work in the
  activity panel
- let Celius remember what matters: it suggests facts, you approve them; it recalls
  earlier chats within the same project; per-chat incognito; delete anything at once

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
- `services/sandbox`: runs model-written code with no network, no secrets and hard
  limits ([ADR 0008](docs/adr/0008-sandbox-and-tools.md)).
- `apps/web`: React + Vite + Tailwind chat UI, served by the API in production. It is an
  installable PWA with an IndexedDB cache and an offline outbox.
- `apps/desktop`: Tauri v2 Windows app that connects to your server.
- `packages/api-client`: TypeScript types generated from the API (`pnpm gen:api`).
- `packages/contracts`: run-event JSON Schema and payload reference.
- `infra/`: Docker images (API + worker + web, and the sandbox) and Compose files.
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
make test             # API tests (SQLite, mocked model server, in-process sandbox)
make test-sandbox     # sandbox service tests (bubblewrap ones skip if unavailable)
make test-api-pg      # same suite on PostgreSQL + migration round-trip
make test-web         # web unit tests + typecheck
make e2e              # Playwright against a full local stack, desktop + mobile viewports
make lint
```

Each phase's exit-gate checks are in [`docs/acceptance-tests/`](docs/acceptance-tests/),
including the manual ones for the real deployment.

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
services/sandbox/     Code execution service
packages/contracts/   JSON Schemas shared with clients
infra/                Dockerfile, Compose files, scripts
docs/                 Backlog, ADRs, deployment, acceptance tests
```

## Architecture decisions

- [0001: Separate app, inference and execution](docs/adr/0001-separate-app-inference-execution.md)
- [0002: Python + FastAPI backend](docs/adr/0002-python-fastapi-backend.md)
- [0003: SSE with persisted run events](docs/adr/0003-sse-with-persisted-run-events.md)
- [0004: Server-side sessions](docs/adr/0004-sessions-and-auth.md)
- [0005: PostgreSQL run queue; API serves the web app](docs/adr/0005-postgres-run-queue.md)
- [0006: Change feed, outbox and branches for multi-device use](docs/adr/0006-sync-and-offline.md)
- [0007: File uploads, storage and processing](docs/adr/0007-file-storage.md)
- [0008: Sandbox, tool policy and approvals](docs/adr/0008-sandbox-and-tools.md)
- [0009: Memory and recall](docs/adr/0009-memory-and-recall.md)

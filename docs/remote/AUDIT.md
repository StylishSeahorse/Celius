# Celius Remote — Phase 0 repository audit

Date: 2026-11-12. Every statement below was checked against the code on branch
`ccr-f650cd7b-39q5nz` (commit `ba2dce0`), not only against the README. File references are
relative to the repository root.

## 1. What exists today

```
                         ┌────────────────────────── Proxmox host (Docker Compose, infra/compose.yaml) ─────────────────────────┐
 Browser / PWA (Android) │                                                                                                       │
 Tauri desktop shell ────┼──HTTPS (reverse proxy)──▶ api  (FastAPI, uvicorn, app/main.py)                                       │
   (apps/desktop: loads  │                            │  REST /api/v1/*, SSE /api/v1/runs/{id}/events (polls run_events)        │
    the server's web app)│                            │  serves apps/web build (app/static.py)                                   │
                         │                            ▼                                                                          │
                         │                         postgres (pgvector 16) ◀── worker (python -m app.worker)                     │
                         │                            ▲   runs / run_events /     │ claims queued runs (SKIP LOCKED + lease)  │
                         │                            │   tool_calls / jobs ...   │ executes model turns + tools               │
                         │                            │                           ├──HTTP──▶ sandbox (bubblewrap runner, :8100) │
                         │                            │                           │            └─▶ egress proxy (public only)  │
                         │                         redis (started, unused by code)│                                            │
                         └────────────────────────────────────────────────────────┼────────────────────────────────────────────┘
                                                                                  └──HTTP──▶ llama-server on the GPU workstation
                                                                                             (MODEL_BASE_URL, outside the stack)
```

| Area | Where | Verified behaviour |
|---|---|---|
| API | `services/api/app/main.py`, `app/api/v1/*` | One router under `/api/v1`. Errors: `AppError` → `{"error": {"code", "message", ...}}` (`app/errors.py`). |
| Auth | `app/auth/*` | Opaque bearer token or cookie; only SHA-256 of the token stored (`sessions` table). `CurrentAuth` dependency. No machine identities. |
| Runs | `app/models/generation.py`, `app/services/runs.py` | `runs` with status `queued/running/awaiting_approval/completed/failed/cancelled`, lease (`worker_id`, `lease_expires_at`, `attempts`), `cancel_requested`. |
| Run events / SSE | `run_events` table, `app/api/v1/runs.py` | Every stream event is a row; SSE polls the table every 0.25 s with a short-lived DB session; resumable with `Last-Event-ID`. Token deltas of finished runs are pruned after 10 min. Event types are a closed enum (`app/schemas/events.py`) mirrored in `packages/contracts/schemas/run-event.schema.json`. |
| Worker | `app/worker/loop.py`, `app/worker/executor.py` | Claims runs with `SELECT … FOR UPDATE SKIP LOCKED` + conditional `UPDATE`; renews its lease about once a second; requeues runs whose lease expired (bounded by `RUN_MAX_ATTEMPTS`). A tool call that was running when a worker died is reported as `interrupted` and **never re-run**. |
| Tools | `app/services/tools/*` | `ToolSpec` registry (`registry.py`), per-run `Toolset` (`toolset.py`), `ToolContext` carries `workspace: Path` and `sandbox`. File tools act on `WORKSPACE_ROOT/<conversation>` in the worker; code/shell/git go to the sandbox service. |
| Approvals | `app/services/tools/policy.py`, `calls.py`, `review.py` | Profiles map risk (`read/write/execute/network/external`) → allow/ask/deny; `always_ask` tools; per-conversation grants. A call needing approval pauses the run (`awaiting_approval`, no worker held). Approve/deny bind to `args_hash`; when the last pending call is decided the run is re-queued. Expiry after `TOOL_APPROVAL_TTL_MINUTES`. All decisions audited. Optional auto-reviewer (C-13). |
| Sandbox | `services/sandbox`, ADR 0008 | Separate container, token-authenticated, bubblewrap, no network except a public-only egress proxy. |
| Sync | `app/services/sync.py`, `sync_events` | Every durable write records a per-user change event; devices page by cursor and refetch. |
| Push | `app/services/push.py` | Web Push for "reply ready" and "approval needed" (queued as jobs). |
| Audit | `app/services/audit.py`, `audit_events` | Written in the same transaction as the change. |
| Clients | `apps/web` (React + Vite), `apps/desktop` (Tauri shell around the server's web app), Android = the installable PWA | One web UI serves all three. Types come from `packages/api-client`, generated from the API's OpenAPI document; CI fails if it is stale. |
| Migrations | `services/api/migrations` (Alembic, 0001–0015) | CI round-trips them on PostgreSQL and requires `alembic check` to find no drift. |

## 2. Reusable components for Celius Remote

1. **PostgreSQL as the bus.** The project already uses Postgres (not Redis) for the run
   queue, run events and the SSE feed. Remote commands and remote session events fit the
   same pattern, so no new broker or service is needed.
2. **Runs, messages, tool calls.** A remote session's turns can be ordinary `runs`, its
   tool activity ordinary `tool_calls`, and its replies ordinary assistant `messages`; the
   chat UI and sync already render and replicate these.
3. **Approval system.** `tool_calls.status = pending_approval` + `/tools/calls/{id}/approve|deny`
   + `resume_if_decided` + expiry + audit + push. Remote approvals must flow through this,
   not a second system.
4. **Executor tool loop.** Native remote execution only needs tools whose handler runs on
   the remote machine instead of the sandbox; the model loop, prompts, memory, policy,
   approvals, leases and recovery stay exactly as they are.
5. **Leases / never re-run.** The executor's guarantee that an interrupted call is never
   repeated must extend to calls dispatched to a remote machine.
6. **SSE pattern** (poll a table, resume from an id) for the remote session event stream.

## 3. Architectural limitations found

| # | Limitation | Consequence for the design |
|---|---|---|
| L1 | No machine/service identity; auth is user sessions only. | New per-machine credentials and short-lived agent tokens are required. |
| L2 | `ToolContext` assumes a local `Path` workspace and an HTTP sandbox. | Remote tools get their own handlers; the context gains a `remote` dispatcher (additive). |
| L3 | API and worker are separate processes that share only PostgreSQL. | The agent's WebSocket ends in the API; the worker talks to it through durable command rows. |
| L4 | `RunEventType` is a closed enum validated on output. | Remote terminal/file/resource events go to a separate session event log with its own SSE stream; run events keep their contract. |
| L5 | Chat allows one active run per conversation; edits/regenerations fork branches. | External agents (Claude Code) cannot fork history: edit/regenerate are refused on external sessions; new instructions are sent between turns. |
| L6 | The worker claims every queued run. | Runs driven by an external agent must be excluded from claiming (new `runs.execution` column). |
| L7 | Redis is deployed but unused. | Not adopted; Postgres polling (250 ms) is the established latency budget. |
| L8 | Two API tests fail before any change (`test_continuous_improvement.py::test_memory_benchmark_*`): `devtools/data/memory_eval.json` is excluded by the `data/` rule in `.gitignore`. | Pre-existing, unrelated; recorded, not fixed here. |

## 4. Assumptions that cannot be verified from the repository

- The production reverse proxy forwards WebSocket upgrades on `/api/v1/remote/agent/connect`
  (nginx/Caddy/Traefik need explicit config for nginx). Documented in deployment notes.
- The API runs as a single uvicorn process (compose shows one `api` container, no
  `--workers`). The design does not rely on it: duplicate-connection protection and
  command delivery go through the database.
- Remote Windows machines have Python 3.10+ (or use a PyInstaller build), and Claude Code
  machines have the Claude Code CLI / Agent SDK installed and signed in.
- Clock skew between agents and the server is small; ordering never relies on agent clocks
  (sequence numbers are used), only display does.
- The Windows agent could not be run in this environment (Linux container). Windows code
  paths are covered by unit tests designed to run on `windows-latest` in CI.

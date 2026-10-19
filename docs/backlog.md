# Implementation backlog

Derived from the October 2026 architecture specification. Phases are in dependency
order; a phase starts only when the previous phase's exit gate passes. No calendar
estimates are given until the model endpoint, sandbox technology and deployment
environment are confirmed.

Status key: ✅ done in repo · 🔶 partially done · ⬜ not started

---

## Phase 1 — Foundation and deployment (blocking)

**Exit gate:** reproducible deployment and persistent database verified.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| F-01 | Monorepo layout, `.gitignore`, `.env.example`, Makefile | ✅ | Secrets, uploads, backups and `*.gguf` ignored; every env var documented |
| F-02 | API service skeleton (FastAPI, typed settings) | ✅ | Settings load from env; production refuses a default/short `APP_SECRET_KEY` |
| F-03 | SQLAlchemy models + Alembic initial migration | ✅ | `alembic upgrade head` / `check` / `downgrade base` succeed on PostgreSQL 16 (tested in CI) |
| F-04 | Single-user auth: argon2 hashes, server-side sessions, cookie + bearer | ✅ | Tokens stored hashed; login failures audited; no username enumeration; revoked/expired/inactive sessions rejected |
| F-05 | Bootstrap account from env | ✅ | Created only when the users table is empty; never overwrites |
| F-06 | Health endpoints | ✅ | `/health/live` for the container; `/health/ready` gates on DB, reports model server without gating |
| F-07 | Dockerfile (non-root, read-only rootfs) and production Compose | 🔶 | Compose validates in CI; **image build and a full `up` on Proxmox not yet run** |
| F-08 | CI: lint, tests, migration round-trip, image build | ✅ | `.github/workflows/api.yml` |
| F-09 | Reverse-proxy guide (TLS, SSE buffering, timeouts) | ✅ | `docs/deployment/reverse-proxy.md` |
| F-10 | Deploy on Proxmox, restart stack, confirm data persists | ⬜ | Data survives `down`/`up` and host reboot |
| F-11 | Login rate limiting | ⬜ | Repeated failures from one IP/username are throttled |
| F-12 | Password change endpoint + CLI to reset the account | ⬜ | Changing the password revokes other sessions |

## Phase 2 — Model gateway and chat

**Exit gate:** end-to-end chat and restart recovery tests pass.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| M-01 | Provider-neutral gateway interface | ✅ | `list_models`, `get_capabilities`, `stream_chat`, `count_tokens`, `create_embeddings`, `health_check` |
| M-02 | llama.cpp / OpenAI-compatible adapter | ✅ | Streams text, reasoning, tool-call deltas and usage; maps errors to stable codes; retries only before first byte |
| M-03 | Capability detection | ✅ | Context and vision from `/props`; tool calling only marked `verified` after a live probe |
| M-04 | `GET /models`, `POST /models/{id}/verify-tools` | ✅ | Model list still returns (empty, with health) when inference is offline |
| M-05 | Verify against the real RTX 3060 server | ⬜ | Record model, quant, `n_ctx`, `--jinja`, measured tokens/s and safe `MODEL_PARALLEL_REQUESTS` in `docs/operations/model-server.md` |
| C-01 | Conversation CRUD (`/conversations`) | ⬜ | Owner-scoped; soft delete; optimistic `version` checks |
| C-02 | Message submission with idempotent `client_message_id` | ⬜ | Message + run committed in one transaction; duplicate submission returns the original IDs |
| C-03 | Run executor (worker) | ⬜ | Picks up `queued` runs, including after an API/worker restart |
| C-04 | `GET /runs/{id}/events` SSE with `Last-Event-ID` resume | ⬜ | Events persisted in `run_events`; reconnect replays missed events |
| C-05 | Cancellation | ⬜ | `POST /runs/{id}/cancel` stops generation within one chunk and marks the run `cancelled` |
| C-06 | Context assembly with token budget | ⬜ | Truncation never drops the system prompt or the latest user message |
| C-07 | Minimal chat UI (React + Vite) | ⬜ | Send, stream, cancel, model override per conversation |

## Phase 3 — Client experience and sync

**Exit gate:** synchronisation, reconnect and duplicate-submission tests pass.

| ID | Item | Acceptance criteria |
| --- | --- | --- |
| S-01 | Generated TS client in `packages/api-client` | CI fails if the client is stale relative to OpenAPI |
| S-02 | Sync change feed (`/sync/changes`, `/sync/ack`) over `sync_events` | Every durable write emits a sync event in the same transaction |
| S-03 | IndexedDB cache + outgoing queue | Offline sends are queued in order and delivered exactly once |
| S-04 | Conflict handling on concurrent edits | Stale `version` returns 409 with server state; UI offers resolution |
| S-05 | Revisions, regenerate, branches UI | Alternate responses never overwrite the original |
| S-06 | Sidebar, search, projects | Search covers titles and message text |
| S-07 | PWA (manifest, service worker) | Installable on Android; app shell loads offline |
| S-08 | Tauri v2 Windows wrapper | Scoped capabilities only; signed installer build |

## Phase 4 — Files and workspaces

**Exit gate:** upload/download integrity, path security and quota tests pass.

| ID | Item | Acceptance criteria |
| --- | --- | --- |
| U-01 | Upload sessions with quarantine and signature checks | Rejects mismatched MIME/signature, oversized files |
| U-02 | Archive/folder handling | Path traversal, symlinks, zip bombs rejected |
| U-03 | Text extraction (PDF, Office, code) and thumbnails | Runs in the worker; status visible on the attachment |
| U-04 | Authorised downloads, exports (Markdown/JSON/ZIP) | No permanent public URLs; ownership checked per request |
| U-05 | Atomic storage quotas | Concurrent uploads cannot exceed the quota |
| U-06 | Approved desktop roots via Tauri | Path containment enforced server-side |

## Phase 5 — Tool execution and autonomy

**Exit gate:** sandbox isolation, policy enforcement, cancellation and audit tests pass.
Arbitrary shell stays disabled until T-01 is decided and validated.

| ID | Item | Acceptance criteria |
| --- | --- | --- |
| T-01 | ADR: sandbox technology (gVisor / Firecracker / rootless containers) | Threat model reviewed; no Docker socket in the tool runner |
| T-02 | Permission engine and profiles | Every call checked server-side regardless of model claims |
| T-03 | Approvals bound to exact args, scope, expiry | Changed arguments require re-approval |
| T-04 | Tool loop limits | Max iterations, wall-clock, output size enforced |
| T-05 | Tools: Python, files, Git, web/HTTP, image, documents | Network off by default; per-tool allowlists |
| T-06 | MCP client | MCP servers registered by admin only |
| T-07 | Scheduled and background jobs | Monitor, cancel and recover; quotas enforced |

## Phase 6 — Memory and retrieval

**Exit gate:** retrieval scope, exclusion and deletion tests pass.

| ID | Item | Acceptance criteria |
| --- | --- | --- |
| R-01 | pgvector + embedding provider config, keyword fallback | Works with embeddings disabled |
| R-02 | Memory proposal extraction with review queue | Nothing becomes durable memory without approval when review is required |
| R-03 | Memory edit/exclude/disable (global, project, conversation) | Excluded sources never retrieved |
| R-04 | Dependency-aware deletion | Deleted memory is unretrievable immediately; UI explains what deletion does not erase |

## Phase 7 — Release hardening

**Exit gate:** all required acceptance tests pass in a clean deployment.

| ID | Item | Acceptance criteria |
| --- | --- | --- |
| H-01 | Coordinated backup (DB + files + config + manifest with checksums) | Encrypted off-host copy |
| H-02 | Restore into a clean environment | Messages, attachments, projects, memory recovered |
| H-03 | Monitoring and alerting | Disk, queue depth, model health, backup age |
| H-04 | Security review | Auth, secrets, file access, sandbox escape tests |
| H-05 | Upgrade and rollback runbook | Pre-upgrade backup; tested rollback |

---

## Remaining assumptions (configurable until verified)

| Assumption | Where it lives |
| --- | --- |
| llama.cpp URL, port, API key | `MODEL_BASE_URL`, `MODEL_API_KEY` |
| Loaded model, quantisation, context | Discovered via `/v1/models` and `/props`; overrides in env |
| Tool-calling support | Verified per model with `POST /api/v1/models/{id}/verify-tools` |
| Parallel request limit | `MODEL_PARALLEL_REQUESTS=1` until benchmarked |
| Sandbox technology | Phase 5 ADR (T-01) |
| Backup destination | Phase 7 (H-01) |

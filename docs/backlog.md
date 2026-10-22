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
| F-07 | Dockerfile (non-root, read-only rootfs) and production Compose | 🔶 | One image for API, worker and web; Compose validates and CI builds the image; **a full `up` on Proxmox has not been run yet** |
| F-08 | CI: lint, tests (SQLite + PostgreSQL), migrations, web, e2e, image build | ✅ | `.github/workflows/ci.yml` |
| F-09 | Reverse-proxy guide (TLS, SSE buffering, timeouts) | ✅ | `docs/deployment/reverse-proxy.md` |
| F-10 | Deploy on Proxmox, restart stack, confirm data persists | ⬜ | Data survives `down`/`up` and host reboot |
| F-11 | Login rate limiting | ✅ | 10 failures per account / 30 per address in 15 min → 429 with Retry-After, even with the right password; CLI reset clears it |
| F-12 | Password change endpoint + CLI to reset the account | ✅ | Changing the password revokes other sessions; `python -m app.ops.users reset-password`; signed-in devices list with sign-out |

## Phase 2 — Model gateway and chat

**Exit gate:** end-to-end chat and restart recovery tests pass.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| M-01 | Provider-neutral gateway interface | ✅ | `list_models`, `get_capabilities`, `stream_chat`, `count_tokens`, `create_embeddings`, `health_check` |
| M-02 | llama.cpp / OpenAI-compatible adapter | ✅ | Streams text, reasoning, tool-call deltas and usage; maps errors to stable codes; retries only before first byte |
| M-03 | Capability detection | ✅ | Context and vision from `/props`; tool calling only marked `verified` after a live probe |
| M-04 | `GET /models`, `POST /models/{id}/verify-tools` | ✅ | Model list still returns (empty, with health) when inference is offline |
| M-05 | Verify against the real RTX 3060 server | ⬜ | Record model, quant, `n_ctx`, `--jinja`, measured tokens/s and safe `MODEL_PARALLEL_REQUESTS` in `docs/operations/model-server.md` |
| C-01 | Conversation CRUD (`/conversations`) | ✅ | Owner-scoped; soft delete; optimistic `version` checks (409 `version_conflict`); cursor pagination; search over titles and message text |
| C-02 | Message submission with idempotent `client_message_id` | ✅ | User message, assistant placeholder and run committed in one transaction; a retry returns the original IDs (200, `duplicate: true`), including under a concurrent race; one active run per conversation |
| C-03 | Run executor (`python -m app.worker`) | ✅ | Claims from PostgreSQL (ADR 0005); leases and heartbeat; requeues runs from dead workers up to `RUN_MAX_ATTEMPTS`; a stale worker can't overwrite a recovered run |
| C-04 | `GET /runs/{id}/events` SSE with `Last-Event-ID` resume | ✅ | Events persisted in `run_events`; replay plus live tail; the stream closes after the terminal event; keep-alives |
| C-05 | Cancellation | ✅ | Queued runs are cancelled immediately; streaming runs stop within one flush (~100 ms), during prompt processing within ~1 s; partial text kept |
| C-06 | Context assembly with token budget | ✅ | Oldest turns dropped first; system prompt and latest user message always kept; `context_overflow` before calling the model; consecutive same-role turns merged |
| C-07 | Minimal chat UI (React + Vite + Tailwind) | ✅ | Login, sidebar, search, new/delete chat, streaming markdown, reasoning disclosure, stop, model override per conversation, resume after reload, mobile layout |
| C-08 | Verify the end-to-end flow on the real deployment | ⬜ | `docs/acceptance-tests/phase-2.md`, manual section |
| C-09 | Prune `message.delta` events after completion | ✅ | Phase 8: deleted `RUN_DELTA_RETENTION_MINUTES` after a run ends; other events kept; late clients reload the message |
| C-10 | `LISTEN/NOTIFY` wake-ups (optional) | ⬜ | Only if polling latency or load is noticeable |

## Phase 3 — Client experience and sync

**Exit gate:** synchronisation, reconnect and duplicate-submission tests pass.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| S-01 | Generated TS client in `packages/api-client` | ✅ | `pnpm gen:api` (openapi-typescript); CI fails if stale |
| S-02 | Sync change feed (`/sync/changes`, `/sync/cursor`, `/sync/ack`) over `sync_events` | ✅ | Every durable write, including run completion, cancel and failure, emits an event in the same transaction; tested on SQLite and PostgreSQL |
| S-03 | IndexedDB cache + outgoing queue | ✅ | Offline sends are queued in order and delivered exactly once; a lost response doesn't duplicate; conversations can be started offline |
| S-04 | Conflict handling on concurrent edits | ✅ | A stale `version` returns 409 with server state; dialog offers "Keep theirs / Use mine"; `parent_not_head` reports the current head |
| S-05 | Revisions, regenerate, branches | ✅ | Edits and regenerations fork branches; "‹ n / m ›" switching; originals never overwritten |
| S-06 | Sidebar, search, projects | ✅ | Projects CRUD with instructions in the system prompt; project filter; search over titles and message text |
| S-07 | PWA (manifest, icons, service worker) | ✅ | Installable; app shell and saved conversations open offline (needs HTTPS or localhost) |
| S-08 | Tauri v2 Windows wrapper | 🔶 | Connect page, menu, no IPC for remote pages; compiles on Linux; CI builds NSIS + MSI on Windows. **Authenticode signing not set up** (needs a certificate); Windows build not yet run |
| S-09 | Open reply links in the system browser from the desktop app | ✅ | Done in phase 4 (see U-06 area) |
| S-10 | Push notifications for finished replies | ⬜ | Optional; Web Push needs HTTPS and a VAPID key |

## Phase 4 — Files and workspaces

**Exit gate:** upload/download integrity, path security and quota tests pass.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| U-01 | Upload sessions with quarantine and signature checks | ✅ | Type decided from the bytes and must match the extension; oversized and over-long uploads refused; optional ClamAV; retry-safe |
| U-02 | Archive and folder handling | ✅ | Traversal, absolute paths, symlinks, bombs (ratio), entry count and total size rejected, including in .docx/.xlsx/.pptx; folder paths normalised |
| U-03 | Text extraction (text, PDF, DOCX, XLSX, PPTX, ZIP) and thumbnails | ✅ | Runs as durable worker jobs; status visible on the attachment; bounded readers |
| U-04 | Authorised downloads, exports (Markdown/JSON/ZIP) | ✅ | Ownership checked per request; nosniff + CSP sandbox; only images and PDFs inline; conversation and project exports |
| U-05 | Atomic storage quotas | ✅ | Concurrent uploads can't overshoot; rejection, expiry and deletion release space |
| U-06 | Approved desktop folders via Tauri | ✅ | Native picker; read-only; containment and symlink checks in Rust (unit-tested); granted only to the connected server's origin |
| U-07 | Attachments and project files in prompts | ✅ | Tagged blocks, per-file and project budgets; images only to vision models; "data, not instructions" |
| U-08 | Reasoning (thinking) on/off/auto per conversation | ✅ | `chat_template_kwargs.enable_thinking`; per-reply override; template support detected |
| S-09 | Reply links open in the system browser from the desktop app | ✅ | `open_external`, http(s) only |
| U-09 | Code-block Copy/Download | ✅ | Client-side, language → file extension |
| U-10 | Previews for PDFs and Office files (page images) | ⬜ | Thumbnails exist for images only |
| U-11 | Resumable chunked uploads | ✅ | Phase 8: 4 MB chunks with `Content-Range`; resume from `GET /files/uploads/{id}` after a dropped connection |
| U-12 | Server-side project workspaces (extract archives to disk) | ⬜ | Needed by tools in phase 5; containment helper `resolve_within` already in place |

## Phase 5 — Tool execution and autonomy

**Exit gate:** sandbox isolation, policy enforcement, cancellation and audit tests pass.
Arbitrary shell stays disabled until T-01 is decided and validated.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| T-01 | ADR: sandbox technology | ✅ | [ADR 0008](adr/0008-sandbox-and-tools.md): a dedicated container (internal network, no secrets, no Docker socket, read-only, no capabilities), bubblewrap per run when the host allows it, rlimits and stray-process reaping always; gVisor/Firecracker rejected for now (need Proxmox host changes) |
| T-02 | Permission engine and profiles | ✅ | Four built-in profiles plus custom ones (per-tool overrides); every call decided server-side; denied tools aren't offered and are refused if called anyway; unknown tools and invalid arguments refused |
| T-03 | Approvals bound to exact args, scope, expiry | ✅ | Approval quotes the SHA-256 of the canonical arguments; a mismatch is refused and audited; "allow for this chat" is per conversation and tool and never overrides a deny; requests expire; runs pause without holding a worker and resume where they stopped |
| T-04 | Tool loop limits | ✅ | Iterations, total tool time, per-call timeout and result size; at the limit the model is told to answer; a call is never run twice after a worker restart |
| T-05 | Tools: Python, shell, files, Git, web, image, documents | ✅ | Code runs in the sandbox with no network; web tools run in the worker behind an SSRF guard (private/LAN addresses refused, DNS pinned, redirects re-checked, size-capped), optional host allowlist; workspace access never follows symlinks; results can be attached to the reply |
| T-06 | MCP client | ✅ | Streamable HTTP; administrators only; credentials encrypted at rest and never returned; tools namespaced, `external` risk, same policy |
| T-07 | Scheduled and background jobs | ✅ | Interval (≥15 min), daily and weekly in the user's time zone; fired once per occurrence by a conditional claim; per-user quota; activity panel lists runs and jobs with cancel |
| T-08 | Run the sandbox image on the real host | ⬜ | `make up` on Proxmox; **Tools → Permissions** shows the sandbox running; Python and a document round-trip |
| T-09 | Tool calling with the real model | ⬜ | Verify with `POST /models/{id}/verify-tools`; record which models and templates call tools reliably |
| T-10 | Tool results in later turns | ✅ | Phase 8: earlier replies are replayed with a short summary of their tool calls (`TOOL_HISTORY_RESULT_CHARS`) |

## Phase 6 — Memory and retrieval

**Exit gate:** retrieval scope, exclusion and deletion tests pass.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| R-01 | pgvector + embedding provider config, keyword fallback | ✅ | [ADR 0009](adr/0009-memory-and-recall.md): optional `/v1/embeddings` endpoint; pgvector cosine ranking on PostgreSQL; word ranking without embeddings or when the server is down (tested) |
| R-02 | Memory proposal extraction with review queue | ✅ | Worker job proposes up to 2 facts per reply, seeing what is already remembered (empty is the normal answer; task details excluded; messages under three words skipped); `remember` tool proposes too; proposals never used until approved (or edited and approved); rejected text erased and not proposed again; duplicates dropped |
| R-03 | Memory edit/exclude/disable (global, project, conversation) | ✅ | Global and project scopes; switches per user, project and chat (incognito from the first message); excluded chats never indexed or recalled; recall stays within the project boundary |
| R-04 | Dependency-aware deletion | ✅ | Deleting a memory or "forget everything" removes rows and embeddings at once; deleting a chat removes its excerpts and, if ticked, the memories learned from it; the UI says what deletion can't erase |
| R-05 | Memory with the real models | ⬜ | Check extraction quality with the deployed model; choose and benchmark an embedding model on the 3060 (or CPU) |
| R-06 | Memory summaries for long histories | ⬜ | Only if recall quality drops as history grows |

## Phase 7 — Release hardening

**Exit gate:** all required acceptance tests pass in a clean deployment.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| H-01 | Coordinated backup (DB + files + config + manifest with checksums) | ✅ | [Backup and restore](operations/backup-restore.md): api/worker paused for a consistent snapshot; manifest with SHA-256 per part and a database census; AES-256 archive; rsync/rclone off-host copy; retention; age shown and alerted |
| H-02 | Restore into a clean environment | ✅ | `restore.sh` on a machine with no settings or volumes recovers chats, files (byte-identical), projects and memories; verified against the manifest; tampered archives refused; `restore-drill.sh` proves a backup next to the live stack (CI release gate) |
| H-03 | Monitoring and alerting | ✅ | [Monitoring](operations/monitoring.md): database, model server, sandbox, worker heartbeat, disk, run/job queues, backup age and off-host copy; status panel, `make status`, Prometheus metrics; webhook (JSON / ntfy / text) on changes, repeated while critical |
| H-04 | Security review | ✅ | [Security review](security-review.md): every route requires auth (generated test), throttling, CSRF origin check, CSP and headers; secrets, file access and sandbox escape tests listed; fixed: a background child could stall a sandbox slot forever |
| H-05 | Upgrade and rollback runbook | ✅ | [Upgrade and rollback](operations/upgrade-and-rollback.md): pre-upgrade backup, saved images, health wait; rollback keeps data when no migrations changed, else restores the pre-upgrade backup; rollback exercised in CI |
| H-06 | First backup and restore drill on the real host | ⬜ | Set `BACKUP_PASSPHRASE` and `BACKUP_OFFSITE_TARGET`; cron; `make restore-drill` passes |
| H-07 | Dependency audit in CI | ✅ | Phase 8: weekly and on lockfile changes; first run found and fixed pypdf (85 advisories), Pillow, lxml, requests, Markdown |

## Phase 8 — Reliability and scale

**Exit gate:** reliability tests pass on CI (API, browser, release gate) and the dependency audit is clean.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| U-11 | Resumable chunked uploads | ✅ | A 9 MB upload whose connection drops mid-way completes byte-identical without starting over (e2e, desktop and phone) |
| C-09 | Prune streamed-token events | ✅ | Finished runs keep only their non-delta events; a late client still sees the outcome and the reply |
| T-10 | Tool use remembered in later turns | ✅ | The next turn's prompt lists the earlier tool calls and a shortened result |
| H-07 | Dependency audit | ✅ | `.github/workflows/audit.yml`; no known vulnerabilities in the API, sandbox service, sandbox image libraries or web production packages |

## Phase 9 — Phone and desktop polish

**Exit gate:** notifications, previews and sharing work in the browser tests on desktop and phone viewports.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| S-10 | Push notifications for finished replies | ✅ | Web Push (RFC 8291/8292; keys derived from `APP_SECRET_KEY`); opt-in per device; replies, failures and approvals; not shown for the chat on screen; only the browser vendors' push services accepted |
| U-10 | Previews for PDFs and Office files | ✅ | PDF first page rendered by pdfium in a limited subprocess; Office files show their embedded preview picture (no LibreOffice); shown as cards in the chat |
| S-11 | Share into Celius from Android | ✅ | PWA share target: text, links and files shared from other apps open a new chat with them attached |
| S-12 | Desktop app notifications and tray | ✅ | Native notification when a reply finishes or needs approval while the window is in the background; closing keeps it in the tray |

## Phase 10 — Multiple users (family)

**Exit gate:** isolation tests between users pass for every resource; admin flows pass in the browser tests.

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| A-01 | Invitations | ✅ | One-time link (shown once, stored only as a hash), 1 hour to 30 days, revocable, optionally for an admin; the invitee picks a username and password |
| A-02 | User management | ✅ | **Account → People**: last seen, storage; disable (signed out at once), enable, reset password, admin rights, delete with all data (content shared with others kept); the last admin can't be removed |
| A-03 | Per-user limits | ✅ | Storage quota, replies per 24 hours, allowed models (others hidden and refused), allowed tool profiles (wider ones narrowed at run time) |
| A-04 | Shared projects | ✅ | Shared by username as "can use" or "can edit"; members use its instructions and files; chats and memories stay private; members can leave |
| A-05 | Isolation review | ✅ | A second user calls every id route with the first user's ids (generated from the API schema): all refused, nothing leaks, nothing changes; listings empty; recall doesn't cross people in a shared project |

## Additions

| ID | Item | Status | Acceptance criteria |
| --- | --- | --- | --- |
| R-07 | Selective memory | ✅ | "Always use" memories reach every chat in scope (at most 10); others only when relevant to the message (embedding similarity ≥ `MEMORY_MIN_SIMILARITY`, or shared words), at most 8; extraction marks which is which; toggle in the memory panel |
| T-11 | Tool calls written as text never shown raw | ✅ | Recovered when the tools are offered (also from the reasoning, never run twice next to a real call); otherwise removed, with a note: the step limit was reached ("continue" carries on), or the tool isn't available. `TOOL_MAX_ITERATIONS` 12 → 25 |
| U-14 | Work panel | ✅ | A reply shows its answer; reasoning, tool calls and what was said between them fold into one panel (closed; live status while working: "Running python…"); approvals stay outside it |
| U-13 | Audio and video uploads | ✅ | mp3, m4a, aac, wav, flac, ogg, opus, weba; mp4, m4v, mov, webm, mkv, ogv, avi. Accepted only when the bytes are that kind of container; own size limit (`MAX_MEDIA_UPLOAD_BYTES`, 2 GB); duration, codecs and a still frame read by ffprobe/ffmpeg in a limited subprocess; plays in the chat with seeking (range requests); the model is told what the file is and tools get it in the workspace. No transcription yet |

---

## Remaining assumptions (configurable until verified)

| Assumption | Where it lives |
| --- | --- |
| llama.cpp URL, port, API key | `MODEL_BASE_URL`, `MODEL_API_KEY` |
| Loaded model, quantisation, context | Discovered via `/v1/models` and `/props`; overrides in env |
| Tool-calling support | Verified per model with `POST /api/v1/models/{id}/verify-tools` |
| Parallel request limit | `MODEL_PARALLEL_REQUESTS=1` until benchmarked |
| Sandbox isolation mode on the host | `SANDBOX_MODE` (auto: bubblewrap if allowed, else process limits) |
| Backup destination | `BACKUP_OFFSITE_TARGET` (rsync or rclone), set on the host (H-06) |

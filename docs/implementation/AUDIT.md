# Phase 0: audit of the existing Celius codebase

Date: 2026-11-05 · Baseline commit `b1628d5` · Baseline tests: **414 passed, 7 skipped**
(SQLite), migrations round-trip on PostgreSQL 16 + pgvector.

This audit was made by reading the source and running the tests, not from the README.
Where the README and the code disagree, the code wins (none found that matter).

## 1. Repository map

```
apps/web/              React 18 + Vite + Tailwind PWA (offline outbox, SSE client)
apps/desktop/          Tauri v2 Windows shell (local folders, tray, notifications)
services/api/          FastAPI API + worker (same codebase, two processes)
  app/api/v1/          REST routes (auth, conversations, messages, runs, files, projects,
                       memory, tools, automation (schedules, MCP, activity), push, sync,
                       exports, admin, models, health)
  app/models/          SQLAlchemy 2 models (one file per area)
  app/services/        Domain logic
    chat.py            submit a message -> run
    context.py         token budget and history trimming
    prompt_files.py    attachments / project files as untrusted <attachment> blocks
    memory/            store, retrieval, embeddings, background jobs   <- Phase 1
    tools/             registry, policy, toolset, builtin tools, MCP client, netguard,
                       sandbox client, web search, workspace
    model_gateway/     OpenAI-compatible client for llama-server, model catalog
    jobs.py            durable background jobs (claim/lease in PostgreSQL)
    schedules.py       scheduled prompts (interval/daily/weekly)
  app/worker/          run executor (agent loop) and worker loop
  migrations/          Alembic 0001..0009 (+0010, 0011 added in this work)
  tests/               pytest, SQLite by default, PostgreSQL with TEST_DATABASE_URL
services/sandbox/      Code execution service (bubblewrap/process), egress proxy
packages/api-client/   Generated TypeScript types (OpenAPI)
infra/                 Dockerfiles, compose, backup/restore/upgrade scripts
docs/                  backlog, ADR 0001..0009, deployment, operations, acceptance tests
```

## 2. Architecture and dependency map

```
 web / PWA / desktop ──HTTPS──► api (FastAPI) ──► PostgreSQL (+pgvector)
                                   │  writes runs, jobs        ▲
                                   ▼                           │ claims with leases
                                 worker ── executor._Loop ─────┘
                                   │  ├─► model_gateway ──► llama-server (OpenAI API)
                                   │  ├─► tools.policy (allow/ask/deny per risk, grants)
                                   │  ├─► tools.builtin ──► sandbox (code) ──► egress
                                   │  ├─► tools.mcp (streamable HTTP)
                                   │  └─► memory.retrieval (prompt) / memory.jobs (after)
                                   └─ jobs: file processing, exports, memory.index,
                                      memory.extract, (new) memory.reembed, skill.propose
```

Module dependencies (inside `services/api/app`): `api -> services -> models/db`.
`worker.executor` depends on `services.{context, prompt_files, memory, tools,
model_gateway}`. `services.tools.builtin` depends on `services.memory`. No cycles.

## 3. Feature-to-code mapping

| Capability | Status | Where |
| --- | --- | --- |
| Conversations, branches, edits, regenerate | Done | `services/conversations.py`, `api/v1/conversations.py`, `messages.py` |
| Streaming, resumable runs | Done | `worker/executor.py`, `services/runs.py`, `api/v1/runs.py` (SSE) |
| Agent loop (model → tools → model) | Done | `worker/executor.py` `_Loop.run` |
| Tool registry | Done | `services/tools/registry.py` (`ToolSpec`: name, description, schema, risk, timeout via settings, sandbox/network needs, unavailability) |
| Tool permissions & approvals | Done, backend-enforced | `services/tools/policy.py`, `api/v1/tools.py`; approvals bound to argument hash |
| Sandbox execution | Done | `services/sandbox/*`, `tools/sandbox_client.py`, `infra/docker/Dockerfile.sandbox` |
| Network guard (SSRF) | Done | `services/tools/netguard.py` |
| MCP tools | Done (HTTP) | `services/tools/mcp.py`, `api/v1/automation.py` |
| Web search / fetch | Done | `tools/websearch.py`, `tools/builtin.py` |
| Files, attachments, previews | Done | `services/files/*` |
| Projects (instructions, files, sharing) | Done | `services/projects.py`, `api/v1/projects.py` |
| Memory: facts (proposed/active/rejected, scopes) | Done | `services/memory/store.py`, `api/v1/memory.py` |
| Memory: recall of other chats' excerpts | Done | `memory/jobs.py` (`memory.index`), `memory/retrieval.py` |
| Memory: extraction after replies | Done (user facts only) | `memory/jobs.py` (`memory.extract`) |
| Embeddings (optional, pgvector) | Done | `memory/embeddings.py`, `models/memory.py` |
| Background jobs | Done | `services/jobs.py`, `worker/loop.py` |
| Scheduled prompts | Done | `services/schedules.py`, `api/v1/automation.py` |
| Audit log | Done | `services/audit.py` (`audit_events`) |
| Multi-user isolation | Done, tested per route | `tests/test_isolation.py` |
| Model catalog (context size, vision, tools) | Done (auto-detected from `/props`) | `model_gateway/openai_compatible.py`, `models/generation.py` |
| Prompt construction | Monolithic string in `_prepare` | `worker/executor.py` |
| Memory categories, importance, confidence | **Missing** → Phase 1 | |
| Contradictions / corrections | **Missing** → Phase 1 | |
| Entities / relationships | **Missing** → Phase 1 | |
| Secret filtering of memory | **Prompt-only** → enforced in Phase 1 | |
| Re-embedding after a model change | **Missing** (mismatched rows silently keyword-only) → Phase 1 | |
| Skills (procedural memory) | **Missing** → Phase 2 | |
| Structured task state / plans / checkpoints | **Partial**: runs persist steps and tool calls and resume after a worker crash; no plan/objective state | Phase 3 |
| Prompt profiles (lite/standard/advanced) | **Missing** | Phase 4 |
| Model capability registry | **Partial**: `model_catalog` has context, vision, tool calling; no profile, structured output, parallel tools, limits | Phase 4 |
| Personal tasks, reminders, daily plan | **Missing** | Phase 5 |
| Calendar / email | **Missing** | Phase 6 |
| Automation engine (triggers/conditions/actions) | **Partial**: scheduled prompts only | Phase 6 |

## 4. Database assessment

- 30 tables, Alembic 0001..0009, naming convention fixed (`db/base.py`), JSONB on
  PostgreSQL / JSON on SQLite, UUID keys, `owner_id` with `ON DELETE CASCADE` everywhere.
- `memories` / `memory_chunks`: pgvector column without fixed dimension; the
  embedding model is stored per row (`embedding_model`), and retrieval only compares
  vectors of the same model and dimension. Good basis; kept.
- Gaps found: no full-text index (keyword candidates used `LIKE '%term%'`, a sequential
  scan); no category/importance/confidence; no history when a fact changes (editing
  overwrote it); no way to re-embed rows after the embedding model changes.
- `alembic check` runs in CI (`tests/test_migrations.py`): models and migrations must agree.

## 5. Gaps and decisions

| Finding | Decision |
| --- | --- |
| Memory is good but single-kind and flat | Extend the same tables (Phase 1), no new store. ADR C-02 |
| Ranking blended only semantic+keyword with fixed 0.7/0.3 | Configurable four-signal hybrid (ADR C-03); recall excerpts keep the old blend (thresholds calibrated on it) |
| Secrets could reach memory if the model ignored the prompt | Backend detector refuses and redacts (ADR C-04) |
| No skills | DB-backed registry with versions, standard SKILL.md format, trust policy (ADR C-06) |
| Prompt assembly is one function | Phase 4 will split it into modules; Phase 1/2 only add bounded blocks |
| Redis in compose, unused | Leave as is (not needed by any phase so far) |
| Hermes Agent's file-based MEMORY.md/USER.md | Not adopted: Celius already has a reviewed, scoped, multi-user DB store. Adopted: "always" vs. "on demand" split, frozen snapshot per run, procedural knowledge in skills (ADR C-01) |

## 6. What should stay untouched

Run queue and leases, SSE event persistence, sync/outbox, file pipeline, sandbox and egress
proxy, tool policy and approval binding, isolation tests. They are complete, tested and
not in the way of the roadmap.

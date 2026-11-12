# Testing Celius Remote

## Running the tests

```bash
# Agent (any OS with Python ≥ 3.10; CI: Ubuntu + Windows × 3.10 + 3.12)
cd services/remote-agent && uv sync && uv run pytest -q

# API unit/integration tests on SQLite (protocol at the WebSocket level, Claude Code
# projection, isolation, retention)
cd services/api && uv run pytest -q tests/test_remote_agents.py tests/test_remote_external.py

# Everything on PostgreSQL, including the end-to-end tests: the real API under uvicorn,
# the real worker loop and the real agent over a WebSocket on localhost
make dev-deps   # or any PostgreSQL 16 with pgvector
cd services/api && TEST_POSTGRES_URL=postgresql+asyncpg://celius:celius@localhost:5432/celius \
  TEST_DATABASE_URL=postgresql+asyncpg://celius:celius@localhost:5432/celius_test \
  uv run pytest -q tests/test_remote_e2e.py tests/test_migrations.py
```

The end-to-end tests are PostgreSQL-only: they run server, worker, agents and test against
one database at once, and SQLite's single-writer lock stalls that combination now and then
(measured: 20/22 runs passed on SQLite, 11/11 on PostgreSQL). CI runs them in the API job's
PostgreSQL step.

### Manual check with the real UI

```bash
pnpm build
DATABASE_URL=postgresql+asyncpg://… (cd services/api && uv run alembic upgrade head)
DATABASE_URL=postgresql+asyncpg://… infra/scripts/e2e-stack.sh      # API + worker + fake model
# Sign in as e2e / e2e-password-123 at http://127.0.0.1:18000
# Remote machines → Add a machine → copy the code, then on the same computer:
cd services/remote-agent
uv run celius-agent register --server http://127.0.0.1:18000 --code XXXX-XXXX-XXXX
#   edit the printed policy.json: "roots": [{"path": "/some/folder", "write": true}],
#   "allow_shell": true
uv run celius-agent run
# Authorise the machine; New chat → "Where this chat runs" → the machine → a folder.
# With the fake model, "[tool:python] …" runs Python there, "[tool:write] …" writes hello.txt.
```

## Status

Legend: **I** implemented · **U** unit tested · **IT** integration/end-to-end tested
(automated) · **M** manually verified (this environment: Linux container, Chromium) ·
**—** not done.

| Area | I | U | IT | M | Notes |
|---|---|---|---|---|---|
| Pairing, explicit authorisation, credentials (hashed), short-lived tokens | ✓ | ✓ | ✓ | ✓ | `test_remote_agents.py`; CLI register against a live stack |
| Credential rotation with grace; revocation closes the socket, ends sessions | ✓ | ✓ | ✓ | — | |
| Heartbeats, resources, online/offline/disconnected/needs attention | ✓ | ✓ | ✓ | ✓ | |
| Duplicate connection protection (newest wins, 4409) | ✓ | ✓ | — | — | |
| Durable commands, at-most-once execution (journal), re-delivery after reconnect | ✓ | ✓ | ✓ | — | agent restart → reported as interrupted, never re-run |
| Durable events: outbox, seq, contiguous acks, gap hold-back, duplicates dropped | ✓ | ✓ | ✓ | — | e2e: disconnect mid-task, events replayed, none lost/duplicated |
| Frame/event size limits (no poison-message loop) | ✓ | ✓ | — | — | |
| Native execution (files, shell, Python, git) with approvals | ✓ | ✓ | ✓ | ✓ | e2e + browser: approval card → command on the machine → result |
| Offline machine: command expires, never runs later | ✓ | — | ✓ | — | |
| Cancel/interrupt kills the process tree | ✓ | ✓ | ✓ | — | POSIX here; Windows (`taskkill /T`) in CI unit tests |
| Path confinement (roots, symlinks, traversal) | ✓ | ✓ | — | — | case-insensitive on Windows: CI |
| Two machines / sessions at once | ✓ | — | ✓ | — | PostgreSQL |
| Client closed while work continues | ✓ | — | ✓ | — | e2e runs complete with no client connected at all |
| Conversation integration (list badge, header bar, selector, outbox) | ✓ | ✓ | ✓ | ✓ | web unit test for the outbox step; Playwright walkthrough |
| Session monitor: terminal, activity, files + diffs + download, resources | ✓ | — | — | ✓ | desktop and phone widths |
| Approval from a phone-sized screen | ✓ | — | — | partly | the same approval card; viewed at 390 px, approved at desktop width |
| Claude Code adapter (Agent SDK, `can_use_tool`, resume, restart) | ✓ | ✓ | — | — | against an SDK stand-in and the real SDK's message classes; **not run against real Claude Code** (needs an account) |
| Claude Code projection into the conversation and approvals | ✓ | — | ✓ | — | protocol-level simulated machine |
| Retention | ✓ | — | ✓ | — | |
| Cross-user isolation of machines and sessions | ✓ | — | ✓ | — | `test_isolation.py` |
| Linux installer + systemd unit | ✓ | — | — | ✓ | ShellCheck clean; installed in a container (`--no-service`), dedicated user, permissions, authorise → connect; systemd itself not available here |
| Windows installer (scheduled task, DPAPI) | ✓ | — | — | — | parsed by PowerShell in CI; **not executed** (no Windows here) |
| Agent on Windows | ✓ | CI | — | — | unit tests on `windows-latest` (3.10, 3.12) |
| `celius-agent.exe` (PyInstaller) | ✓ | — | — | Linux build ✓ | Windows build in CI (`remote-agent-exe`) |
| Celius in Docker with remote agents | ✓ | — | — | — | the image builds and passes the release gate in CI; agents not connected to the container |
| Migration 0016 up/down/`alembic check` | ✓ | — | ✓ | ✓ | |

Known pre-existing failure, unrelated to this work: `tests/test_continuous_improvement.py`
(two memory benchmark tests) needs `services/api/devtools/data/memory_eval.json`, which was
never committed because `.gitignore` excludes `data/`. It fails on `main` too, so CI's API
job — and the browser e2e job that depends on it — have been red since commit `d6a1f63`.

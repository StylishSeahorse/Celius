# Phase 2 acceptance tests: model gateway and chat

Exit gate: **end-to-end chat and restart recovery tests pass.**

## Automated

| Suite | Command | Covers |
| --- | --- | --- |
| API unit and integration (SQLite) | `make test` | CRUD, idempotent submission, streaming, cancel, failures, context budget, lease recovery, claim exclusivity |
| Same suite on PostgreSQL | `make test-api-pg` | Real `SKIP LOCKED`, `RETURNING`, enums, migration round-trip |
| Web unit | `pnpm test` | SSE parser, reconnect with `after=<last id>` |
| Browser end-to-end | `make e2e` | Real API, worker process and fake llama-server, in desktop and Pixel 7 viewports |

`make e2e` starts `infra/scripts/e2e-stack.sh`. That script runs
`devtools/fake_llama_server.py`, the API serving `apps/web/dist`, and a separate
worker. The fake server echoes the prompt. `[slow]` in a message makes it stream
slowly and `[fail]` makes it return HTTP 400.

### Browser scenarios
1. Send a message: the reply streams in and renders markdown (bold, list, code
   block), and the follow-up includes history ("turn 2"). After a reload the conversation persists.
2. Reload the page mid-reply: the reply resumes from persisted events and completes.
3. Stop mid-reply: generation stops, the partial text is kept, and the next message works.
4. Model error: the error is shown and the conversation is not blocked.

## Manual: worker restart recovery

With the e2e stack running:

```sh
# send "[slow] recovery test" in the UI, then while it streams:
kill -9 "$(cat .e2e/worker.pid)"                   # the real worker process, not a wrapper
services/api/.venv/bin/python -m app.worker &      # new worker (same env as e2e-stack.sh)
```

Expected: within `RUN_LEASE_SECONDS` plus 0.5 s the new worker requeues the run. The UI
clears the partial text, streams the reply again from the start, and completes.
An automated version of this check is in `tests/test_chat.py`
(`test_run_from_dead_worker_is_requeued_and_completed`).

### Results, 2026-10-19 (local stack on PostgreSQL 16, fake model server)

| Check | Result |
| --- | --- |
| `pytest` on SQLite and PostgreSQL | All passed on both; the migration round-trip ran in the PostgreSQL pass |
| Web unit tests | 9 passed |
| Playwright, desktop + Pixel 7 | 8/8 passed |
| `kill -9` on the worker mid-reply, then a new worker started | Run requeued about 9 s later (6 s lease plus the recovery interval); the client got `run.progress restarted`, discarded 19 stale characters and received the complete reply; `attempts = 2` |
| API SIGTERM mid-reply, then a new API started | The old API exited after its 2 s grace period; the client reconnected with `after=472` and received the rest with no gaps or duplicates; the run was unaffected (`attempts = 1`) |

## Manual: against the real RTX 3060 server (M-05)

1. Point `MODEL_BASE_URL` at llama-server and check that `GET /api/v1/models` shows it healthy,
   with the right `context_tokens`.
2. Hold a 10-turn conversation and confirm the replies keep context.
3. Send a message longer than the context window: the run fails with
   `context_overflow`, and the server is never asked.
4. Stop a long reply and confirm in llama-server's log that generation stopped.
5. Record the model, quantisation, `n_ctx`, tokens/s and the safe `--parallel` value
   in `docs/operations/model-server.md`.

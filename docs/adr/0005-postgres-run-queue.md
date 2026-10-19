# ADR 0005: Claim generation runs from PostgreSQL, not Celery

Status: accepted · 2026-10-19

## Context
The specification proposes Redis + Celery for background work. Generation runs have
needs that a fire-and-forget task queue doesn't meet well:

- A run is long-lived and streams. Its progress is already persisted (`run_events`, ADR 0003).
- Cancellation must reach a run that is mid-stream, within about a second.
- After a crash, exactly one worker must resume the run, and a stale worker must not
  overwrite the result.
- Concurrency must match llama-server's slot count, not a generic worker pool size.

## Decision
`runs` is the queue. Workers (`python -m app.worker`):

1. **Claim** with `SELECT … FOR UPDATE SKIP LOCKED`, then a conditional
   `UPDATE … WHERE status = 'queued'`. The conditional update is what guarantees
   a run is claimed at most once.
2. Hold a **lease** (`lease_expires_at`, `worker_id`). It is renewed with every delta
   flush and on a ≤1 s heartbeat, because no tokens arrive during long prompt
   processing. Each renewal is conditional on `worker_id`, so a worker that lost its
   lease writes nothing.
3. Read `cancel_requested` on every renewal. On cancel, closing the HTTP stream makes
   llama-server stop generating.
4. **Recover** runs whose lease expired. Each is requeued up to `RUN_MAX_ATTEMPTS`
   times, with a `run.progress {stage: "restarted"}` event so clients discard partial
   text. After that it fails with `worker_lost`.

The worker polls for new runs every 0.5 s and SSE endpoints poll for events every 0.25 s.

Redis and Celery stay in the stack for the later, short, fire-and-forget jobs
(extraction, exports, scheduled tasks).

## Consequences
- No extra moving parts in the critical path: if PostgreSQL is up, chat works.
- Polling costs a few cheap indexed queries per second. That is negligible for one user.
  If it ever matters, `LISTEN/NOTIFY` can wake workers and streams without changing
  the data model.
- Concurrency is set with `MODEL_PARALLEL_REQUESTS`. Running more worker replicas is
  safe but doesn't add GPU capacity.

# Addendum: the API serves the web app

The built React app is copied into the API image and served by the API
(`STATIC_ROOT`), with a fallback to `index.html` for client-side routes.
One origin keeps the session cookie `SameSite=Strict` and lets SSE work without CORS.
The reverse proxy needs only one upstream.

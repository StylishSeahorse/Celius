# ADR 0003: SSE streaming backed by persisted run events

Status: accepted · 2026-10-19

## Decision
Every generation is a durable `runs` row. Its progress is written to `run_events`,
whose auto-increment `id` is the SSE event id. `GET /runs/{id}/events` replays
from `Last-Event-ID` and then tails new events. Generation happens in a worker,
not in the HTTP request.

## Rationale
Mobile connections drop often. A stream bound to the request would lose output on
every reconnect. Persisting events lets any device resume, and lets a restarted
worker find unfinished runs.

## Consequences
- `message.delta` events can be coalesced (for example every 50–100 ms) to limit write volume.
- Old delta events can be pruned once the run completes, because the final message is
  stored in `message_parts`. Clients that reconnect after pruning fetch the message state.
- The reverse proxy must disable buffering on the events endpoint.

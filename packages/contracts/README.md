# Contracts

Language-neutral definitions shared by the API and clients. The Pydantic models in
`services/api/app/schemas/` must stay in sync with these files; the generated
TypeScript client (phase 3) is produced from the API's OpenAPI document.

| File | Pydantic model |
| --- | --- |
| `schemas/run-event.schema.json` | `app.schemas.events.RunEventEnvelope` |

## Run event `data` payloads (phase 2)

| Type | `data` |
| --- | --- |
| `run.started` | `message_id`, `model_id`, `attempt`, `estimated_prompt_tokens`, `dropped_messages` |
| `message.delta` | `message_id`, plus `text` and/or `reasoning` (appended, coalesced to roughly 100 ms) |
| `run.progress` | `stage: "restarted"`, `message_id`, `attempt`. **Discard any partial output**: the worker restarted generation |
| `run.completed` | `message_id`, `usage`, `finish_reason` (`"length"` means the output was truncated) |
| `run.failed` | `message_id`, `code`, `message`, `retryable` |
| `run.cancelled` | `message_id` |

SSE wire format: `id: <event_id>` / `event: <type>` / `data: <envelope JSON>`. The server
closes the stream after the terminal event. Resume with the `Last-Event-ID` header or `?after=<id>`.

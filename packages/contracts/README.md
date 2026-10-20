# Contracts

Language-neutral definitions shared by the API and clients. The Pydantic models in
`services/api/app/schemas/` must stay in sync with these files; the generated
TypeScript client (phase 3) is produced from the API's OpenAPI document.

| File | Pydantic model |
| --- | --- |
| `schemas/run-event.schema.json` | `app.schemas.events.RunEventEnvelope` |

## Run event `data` payloads

| Type | `data` |
| --- | --- |
| `run.started` | `message_id`, `model_id`, `attempt`, `estimated_prompt_tokens`, `dropped_messages` |
| `message.delta` | `message_id`, plus `text` and/or `reasoning` (appended, coalesced to roughly 100 ms) |
| `message.delta` (with tools) | also `step` (0, 1, …): text of a later step starts after that step's tool calls |
| `run.progress` | `stage: "restarted"`, `message_id`, `attempt`. **Discard any partial output**: the worker restarted generation |
| `run.progress` | `stage: "awaiting_approval"`: paused until every pending tool call is decided; `stage: "resumed"`, `step`: continuing |
| `tool_call.proposed` | `message_id`, `tool_call` (ToolCallOut). The step's text is now saved on the message |
| `tool_call.awaiting_approval` | `message_id`, `tool_call`. Approve with `POST /tool-calls/{id}/approve {args_hash}` |
| `tool_call.decided` | `message_id`, `tool_call` (approved, denied or expired) |
| `tool_call.started` / `tool_call.completed` | `message_id`, `tool_call` (with `result` when completed) |
| `run.completed` | `message_id`, `usage`, `finish_reason` (`"length"` means the output was truncated) |
| `run.failed` | `message_id`, `code`, `message`, `retryable` |
| `run.cancelled` | `message_id` |

SSE wire format: `id: <event_id>` / `event: <type>` / `data: <envelope JSON>`. The server
closes the stream after the terminal event. Resume with the `Last-Event-ID` header or `?after=<id>`.

`message.delta` events of a run are deleted `RUN_DELTA_RETENTION_MINUTES` (10) after it
finishes; every other event is kept. A client that opens a finished run's stream later
gets its other events and the terminal one, and should load the reply from the message
(the web app reloads the conversation on any terminal event).

## Uploads

`POST /files/uploads` → `PUT {upload_url}` with the bytes → `POST /files/uploads/{id}/complete`.
The PUT body is either the whole file (re-sending replaces it) or, with
`Content-Range: bytes <first>-<last>/<total>`, one chunk. A chunk may start at most at the
bytes already received (`409 upload_offset_mismatch` carries `received_bytes` otherwise);
overlapping bytes replace what was there. `GET /files/uploads/{id}` returns
`received_bytes`, to resume after a dropped connection. Each chunk extends the upload's
expiry. The web app uses 4 MB chunks for files above 4 MB.

## Sync change feed

`entity_type` is one of `conversation`, `message`, `project`, `file`, `memory`. A `memory`
change means the memory list or review queue changed (reload it).

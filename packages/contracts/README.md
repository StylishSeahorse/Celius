# Contracts

Language-neutral definitions shared by the API and clients. The Pydantic models in
`services/api/app/schemas/` must stay in sync with these files; the generated
TypeScript client (phase 3) is produced from the API's OpenAPI document.

| File | Pydantic model |
| --- | --- |
| `schemas/run-event.schema.json` | `app.schemas.events.RunEventEnvelope` |

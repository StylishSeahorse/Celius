# ADR 0001: Separate the application, inference and tool execution

Status: accepted · 2026-10-19

## Context
Inference runs on an RTX 3060 (12 GB VRAM) on a separate machine. The application
needs durable state, file storage, background work and permission enforcement, and
will eventually execute untrusted code.

## Decision
Three zones with separate trust levels:

- **Client zone**: Tauri, PWA and browser. They never hold DB, model-server or tool credentials.
- **Application zone**: the API, PostgreSQL, Redis and file storage. Only the API serves users.
- **Execution zone**: workers and sandboxes. Each task gets only the files, network
  and credentials it is explicitly granted.

The llama.cpp server is not in the application Compose stack. The API reaches it
over the private network through `MODEL_BASE_URL`.

## Consequences
- The app stays usable (history, files, search) when inference is offline. Readiness does not depend on the model server.
- The model is never trusted to enforce permissions. The API validates every tool call.
- The tool runner never gets the Docker socket, host mounts or privileged mode.

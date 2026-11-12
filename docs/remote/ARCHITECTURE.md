# Celius Remote — distributed agent execution

Status: accepted (ADR C-16) and implemented through Phase 6. See [PROGRESS.md](PROGRESS.md)
for what is done and outstanding, [TESTING.md](TESTING.md) for how each part is tested.
The audit this builds on is [AUDIT.md](AUDIT.md). Also: [INSTALL.md](INSTALL.md),
[API.md](API.md), [OPERATIONS.md](OPERATIONS.md), [TROUBLESHOOTING.md](TROUBLESHOOTING.md),
[ROADMAP.md](ROADMAP.md).

## 1. Shape of the system

```
 Clients (web / PWA on Android / Tauri desktop) — unchanged transport: REST + SSE
        │  conversations, messages, approvals (existing routes)
        │  /api/v1/remote/* (machines, sessions, session event stream)
        ▼
 ┌──────────────── Celius Central (existing Docker Compose on Proxmox) ───────────────┐
 │ api ──────────── WebSocket /api/v1/remote/agent/connect ◀───────────── outbound ───┼──── Celius Remote Agent
 │  │  ingest: remote_session_events (dedup by session+seq)                           │     (Windows / Linux service)
 │  │  projection: external-agent events → messages, tool_calls, run_events           │      ├─ connection + outbox
 │  │  delivery: remote_commands → agent                                              │      ├─ execution runtime
 │  ▼                                                                                  │      │   native tools (files, shell)
 │ postgres  ◀── worker: claims runs (execution = local | remote)                      │      ├─ adapters: claude_code
 │                 remote runs: same model loop; tools become remote_commands          │      └─ resource monitor
 │                 and wait for their results                                          │
 │ sandbox (unchanged, used for execution = local)                                     │
 └──────────────────────────────────────────────────────────────────────────────────────┘
```

Three components, as asked:

- **Celius Central** stays the source of truth: users, conversations, messages, runs,
  tool calls/approvals, agent registrations, session state, events, audit.
- **Celius Remote Agent** (`services/remote-agent`, Python ≥ 3.10, one runtime dependency:
  `websockets`) runs on each machine, connects *out* to Central, executes what it is sent
  within its local policy, and streams durable events back. No inbound port, no SSH.
- **Clients**: the existing web app (which is also the desktop and Android client) gains a
  Remote Machines panel, an execution-target selector and a session monitor. Remote
  sessions are ordinary conversations.

### Why Python for the agent
The backend and sandbox are Python; the desktop shell is Rust but holds no business logic.
A Python agent shares conventions, tooling (uv, ruff, pytest) and can use the official
Claude Agent SDK (Python) for the Claude Code adapter. It needs one dependency
(`websockets`, pure Python); `psutil` and `claude-agent-sdk` are optional extras. Windows
gets a PyInstaller one-file build; Linux an install script + systemd unit.

## 2. Execution modes

| | Mode 1 — native (`mode=native`) | Mode 2 — external (`mode=external`, `adapter=claude_code`) |
|---|---|---|
| Who runs the model loop | Central worker (existing executor: model config, prompts, memory, skills, tool policy, approvals, leases) | The external agent on the remote machine (Claude Code via the Agent SDK) |
| What the remote machine does | Executes tool calls: `list_files`, `read_file`, `write_file`, `run_shell`, `run_python`, `git` in the session's working directory | Runs the whole session; Celius relays user turns, permission decisions, interrupts |
| How approvals work | Unchanged: policy decides, `pending_approval` pauses the run, user decides in the chat | Claude Code asks via `can_use_tool` → agent emits `permission.request` → Central creates a `tool_calls` row `pending_approval` in the chat → user decides with the **same** approve/deny routes → decision sent back as a command |
| Run claimed by worker | Yes (`runs.execution = remote`) | No (`runs.execution = external`); the run is driven by ingested events |
| Conversation features | All (branches, edit, regenerate) | New turns only; edit/regenerate are refused (Claude Code history cannot fork) |

Both modes share: `remote_sessions`, `remote_commands`, `remote_session_events`, the agent
connection, the monitor UI, the approval UI.

## 3. Data model (migration 0016, reversible)

Reused unchanged: `conversations`, `messages`, `message_parts`, `runs`, `run_events`,
`tool_calls` (approvals), `audit_events`, `sync_events`, push.

New tables:

| Table | Purpose | Key columns / constraints |
|---|---|---|
| `remote_agents` | A registered machine | `owner_id` → users (cascade); `name`, `hostname`, `os`, `os_version`, `arch`, `agent_version`, `protocol_version`; `status` `pending\|active\|revoked`; `capabilities` (reported), `local_policy` (reported by the agent: roots, shell, write, adapters), `config` (server-side policy); `resources` (last snapshot); connection: `connection_id`, `connected_at`, `last_seen_at`, `disconnected_at`, `disconnect_reason`; `authorized_at`, `revoked_at`. Index `(owner_id)`. |
| `remote_agent_credentials` | Per-machine secrets (hash only) | `agent_id` (cascade), `secret_hash` unique, `expires_at` (grace after rotation), `revoked_at`, `last_used_at`. |
| `remote_enrollments` | One-time pairing codes | `owner_id`, `code_hash` unique, `expires_at` (15 min), `used_at`, `agent_id`. |
| `remote_sessions` | A conversation's execution on a machine | `owner_id`, `conversation_id` (cascade), `agent_id` (set null), `mode`, `adapter`, `working_directory`, `status` `opening\|idle\|running\|awaiting_approval\|failed\|terminated`, `status_detail`, `permissions` (session-level), `external_session_id`, `current_run_id`, `last_seq`, `opened_at`, `ended_at`. Index `(conversation_id)`, `(agent_id, status)`. |
| `remote_session_events` | Durable, ordered agent → Central event log | `session_id` (cascade), `agent_id`, `run_id`, `seq`, `type`, `data`, `occurred_at`, `created_at`. **Unique `(session_id, seq)`** — duplicates are dropped at insert. |
| `remote_commands` | Durable Central → agent instructions | `agent_id` (cascade), `session_id`, `run_id`, `tool_call_id`, `kind`, `payload`, `status` `pending\|delivered\|running\|completed\|failed\|cancelled\|expired`, `result`, `expires_at`, `delivered_at`, `completed_at`. Index `(agent_id, status)`. |

Changed: `runs` gains `execution` (`local` default, `remote`, `external`; server default
`'local'`, so existing rows and code are unaffected) and `remote_session_id` (set null).

Retention: `terminal.output` session events older than `REMOTE_OUTPUT_RETENTION_DAYS`
(default 30) are pruned by the worker's maintenance pass; other session events stay as
the session's history and are deleted with the conversation (cascade). Finished commands
older than 30 days are pruned. Artifacts are not copied centrally by default (see §8).

## 4. Protocol

Transport: HTTPS for enrollment and token exchange, one WebSocket per agent for everything
else, both through the existing public HTTPS endpoint (`wss://…/api/v1/remote/agent/connect`).
Frames are JSON objects with a `type`. `PROTOCOL_VERSION = 1`.

### 4.1 Identity
1. A user creates a pairing code in Celius (valid 15 min, single use, only its hash stored).
2. On the machine: `celius-agent register --server https://celius.example --code XXXX-XXXX`.
   The agent POSTs `/api/v1/remote/enroll` with the code and machine facts and receives
   `agent_id` + a 256-bit **credential** (shown once). The credential is stored with
   DPAPI on Windows, mode 0600 on Linux.
3. The machine appears as **Awaiting approval**. The user must explicitly **Authorise** it
   (explicit device authorisation); until then token requests return 403.
4. To connect, the agent exchanges its credential at `POST /api/v1/remote/token` for an
   **access token** valid 15 minutes (HMAC-SHA256 over `agent_id|expiry|nonce`, key derived
   from `APP_SECRET_KEY`; not stored). The WebSocket requires it in `Authorization: Bearer`.
5. **Rotation**: `POST /api/v1/remote/credentials/rotate` (agent-authenticated) issues a new
   credential; the old one stays valid for a 1-hour grace and is revoked as soon as the new
   one is used. **Revocation** (user action) revokes all credentials, closes the live
   connection within one poll interval, and cancels pending commands.

### 4.2 Connection
- Agent → `hello {protocol, agent_version, capabilities, local_policy, sessions: {id: last_seq_acked}, commands: {id: state}}`.
- Central → `welcome {agent_id, heartbeat_seconds, sessions: {id: {last_seq, status}}}`.
- Agent → `heartbeat {resources}` every `heartbeat_seconds` (default 15). Central updates
  `last_seen_at` and the resource snapshot. A machine is *online* when it has a current
  `connection_id` and was seen within 3 heartbeats; otherwise *offline*, and *disconnected
  unexpectedly* when the last connection ended without a clean `bye`.
- **Duplicate connections**: each accepted socket gets a fresh `connection_id` written to
  the agent row; a socket that sees a different id is closed with code 4409 ("superseded").
  Newest wins, which also recovers half-open TCP connections after a network drop.
- **Reconnect**: exponential backoff with full jitter, 1 s → 60 s cap, reset after a
  connection that lived 60 s. Token refreshed before expiry and on 4401.

### 4.3 Commands (Central → agent), at-least-once, executed at most once
`command {id, kind, session_id, payload}`; kinds: `session.open`, `session.close`,
`tool.execute`, `session.message` (external), `permission.decision` (external),
`interrupt`, `cancel` (a command), `file.fetch`, `agent.disconnect`.
- Central marks a command `delivered` when sent and re-sends `pending`/`delivered`
  commands on every new connection.
- The agent records each command id durably **before** acting on it; a re-delivered id is
  never executed again — the agent re-reports its state or result instead.
- A `tool.execute` that is still `pending` when its waiter gives up is atomically marked
  `expired` (it will never be sent); one already delivered gets a `cancel`.

### 4.4 Events (agent → Central), durable and ordered
`events {session_id, events: [{seq, type, run_id?, occurred_at, data}]}`.
- `seq` is per session, assigned by the agent, strictly increasing. Each event is appended
  to the agent's on-disk **outbox** before it is sent, so nothing is lost when the network
  or the agent restarts.
- Central inserts in `seq` order with `ON CONFLICT DO NOTHING` semantics (unique
  `(session_id, seq)`), applies projections only for newly inserted rows (no duplicate
  processing), and replies `ack {session_id, seq}` with the highest **contiguous** seq
  stored. The agent drops acked events from its outbox and resends the rest after
  reconnecting. A gap (seq jump) is held back: Central acks only the contiguous prefix,
  and the agent resends.
- `welcome` tells the agent Central's `last_seq` per session, so a reinstalled agent
  continues numbering after it.

Frames stay below uvicorn's 16 MB WebSocket limit: agents send at most ~2 MB per events
frame and trim any single event above 8 MB when writing it to the outbox, so no event can
block the stream.

Canonical event types: `session.opened`, `session.failed`, `session.closed`, `session.started` (external agent's own session id),
`command.started`, `command.completed` (`{command_id, status, result}`), `terminal.output`
(`{command_id, stream, text}`), `file.changed` (`{path, action, size, sha256, diff?}`),
`turn.started`, `assistant.text`, `assistant.reasoning`, `tool.started`, `tool.completed`,
`permission.request`, `turn.completed`, `warning`, `error`, `resources`.

## 5. Native execution flow (mode 1)

1. User creates a conversation and picks a machine + working directory (selector).
   `POST /api/v1/remote/sessions` creates `remote_sessions` (`opening`) and a
   `session.open` command. The agent validates the directory against its policy and
   answers `session.opened` (→ `idle`) or `session.failed`.
2. User sends a message (existing route). `chat._enqueue_reply` sees the conversation's
   open session and creates the run with `execution=remote`, `remote_session_id`.
3. The worker claims the run as always. `_prepare` builds the toolset with remote specs
   (same names and argument models as the built-in tools; descriptions say where they
   run) intersected with the machine and session permissions, and adds a prompt section
   naming the machine, OS, shell and directory.
4. When a remote tool runs, its handler inserts a `tool.execute` command and waits for
   `command.completed` (polling the command row, while `_supervised` keeps renewing the
   run lease and watching for cancellation). Output streams meanwhile as `terminal.output`.
5. Results return to the model as with the sandbox; the reply streams over the existing
   run SSE. The monitor shows terminal/file events from the session SSE.
6. Cancellation → `cancel` command, the agent kills the process tree. A worker crash →
   the call is reported `interrupted` (existing rule) and a `cancel` is sent for it.

## 6. External agent flow (mode 2, Claude Code)

1. Session created with `mode=external, adapter=claude_code`; the agent checks the SDK and
   CLI are present (advertised in capabilities) before accepting.
2. Message → run with `execution=external` plus a `session.message` command, in one
   transaction. The worker never claims it.
3. The adapter (`ClaudeSDKClient`, `can_use_tool`, `resume=<external session id>`) streams
   SDK messages; the translator emits canonical events.
4. Central projects them into the conversation: `assistant.text` → text parts +
   `message.delta`; `tool.started/completed` → `tool_calls` rows (`decision_source=external`)
   + run events; `permission.request` → the matching `tool_calls` row becomes
   `pending_approval`, the run `awaiting_approval`, a push is queued;
   `turn.completed` → run `completed`/`failed`, message complete.
5. Approve/deny via existing routes → `permission.decision` command; the run goes back to
   `running` (not `queued`). Expiry denies.
6. Stop → `interrupt` command, run `cancelled`.

## 7. API (all under `/api/v1`, existing conventions: bearer/cookie auth, `AppError`)

User-facing (`CurrentAuth`):

| Method | Path | |
|---|---|---|
| POST | `/remote/enrollments` | New pairing code (shown once) |
| GET | `/remote/agents` | Machines with derived state (online/offline/busy/idle/needs attention/disconnected unexpectedly) |
| GET/PATCH/DELETE | `/remote/agents/{id}` | Details; rename / server-side policy; delete |
| POST | `/remote/agents/{id}/authorize` · `/revoke` · `/disconnect` | Explicit authorisation, revocation, ask the agent to reconnect |
| GET | `/remote/agents/{id}/sessions` | Running sessions and history |
| POST | `/remote/sessions` | Open a session on a machine (optionally creating the conversation) |
| GET | `/remote/sessions`, `/remote/sessions/{id}` | List / details (with agent state) |
| GET | `/remote/sessions/{id}/events` | Event replay (JSON, `after`, `limit`, `types`) |
| GET | `/remote/sessions/{id}/stream` | SSE of session events, resumable with `Last-Event-ID` = seq |
| POST | `/remote/sessions/{id}/interrupt` · `/terminate` | Controls |
| GET | `/remote/sessions/{id}/file?path=` | Download a file from the working directory (via `file.fetch`) |
| GET | `/conversations/{id}` | Gains `remote_session` (summary) — additive |

Messages, approvals, cancellation: the **existing** routes.

Agent-facing (credential or access token, never user sessions):
`POST /remote/enroll`, `POST /remote/token`, `POST /remote/credentials/rotate`,
`WS /remote/agent/connect`.

## 8. Security

| Requirement | Implementation |
|---|---|
| Encrypted transport | Agents refuse non-`https`/`wss` servers (except `localhost`) unless registered with `--allow-insecure-http`; `--ca-file` for a private CA. |
| Per-device identity, rotation, revocation, short-lived tokens | §4.1. Credentials hashed (SHA-256 of 256-bit random); tokens HMAC-signed, 15 min. |
| Explicit authorisation | New machines are `pending` until a user authorises them. |
| Secure credential storage | Windows: DPAPI (CurrentUser) via ctypes; Linux: file 0600 in a 0700 directory. |
| Least privilege / no unrestricted shell by default | Two independent gates, both required: the agent's **local policy** file on the machine (`allow_shell: false`, `allow_write: true`, `roots: []`, `adapters: []` by default — nothing runs until the machine's owner lists folders) and Central's per-machine **config**. The effective permission is the intersection. |
| Approvals | Remote write/execute tools are `always_ask` unless the machine config sets `approval: "profile"`; read tools follow the profile. Claude Code permission prompts become Celius approvals. |
| Path traversal | Agent resolves every path with `realpath` and requires it inside the session directory (which must be inside an allowed root); case-insensitive on Windows; symlinks escaping are refused. |
| Command controls / resource limits | Per-call timeout, output cap, concurrency cap; process trees killed on timeout/cancel (process group on POSIX, `taskkill /T` on Windows). Optional memory limit on Linux (`RLIMIT_AS`). |
| Separate security boundary | Agents never see the database or internal APIs; they can only reach the four agent endpoints, and only for their own identity. Every frame is validated; session ids are checked to belong to that agent. |
| Audit | Enrollment, authorisation, rename/config change, revoke, rotation, session open/terminate, connection open/close are written to `audit_events`. |
| Network policy | Recorded per machine and enforced where Celius controls it (web tools are not offered remotely). Shell commands on a real machine can reach the network; OS-level egress control is outside the agent's scope and documented as such. |

## 9. Risks

| Risk | Mitigation |
|---|---|
| Duplicate execution after reconnect/failover | Durable command ids + agent-side "seen" store; expire-or-cancel on timeout; no automatic machine failover. |
| Event loss | Agent outbox on disk; Central acks only contiguous stored prefixes; replay on reconnect. |
| Approval silently lost | Approvals are `tool_calls` rows with expiry and push; external permission requests block the Claude Code tool call until a decision (or expiry → deny) arrives. |
| Claude Code SDK/CLI changes | Translator keyed on documented message types; unknown types become `warning` events; adapter disabled unless SDK import succeeds. |
| API restart while agents connected | Agents reconnect with backoff; commands/events are durable. |
| Polling latency (250 ms) | Matches the existing SSE/run design; acceptable for tool calls that take seconds. |
| Windows untestable here | Windows-specific code isolated and unit-tested in CI on `windows-latest`. |

## 10. Implementation sequence

Phase 0 audit (this) → 1 registry/auth/connection + agent prototype → 2 native execution
and durable events → 3 conversation integration → 4 Claude Code adapter → 5 UI →
6 hardening, deployment, docs. Phase 7 (multi-agent routing, delegation, automatic
selection) is designed for — `remote_sessions` per conversation, capabilities on agents,
`selection` field reserved on session creation — but not built. See
[ROADMAP.md](ROADMAP.md).

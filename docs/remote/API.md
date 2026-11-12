# Celius Remote API

All paths are under `/api/v1/remote`, follow the existing API conventions (bearer token or
session cookie for users; errors as `{"error": {"code", "message", …}}`) and appear in the
OpenAPI document (`/api/docs` outside production) and the generated client
(`packages/api-client`). `REMOTE_AGENTS_ENABLED=false` makes every one answer 404
`remote_disabled`.

Messages, approvals and cancellation of remote conversations use the **existing** routes:
`POST /conversations/{id}/messages`, `POST /tool-calls/{id}/approve|deny`,
`POST /runs/{id}/cancel`, `GET /runs/{id}/events` (SSE). `GET /conversations` and
`GET /conversations/{id}` include `remote` (where the conversation runs) when it has a session.

## For users

| Method & path | Body / query | Result |
|---|---|---|
| `POST /enrollments` | `{name?}` | `{code, expires_at, command}`: one-time pairing code |
| `GET /agents` | | `{items: [Machine]}` |
| `GET /agents/{id}` | | `Machine` |
| `PATCH /agents/{id}` | `{name?, config?}` | `Machine` (`config`: `allow_shell`, `allow_write`, `approval` = `always`\|`profile`, `adapters`, `max_sessions`, `network`) |
| `POST /agents/{id}/authorize` | | `Machine`: explicit authorisation of a pending machine |
| `POST /agents/{id}/revoke` | | `Machine`: credentials revoked, socket closed, sessions ended, queued commands cancelled |
| `POST /agents/{id}/disconnect` | | 202: the agent closes its connection and reconnects |
| `DELETE /agents/{id}` | | 204: revoke and forget (conversations stay) |
| `GET /agents/{id}/sessions` | `limit` | `{items: [Session]}`, open ones first |
| `POST /sessions` | `{agent_id, conversation_id?, title?, project_id?, adapter: celius\|claude_code, working_directory, permissions?}` | `Session` (201). Creates the conversation when `conversation_id` is absent. Claude Code needs a new conversation. |
| `GET /sessions` | `conversation_id?`, `agent_id?`, `active?`, `limit` | `{items: [Session]}` |
| `GET /sessions/{id}` | | `Session` |
| `POST /sessions/{id}/interrupt` | | Stops the current reply on the machine; the session stays open |
| `POST /sessions/{id}/terminate` | | Ends the session on the machine (the conversation stays) |
| `GET /sessions/{id}/events` | `after` (seq), `limit` ≤ 1000, `types` (comma list) | `{items: [Event], last_seq, more}` |
| `GET /sessions/{id}/stream` | `after` or `Last-Event-ID` | SSE: `id` = seq, `event` = type, `data` = Event; `session.state` keep-alives |
| `GET /sessions/{id}/file` | `path` | The file's bytes (≤ `REMOTE_FILE_FETCH_MAX_BYTES`), read by the agent with its path checks; waits ≤ 30 s |

`Machine.state` is one of `awaiting_authorization`, `revoked`, `needs_attention` (a session
waits for approval), `busy`, `idle`, `disconnected` (connection lost without a goodbye),
`offline`. `Machine.permissions` is the effective policy: the machine's `local_policy` ∩
Central's `config`.

`Session.status`: `opening`, `idle`, `running`, `awaiting_approval`, `failed`,
`terminated` (live states come from the session's current run).

Common error codes: `agent_not_active`, `adapter_not_allowed`, `adapter_unavailable`,
`invalid_directory`, `session_exists`, `conversation_not_empty`, `too_many_sessions`,
`session_ended`, `remote_session_ended` (sending to a conversation whose session ended:
open one again; Celius never moves it back to the sandbox), `not_supported_for_external_agent`
(edit/regenerate on a Claude Code session), `file_unavailable`, `machine_timeout`.

## For agents

Agents never use user sessions, and user tokens are refused here.

| Method & path | Auth | Body | Result |
|---|---|---|---|
| `POST /enroll` | pairing code | `{code, hostname, os, os_version, arch, agent_version, protocol_version}` | `{agent_id, credential, status: "pending", name}` |
| `POST /token` | credential | `{credential}` | `{access_token, expires_at, agent_id}`; 403 `agent_not_authorized` while pending; 401 when revoked/invalid |
| `POST /credentials/rotate` | `Bearer` access token | | `{credential}`; the old one works for `REMOTE_CREDENTIAL_GRACE_MINUTES` or until the new one is used |
| `WS /agent/connect` | `Authorization: Bearer` access token | | The agent protocol below |

## Agent protocol (WebSocket, JSON text frames, `protocol: 1`)

```
agent → hello      {protocol, agent_version, hostname, os, os_version, arch,
                    capabilities, local_policy, resources, sessions: {id: acked_seq}}
      ← welcome    {agent_id, connection_id, heartbeat_seconds, server_time,
                    sessions: {id: {last_seq, status, mode, adapter, working_directory,
                                    permissions, external_session_id, next_seq}}}
agent → heartbeat  {resources}                      every heartbeat_seconds
      ← command    {id, kind, session_id, run_id, payload}
agent → events     {session_id, events: [{seq, type, run_id?, occurred_at, data}]}
      ← ack        {session_id, seq, error?}         highest contiguous seq stored
agent → ping / ← pong;   agent → bye (clean shutdown)
close codes: 4401 not authorised / revoked, 4409 replaced by a newer connection,
             4400 protocol error
```

Command kinds and payloads:

| Kind | Payload | Agent answers with |
|---|---|---|
| `session.open` | `{mode, adapter, working_directory, permissions, external_session_id, next_seq}` | `session.opened {working_directory, writable, shell}` or `session.failed {reason}` |
| `session.close` | `{reason}` | `session.closed` |
| `tool.execute` | `{tool, arguments, timeout_seconds}` (tools: `list_files`, `read_file`, `write_file`, `run_shell`, `run_python`, `git`) | `command.started`, `terminal.output*`, `file.changed*`, `command.completed {command_id, status, result: {content, data, is_error}}` |
| `file.fetch` | `{path, max_bytes}` | `command.completed` with `result.data = {name, size, sha256, content_b64}` |
| `cancel` | `{command_id}` | the command completes as cancelled |
| `interrupt` | | running commands cancelled; external agent interrupted |
| `session.message` | `{text}` (external) | `turn.started`, `assistant.text`, `assistant.reasoning`, `tool.started`, `tool.completed`, `permission.request`, `file.changed`, `turn.completed` |
| `permission.decision` | `{request_id, behavior: allow\|deny, message}` | the external agent continues |
| `agent.disconnect` | `{reconnect: true}` | the agent closes and reconnects |

Delivery: commands at least once, executed at most once (the agent journals ids).
Events: written to the agent's outbox before sending; stored by Central once per
`(session_id, seq)`; acked as the highest contiguous seq; resent after reconnecting.

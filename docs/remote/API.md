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
| `GET /devices` | | `{items: [DeviceRequest], login_command}`: devices that ran `celius login --user <you>` and wait for you (`user_code`, `name`, `hostname`, `os`, `os_version`, `ip_address`, `created_at`, `expires_at`) |
| `POST /devices/accept` | `{user_code, name?}` | `Machine` (201): the device becomes one of your machines and signs in on its next poll |
| `POST /devices/refuse` | `{user_code}` | 204: the device is told and stops waiting |
| `GET /agents` | | `{items: [Machine]}` |
| `GET /agents/{id}` | | `Machine` |
| `PATCH /agents/{id}` | `{name?, config?}` | `Machine` (`config`: `allow_shell`, `allow_write`, `approval` = `always`\|`profile`, `max_sessions`, `network`) |
| `PUT /agents/{id}/policy` | `{roots?: [{path, write}], allow_shell?, allow_write?}` | `Machine` with the new `local_policy`, once the machine has checked and saved it (waits ≤ 30 s). 400 `policy_refused` with the machine's reason; 409 `policy_locked` (`allow_remote_edit: false`), `agent_outdated` (agent before 0.3), `machine_offline`; 504 `machine_timeout` |
| `POST /agents/{id}/revoke` | | `Machine`: credentials revoked, socket closed, sessions ended, queued commands cancelled |
| `POST /agents/{id}/disconnect` | | 202: the agent closes its connection and reconnects |
| `DELETE /agents/{id}` | | 204: revoke and forget (conversations stay) |
| `GET /agents/{id}/sessions` | `limit` | `{items: [Session]}`, open ones first |
| `POST /sessions` | `{agent_id, conversation_id?, title?, project_id?, working_directory, permissions?}` | `Session` (201). Creates the conversation when `conversation_id` is absent. |
| `GET /sessions` | `conversation_id?`, `agent_id?`, `active?`, `limit` | `{items: [Session]}` |
| `GET /sessions/{id}` | | `Session` |
| `POST /sessions/{id}/interrupt` | | Stops the current reply on the machine; the session stays open |
| `POST /sessions/{id}/terminate` | | Ends the session on the machine (the conversation stays) |
| `GET /sessions/{id}/events` | `after` (seq), `limit` ≤ 1000, `types` (comma list) | `{items: [Event], last_seq, more}` |
| `GET /sessions/{id}/stream` | `after` or `Last-Event-ID` | SSE: `id` = seq, `event` = type, `data` = Event; `session.state` keep-alives |
| `GET /sessions/{id}/file` | `path` | The file's bytes (≤ `REMOTE_FILE_FETCH_MAX_BYTES`), read by the agent with its path checks; waits ≤ 30 s |

`Machine.state` is one of `revoked`, `needs_attention` (a session waits for approval),
`busy`, `idle`, `disconnected` (connection lost without a goodbye), `offline`.
`Machine.permissions` is the effective policy: the machine's `local_policy` ∩ Central's
`config`.

`Session.origin`: `portal` (started in Celius) or `device` (started with `celius` on the
machine). `Session.status`: `opening`, `idle`, `running`, `awaiting_approval`, `failed`,
`terminated` (live states come from the session's current run).

Common error codes: `unknown_code` (accepting a code nobody is waiting with, or that
expired), `agent_not_active`, `invalid_directory`, `session_exists`, `too_many_sessions`,
`session_ended`, `remote_session_ended` (sending to a conversation whose session ended:
open one again; Celius never moves it back to the sandbox), `file_unavailable`,
`machine_timeout`.

## Installers (public, outside `/api`)

| Method & path | Query | Result |
|---|---|---|
| `GET /install.sh` | `user?` | The Linux installer with this server's `APP_PUBLIC_URL`, its package and the user name filled in: `curl -fsSL …/install.sh \| sudo bash` |
| `GET /install.ps1` | `user?`, `upgrade?`, `uninstall?` | The Windows installer wrapped to run with those values: `irm …/install.ps1 \| iex` |
| `GET /install/{wheel}` | | The `celius` package (`celius_agent-<version>-py3-none-any.whl`) the installers download |

`user` must look like a user name (400 `invalid_user` otherwise; it is placed in a script).
503 `installer_unavailable` when the server has no package (see OPERATIONS.md); 404 when
remote machines are off. `GET /remote/devices` also returns `install_linux` and
`install_windows`: these commands for the signed-in user.

## Device sign-in (public)

The device authorization grant of RFC 8628, adapted: the device starts the sign-in and a
signed-in user accepts it in Celius.

| Method & path | Body | Result |
|---|---|---|
| `POST /device/login` | `{username?, name?, hostname, os, os_version, arch, agent_version, protocol_version}` | 201 `{device_code, user_code, verification_url, expires_in, interval}` |
| `POST /device/login/poll` | `{device_code}` | `{agent_id, credential, name, username}` once accepted; until then an error (below) |

- `device_code` (`cdc_…`) is the device's secret; only its hash is stored. `user_code` is
  8 characters (`XXXX-XXXX`, no vowels or look-alikes) and is what a person compares.
- Poll errors: 428 `authorization_pending` (keep polling every `interval` seconds),
  403 `access_denied` (refused), 410 `expired_token`, 400 `invalid_grant` (unknown or
  already used), 429 `slow_down`.
- `username` puts the request in that user's `GET /devices`; an unknown name is accepted
  silently (nothing reveals which user names exist). Limits: 10 sign-ins per address per
  hour, 10 waiting per user. The credential is handed out once.

## For devices

Authenticated by the device's own credential, never by user sessions or tokens.

| Method & path | Auth | Body | Result |
|---|---|---|---|
| `POST /token` | credential | `{credential}` | `{access_token, expires_at, agent_id}` (15 min); 401 when revoked/invalid |
| `POST /credentials/rotate` | access token | | `{credential}`; the old one works for `REMOTE_CREDENTIAL_GRACE_MINUTES` or until the new one is used |
| `POST /device/logout` | access token | | 204: the device revokes itself |
| `WS /agent/connect` | access token | | The agent protocol below |

The `celius` command on the device uses these, as the machine's owner but only for
conversations whose session runs on **this** machine (anything else is 404):

| Method & path | Body / query | Result |
|---|---|---|
| `POST /device/chats` | `{working_directory, title?, model?}` | 201 `{conversation_id, session_id, title, working_directory, status, origin: "device", updated_at, url}` |
| `GET /device/chats` | `limit` | `{items: [DeviceChat]}`, most recent first |
| `GET /device/chats/{conversation_id}` | | The conversation as `GET /conversations/{id}` returns it |
| `POST /device/chats/{conversation_id}/messages` | `{client_message_id, content}` | As `POST /conversations/{id}/messages` (the run) |
| `GET /device/runs/{run_id}/events` | `Last-Event-ID` | As `GET /runs/{id}/events` (SSE) |
| `POST /device/runs/{run_id}/cancel` | | 204 |
| `POST /device/tool-calls/{id}/approve` | `{args_hash, remember?}` | As `POST /tool-calls/{id}/approve` |
| `POST /device/tool-calls/{id}/deny` | `{reason?}` | As `POST /tool-calls/{id}/deny` |

## Agent protocol (WebSocket, JSON text frames, `protocol: 1`)

```
agent → hello      {protocol, agent_version, hostname, os, os_version, arch,
                    capabilities, local_policy, resources, sessions: {id: acked_seq}}
      ← welcome    {agent_id, connection_id, heartbeat_seconds, server_time,
                    sessions: {id: {last_seq, status, origin, working_directory,
                                    permissions, next_seq}}}
agent → heartbeat  {resources}                      every heartbeat_seconds
agent → policy     {local_policy}                   the policy file changed
agent → result     {command_id, status, result?, error?}   outcome of a command with no session
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
| `session.open` | `{origin, working_directory, permissions, next_seq}` | `session.opened {working_directory, writable, shell}` or `session.failed {reason}`; the agent re-reads its policy file first |
| `session.close` | `{reason}` | `session.closed` |
| `tool.execute` | `{tool, arguments, timeout_seconds}` (tools: `list_files`, `read_file`, `write_file`, `run_shell`, `run_python`, `git`) | `command.started`, `terminal.output*`, `file.changed*`, `command.completed {command_id, status, result: {content, data, is_error}}` |
| `file.fetch` | `{path, max_bytes}` | `command.completed` with `result.data = {name, size, sha256, content_b64}` |
| `cancel` | `{command_id}` | the command completes as cancelled |
| `interrupt` | | running commands cancelled |
| `agent.disconnect` | `{reconnect: true}` | the agent closes and reconnects |
| `policy.update` | `{policy: {roots?, allow_shell?, allow_write?}}` (no session) | `result` (completed with `{local_policy}`, or failed with the reason) and a `policy` frame |

Delivery: commands at least once, executed at most once (the agent journals ids).
Events: written to the agent's outbox before sending; stored by Central once per
`(session_id, seq)`; acked as the highest contiguous seq; resent after reconnecting.

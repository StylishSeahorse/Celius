# Operating Celius Remote

## Central deployment (Proxmox / Docker Compose)

Nothing new to deploy: the agent WebSocket is served by the existing `api` container and
the remote tool calls by the existing `worker`. No new container, port, volume or broker.

| Item | What to do |
|---|---|
| Migration | `0016` (new tables, two nullable/defaulted columns on `runs`). Applied by the `migrate` step of `make up` / `make upgrade` as usual. Reversible: `alembic downgrade 0015`. |
| Settings | `.env.example` → *Celius Remote*. All have safe defaults; `REMOTE_AGENTS_ENABLED=false` turns the feature off (404). |
| Network | Machines need outbound HTTPS to your public Celius address. Nothing connects to them. |
| Reverse proxy | Pass WebSocket upgrades on `/api/v1/remote/agent/connect`, no buffering on `/api/v1/remote/sessions/*/stream`: [reverse-proxy.md](../deployment/reverse-proxy.md). Caddy needs nothing extra. |
| TLS | Agents refuse plain `http://` (except `localhost`) unless registered with `--allow-insecure-http`. A private CA: `--ca-file`. |
| Storage | All remote state is in PostgreSQL (`remote_*` tables). Downloads are passed through, not stored. |
| Backups | Covered by the existing `make backup` (database dump). Agents keep their own credential and outbox on the machine; nothing there needs backing up: after a restore, agents reconnect and resend anything Central doesn't have. |
| Retention | The worker's maintenance pass deletes terminal output older than `REMOTE_OUTPUT_RETENTION_DAYS` (30), finished commands older than 30 days, and stale pairing codes. Other session events go when their conversation is deleted. |
| Audit | `audit_events` actions `remote_enrollment.created`, `remote_agent.enrolled/authorized/updated/revoked/deleted/connected/disconnected/credential_rotated/disconnect_requested`, `remote_session.opened/interrupted/terminated`, `remote_permission.decided`, plus the existing `tool_call.*` for every approval. |

### Upgrade and rollback

- Upgrade: the usual `make upgrade` (backup → pull → migrate → restart). Connected agents
  are disconnected by the API restart and reconnect within a minute (backoff with jitter);
  running commands continue on the machines and their events arrive after reconnecting.
- Rollback to a version before Celius Remote: `make rollback` restores the pre-upgrade
  backup. If you only roll the code back, first `alembic downgrade 0015` (drops the remote
  tables and the two `runs` columns). Stop agents first, or they will retry quietly until
  the server has the feature again.
- Server and agent versions are independent; the protocol is versioned (`protocol: 1`).

### Capacity

Each connected machine costs one WebSocket and one database query per poll interval
(`REMOTE_POLL_INTERVAL_SECONDS`, 0.25 s) while idle: a read-only `SELECT`, no write
transaction. Heartbeats write once per `REMOTE_HEARTBEAT_SECONDS` (15 s). Tens of machines
are comfortably within a small Proxmox VM; for hundreds, raise the poll interval.

### Monitoring

- *Remote machines* in the web app shows each machine's state, load and sessions.
- A machine shows **Disconnected unexpectedly** when its connection ended without a
  goodbye (network drop, crash, sleep), **Offline** after a clean stop.
- `SELECT kind, status, count(*) FROM remote_commands GROUP BY 1, 2;` — a growing number of
  `pending` commands means a machine is offline with work waiting (they expire after
  `REMOTE_OFFLINE_WAIT_SECONDS` for tool calls; session/close commands wait).

## Machines

| Task | How |
|---|---|
| Add | *Remote machines → Add a machine*, then the installer (INSTALL.md), then *Authorise*. |
| Restrict | The machine's `policy.json` (what it allows at all) and the machine's settings in Celius (narrower). |
| Rotate its credential | On the machine: `celius-agent rotate-credential`, then restart the agent. The old credential stops working once the new one is used, or after `REMOTE_CREDENTIAL_GRACE_MINUTES`. |
| Revoke | *Revoke*: its credential and tokens stop working at once, its connection closes, its sessions end and queued commands are cancelled. Register it again to reuse it. |
| Remove | *Remove*: revoke and forget; its conversations stay. |
| Reconnect | *Reconnect agent*: the agent closes its connection and reconnects (work continues). |
| Lost or stolen machine | *Revoke* it in Celius. Nothing on Central depends on the machine. |

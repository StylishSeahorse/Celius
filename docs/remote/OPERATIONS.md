# Operating Celius Remote

## Central deployment (Proxmox / Docker Compose)

Nothing new to deploy: the agent WebSocket is served by the existing `api` container and
the remote tool calls by the existing `worker`. No new container, port, volume or broker.

| Item | What to do |
|---|---|
| Migration | `0016` (new tables, two nullable/defaulted columns on `runs`) and `0017` (device sign-in: `remote_device_requests` replaces `remote_enrollments`; sessions get `origin`). Applied by the `migrate` step of `make up` / `make upgrade` as usual. `0017` ends any Claude Code sessions left from an earlier build. Reversible: `alembic downgrade 0015` (this also removes the later `0018`, steering). |
| Settings | `.env.example` → *Celius Remote*. All have safe defaults; `REMOTE_AGENTS_ENABLED=false` turns the feature off (404). |
| Public address | `APP_PUBLIC_URL` is used in the link `celius login` prints (`…/#/devices?code=…`) and in the commands *Add a device* shows; set it to the address people open. |
| Device installers | The image builds the `celius` package and serves it with the installers at `/install.sh`, `/install.ps1` and `/install/<wheel>` (`REMOTE_AGENT_DIST_DIR=/srv/agent-dist`, set in the image). Running from source: `make agent-wheel` once. After upgrading the server, devices upgrade with the commands in INSTALL.md. |
| Network | Machines need outbound HTTPS to your public Celius address. Nothing connects to them. |
| Reverse proxy | Pass WebSocket upgrades on `/api/v1/remote/agent/connect`, no buffering on `/api/v1/remote/sessions/*/stream`: [reverse-proxy.md](../deployment/reverse-proxy.md). Caddy needs nothing extra. |
| TLS | Devices refuse plain `http://` (except `localhost`) unless signed in with `celius login --allow-insecure-http`. A private CA: `--ca-file`. |
| Storage | All remote state is in PostgreSQL (`remote_*` tables). Downloads are passed through, not stored. |
| Backups | Covered by the existing `make backup` (database dump). Agents keep their own credential and outbox on the machine; nothing there needs backing up: after a restore, agents reconnect and resend anything Central doesn't have. |
| Retention | The worker's maintenance pass deletes terminal output older than `REMOTE_OUTPUT_RETENTION_DAYS` (30), finished commands older than 30 days, and device sign-in requests a day after they expire. Other session events go when their conversation is deleted. |
| Audit | `audit_events` actions `remote_device.login_requested/refused`, `remote_agent.accepted/signed_in/updated/revoked/deleted/connected/disconnected/credential_rotated/disconnect_requested`, `remote_session.opened/interrupted/terminated`, plus the existing `tool_call.*` for every approval. |

### Upgrade and rollback

- Upgrade: the usual `make upgrade` (backup → pull → migrate → restart). Connected agents
  are disconnected by the API restart and reconnect within a minute (backoff with jitter);
  running commands continue on the machines and their events arrive after reconnecting.
- Rollback to a version before Celius Remote: `make rollback` restores the pre-upgrade
  backup. If you only roll the code back, first `alembic downgrade 0015` (also drops steering's `run_steers`, 0018; drops the remote
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
| Add | Install on the device (INSTALL.md) or run `celius login --server … --user you` there; accept it under *Remote machines → Waiting for you to accept* (or *Add a device* and type its code). |
| Restrict | The machine's `policy.json` (what it allows at all; re-read when a session opens) and the machine's settings in Celius (narrower). |
| Rotate its credential | On the machine: `celius rotate-credential`, then restart the background agent. The old credential stops working once the new one is used, or after `REMOTE_CREDENTIAL_GRACE_MINUTES`. |
| Revoke | *Revoke*: its credential and tokens stop working at once, its connection closes, its sessions end and queued commands are cancelled. Sign it in again (`celius login --force`) to reuse it. |
| Remove | *Remove* in Celius, or `celius logout` on the machine: revoke and forget; its conversations stay. |
| Reconnect | *Reconnect agent*: the agent closes its connection and reconnects (work continues). |
| Lost or stolen machine | *Revoke* it in Celius. Nothing on Central depends on the machine. |

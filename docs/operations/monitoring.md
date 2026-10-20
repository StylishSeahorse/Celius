# Monitoring and alerts

## Checks

| Check | OK | Warning | Critical |
| --- | --- | --- | --- |
| `database` | answers | | unreachable |
| `model_server` | `/health` answers | offline (the app still works, without replies) | |
| `sandbox` | running (mode shown) | not reachable from the worker | |
| `worker` | heartbeat < 3 min old | | silent ≥ 3 min |
| `disk_storage`, `disk_workspaces`, `disk_backups` (one per filesystem) | | < `ALERT_DISK_WARNING_PERCENT` free (15) | < `ALERT_DISK_CRITICAL_PERCENT` (5) |
| `run_queue` | oldest waiting reply < 10 min | ≥ `ALERT_QUEUE_WARNING_MINUTES` | ≥ `ALERT_QUEUE_CRITICAL_MINUTES` (60) |
| `job_queue` | | a job waiting ≥ 10 min, or ≥ `ALERT_FAILED_JOBS_PER_DAY` failures in 24 h | |
| `approvals` | informational: tool calls waiting for you | | |
| `backup` | last success < 26 h ago, copied off-host | older than `ALERT_BACKUP_WARNING_HOURS`, never, or only on this machine | older than `ALERT_BACKUP_CRITICAL_HOURS` (72) |

Where to see them:

- **Account → System status** in any client (administrators), refreshed every 15 s.
- `make status` on the host. The exit code is 0 for OK, 1 for a warning and 2 for
  critical, for an external monitor.
- `GET /api/v1/status` (administrators).
- `GET /api/v1/metrics` in Prometheus format, with `Authorization: Bearer $METRICS_TOKEN`.
  It is off (404) unless `METRICS_TOKEN` is set. It reports `celius_check_level{check=…}`
  (0/1/2) and `celius_check_value{check=…}`.

## Alerts

The worker evaluates the checks every minute. It posts to `ALERT_WEBHOOK_URL` when a check
gets worse or recovers, and every `ALERT_REPEAT_HOURS` (6) while a check stays critical.

| `ALERT_WEBHOOK_FORMAT` | Body | Good for |
| --- | --- | --- |
| `json` (default) | `{"title", "message", "level", "check": {…}}` | Home Assistant webhook automations, n8n, anything custom |
| `ntfy` | plain text, with `Title` and `Priority` headers | `https://ntfy.sh/<topic>` or a self-hosted ntfy: phone push in one line |
| `text` | plain text | Gotify-style or chat webhooks that take raw text |

Example for ntfy:

```sh
ALERT_WEBHOOK_URL=https://ntfy.example.net/celius-alerts
ALERT_WEBHOOK_FORMAT=ntfy
```

Alert state is kept in `app-data/status/alerts.json`, so a restart doesn't repeat alerts.
The worker sends the alerts, so a dead worker can't report itself. Point an external
monitor such as Uptime Kuma at `make status`, or at `/api/v1/metrics` with
`celius_check_level{check="worker"}`.

## What to do

| Alert | Usually |
| --- | --- |
| worker silent | `docker compose -f infra/compose.yaml ps worker`, then `… logs --tail 100 worker` |
| run queue critical | The model server is down or too slow: check `model_server`, then llama-server on the GPU machine |
| disk | `docker system df`; prune old images; check `BACKUP_RETENTION_DAYS`; the **Files** usage page |
| backup old | Read `backups/backup.log` (cron output); run `make backup` by hand |
| backup only on this machine | Set `BACKUP_OFFSITE_TARGET`, or fix the SSH key / rclone remote |
| job failures | **Activity** panel → failed jobs show their error |

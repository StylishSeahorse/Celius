# Phase 7 acceptance tests: release hardening

Exit gate: **all required acceptance tests pass in a clean deployment.**

The clean deployment is CI's `image` job, on a fresh GitHub runner: it builds the
production images, then `infra/scripts/test-clean-deployment.sh` runs the stack exactly as
`infra/compose.yaml` defines it.

## Automated

| Suite | Covers |
| --- | --- |
| `infra/scripts/test-clean-deployment.sh` (CI, real Docker) | **Fresh deployment** from `.env.example` with only secrets filled in; becomes ready; admin bootstrapped. **Data**: a chat, a 200 KB binary upload, a memory. **Backup**: an encrypted archive (not readable as tar) with its `.sha256`; the app comes back; the status page sees the backup. **Disaster**: `docker compose down -v` and `.env` deleted. **Restore** with only the archive and the passphrase: settings come back from the backup; sign-in works with the old password; the chat is there; the newer chat isn't; the file is byte-identical; the memory is there; `--deep` verification re-hashes every stored file. **Tampered archive** refused without disturbing the running deployment. **Restore drill** in a separate project next to the live one, which keeps working. **Rollback** with no schema change keeps the data |
| `services/api/tests/test_backup.py` | Manifest checksums every part and counts every table; tampering and missing parts detected; verification passes for a faithful restore and reports lost rows, missing tables, missing or changed file content |
| `services/api/tests/test_security.py` | Every API route (from the OpenAPI schema) refuses unauthenticated requests; login throttled per account and per address, with the right password too, and cleared by the CLI reset; password change needs the current password, enforces length, signs out other devices; revoke other sessions; security headers; strict CSP for the web app; cross-site cookie requests refused, bearer requests allowed |
| `services/api/tests/test_monitoring.py` | Status lists every check for admins only; worker heartbeat and backup age thresholds; backups only on this machine or with a failed off-host copy are warnings; disk thresholds; queue age; alerts sent once per change, on recovery, and repeated while critical; ntfy format; metrics need their token |
| `services/sandbox/tests/` | Escape tests (background processes, setsid, fork bombs, memory, output floods, timeouts, workspace escape, environment, non-dumpable service, egress proxy rules); now also run on Python 3.12 in CI |
| `tests/test_compose_policy.py` | Sandbox and egress get no app secrets, volumes or ports; internal networks; backups mounted read-only |

## Results

See below; filled from the CI run on the Phase 7 head.

## Manual (on the real deployment, H-06)

1. Set `BACKUP_PASSPHRASE` (saved in a password manager) and `BACKUP_OFFSITE_TARGET`;
   `make backup`; check the archive arrived on the NAS.
2. `make restore-drill`: "restore drill passed".
3. Install the cron line; next morning, **Account → System status → Backups** shows
   "last one … ago" in green.
4. Set `ALERT_WEBHOOK_URL` (ntfy or Home Assistant); `docker compose … stop worker` for
   five minutes; an alert arrives, and a recovery message after starting it again.
5. Change your password in **Account**; the phone is signed out.
6. Next update: `make upgrade`, then `make rollback`, then `make upgrade` again.

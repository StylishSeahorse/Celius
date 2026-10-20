#!/usr/bin/env bash
# Prove a backup restores, without touching the live deployment.
#
#   infra/scripts/restore-drill.sh [ARCHIVE] [--keep]
#
# Restores ARCHIVE (default: the newest in BACKUP_DIR) into a separate compose project,
# "celius-drill", with its own database and volumes, on API_PORT 18099. The worker is
# not started (scheduled tasks must not run twice). After the verification inside
# restore.sh, it checks the API answers and that the backup's accounts are there, then
# removes the drill project (--keep leaves it running for a look around).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

archive=""
keep=0
while [ $# -gt 0 ]; do
  case "$1" in
    --keep) keep=1; shift ;;
    -h | --help) sed -n '2,10p' "$0"; exit 0 ;;
    *) archive="$1"; shift ;;
  esac
done
if [ -z "$archive" ]; then
  archive="$(ls -1t "$(backup_dir)"/celius-*.tar.enc 2>/dev/null | head -n 1)"
  [ -n "$archive" ] || die "no backups in $(backup_dir)"
fi
need docker openssl tar
passphrase
umask 077

drill="$(mktemp -d "${TMPDIR:-/var/tmp}/celius-drill-XXXXXX")"
export COMPOSE_PROJECT_NAME=celius-drill
export ENV_FILE="$drill/env" CELIUS_ENV_FILE="$drill/env"
teardown() {
  if [ "$keep" = 1 ]; then
    log "drill left running (project celius-drill, settings $drill/env)."
    log "remove it with: COMPOSE_PROJECT_NAME=celius-drill CELIUS_ENV_FILE=$drill/env docker compose -f infra/compose.yaml --env-file $drill/env down -v"
  else
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$drill"
  fi
}
trap teardown EXIT

# The backup's own settings, moved off the live deployment's port.
decrypt -in "$archive" | tar -xO --wildcards -f - '*/config/env' >"$drill/env"
{
  echo "API_PORT=18099"
  echo "API_BIND_ADDRESS=127.0.0.1"
  echo "ALERT_WEBHOOK_URL="
  echo "BOOTSTRAP_USERNAME="
} >>"$drill/env"

log "restoring $(basename "$archive") into the drill project"
"$ROOT/infra/scripts/restore.sh" "$archive" --yes --deep --services "api"

users="$(compose exec -T api python -m app.ops.users list)"
[ -n "$users" ] || die "the restored deployment has no accounts"
log "accounts restored: $(echo "$users" | cut -f1 | paste -sd, -)"
code="$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18099/api/v1/health/ready || true)"
[ "$code" = 200 ] || die "the drill api answered $code on its published port"
log "restore drill passed for $(basename "$archive")"

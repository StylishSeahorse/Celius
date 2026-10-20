#!/usr/bin/env bash
# Coordinated backup of a Celius deployment (docs/operations/backup-restore.md).
#
#   infra/scripts/backup.sh [--label NAME] [--no-offsite]
#
# Briefly stops the api and worker so the database and the stored files are captured at
# the same moment, then: pg_dump, the file store (and tool workspaces), the settings
# (.env), and a manifest with a checksum for every part and a census of the database.
# The result is one encrypted archive in BACKUP_DIR, copied to BACKUP_OFFSITE_TARGET when
# set; old archives are pruned after BACKUP_RETENTION_DAYS. Success is recorded in
# BACKUP_DIR/last-success.json, which the status page and alerts read.
#
# Cron, nightly:  15 3 * * *  cd /opt/celius && infra/scripts/backup.sh >>backups/backup.log 2>&1
set -euo pipefail
source "$(dirname "$0")/lib.sh"

label=""
offsite=1
while [ $# -gt 0 ]; do
  case "$1" in
    --label) label="-${2:?--label needs a name}"; shift 2 ;;
    --no-offsite) offsite=0; shift ;;
    -h | --help) sed -n '2,13p' "$0"; exit 0 ;;
    *) die "unknown option $1" ;;
  esac
done

need docker openssl tar sha256sum
[ -f "$ENV_FILE" ] || die "no settings file at $ENV_FILE"
passphrase
umask 077

dir="$(backup_dir)"
chmod 755 "$dir" # the api reads last-success.json through a read-only mount
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
name="celius-$stamp$label"
work="$(mktemp -d "$dir/.work-XXXXXX")"
staging="$work/$name"
mkdir -p "$staging/config"
chmod 777 "$staging" # written to by the containers' user (uid 10001)

stopped=()
restart() {
  if [ "${#stopped[@]}" -gt 0 ]; then
    log "starting ${stopped[*]} again"
    compose start "${stopped[@]}" >/dev/null || log "warning: could not restart ${stopped[*]}"
    stopped=()
  fi
}
cleanup() {
  restart
  rm -rf "$work"
}
trap cleanup EXIT

compose ps --services --status running | grep -qx postgres || die "postgres is not running"
for service in api worker; do
  if running_services | grep -qx "$service"; then stopped+=("$service"); fi
done
if [ "${#stopped[@]}" -gt 0 ]; then
  log "pausing ${stopped[*]} for a consistent snapshot"
  compose stop "${stopped[@]}" >/dev/null
fi

log "dumping the database"
compose exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' >"$staging/db.dump"

# Uploads in progress, temp files and status files are not worth keeping.
log "archiving stored files"
compose run --rm --no-deps -T -v "$staging:/backup" --entrypoint tar api \
  -C /data -czf /backup/app-data.tar.gz \
  --exclude=app/tmp --exclude=app/quarantine --exclude=app/status app

if [ "$(setting BACKUP_INCLUDE_WORKSPACES true)" = "true" ]; then
  log "archiving tool workspaces"
  compose run --rm --no-deps -T -v "$staging:/backup" --entrypoint tar api \
    -C /data -czf /backup/workspaces.tar.gz workspaces
fi

cp "$ENV_FILE" "$staging/config/env"
cp "$ROOT/infra/compose.yaml" "$staging/config/compose.yaml"
revision="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)"
echo "$revision" >"$staging/config/git-revision"

compose run --rm --no-deps -T -v "$staging:/backup" api \
  python -m app.ops.backup manifest /backup --git-revision "$revision" >&2
restart

log "encrypting"
archive="$dir/$name.tar.enc"
tar -C "$work" -cf - "$name" | encrypt -out "$archive.part"
mv "$archive.part" "$archive"
(cd "$dir" && sha256sum "$name.tar.enc" >"$name.tar.enc.sha256")
bytes="$(stat -c %s "$archive")"

target="$(setting BACKUP_OFFSITE_TARGET)"
copied=false
if [ "$offsite" = 1 ] && [ -n "$target" ]; then
  log "copying off this machine to $target"
  case "$target" in
    rclone:*)
      need rclone
      rclone copy "$archive" "${target#rclone:}" && rclone copy "$archive.sha256" "${target#rclone:}" && copied=true ;;
    *)
      need rsync
      rsync -t "$archive" "$archive.sha256" "$target/" && copied=true ;;
  esac
  [ "$copied" = true ] || log "warning: the off-host copy failed; the archive is only on this machine"
fi

days="$(setting BACKUP_RETENTION_DAYS 30)"
find "$dir" -maxdepth 1 -name 'celius-*.tar.enc*' -mtime "+$days" -print -delete | sed 's/^/pruned /' >&2

printf '{"finished_at": %s, "archive": "%s", "bytes": %s, "git_revision": "%s", "offsite_target": %s, "copied_off_host": %s}\n' \
  "$(date +%s)" "$name.tar.enc" "$bytes" "$revision" \
  "$([ -n "$target" ] && echo true || echo false)" "$copied" >"$dir/last-success.json.tmp"
chmod 644 "$dir/last-success.json.tmp"
mv "$dir/last-success.json.tmp" "$dir/last-success.json"

log "backup complete: $archive ($((bytes / 1024 / 1024)) MB)"
echo "$archive"
[ -z "$target" ] || [ "$copied" = true ] || [ "$offsite" = 0 ] || exit 2

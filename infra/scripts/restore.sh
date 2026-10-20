#!/usr/bin/env bash
# Restore a backup made by backup.sh (docs/operations/backup-restore.md).
#
#   infra/scripts/restore.sh ARCHIVE [--yes] [--with-config] [--deep] [--services "api worker ..."]
#
# Works on a clean machine (a checkout of the repository, Docker, the archive and its
# passphrase) as well as over an existing deployment, whose database and files are
# REPLACED. Steps: check the archive's checksums, stop the app, recreate the database
# from the dump, put back the files, verify row counts and stored files against the
# backup's manifest, apply any newer migrations, start everything and wait until ready.
#
# Settings: when ENV_FILE does not exist, the backup's copy is used. An existing one is
# kept unless --with-config (the old one is saved as .env.before-restore).
# --deep re-hashes every stored file during verification.
# --services limits what is started afterwards (default: the whole stack).
set -euo pipefail
source "$(dirname "$0")/lib.sh"

archive=""
yes=0
with_config=0
deep=""
services=""
while [ $# -gt 0 ]; do
  case "$1" in
    --yes) yes=1; shift ;;
    --with-config) with_config=1; shift ;;
    --deep) deep="--deep"; shift ;;
    --services) services="${2:?--services needs a list}"; shift 2 ;;
    -h | --help) sed -n '2,16p' "$0"; exit 0 ;;
    -*) die "unknown option $1" ;;
    *) archive="$1"; shift ;;
  esac
done
[ -n "$archive" ] || die "usage: restore.sh ARCHIVE [--yes] [--with-config] [--deep]"
[ -f "$archive" ] || die "no such file: $archive"
archive="$(cd "$(dirname "$archive")" && pwd)/$(basename "$archive")"

need docker openssl tar sha256sum
passphrase
umask 077

if [ -f "$archive.sha256" ]; then
  log "checking the archive against its .sha256"
  (cd "$(dirname "$archive")" && sha256sum --quiet -c "$(basename "$archive").sha256") \
    || die "the archive does not match its checksum file"
fi

work="$(mktemp -d "${TMPDIR:-/var/tmp}/celius-restore-XXXXXX")"
trap 'rm -rf "$work"' EXIT
log "decrypting"
decrypt -in "$archive" | tar -C "$work" -xf - || die "could not decrypt (wrong passphrase?)"
staging="$(find "$work" -mindepth 1 -maxdepth 1 -type d -name 'celius-*' | head -n 1)"
[ -n "$staging" ] && [ -f "$staging/manifest.json" ] || die "not a Celius backup"
chmod -R a+rX "$work" # read by the containers' user through the bind mount…
chmod 700 "$work"      # …but by nobody else on this machine

if [ ! -f "$ENV_FILE" ]; then
  log "no settings at $ENV_FILE: using the backup's"
  cp "$staging/config/env" "$ENV_FILE"
elif [ "$with_config" = 1 ]; then
  cp "$ENV_FILE" "$ENV_FILE.before-restore"
  cp "$staging/config/env" "$ENV_FILE"
  log "settings replaced from the backup (previous ones: $ENV_FILE.before-restore)"
elif ! cmp -s "$ENV_FILE" "$staging/config/env"; then
  log "note: $ENV_FILE differs from the backup's settings; keeping it (--with-config to replace)"
fi
chmod 600 "$ENV_FILE"

image="$(setting CELIUS_API_IMAGE celius-api:local)"
if ! docker image inspect "$image" >/dev/null 2>&1; then
  log "building images (none found for $image)"
  compose build >&2
fi

log "checking the backup's contents"
compose run --rm --no-deps -T -v "$staging:/backup:ro" api python -m app.ops.backup check /backup >&2

created="$(sed -n 's/.*"created_at": \([0-9]*\).*/\1/p' "$staging/manifest.json" | head -n 1)"
when="$(date -u -d "@${created:-0}" '+%Y-%m-%d %H:%M UTC' 2>/dev/null || echo "?")"
if [ "$yes" != 1 ]; then
  echo "This replaces the database and files of project '${COMPOSE_PROJECT_NAME:-celius}' with the backup from $when." >&2
  read -r -p "Type 'restore' to continue: " answer
  [ "$answer" = "restore" ] || die "cancelled"
fi

log "stopping the app"
compose stop api worker >/dev/null 2>&1 || true
compose up -d postgres redis >&2
for _ in $(seq 1 60); do
  compose exec -T postgres sh -c 'pg_isready -q -U "$POSTGRES_USER" -d postgres' && break
  sleep 2
done

log "recreating the database"
compose exec -T postgres sh -c \
  'dropdb -U "$POSTGRES_USER" --if-exists --force "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'
compose exec -T postgres sh -c \
  'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB" --no-owner --exit-on-error' <"$staging/db.dump"

log "restoring stored files"
compose run --rm --no-deps -T -v "$staging:/backup:ro" --entrypoint sh api -c \
  'find /data/app -mindepth 1 -delete && tar -C /data -xzf /backup/app-data.tar.gz'
if [ -f "$staging/workspaces.tar.gz" ]; then
  log "restoring tool workspaces"
  compose run --rm --no-deps -T -v "$staging:/backup:ro" --entrypoint sh api -c \
    'find /data/workspaces -mindepth 1 -delete && tar -C /data -xzf /backup/workspaces.tar.gz'
fi

log "verifying against the manifest"
compose run --rm --no-deps -T -v "$staging:/backup:ro" api \
  python -m app.ops.backup verify /backup/manifest.json $deep >&2

log "applying migrations and starting"
compose run --rm --no-deps -T migrate >&2
# shellcheck disable=SC2086  # a list of service names
compose up -d $services >&2
if wait_ready 180; then
  log "restore complete: the backup from $when is live"
else
  die "restored, but the api did not become ready; see: docker compose logs api"
fi

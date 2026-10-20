#!/usr/bin/env bash
# Upgrade a deployment to a newer revision (docs/operations/upgrade-and-rollback.md).
#
#   infra/scripts/upgrade.sh [REVISION]     # a tag, branch or commit; default: pull the current branch
#
# 1. Takes a labelled backup (aborts if it fails).
# 2. Keeps the running images as :rollback.
# 3. Checks out REVISION, builds, starts (the migrate step applies new migrations).
# 4. Waits until the API is ready.
# What to roll back to is written to BACKUP_DIR/last-upgrade.env; infra/scripts/rollback.sh
# uses it.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

target="${1:-}"
need docker git
[ -f "$ENV_FILE" ] || die "no settings file at $ENV_FILE"
if [ -n "$(git -C "$ROOT" status --porcelain --untracked-files=no)" ]; then
  die "the checkout has local changes; commit or stash them first"
fi

dir="$(backup_dir)"
from="$(git -C "$ROOT" rev-parse HEAD)"
api_image="$(setting CELIUS_API_IMAGE celius-api:local)"
sandbox_image="$(setting CELIUS_SANDBOX_IMAGE celius-sandbox:local)"

log "fetching"
git -C "$ROOT" fetch --tags --quiet origin
if [ -n "$target" ]; then
  to="$(git -C "$ROOT" rev-parse --verify "$target^{commit}" 2>/dev/null ||
    git -C "$ROOT" rev-parse --verify "origin/$target^{commit}")" || die "unknown revision $target"
else
  to="$(git -C "$ROOT" rev-parse '@{upstream}')" || die "no upstream branch; name a REVISION"
fi
[ "$from" != "$to" ] || { log "already at $(git -C "$ROOT" rev-parse --short "$to")"; exit 0; }
log "upgrading $(git -C "$ROOT" rev-parse --short "$from") -> $(git -C "$ROOT" rev-parse --short "$to")"
git -C "$ROOT" log --oneline "$from..$to" | head -n 20 >&2 || true

log "pre-upgrade backup"
archive="$("$ROOT/infra/scripts/backup.sh" --label "pre-upgrade" | tail -n 1)" ||
  [ $? -eq 2 ] || die "the backup failed; nothing was changed"
[ -f "$archive" ] || die "the backup did not produce an archive; nothing was changed"

for image in "$api_image" "$sandbox_image"; do
  if docker image inspect "$image" >/dev/null 2>&1; then
    docker tag "$image" "${image%:*}:rollback"
  fi
done
cat >"$dir/last-upgrade.env" <<EOF
FROM_REVISION=$from
TO_REVISION=$to
BACKUP_ARCHIVE=$archive
API_IMAGE=$api_image
SANDBOX_IMAGE=$sandbox_image
UPGRADED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF

if [ -n "$target" ]; then
  git -C "$ROOT" checkout --quiet "$to"
else
  git -C "$ROOT" merge --ff-only --quiet "$to"
fi
log "building"
compose build >&2
log "starting"
compose up -d >&2
if wait_ready 180; then
  log "upgrade complete: now at $(git -C "$ROOT" rev-parse --short HEAD)"
else
  log "the api is not ready after the upgrade. Logs: docker compose -f infra/compose.yaml logs api migrate"
  die "to go back: infra/scripts/rollback.sh"
fi

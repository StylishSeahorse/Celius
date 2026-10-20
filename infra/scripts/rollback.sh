#!/usr/bin/env bash
# Undo the last upgrade.sh (docs/operations/upgrade-and-rollback.md).
#
#   infra/scripts/rollback.sh [--yes]
#
# Goes back to the revision and images from before the upgrade. If the upgrade brought
# database migrations, the data is also put back from the pre-upgrade backup (anything
# written since the upgrade is lost, so this asks first). Without new migrations the data
# is kept as it is.
set -euo pipefail
source "$(dirname "$0")/lib.sh"

yes=0
[ "${1:-}" = "--yes" ] && yes=1
need docker git
dir="$(backup_dir)"
state="$dir/last-upgrade.env"
[ -f "$state" ] || die "no upgrade to roll back ($state not found)"
# shellcheck source=/dev/null  # written by upgrade.sh
source "$state"
[ -n "${FROM_REVISION:-}" ] && [ -n "${BACKUP_ARCHIVE:-}" ] || die "$state is incomplete"

migrations="$(git -C "$ROOT" diff --name-only "$FROM_REVISION" "$TO_REVISION" -- services/api/migrations/versions)"
short="$(git -C "$ROOT" rev-parse --short "$FROM_REVISION")"
if [ -n "$migrations" ]; then
  echo "The upgrade changed the database schema, so rolling back to $short also restores" >&2
  echo "the pre-upgrade backup ($(basename "$BACKUP_ARCHIVE")). Changes since $UPGRADED_AT are lost." >&2
else
  echo "Rolling back to $short. The upgrade added no migrations: data is kept." >&2
fi
if [ "$yes" != 1 ]; then
  read -r -p "Type 'rollback' to continue: " answer
  [ "$answer" = "rollback" ] || die "cancelled"
fi

compose stop api worker >/dev/null 2>&1 || true
git -C "$ROOT" checkout --quiet "$FROM_REVISION"

restored_images=1
for image in "$API_IMAGE" "$SANDBOX_IMAGE"; do
  if docker image inspect "${image%:*}:rollback" >/dev/null 2>&1; then
    docker tag "${image%:*}:rollback" "$image"
  else
    restored_images=0
  fi
done
if [ "$restored_images" = 0 ]; then
  log "no saved images; building $short"
  compose build >&2
fi

if [ -n "$migrations" ]; then
  "$ROOT/infra/scripts/restore.sh" "$BACKUP_ARCHIVE" --yes
else
  compose up -d >&2
  wait_ready 180 || die "the api did not become ready after the rollback"
fi
mv "$state" "$state.rolled-back"
log "rolled back to $short"

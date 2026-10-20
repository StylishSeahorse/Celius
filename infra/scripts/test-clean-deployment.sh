#!/usr/bin/env bash
# Release gate (Phase 7): deploy from scratch, use it, back it up, lose everything,
# restore onto a clean machine state, and check nothing was lost. Then a restore drill
# and a rollback. Run by CI after the images are built; safe to run locally ONLY on a
# machine without a real deployment in this checkout (it deletes the stack's volumes).
#
#   CELIUS_API_IMAGE=celius-api:ci CELIUS_SANDBOX_IMAGE=celius-sandbox:ci \
#     infra/scripts/test-clean-deployment.sh
set -euo pipefail
source "$(dirname "$0")/lib.sh"
need docker curl python3 openssl

[ ! -f "$ENV_FILE" ] || [ "${CELIUS_TEST_OVERWRITE:-}" = 1 ] ||
  die "$ENV_FILE exists; this test would replace that deployment (CELIUS_TEST_OVERWRITE=1 to allow)"

API="http://127.0.0.1:8000/api/v1"
PASSWORD="clean deployment password 1"
export BACKUP_PASSPHRASE="ci backup passphrase 123"
backups="$(mktemp -d)"

json() { python3 -c "import json,sys; d=json.load(sys.stdin); print($1)"; }
step() { printf '\n=== %s\n' "$*" >&2; }
fail() { compose logs --tail 80 api migrate >&2 || true; die "$*"; }
trap 'compose down -v --remove-orphans >/dev/null 2>&1 || true; rm -rf "$backups"' EXIT

step "fresh deployment"
cp "$ROOT/.env.example" "$ENV_FILE"
cat >>"$ENV_FILE" <<EOF
APP_SECRET_KEY=$(openssl rand -hex 32)
POSTGRES_PASSWORD=$(openssl rand -hex 16)
BOOTSTRAP_USERNAME=admin
BOOTSTRAP_PASSWORD=$PASSWORD
APP_COOKIE_SECURE=false
MODEL_BASE_URL=http://127.0.0.1:9/v1
SANDBOX_TOKEN=$(openssl rand -hex 32)
SANDBOX_EGRESS_TOKEN=$(openssl rand -hex 32)
BACKUP_DIR=$backups
BACKUP_PASSPHRASE=$BACKUP_PASSPHRASE
CELIUS_API_IMAGE=${CELIUS_API_IMAGE:-celius-api:local}
CELIUS_SANDBOX_IMAGE=${CELIUS_SANDBOX_IMAGE:-celius-sandbox:local}
EOF
compose up -d >&2
wait_ready 180 || fail "the fresh deployment did not become ready"

login() {
  curl -fsS "$API/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\": \"admin\", \"password\": \"$PASSWORD\"}" | json 'd["access_token"]'
}
token="$(login)"
call() {
  local method="$1" path="$2"
  shift 2
  curl -fsS -X "$method" "$API$path" -H "Authorization: Bearer $token" "$@"
}

step "create data"
call POST /conversations -H 'Content-Type: application/json' -d '{"title": "Before the backup"}' >/dev/null
head -c 150000 /dev/urandom | base64 >"$backups/upload.bin" # an accepted type (text) of random content
size="$(stat -c %s "$backups/upload.bin")"
upload="$(call POST /files/uploads -H 'Content-Type: application/json' \
  -d "{\"filename\": \"data.txt\", \"size\": $size}" | json 'd["id"]')"
call PUT "/files/uploads/$upload/content" --data-binary "@$backups/upload.bin" >/dev/null
file_id="$(call POST "/files/uploads/$upload/complete" | json 'd["id"]')"
call POST /memories -H 'Content-Type: application/json' \
  -d '{"content": "The drill cat is called Backup."}' >/dev/null

step "backup"
archive="$("$ROOT/infra/scripts/backup.sh" | tail -n 1)"
[ -f "$archive" ] && [ -f "$archive.sha256" ] || fail "no archive"
if tar -tf "$archive" >/dev/null 2>&1; then fail "the archive is not encrypted"; fi
wait_ready 120 || fail "the app did not come back after the backup"
token="$(login)"
summary="$(call GET /status | json '[c["summary"] for c in d["checks"] if c["name"] == "backup"][0]')"
echo "backup check: $summary" >&2
case "$summary" in "last one"*) ;; *) fail "the status page does not see the backup" ;; esac

call POST /conversations -H 'Content-Type: application/json' -d '{"title": "After the backup"}' >/dev/null

step "disaster: the stack, its volumes and its settings are gone"
compose down -v --remove-orphans >&2
rm -f "$ENV_FILE"

step "restore onto a clean state"
# The settings come back from the backup; BACKUP_PASSPHRASE is in the environment.
"$ROOT/infra/scripts/restore.sh" "$archive" --yes --deep
token="$(login)" || fail "cannot sign in after the restore"
titles="$(call GET /conversations | json '"|".join(c["title"] or "" for c in (d["items"] if isinstance(d, dict) else d))')"
echo "conversations: $titles" >&2
case "$titles" in *"Before the backup"*) ;; *) fail "a conversation was lost" ;; esac
case "$titles" in *"After the backup"*) fail "data newer than the backup survived the disaster?" ;; esac
call GET "/files/$file_id/download" -o "$backups/downloaded.bin"
cmp -s "$backups/upload.bin" "$backups/downloaded.bin" || fail "the restored file differs"
memories="$(call GET /memories | json 'len(d["items"] if isinstance(d, dict) else d)')"
[ "$memories" -ge 1 ] || fail "memories were lost"

step "a tampered archive is refused"
cp "$archive" "$backups/tampered.tar.enc"
printf 'x' | dd of="$backups/tampered.tar.enc" bs=1 seek=5000 conv=notrunc status=none
if "$ROOT/infra/scripts/restore.sh" "$backups/tampered.tar.enc" --yes >/dev/null 2>&1; then
  fail "a corrupted archive was restored"
fi
wait_ready 60 || fail "a refused restore broke the running deployment"

step "restore drill next to the live deployment"
"$ROOT/infra/scripts/restore-drill.sh" "$archive"
wait_ready 30 || fail "the drill disturbed the live deployment"

step "rollback (no schema change between the revisions: data is kept)"
head="$(git -C "$ROOT" rev-parse HEAD)"
for image in "${CELIUS_API_IMAGE:-celius-api:local}" "${CELIUS_SANDBOX_IMAGE:-celius-sandbox:local}"; do
  docker tag "$image" "${image%:*}:rollback" # what upgrade.sh would have kept
done
cat >"$backups/last-upgrade.env" <<EOF
FROM_REVISION=$head
TO_REVISION=$head
BACKUP_ARCHIVE=$archive
API_IMAGE=${CELIUS_API_IMAGE:-celius-api:local}
SANDBOX_IMAGE=${CELIUS_SANDBOX_IMAGE:-celius-sandbox:local}
UPGRADED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
EOF
"$ROOT/infra/scripts/rollback.sh" --yes
token="$(login)"
call GET "/files/$file_id" >/dev/null || fail "data lost in the rollback"

step "clean deployment test passed"

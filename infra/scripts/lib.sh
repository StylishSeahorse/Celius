# shellcheck shell=bash
# Shared by the operations scripts (backup, restore, restore-drill, upgrade, rollback).
# Sourced, not run.
#
# ENV_FILE             the deployment's settings (default: <repo>/.env)
# COMPOSE_PROJECT_NAME which compose project to act on (default: celius, from compose.yaml)

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"
case "$ENV_FILE" in /*) ;; *) ENV_FILE="$PWD/$ENV_FILE" ;; esac
export CELIUS_ENV_FILE="$ENV_FILE"

log() { printf '%s %s\n' "$(date -u +%H:%M:%S)" "$*" >&2; }
die() { log "error: $*"; exit 1; }

need() {
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null || die "$cmd is required but not installed"
  done
}

compose() { docker compose -f "$ROOT/infra/compose.yaml" --env-file "$ENV_FILE" "$@"; }

# A setting: the environment wins, then the last KEY=value line of ENV_FILE.
setting() {
  local key="$1" default="${2:-}" value
  if [ -n "${!key:-}" ]; then
    printf '%s' "${!key}"
    return
  fi
  value="$(sed -n "s/^${key}=//p" "$ENV_FILE" 2>/dev/null | tail -n 1)"
  value="${value%$'\r'}"
  value="${value#\"}" && value="${value%\"}"
  value="${value#\'}" && value="${value%\'}"
  printf '%s' "${value:-$default}"
}

# BACKUP_DIR is relative to infra/ in .env (like the compose file); make it absolute.
backup_dir() {
  local dir
  dir="$(setting BACKUP_DIR ../backups)"
  case "$dir" in /*) ;; *) dir="$ROOT/infra/$dir" ;; esac
  mkdir -p "$dir"
  (cd "$dir" && pwd)
}

passphrase() {
  local value
  value="$(setting BACKUP_PASSPHRASE)"
  if [ -z "$value" ] && [ -t 0 ]; then
    read -r -s -p "Backup passphrase: " value
    echo >&2
  fi
  [ -n "$value" ] || die "set BACKUP_PASSPHRASE (in .env or the environment)"
  case "$value" in REPLACE_WITH*) die "BACKUP_PASSPHRASE is still the placeholder" ;; esac
  [ "${#value}" -ge 12 ] || die "BACKUP_PASSPHRASE must be at least 12 characters"
  export BACKUP_PASSPHRASE="$value"
}

# openssl enc -pass env: keeps the passphrase off the command line (ps).
encrypt() { openssl enc -aes-256-cbc -pbkdf2 -iter 200000 -salt -pass env:BACKUP_PASSPHRASE "$@"; }
decrypt() { openssl enc -d -aes-256-cbc -pbkdf2 -iter 200000 -pass env:BACKUP_PASSPHRASE "$@"; }

running_services() { compose ps --services --status running 2>/dev/null || true; }

# Wait for the API's readiness endpoint (checked from inside the container).
wait_ready() {
  local timeout="${1:-120}" waited=0
  while [ "$waited" -lt "$timeout" ]; do
    if compose exec -T api python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health/ready', timeout=3).status == 200 else 1)" >/dev/null 2>&1; then
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

#!/usr/bin/env bash
# Install the Celius agent on Linux as a systemd service (docs/remote/INSTALL.md).
#
#   sudo ./install-linux.sh --server https://celius.example --code ABCD-EFGH-JKLM
#
# Options:
#   --user NAME            Run the agent as this user (default: the user who ran sudo, so it
#                          can reach that user's code; with no sudo user, a dedicated
#                          'celius-agent' system user is created).
#   --claude               Also install Claude Code support (claude-agent-sdk).
#   --allow-insecure-http  Allow a plain http:// server (trusted networks only).
#   --ca-file PATH         CA bundle for a server with a private certificate authority.
#   --no-service           Install and register only (containers without systemd).
#   --upgrade              Reinstall the agent from this folder and restart the service.
#   --uninstall            Stop and remove the service and the program (keeps its data).
set -euo pipefail

SERVER="" CODE="" RUN_AS="${SUDO_USER:-}" EXTRAS="monitor" INSECURE="" CA_FILE="" SERVICE=1 MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --code) CODE="$2"; shift 2 ;;
    --user) RUN_AS="$2"; shift 2 ;;
    --claude) EXTRAS="monitor,claude"; shift ;;
    --allow-insecure-http) INSECURE="--allow-insecure-http"; shift ;;
    --ca-file) CA_FILE="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --upgrade) MODE=upgrade; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${CELIUS_AGENT_PREFIX:-/opt/celius-agent}"
UNIT=/etc/systemd/system/celius-agent.service
have_systemd() { [ "$SERVICE" -eq 1 ] && command -v systemctl >/dev/null && [ -d /run/systemd/system ]; }

if [ "$MODE" = uninstall ]; then
  if have_systemd; then systemctl disable --now celius-agent 2>/dev/null || true; fi
  rm -f "$UNIT"
  if have_systemd; then systemctl daemon-reload; fi
  rm -rf "$PREFIX"
  echo "Removed. The agent's data (credential, policy, state) is kept in its home folder;"
  echo "revoke the machine in Celius and delete that folder to remove everything."
  exit 0
fi

PYTHON="${PYTHON:-python3}"
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' \
  || { echo "Python 3.10 or newer is required ($PYTHON)." >&2; exit 1; }
"$PYTHON" -m venv --help >/dev/null 2>&1 || { echo "Install python3-venv first." >&2; exit 1; }

if [ -z "$RUN_AS" ] || [ "$RUN_AS" = root ]; then
  RUN_AS=celius-agent
  if ! id -u "$RUN_AS" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/celius-agent --shell /usr/sbin/nologin "$RUN_AS"
  fi
fi
id -u "$RUN_AS" >/dev/null 2>&1 || { echo "No such user: $RUN_AS" >&2; exit 1; }
RUN_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
AGENT_HOME="${CELIUS_AGENT_HOME:-$RUN_HOME/.config/celius-agent}"

echo "Installing the agent into $PREFIX"
"$PYTHON" -m venv "$PREFIX"
"$PREFIX/bin/pip" install --quiet --upgrade pip
"$PREFIX/bin/pip" install --quiet "${SRC}[${EXTRAS}]"

if [ "$MODE" = install ]; then
  if [ -z "$SERVER" ] || [ -z "$CODE" ]; then
    echo "--server and --code are required." >&2
    exit 2
  fi
  install -d -m 700 -o "$RUN_AS" "$AGENT_HOME"
  args=(register --server "$SERVER" --code "$CODE")
  [ -n "$INSECURE" ] && args+=("$INSECURE")
  [ -n "$CA_FILE" ] && args+=(--ca-file "$CA_FILE")
  runuser -u "$RUN_AS" -- env CELIUS_AGENT_HOME="$AGENT_HOME" "$PREFIX/bin/celius-agent" "${args[@]}"
fi

if have_systemd; then
  sed -e "s|@USER@|$RUN_AS|g" -e "s|@PREFIX@|$PREFIX|g" -e "s|@HOME@|$AGENT_HOME|g" \
    "$SRC/install/celius-agent.service" >"$UNIT"
  systemctl daemon-reload
  systemctl enable celius-agent >/dev/null
  systemctl restart celius-agent
  echo "Service started: systemctl status celius-agent   logs: journalctl -u celius-agent -f"
else
  echo "No systemd here: start it with"
  echo "  runuser -u $RUN_AS -- env CELIUS_AGENT_HOME=$AGENT_HOME $PREFIX/bin/celius-agent run"
fi
if [ "$MODE" = install ]; then
  echo
  echo "Now: list the folders Celius may use in $AGENT_HOME/policy.json (\"roots\"),"
  echo "set \"allow_shell\": true if it may run commands, restart the agent, and authorise"
  echo "the machine in Celius (Remote machines)."
fi

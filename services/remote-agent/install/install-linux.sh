#!/usr/bin/env bash
# Install Celius on a Linux device (docs/remote/INSTALL.md): the `celius` command, and a
# systemd service that lets Celius use this device while you are away from it.
#
#   sudo ./install-linux.sh --server https://celius.example [--celius-user alice]
#
# It signs the device in: it shows a code, and you accept the device in Celius
# (Remote machines). Then run `celius` in a folder here to chat on this device.
#
# Options:
#   --celius-user NAME     Your Celius user name: the device waits in your list in Celius.
#   --name NAME            A name for this device in Celius (default: its hostname).
#   --run-as USER          Linux user the agent runs as (default: the user who ran sudo, so
#                          it can reach that user's code and `celius` shares its sign-in;
#                          with no sudo user, a dedicated 'celius-agent' system user).
#   --allow-insecure-http  Allow a plain http:// server (trusted networks only).
#   --ca-file PATH         CA bundle for a server with a private certificate authority.
#   --no-service           Install and sign in only (containers without systemd).
#   --upgrade              Reinstall Celius from this folder and restart the service.
#   --uninstall            Stop and remove the service and the program (keeps its data).
set -euo pipefail

SERVER="" CELIUS_USER="" NAME="" RUN_AS="${SUDO_USER:-}" INSECURE="" CA_FILE="" SERVICE=1 MODE=install
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --celius-user) CELIUS_USER="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --run-as) RUN_AS="$2"; shift 2 ;;
    --allow-insecure-http) INSECURE="--allow-insecure-http"; shift ;;
    --ca-file) CA_FILE="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --upgrade) MODE=upgrade; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root (sudo)." >&2; exit 1; }
SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${CELIUS_AGENT_PREFIX:-/opt/celius-agent}"
UNIT=/etc/systemd/system/celius-agent.service
LINK=/usr/local/bin/celius
have_systemd() { [ "$SERVICE" -eq 1 ] && command -v systemctl >/dev/null && [ -d /run/systemd/system ]; }

if [ "$MODE" = uninstall ]; then
  if have_systemd; then systemctl disable --now celius-agent 2>/dev/null || true; fi
  rm -f "$UNIT"
  if have_systemd; then systemctl daemon-reload; fi
  if [ -L "$LINK" ]; then rm -f "$LINK"; fi
  rm -rf "$PREFIX"
  echo "Removed. The device's data (sign-in, policy, state) is kept in its home folder;"
  echo "remove the device in Celius and delete that folder to remove everything."
  exit 0
fi

if [ "$MODE" = install ] && [ -z "$SERVER" ]; then
  echo "--server is required, e.g. --server https://celius.example" >&2
  exit 2
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
as_agent() { runuser -u "$RUN_AS" -- env CELIUS_AGENT_HOME="$AGENT_HOME" "$@"; }

echo "Installing Celius into $PREFIX"
"$PYTHON" -m venv "$PREFIX"
"$PREFIX/bin/pip" install --quiet --upgrade pip
"$PREFIX/bin/pip" install --quiet "${SRC}[monitor]"
ln -sfn "$PREFIX/bin/celius" "$LINK"

if [ "$MODE" = install ]; then
  install -d -m 700 -o "$RUN_AS" "$AGENT_HOME"
  if [ -s "$AGENT_HOME/credential" ]; then
    echo "This device is already signed in ($AGENT_HOME); keeping that sign-in."
  else
    args=(login --server "$SERVER")
    [ -n "$CELIUS_USER" ] && args+=(--user "$CELIUS_USER")
    [ -n "$NAME" ] && args+=(--name "$NAME")
    [ -n "$INSECURE" ] && args+=("$INSECURE")
    [ -n "$CA_FILE" ] && args+=(--ca-file "$CA_FILE")
    echo
    as_agent "$PREFIX/bin/celius" "${args[@]}" || { echo "Sign-in did not finish; run the installer again." >&2; exit 1; }
  fi
fi

if have_systemd; then
  sed -e "s|@USER@|$RUN_AS|g" -e "s|@PREFIX@|$PREFIX|g" -e "s|@HOME@|$AGENT_HOME|g" \
    "$SRC/install/celius-agent.service" >"$UNIT"
  systemctl daemon-reload
  systemctl enable celius-agent >/dev/null
  systemctl restart celius-agent
  echo "Service started: systemctl status celius-agent   logs: journalctl -u celius-agent -f"
else
  echo "No systemd here: start the agent with"
  echo "  runuser -u $RUN_AS -- env CELIUS_AGENT_HOME=$AGENT_HOME $PREFIX/bin/celius agent"
fi
if [ "$MODE" = install ]; then
  echo
  if [ "$RUN_AS" = "${SUDO_USER:-}" ]; then
    echo "Chat on this device: run 'celius' in a project folder (it asks before working there)."
  else
    echo "The agent runs as $RUN_AS; to chat here as that user:"
    echo "  sudo -u $RUN_AS env CELIUS_AGENT_HOME=$AGENT_HOME celius"
  fi
  echo "To use this device from Celius, list its folders in $AGENT_HOME/policy.json (\"roots\")"
  echo "and set \"allow_shell\": true if it may run commands (applies to the next session)."
fi

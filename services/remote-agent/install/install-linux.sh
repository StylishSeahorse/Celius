#!/usr/bin/env bash
# Install Celius on a Linux device (docs/remote/INSTALL.md): the `celius` command, and a
# systemd service that lets Celius use this device while you are away from it.
#
# From your Celius server (nothing else needed on the device):
#   curl -fsSL https://celius.example/install.sh | sudo bash
#   curl -fsSL "https://celius.example/install.sh?user=alice" | sudo bash
# From a copy of the repository (services/remote-agent):
#   sudo ./install/install-linux.sh --server https://celius.example [--celius-user alice]
#
# It signs the device in: it shows a code, and you accept the device in Celius
# (Remote machines). Then run `celius` in a folder here to chat on this device.
#
# Options (after `| sudo bash -s --` when piped):
#   --celius-user NAME     Your Celius user name: the device waits in your list in Celius.
#   --name NAME            A name for this device in Celius (default: its hostname).
#   --run-as USER          Linux user the agent runs as (default: the user who ran sudo, so
#                          it can reach that user's code and `celius` shares its sign-in;
#                          with no sudo user, a dedicated 'celius-agent' system user, which
#                          root's `celius` then runs as). `--run-as root` gives Celius the
#                          whole machine: for a container or VM that is only for this.
#   --allow-insecure-http  Allow a plain http:// server (trusted networks only).
#   --ca-file PATH         CA bundle for a server with a private certificate authority.
#   --no-service           Install and sign in only (containers without systemd).
#   --upgrade              Reinstall Celius and restart the service.
#   --uninstall            Stop and remove the service and the program (keeps its data).
set -euo pipefail

# Filled in when Celius serves this script (GET /install.sh); empty in the repository.
SERVED_SERVER="" SERVED_PACKAGE="" SERVED_USER=""

SERVER="$SERVED_SERVER" CELIUS_USER="$SERVED_USER" NAME="" RUN_AS="${SUDO_USER:-}" INSECURE=""
CA_FILE="" SERVICE=1 MODE=install PACKAGE="$SERVED_PACKAGE" RUN_AS_CHOSEN=0
case "$SERVER" in
  http://localhost* | http://127.0.0.1* | "") ;;
  http://*) INSECURE="--allow-insecure-http" ;; # the script itself came from this address
esac
while [ $# -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --celius-user) CELIUS_USER="$2"; shift 2 ;;
    --name) NAME="$2"; shift 2 ;;
    --run-as) RUN_AS="$2"; RUN_AS_CHOSEN=1; shift 2 ;;
    --allow-insecure-http) INSECURE="--allow-insecure-http"; shift ;;
    --ca-file) CA_FILE="$2"; shift 2 ;;
    --no-service) SERVICE=0; shift ;;
    --upgrade) MODE=upgrade; shift ;;
    --uninstall) MODE=uninstall; shift ;;
    -h|--help) echo "See the comments at the top of this script, or docs/remote/INSTALL.md."; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

[ "$(id -u)" -eq 0 ] || { echo "Run as root: ... | sudo bash" >&2; exit 1; }
if [ -z "$PACKAGE" ]; then
  # Run from the repository: install the package next to this script.
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  [ -f "$SRC/pyproject.toml" ] || { echo "Run this from services/remote-agent, or get it from your Celius server: curl -fsSL https://<celius>/install.sh | sudo bash" >&2; exit 1; }
  PACKAGE="${SRC}[monitor]"
fi
PREFIX="${CELIUS_AGENT_PREFIX:-/opt/celius-agent}"
UNIT=/etc/systemd/system/celius-agent.service
LINKS="/usr/local/bin/celius /usr/bin/celius" # /usr/bin too: root shells such as `pct enter` lack /usr/local/bin
SYSTEM_CONF=/etc/celius-agent.json # which user and home root's `celius` uses
have_systemd() { [ "$SERVICE" -eq 1 ] && command -v systemctl >/dev/null && [ -d /run/systemd/system ]; }

if [ "$MODE" = uninstall ]; then
  if have_systemd; then systemctl disable --now celius-agent 2>/dev/null || true; fi
  rm -f "$UNIT"
  if have_systemd; then systemctl daemon-reload; fi
  for link in $LINKS; do
    if [ -L "$link" ] && [ "$(readlink "$link")" = "$PREFIX/bin/celius" ]; then rm -f "$link"; fi
  done
  rm -f "$SYSTEM_CONF"
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
python_ok() {
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null \
    && "$PYTHON" -m venv --help >/dev/null 2>&1 && "$PYTHON" -c 'import ensurepip' 2>/dev/null
}
if ! python_ok; then
  echo "Installing Python (3.10 or newer is needed)…"
  if command -v apt-get >/dev/null; then
    apt-get update -qq </dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv </dev/null
  elif command -v dnf >/dev/null; then
    dnf install -y -q python3 </dev/null
  elif command -v yum >/dev/null; then
    yum install -y -q python3 </dev/null
  elif command -v zypper >/dev/null; then
    zypper --non-interactive install python3 </dev/null
  elif command -v pacman >/dev/null; then
    pacman -Sy --noconfirm python </dev/null
  fi
  python_ok || { echo "Python 3.10 or newer with venv is required ($PYTHON); install it and run this again." >&2; exit 1; }
fi

if [ -z "$RUN_AS" ] || { [ "$RUN_AS" = root ] && [ "$RUN_AS_CHOSEN" -eq 0 ]; }; then
  RUN_AS=celius-agent
  if ! id -u "$RUN_AS" >/dev/null 2>&1; then
    useradd --system --create-home --home-dir /var/lib/celius-agent --shell /usr/sbin/nologin "$RUN_AS"
  fi
fi
id -u "$RUN_AS" >/dev/null 2>&1 || { echo "No such user: $RUN_AS" >&2; exit 1; }
RUN_HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)"
AGENT_HOME="${CELIUS_AGENT_HOME:-$RUN_HOME/.config/celius-agent}"
as_agent() { runuser -u "$RUN_AS" -- env CELIUS_AGENT_HOME="$AGENT_HOME" "$@"; }
# Where an earlier install kept this device's sign-in (to keep it when --run-as changes).
OLD_HOME=""
if [ -f "$SYSTEM_CONF" ]; then
  OLD_HOME="$(sed -n 's/.*"home": "\([^"]*\)".*/\1/p' "$SYSTEM_CONF")"
elif [ -s /var/lib/celius-agent/.config/celius-agent/credential ]; then
  OLD_HOME=/var/lib/celius-agent/.config/celius-agent
fi

echo "Installing Celius into $PREFIX"
pip_args=(--quiet --disable-pip-version-check)
[ -n "$CA_FILE" ] && pip_args+=(--cert "$CA_FILE")
"$PYTHON" -m venv "$PREFIX"
"$PREFIX/bin/pip" install "${pip_args[@]}" --upgrade pip </dev/null
"$PREFIX/bin/pip" install "${pip_args[@]}" --upgrade --force-reinstall --no-deps "$PACKAGE" </dev/null
"$PREFIX/bin/pip" install "${pip_args[@]}" "$PACKAGE" </dev/null
for link in $LINKS; do
  # Never replace a celius that is not ours.
  if [ ! -e "$link" ] || { [ -L "$link" ] && [ "$(readlink "$link")" = "$PREFIX/bin/celius" ]; }; then
    ln -sfn "$PREFIX/bin/celius" "$link"
  fi
done
printf '{"user": "%s", "home": "%s"}\n' "$RUN_AS" "$AGENT_HOME" >"$SYSTEM_CONF"
chmod 644 "$SYSTEM_CONF"
if [ "$RUN_AS" = root ]; then
  echo "Note: the agent runs as root, so Celius can reach the whole machine (within the folders you allow)."
fi

if [ "$MODE" = install ]; then
  install -d -m 700 -o "$RUN_AS" "$AGENT_HOME"
  if [ ! -s "$AGENT_HOME/credential" ] && [ -n "$OLD_HOME" ] && [ "$OLD_HOME" != "$AGENT_HOME" ] \
    && [ -s "$OLD_HOME/credential" ]; then
    echo "Moving this device's sign-in from $OLD_HOME to $AGENT_HOME"
    if have_systemd; then systemctl stop celius-agent 2>/dev/null || true; fi
    cp -a "$OLD_HOME/." "$AGENT_HOME/"
    rm -f "$AGENT_HOME/agent.lock"
    chown -R "$RUN_AS": "$AGENT_HOME"
  fi
  if [ -s "$AGENT_HOME/credential" ]; then
    echo "This device is already signed in ($AGENT_HOME); keeping that sign-in."
  else
    args=(login --server "$SERVER")
    [ -n "$CELIUS_USER" ] && args+=(--user "$CELIUS_USER")
    [ -n "$NAME" ] && args+=(--name "$NAME")
    [ -n "$INSECURE" ] && args+=("$INSECURE")
    [ -n "$CA_FILE" ] && args+=(--ca-file "$CA_FILE")
    echo
    as_agent "$PREFIX/bin/celius" "${args[@]}" </dev/null \
      || { echo "Sign-in did not finish; run the installer again." >&2; exit 1; }
  fi
fi

if have_systemd; then
  if [ "$RUN_AS" = root ]; then
    # Chosen to manage the whole machine: /etc, /usr and the real /tmp must stay usable.
    SANDBOX="# --run-as root: no file system sandbox (the machine is Celius's to manage)."
  else
    SANDBOX="ProtectSystem=full
PrivateTmp=yes"
  fi
  cat >"$UNIT" <<UNIT
# Celius agent (installed by install-linux.sh).
[Unit]
Description=Celius agent: lets Celius use this device
Documentation=https://github.com/StylishSeahorse/Celius/tree/main/docs/remote
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=$RUN_AS
Environment=CELIUS_AGENT_HOME=$AGENT_HOME
Environment=PYTHONUNBUFFERED=1
ExecStart=$PREFIX/bin/celius agent
# Reconnecting is the agent's job; this only covers crashes.
Restart=always
RestartSec=5
# Running commands are stopped with the agent (they would be reported as interrupted).
KillMode=control-group
TimeoutStopSec=30

# Hardening that leaves the user's own folders (the policy's roots) usable.
NoNewPrivileges=yes
$SANDBOX
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
LockPersonality=yes

[Install]
WantedBy=multi-user.target
UNIT
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
  echo "Chat on this device: run 'celius' in a project folder (it asks before working there)."
  if [ "$RUN_AS" != root ] && [ "$RUN_AS" != "${SUDO_USER:-}" ]; then
    echo "It runs as '$RUN_AS' (also when root types celius), so it can only work in folders"
    echo "that user can read. For the whole machine (a container just for this), install again"
    echo "with: ... | bash -s -- --run-as root"
  fi
  echo "To use this device from Celius, list its folders in $AGENT_HOME/policy.json (\"roots\")"
  echo "and set \"allow_shell\": true if it may run commands (applies from the next command)."
fi

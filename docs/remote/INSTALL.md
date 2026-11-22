# Installing Celius on a device

Celius runs on your own computers too: a Windows PC, a Linux workstation, server or VM.
Install it on the device, sign the device in from its command line, and accept it in
Celius. Then you can use the device in two ways, both with local access to it:

- **From Celius, anywhere.** Start a chat in Celius (web, phone, desktop app), choose the
  device and a folder; the agent on the device does the work. The device's background agent
  must be running (the installers set that up).
- **On the device.** Run `celius` in a project folder, like any terminal coding agent. The
  chat is also in Celius, where you can follow it, answer its approvals or continue it.

The device connects **out** to your Celius server over HTTPS; nothing on it is opened to the
network and no SSH is involved. It needs outbound HTTPS to your Celius address and to PyPI
(for the installer's two small dependencies); the installers install Python 3.10+ if it is
missing.

## Quick install: one command on the device

Your Celius server serves the installers itself; the device needs nothing else (no
repository, no git). *Remote machines → Add a device* shows these with your address and
user name filled in:

**Windows** (PowerShell, as yourself, **not** as administrator):

```powershell
irm "https://celius.example.com/install.ps1?user=alice" | iex
```

**Linux:**

```bash
curl -fsSL "https://celius.example.com/install.sh?user=alice" | sudo bash
```

Each one installs Celius (and Python via winget or the package manager if there is none),
puts `celius` on the PATH, shows a sign-in code and waits. Accept the device in Celius (see
the next section); the installer then finishes and starts the background agent. `?user=`
makes the device wait in that user's list; without it, type the code in Celius.

| | Windows | Linux |
|---|---|---|
| Upgrade | `irm https://…/install.ps1?upgrade=1 \| iex` | `curl -fsSL https://…/install.sh \| sudo bash -s -- --upgrade` |
| Remove | `irm https://…/install.ps1?uninstall=1 \| iex` | `curl -fsSL https://…/install.sh \| sudo bash -s -- --uninstall` |
| Options | — | after `sudo bash -s --`: `--name`, `--run-as USER` (`root` for a dedicated container), `--no-service`, `--ca-file` |

The server fills in its own `APP_PUBLIC_URL` and serves the `celius` package it was built
with (`/install/celius_agent-<version>-py3-none-any.whl`). The sections below describe what
the installers do, and how to run them from a copy of the repository instead.

## Signing a device in

```
celius login --server https://celius.example.com --user alice
```

The installers run this for you. It shows a code such as `XV7C-3K7F` and waits:

```
  Sign-in code:  XV7C-3K7F

  In Celius, open Remote machines: this device waits there for alice.
  Or open: https://celius.example.com/#/devices?code=XV7C-3K7F
  Check the code matches, then accept the device. Waiting… (Ctrl+C to cancel)
```

In Celius, *Remote machines* shows a badge and the device under **Waiting for you to
accept**, with its name, operating system, address and code. Check the code is the one on
the device's screen and choose **Accept**. The device signs in at once.

- Without `--user`, the device waits in nobody's list: open the link it prints, or *Remote
  machines → Add a device*, and type the code.
- `--name` names the device (default: its hostname); you can also name it when accepting.
- The code is valid 15 minutes (`REMOTE_DEVICE_LOGIN_TTL_MINUTES`) and works once. *Refuse*
  ends the request; the device says so and stops waiting.
- The device keeps a secret it shows nobody; the code alone cannot sign anything in, and
  naming a user name that does not exist reveals nothing (the request just waits).
- `celius logout` signs the device out and removes it from your machines. Removing it in
  Celius has the same effect from the other side.

## Windows 10/11

Quick install: `irm https://celius.example.com/install.ps1 | iex` (above). From the
`services/remote-agent` folder of a copy of the repository instead, in PowerShell **as
yourself, not as administrator**:

```powershell
Set-ExecutionPolicy -Scope Process Bypass   # this window only
.\install\install-windows.ps1 -Server https://celius.example.com -CeliusUser alice
# -Name "Workstation", -CaFile C:\path\ca.pem for a private CA, -NoTask for no background agent
```

It creates a private Python environment in `%LOCALAPPDATA%\CeliusAgent\venv`, adds its
`Scripts` folder to your PATH (open a new terminal for `celius` to be found), signs the PC
in, and adds a scheduled task **“Celius Agent”** (`celius agent`) that starts at Windows
sign-in and restarts after a failure (once a minute, indefinitely). Files:

| Path | What |
|---|---|
| `%LOCALAPPDATA%\CeliusAgent\policy.json` | What Celius may do here |
| `%LOCALAPPDATA%\CeliusAgent\credential` | The device credential, encrypted with DPAPI for your Windows account |
| `%LOCALAPPDATA%\CeliusAgent\logs\agent.log` | Log (rotated, 3 × 5 MB) |
| `%LOCALAPPDATA%\CeliusAgent\state\` | Unsent events and the command journal |

Start/stop the background agent: `Start-ScheduledTask "Celius Agent"` /
`Stop-ScheduledTask "Celius Agent"`, or Task Scheduler. Upgrade:
`.\install\install-windows.ps1 -Upgrade` from a newer copy. Remove:
`.\install\install-windows.ps1 -Uninstall` (removes the program, the task and the PATH entry;
keeps the data folder: remove the device in Celius and delete the folder to remove
everything).

**Why a scheduled task and not a Windows service?** The agent should act as *you*: on your
folders, with a credential only your account can decrypt. A service would run as
LocalSystem (far more privilege than needed) or need your password stored. The consequence:
the background agent runs while you are signed in to Windows. To keep it running signed out,
edit the task in Task Scheduler → *Run whether user is logged on or not* (Windows then asks
for your password and stores it itself).

**Portable executable.** CI builds `celius.exe` (artifact `celius-windows`), a single file
with Python inside. Put it in a folder on your PATH and run `celius login --server …`; then
`celius` to chat, and a scheduled task for `celius.exe agent` (the settings above) to use the
PC from Celius. `run_python` then uses a Python installed on the PC, if any.

## Linux (desktop, server, VM)

Quick install: `curl -fsSL https://celius.example.com/install.sh | sudo bash` (above). From
a copy of the repository instead:

```bash
cd services/remote-agent
sudo ./install/install-linux.sh --server https://celius.example.com --celius-user alice
# --name NAME; --ca-file /path/ca.pem; --run-as USER to choose the account; --no-service
```

- Installs into `/opt/celius-agent` (a venv), links `/usr/local/bin/celius`, signs the device
  in, and installs a systemd unit `celius-agent.service` (`celius agent`) with
  `Restart=always` and sandboxing that keeps your own folders usable (`ProtectSystem=full`,
  `NoNewPrivileges`, `PrivateTmp`).
- Runs as **the user who ran sudo** (so it can work on that user's code, and `celius` run by
  that user shares the sign-in), or, when there is none (logged in as root, e.g. `pct enter`
  on a Proxmox container), as a dedicated `celius-agent` system user. `celius` typed by root
  then runs as that user, with its sign-in, so it can only work in folders `celius-agent` can
  read (not `/root`).
- **A container or VM that is just for Celius:** `--run-as root` lets it reach the whole
  machine (within the folders you allow). Re-running the installer with it moves an existing
  sign-in, so the device doesn't need accepting again:
  `curl -fsSL https://celius.example.com/install.sh | bash -s -- --run-as root`.
- `celius` is linked into `/usr/local/bin` and `/usr/bin` (root shells such as `pct enter`
  leave `/usr/local/bin` off the PATH).
- Device home: `~/.config/celius-agent/` of that user (`policy.json`, `credential` mode 0600
  in a 0700 folder, `state/`, `logs/`). `CELIUS_AGENT_HOME` or `celius --home` overrides it.
- Logs: `journalctl -u celius-agent -f` (and `logs/agent.log`).
- Upgrade: `sudo ./install/install-linux.sh --upgrade` from a newer copy. Remove:
  `--uninstall` (removes the program, the link and the unit; keeps the device home).
- Containers without systemd: `--no-service`, then start the printed command under your
  process supervisor.
- Already signed in? Re-running the installer keeps the existing sign-in.

**Without the installer** (any OS with Python): `pip install ./services/remote-agent`, then
`celius login --server …`. Use `celius agent` under your own supervisor for the background
agent, or just `celius` to chat.

## Chatting on the device: `celius`

```
cd ~/code/billing-app
celius                     # chat here
celius -p "why does the build fail?"   # one message, print the reply, exit
celius -c                  # continue the latest chat in this folder
celius --dir PATH --model NAME
```

- The first time in a folder outside the policy's `roots`, it asks whether Celius may work
  there, and whether it may run commands; your answers are saved in `policy.json`.
- The model, memory and tools are the ones you have in Celius; the tools act on this
  folder. Each change and command shows in the terminal and, unless your settings for the
  device say otherwise, asks first: answer `y`/`n` (or `a` to always allow that tool in this
  chat). Without a terminal (`-p` in a script), approvals wait for you in Celius.
- In the chat: `/new`, `/chats`, `/resume N`, `/open` (the link in Celius), `/help`,
  `/exit`. Ctrl+C stops the current reply.
- If the background agent is running, `celius` uses it; otherwise it runs the agent itself
  while it is open. The chat stays in Celius either way, marked *Started on the device*.

## The policy file

`policy.json` is the device owner's word. Celius can narrow it (per device, per session),
never widen it. Changes apply to the next session that opens; nothing needs restarting.

```json
{
  "roots": [
    {"path": "C:\\Users\\me\\code", "write": true},
    {"path": "D:\\datasets", "write": false}
  ],
  "allow_shell": true,
  "allow_write": true,
  "max_concurrent_commands": 2,
  "max_command_seconds": 3600,
  "max_output_chars": 1000000,
  "memory_limit_mb": null,
  "env_deny": ["AWS_SECRET_ACCESS_KEY"]
}
```

| Key | Default | Meaning |
|---|---|---|
| `roots` | `[]` | Folders sessions may use. A session's folder must be inside one; every path is resolved (symlinks followed) and must stay inside the session's folder. `write: false` makes a root read-only. `celius` adds a folder here when you allow it. |
| `allow_shell` | `false` | Shell commands, Python and git. Without it only list/read/write files work. |
| `allow_write` | `true` | Creating and changing files. |
| `max_concurrent_commands` | 2 | Commands running at once on this device. |
| `max_command_seconds` | 3600 | Upper bound for any command's timeout. |
| `max_output_chars` | 1 000 000 | Output kept per stream; 20 times that stops the command. |
| `memory_limit_mb` | none | Linux/macOS: address-space limit per command. |
| `env_deny` | `[]` | Environment variables never passed to commands (the agent's own are never passed). |

In Celius (*Remote machines → the device*) you can additionally switch off commands or
writing, and choose whether every change and command asks first (default) or the chat's
tool profile decides.

## Updating

There is no self-updater: a program that replaces its own code from the network is a
software-supply risk, so updates are explicit. After upgrading the Celius server, run the
upgrade command from the table above on each device (it installs the package the server
now serves), or re-run a repository copy of the installer with `-Upgrade` / `--upgrade`. Celius shows each device's version; the protocol is
versioned (`protocol: 1`) so older agents keep working with a newer server.

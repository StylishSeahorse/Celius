# Installing the Celius agent on a machine

The agent lets Celius work on one of your computers: a Windows PC, a Linux workstation,
server or VM. It connects **out** to your Celius server over HTTPS; nothing on the machine
is opened to the network, and no SSH is involved. Requirements: Python 3.10 or newer (or the
portable `celius-agent.exe` on Windows) and outbound HTTPS to your Celius address.

Every machine goes through the same three steps:

1. **Pair.** In Celius: *Remote machines → Add a machine*. You get a one-time pairing code
   (valid 15 minutes) and the commands below with it filled in.
2. **Allow.** On the machine, list in its `policy.json` the folders Celius may use. Nothing
   is allowed until you do, and commands stay off unless you turn them on.
3. **Authorise.** Back in Celius, the machine shows *Awaiting your approval*. Choose
   *Authorise this machine*. Until then it gets no work.

## Windows 10/11

From the `services/remote-agent` folder of the Celius repository (or a copy of it), in
PowerShell **as yourself, not as administrator**:

```powershell
Set-ExecutionPolicy -Scope Process Bypass   # this window only
.\install\install-windows.ps1 -Server https://celius.example.com -Code ABCD-EFGH-JKLM
# add -Claude for Claude Code sessions, -CaFile C:\path\ca.pem for a private CA
```

It creates a private Python environment in `%LOCALAPPDATA%\CeliusAgent\venv`, registers the
PC, and adds a scheduled task **“Celius Agent”** that starts at sign-in and restarts after a
failure (once a minute, indefinitely). Files:

| Path | What |
|---|---|
| `%LOCALAPPDATA%\CeliusAgent\policy.json` | What Celius may do here (edit this) |
| `%LOCALAPPDATA%\CeliusAgent\credential` | The machine credential, encrypted with DPAPI for your Windows account |
| `%LOCALAPPDATA%\CeliusAgent\logs\agent.log` | Log (rotated, 3 × 5 MB) |
| `%LOCALAPPDATA%\CeliusAgent\state\` | Unsent events and the command journal |

Start/stop: `Start-ScheduledTask "Celius Agent"` / `Stop-ScheduledTask "Celius Agent"`, or
Task Scheduler. Upgrade: `.\install\install-windows.ps1 -Upgrade` from a newer copy.
Remove: `.\install\install-windows.ps1 -Uninstall` (keeps the data folder; revoke the machine
in Celius and delete the folder to remove everything).

**Why a scheduled task and not a Windows service?** The agent should act as *you*: on your
folders, with your Claude Code sign-in, with a credential only your account can decrypt.
A service would run as LocalSystem (far more privilege than needed) or need your password
stored. The consequence: the agent runs while you are signed in. To keep it running signed
out, edit the task in Task Scheduler → *Run whether user is logged on or not* (Windows then
asks for your password and stores it itself).

**Portable executable.** CI builds `celius-agent.exe` (artifact `celius-agent-windows`), a
single file with Python inside. Put it in a folder of your choice and run
`celius-agent.exe register --server … --code …`, then add a scheduled task for
`celius-agent.exe run` (the same settings as above). `run_python` then uses a Python
installed on the PC, if any.

## Linux (desktop, server, VM)

```bash
cd services/remote-agent
sudo ./install/install-linux.sh --server https://celius.example.com --code ABCD-EFGH-JKLM
# --claude for Claude Code; --ca-file /path/ca.pem; --user NAME to choose the account
```

- Installs into `/opt/celius-agent` (a venv) and a systemd unit `celius-agent.service` with
  `Restart=always` and sandboxing that keeps your own folders usable (`ProtectSystem=full`,
  `NoNewPrivileges`, `PrivateTmp`).
- Runs as **the user who ran sudo** (so it can work on that user's code), or, when there is
  none, as a dedicated `celius-agent` system user. Never as root.
- Agent home: `~/.config/celius-agent/` of that user (`policy.json`, `credential` mode 0600
  in a 0700 folder, `state/`, `logs/`).
- Logs: `journalctl -u celius-agent -f` (and `logs/agent.log`).
- Upgrade: `sudo ./install/install-linux.sh --upgrade` from a newer copy. Remove:
  `--uninstall` (keeps the agent home).
- Containers without systemd: `--no-service`, then start the printed command under your
  process supervisor.

## The policy file

`policy.json` is the machine owner's word. Celius can narrow it (per machine, per session),
never widen it. Restart the agent after editing.

```json
{
  "roots": [
    {"path": "C:\\Users\\me\\code", "write": true},
    {"path": "D:\\datasets", "write": false}
  ],
  "allow_shell": true,
  "allow_write": true,
  "adapters": ["celius", "claude_code"],
  "max_concurrent_commands": 2,
  "max_command_seconds": 3600,
  "max_output_chars": 1000000,
  "memory_limit_mb": null,
  "env_deny": ["AWS_SECRET_ACCESS_KEY"]
}
```

| Key | Default | Meaning |
|---|---|---|
| `roots` | `[]` | Folders sessions may use. A session's folder must be inside one; every path is resolved (symlinks followed) and must stay inside the session's folder. `write: false` makes a root read-only. |
| `allow_shell` | `false` | Shell commands, Python and git. Without it only list/read/write files work. |
| `allow_write` | `true` | Creating and changing files. |
| `adapters` | `["celius"]` | Agent types: `celius` (Celius's own agent) and `claude_code`. |
| `max_concurrent_commands` | 2 | Commands running at once on this machine. |
| `max_command_seconds` | 3600 | Upper bound for any command's timeout. |
| `max_output_chars` | 1 000 000 | Output kept per stream; 20 times that stops the command. |
| `memory_limit_mb` | none | Linux/macOS: address-space limit per command. |
| `env_deny` | `[]` | Environment variables never passed to commands (the agent's own are never passed). |

In Celius (*Remote machines → the machine*) you can additionally switch off commands or
writing, choose whether every change and command asks first (default) or the chat's tool
profile decides, and allow or block Claude Code.

## Claude Code sessions

1. Install with Claude Code support (`-Claude` / `--claude`). This installs the official
   Claude Agent SDK, which includes the Claude Code CLI.
2. Sign Claude Code in **as the agent's user** once: run `claude` in a terminal and log
   in, or set `ANTHROPIC_API_KEY` for the agent (task/service environment).
3. Add `"claude_code"` to `adapters` in `policy.json` and restart the agent.

Celius then offers *Claude Code* when you start a chat on that machine. Claude Code runs
there with `permission_mode=default`: whatever your own Claude Code settings don't already
allow comes to Celius as an approval request, on any device. Its project/user settings and
`CLAUDE.md` files apply as usual.

## Updating

There is no self-updater: an agent that replaces its own code from the network is a
software-supply risk, so updates are explicit. Re-run the installer with `-Upgrade` /
`--upgrade` from the new version. Celius shows each machine's agent version; the protocol
is versioned (`protocol: 1`) so older agents keep working with a newer server.

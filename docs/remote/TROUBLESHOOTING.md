# Celius Remote troubleshooting

Start with `celius status` on the device (sign-in, policy, whether the background agent is
running, and whether Celius accepts its credential) and the agent log
(`journalctl -u celius-agent -f` on Linux, `%LOCALAPPDATA%\CeliusAgent\logs\agent.log` on
Windows).

| Symptom | Likely cause | Fix |
|---|---|---|
| `celius login`: the device never shows in *Waiting for you to accept* | No `--user`, a misspelt user name, or signed in to Celius as someone else. | Open the link `celius login` printed, or *Remote machines → Add a device*, and type the code. |
| `celius login`: *The sign-in expired* | Not accepted within 15 minutes (`REMOTE_DEVICE_LOGIN_TTL_MINUTES`). | Run `celius login` again. |
| `celius login`: *The sign-in was refused in Celius* | Someone chose *Refuse*. | Run it again and accept it. |
| `celius login`: *Too many sign-in requests from this address* | More than 10 sign-ins from one address in an hour, or 10 devices already waiting for that user. | Accept or refuse the waiting ones; try again later. |
| Accepting: *No device is waiting with this code* | Expired, already accepted, or mistyped. | Check the code on the device's screen; start again if it expired. |
| `celius login`: *Refusing plain http://* | The server address is `http://` and not localhost. | Use the HTTPS address. On a trusted LAN only: `--allow-insecure-http`. |
| `celius login`: certificate errors | Private CA or self-signed certificate. | `--ca-file /path/to/ca.pem`. |
| `celius: command not found` after installing (as root, e.g. in `pct enter`) | Installed before `celius` was also linked into `/usr/bin`; that shell's PATH lacks `/usr/local/bin`. | `/usr/local/bin/celius`, or run the install command again with `--upgrade`. |
| `celius`: *… can't be read by 'celius-agent'* | Installed as root, so the agent runs as the `celius-agent` user, which can't read e.g. `/root`. | Work in a folder it can read, or install again with `--run-as root` on a machine that is just for Celius (keeps the sign-in). |
| `celius`: *This device isn't signed in* | No sign-in in this device home. On Linux the service's user may differ from yours. | `celius login …`, or run `celius` as the agent's user (the installer prints the command). |
| `celius`: *Celius may not work in …* | You declined the folder, or it's outside `roots`. | Answer yes when asked, add it to `roots` in `policy.json`, or start `celius` in an allowed folder. |
| `celius -p` waits after a tool call | No terminal to ask in, so the approval waits in Celius. | Answer it in Celius, or run without `-p`. |
| Log: *rejected this machine's credential* | Removed or revoked in Celius, or the credential file was replaced. | `celius login --force` and accept it again. |
| Log: *Another agent with this machine's identity connected* | The same device home is running twice (two tasks/services, or a copied home folder). | Stop the duplicate; never copy a device home to another machine — sign each one in. `celius` uses a running background agent instead of starting its own. |
| Machine stays **Disconnected** | No outbound HTTPS, a proxy, or the reverse proxy doesn't pass WebSockets. | `curl -I https://celius…/api/v1/health/live` from the machine; the agent honours `HTTPS_PROXY`; see reverse-proxy.md for the WebSocket location. A 4xx/5xx on connect in the log points at the proxy. |
| Machine flips Online/Disconnected | Proxy idle timeout shorter than the agent's 20 s pings. | Raise the proxy's read timeout for the WebSocket path (≥ 120 s). |
| Machine shows **Offline** after a `celius` chat | No background agent: `celius` ran the agent only while it was open. | Start the background agent (scheduled task / service, or `celius agent`) to use the machine from Celius while away. |
| Session: *Couldn't start: … not inside a folder this machine allows* | The folder isn't under a `roots` entry of `policy.json`. | Add the folder (or a parent) to `roots`, then *Open again* (the policy is re-read when a session opens). |
| *Commands are not allowed in this session* although they are turned on | Agent before 0.3.1, or Celius before this fix: a session kept the permissions it had when it opened. | Upgrade the server and the device (`--upgrade`); open sessions then follow the current settings. |
| *Permission denied: … Celius runs as 'celius-agent'* | The agent's user can't write there (e.g. a root-owned `/opt/app/config`). | Give that user access (`chown`/`chmod`, or a group), or install with `--run-as root` on a machine that is just for Celius. |
| Tools missing in a remote chat (no `run_shell` …) | Commands are off: `allow_shell` false in `policy.json`, or *Allow commands* off in Celius, or the session limited them. | Turn them on where appropriate. Celius only narrows the machine's own policy. |
| Every command asks for approval | Default for real machines. | Machine settings in Celius → *Follow the chat's tool profile*, then choose a profile for the chat. |
| Tool result *… was offline, so this was not run* | The machine was away for `REMOTE_OFFLINE_WAIT_SECONDS`. The command expired and will never run later. | Ask again when it is back. |
| *This call was interrupted (the worker restarted) …* | The Celius worker restarted mid-call. The machine was told to stop it; it is never re-run automatically. | Check its effects, then ask again. |
| Sending in a chat: *session on its machine has ended* | The session was ended or failed. Celius doesn't silently move the chat back to the sandbox. | *Open again* in the session bar. |
| Non-ASCII output garbled on Windows | A program writing the console code page. | Python and PowerShell are set to UTF-8 by the agent; for other tools use their UTF-8 option (e.g. `chcp 65001` inside the command). |
| `celius` not found on Windows after installing | The PATH change applies to new terminals. | Open a new terminal (or sign out and in). |
| Download: *too large* | Over `REMOTE_FILE_FETCH_MAX_BYTES` (8 MB). | Fetch it another way, or raise the setting (≤ 10 MB). |

## Inspecting state

```sql
-- machines and their connections
SELECT name, status, connection_id IS NOT NULL AS connected, last_seen_at, disconnect_reason
FROM remote_agents ORDER BY name;
-- devices waiting to be accepted
SELECT user_code, name, ip_address, status, expires_at FROM remote_device_requests
WHERE status = 'pending' ORDER BY created_at;
-- open sessions
SELECT id, origin, working_directory, status, last_seq FROM remote_sessions
WHERE status NOT IN ('failed', 'terminated');
-- what is waiting for a machine
SELECT kind, status, created_at, expires_at FROM remote_commands
WHERE status IN ('pending', 'delivered', 'running') ORDER BY created_at;
```

On the machine, `state/outbox/<session>.jsonl` holds events not yet acknowledged by Celius
(normally empty or short) and `state/commands/` the command journal.

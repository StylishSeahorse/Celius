# Celius Remote troubleshooting

Start with `celius-agent status` on the machine (configuration, policy, and whether Celius
accepts its credential) and the agent log (`journalctl -u celius-agent -f` on Linux,
`%LOCALAPPDATA%\CeliusAgent\logs\agent.log` on Windows).

| Symptom | Likely cause | Fix |
|---|---|---|
| `register`: *pairing code is invalid or has expired* | Codes last 15 minutes and work once. | Create a new code. |
| `register`: *Refusing plain http://* | The server address is `http://` and not localhost. | Use the HTTPS address. On a trusted LAN only: `--allow-insecure-http`. |
| `register`: certificate errors | Private CA or self-signed certificate. | `--ca-file /path/to/ca.pem`. |
| Log: *Waiting for this machine to be authorised* | Pending in Celius. | *Remote machines → the machine → Authorise this machine*. |
| Log: *rejected this machine's credential* | Revoked, removed, or the credential file was replaced. | Register again with a new pairing code. |
| Log: *Another agent with this machine's identity connected* | The same agent home is running twice (two tasks/services, or a copied home folder). | Stop the duplicate; never copy an agent home to another machine — register each machine. |
| Machine stays **Disconnected** | No outbound HTTPS, a proxy, or the reverse proxy doesn't pass WebSockets. | `curl -I https://celius…/api/v1/health/live` from the machine; the agent honours `HTTPS_PROXY`; see reverse-proxy.md for the WebSocket location. A 4xx/5xx on connect in the log points at the proxy. |
| Machine flips Online/Disconnected | Proxy idle timeout shorter than the agent's 20 s pings. | Raise the proxy's read timeout for the WebSocket path (≥ 120 s). |
| Session: *Couldn't start: … not inside a folder this machine allows* | The folder isn't under a `roots` entry of `policy.json`. | Add the folder (or a parent) to `roots`, restart the agent, *Open again*. |
| Session: *Couldn't start: … doesn't allow claude_code* | `adapters` in `policy.json` lacks `claude_code`. | Add it and restart. |
| *Claude Code could not start: claude-agent-sdk is not installed* | Installed without Claude support. | Re-run the installer with `-Claude` / `--claude`. |
| Claude Code turn fails at once (auth) | Claude Code isn't signed in for the agent's user. | Run `claude` once as that user and sign in, or set `ANTHROPIC_API_KEY` in the task/service environment. |
| Tools missing in a remote chat (no `run_shell` …) | Commands are off: `allow_shell` false in `policy.json`, or *Allow commands* off in Celius, or the session limited them. | Turn them on where appropriate. Celius only narrows the machine's own policy. |
| Every command asks for approval | Default for real machines. | Machine settings in Celius → *Follow the chat's tool profile*, then choose a profile for the chat. |
| Tool result *… was offline, so this was not run* | The machine was away for `REMOTE_OFFLINE_WAIT_SECONDS`. The command expired and will never run later. | Ask again when it is back. |
| *This call was interrupted (the worker restarted) …* | The Celius worker restarted mid-call. The machine was told to stop it; it is never re-run automatically. | Check its effects, then ask again. |
| Sending in a chat: *session on its machine has ended* | The session was ended or failed. Celius doesn't silently move the chat back to the sandbox. | *Open again* in the session bar (same or other machine). |
| Non-ASCII output garbled on Windows | A program writing the console code page. | Python and PowerShell are set to UTF-8 by the agent; for other tools use their UTF-8 option (e.g. `chcp 65001` inside the command). |
| Download: *too large* | Over `REMOTE_FILE_FETCH_MAX_BYTES` (8 MB). | Fetch it another way, or raise the setting (≤ 10 MB). |

## Inspecting state

```sql
-- machines and their connections
SELECT name, status, connection_id IS NOT NULL AS connected, last_seen_at, disconnect_reason
FROM remote_agents ORDER BY name;
-- open sessions
SELECT id, mode, adapter, working_directory, status, last_seq FROM remote_sessions
WHERE status NOT IN ('failed', 'terminated');
-- what is waiting for a machine
SELECT kind, status, created_at, expires_at FROM remote_commands
WHERE status IN ('pending', 'delivered', 'running') ORDER BY created_at;
```

On the machine, `state/outbox/<session>.jsonl` holds events not yet acknowledged by Celius
(normally empty or short) and `state/commands/` the command journal.

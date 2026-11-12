# Celius Remote Agent

Runs Celius work on this machine — Celius's own agent's tools, or a Claude Code session —
for conversations you start in Celius on any device. It connects **out** to your Celius
server (HTTPS + one WebSocket); it opens no port and needs no SSH.

- Install: [docs/remote/INSTALL.md](../../docs/remote/INSTALL.md)
  (`install/install-windows.ps1`, `install/install-linux.sh`)
- How it works: [docs/remote/ARCHITECTURE.md](../../docs/remote/ARCHITECTURE.md)
- Protocol: [docs/remote/API.md](../../docs/remote/API.md)

```
celius-agent register --server https://celius.example --code ABCD-EFGH-JKLM
celius-agent run          # foreground (the service runs this)
celius-agent status       # configuration, policy, and whether Celius accepts it
celius-agent policy       # what this machine allows
celius-agent rotate-credential
```

Python ≥ 3.10; one dependency (`websockets`). Extras: `monitor` (psutil), `claude`
(the Claude Agent SDK, which includes the Claude Code CLI).

| Module | Role |
|---|---|
| `cli.py` | Commands, logging, single-instance lock |
| `config.py` | Agent home, config and the machine's policy file |
| `credentials.py` | Credential storage (DPAPI on Windows, 0600 elsewhere) |
| `connection.py` | Token exchange, WebSocket session, heartbeats, outbox sending, reconnect with backoff |
| `runtime.py` | Commands (journalled, never run twice), sessions, tool execution |
| `state.py` | Per-session event outbox and command journal on disk |
| `tools.py` | Native tools, confined to the session folder |
| `paths.py` | Path checks (roots, symlinks, traversal) |
| `process.py` | Processes: streaming output, timeouts, process-tree kill |
| `monitor.py` | Machine facts and resource snapshots |
| `adapters/claude_code.py` | Claude Code via the Agent SDK; SDK messages → canonical events |

Development: `uv sync && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

# Celius on a device

The `celius` command and agent for your own computers (Windows, Linux). Sign the device in
from here and accept it in Celius; then either chat on the device with `celius`, or let
Celius use it from anywhere through the background agent. It connects **out** to your
Celius server (HTTPS + one WebSocket); it opens no port and needs no SSH.

- Install: [docs/remote/INSTALL.md](../../docs/remote/INSTALL.md)
  (`install/install-windows.ps1`, `install/install-linux.sh`)
- How it works: [docs/remote/ARCHITECTURE.md](../../docs/remote/ARCHITECTURE.md)
- Protocol and API: [docs/remote/API.md](../../docs/remote/API.md)

```
celius login --server https://celius.example --user alice   # shows a code; accept it in Celius
celius                    # chat in this folder (-p "…" for one message, -c to continue)
celius agent              # the background agent: Celius can use this device (the service runs this)
celius status             # sign-in, policy, and whether Celius accepts this device
celius policy             # what this device allows
celius rotate-credential
celius logout             # sign out and remove the device from Celius
```

`celius-agent` is the same program under its earlier name; `celius run` is `celius agent`.

Python ≥ 3.10; one dependency (`websockets`). Extra: `monitor` (psutil, for CPU/memory/disk
figures).

| Module | Role |
|---|---|
| `cli.py` | Commands, logging, single-instance lock, the in-process agent for `celius` |
| `login.py` | Device sign-in: shows the code, waits for you to accept it in Celius |
| `client.py` | The device API (`/api/v1/remote/device/…`) with the device's own token |
| `chat.py` | `celius` in the terminal: the chat, approvals, folder trust |
| `config.py` | Device home, config and the device's policy file |
| `credentials.py` | Credential storage (DPAPI on Windows, 0600 elsewhere) |
| `connection.py` | Token exchange, WebSocket session, heartbeats, outbox sending, reconnect with backoff |
| `runtime.py` | Commands (journalled, never run twice), sessions, tool execution |
| `state.py` | Per-session event outbox and command journal on disk |
| `tools.py` | Native tools, confined to the session folder |
| `paths.py` | Path checks (roots, symlinks, traversal) |
| `process.py` | Processes: streaming output, timeouts, process-tree kill |
| `monitor.py` | Device facts and resource snapshots |

Development: `uv sync && uv run pytest -q && uv run ruff check . && uv run ruff format --check .`

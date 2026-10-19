# ADR 0008: Sandbox, tool policy and approvals

Status: accepted · 2026-10-19

## Context

From Phase 5 the model can call tools: run Python and shell commands, read and write
files, use Git, fetch web pages, make images and documents, and use MCP servers.
Everything the model asks for has to be treated as written by an attacker. A model
with tools can be steered by a prompt injection hidden in a web page, an attachment, a
repository or a tool result.

The deployment is one Proxmox LXC running Docker Compose (ADR 0001, deployment guide).
Inside an LXC, gVisor (runsc) and Firecracker (KVM) usually aren't available without
changing the Proxmox host. Unprivileged user namespaces may or may not be allowed,
depending on the host, the LXC's nesting setting and Docker's seccomp profile.

## Threat model

| Asset | Threat | Control |
| --- | --- | --- |
| Database, sessions, `APP_SECRET_KEY`, model API key | Code reads env, files or sockets of the app | Code runs in a **separate container** that has none of them: no env file, no app-data volume, no route to Postgres/Redis/API |
| The host and Docker | Escape via the Docker socket or privileges | **No Docker socket anywhere in the stack**; non-root, `cap_drop: ALL`, `no-new-privileges`, read-only root |
| The LAN and internet | Exfiltration, SSRF against the router or llama.cpp, scanning | Sandbox on an `internal: true` network: no egress at all. Web access only through the worker's HTTP tool, which has an SSRF guard and an allowlist |
| Other workspaces | One conversation reads another's files | One workspace directory per conversation. Under bubblewrap, only that one is mounted. Ids are validated, and the worker resolves every path inside the workspace (no `..` or symlink escapes) |
| Availability | Fork bombs, memory or disk exhaustion, infinite loops, output floods | Container pid/memory/CPU limits; per-run rlimits (CPU, address space, file size, processes, files); wall-clock timeout; output caps; workspace size check; concurrency limit |
| The user's intent | The model does something the user wouldn't approve | A server-side policy on every call, approvals bound to exact arguments, and audit (below) |

Out of scope: a kernel exploit from inside the container. Mitigations are reduced
kernel surface (seccomp default profile, no capabilities, optional bubblewrap) and
keeping the Proxmox host patched. If stronger isolation is needed later, the sandbox
interface (one HTTP endpoint) lets gVisor or a microVM runner replace it without other
changes.

## Decisions

### A dedicated sandbox container, no Docker socket
`services/sandbox` is a small FastAPI service with one working endpoint,
`POST /v1/executions`. It runs a Python or shell snippet in a named workspace and
returns its exit code, capped output and changed files. It authenticates the worker
with a shared bearer token (`SANDBOX_TOKEN`). It does not spawn containers, so it needs
no Docker socket: rejected because socket access is root on the host.

### Layered isolation inside it
1. **Container** (always): internal network, no secrets, read-only root, non-root uid,
   all capabilities dropped, pids/memory/CPU limits, `/tmp` as a size-limited tmpfs.
2. **bubblewrap** (`SANDBOX_MODE=auto` uses it when it works): per execution, new
   network, PID, IPC and UTS namespaces; the filesystem read-only; private `/tmp`; the
   workspaces root hidden behind an empty tmpfs; and only this execution's workspace
   bound at `/home/workspace`. Docker's default seccomp profile blocks the namespace
   calls bubblewrap needs, so it is off in a stock deployment. The deployment guide
   explains how to enable it (a seccomp profile that allows `clone`/`unshare` with
   user namespaces) for those who want the extra layer.
3. **Process limits** (always): rlimits for CPU seconds, address space, file size,
   process and file counts; a wall-clock timeout; output captured with a cap (a flood is
   killed); a fresh session killed as a whole; a scrubbed environment. The service is a
   child subreaper, so daemonised children (`setsid`, `nohup … &`) are reparented to it
   and killed when the execution ends.

Considered and rejected for now: gVisor and Firecracker need host changes on Proxmox.
Rootless Docker/Podman inside the stack needs a socket or nested privileges.

### Network off by default
The sandbox has no network. Code can't `pip install`; a fixed set of libraries
(numpy, pandas, matplotlib, Pillow, python-docx, openpyxl, python-pptx, fpdf2) comes
with the image. Web access is a separate tool, `web_fetch`, run by the worker. It
allows only http(s) on ports 80/443. It resolves the host and refuses private,
loopback, link-local, CGNAT and multicast addresses, also after redirects. Each
redirect is checked and the connection is pinned to the checked address. Responses
are size-capped. An optional host allowlist (`TOOL_WEB_ALLOWLIST`) narrows it
further.

### The server decides, every call
Each tool has a risk class (`read`, `write`, `execute`, `network`, `external`). A
**tool profile** maps every tool to `allow`, `ask` or `deny`. A conversation uses one
profile (default from user settings). There are four built-in profiles:

| Profile | Reads | Writes files | Python/shell/Git | Web | MCP |
| --- | --- | --- | --- | --- | --- |
| `read_only` | allow | deny | deny | ask | deny |
| `standard` | allow | ask | ask | ask | ask |
| `developer` | allow | allow | allow | ask | ask |
| `administrator` | allow | allow | allow | allow | allow |

The model can't choose or change the profile. It is never told anything that makes a
call bypass the check. Disabled or unknown tools are not offered to the model, and a
call to one is refused anyway.

### Approvals are bound to exact arguments
When a call needs approval, the run pauses (`awaiting_approval`) and frees its worker
slot. The pending call stores the canonical JSON of its arguments and their SHA-256.
Approving requires that hash. A client that shows the user one thing and approves
another fails, and any change to the arguments means a new approval. Approvals expire
(`TOOL_APPROVAL_TTL_MINUTES`). An expired or denied call is reported to the model as
refused, and the run continues. "Always allow <tool>" stores a grant scoped to
the conversation and that tool. "Allow all for this chat" stores a grant for every tool
in that conversation and approves the calls already waiting there. Neither crosses
conversations, and neither overrides a profile's deny. The chat shows a banner while
"allow all" is on, with "Ask again" to remove it.

A call identical to one that already failed in the same reply is refused without running,
until something changes (a successful call that writes, runs code or uses the network).
The model is also told which tools the chat's profile leaves out, so it asks the user to
change the setting instead of working around it.

### Limits on the loop
Per run: tool iterations (`TOOL_MAX_ITERATIONS`), total tool wall-clock
(`TOOL_MAX_RUN_SECONDS`), per-call timeout, and the size of each result fed back to the
model (`TOOL_MAX_RESULT_CHARS`). When a limit is hit, the model is told and asked to
answer with what it has. Cancelling a run cancels the in-flight sandbox request, and
closing that request kills the code.

### Health
Only the worker can reach the sandbox, so the worker records the sandbox's health (mode,
limits) on the app-data volume every minute, and the API shows it in settings.

### Audit
Every tool call is a `tool_calls` row: tool, arguments, decision and its source
(policy, user, grant, expired), timings and result summary. Policy decisions, approvals,
denials and profile/MCP/schedule changes also go to `audit_events`.

### MCP: registered by the administrator only
MCP servers (streamable HTTP) are added through the settings API, never by the model
or a tool result. Their credentials are stored encrypted (Fernet, key derived from
`APP_SECRET_KEY`). Their tools are namespaced `mcp__<server>__<tool>`, have risk class
`external`, and go through the same policy. Hosts are checked by the same SSRF guard
unless the administrator marks the server as on the LAN.

### Scheduled tasks
A scheduled task is a stored prompt that starts a new run in its own conversation on a
schedule: every N minutes (at least 15), daily or weekly, in the user's time zone. The
worker's scheduler claims due tasks with the same conditional update as jobs, so two
workers never fire one twice. A task runs with its chosen profile; calls that would
ask the user pause it, like any run. Quotas: active tasks per user, and the run and
storage quotas that already apply.

## Consequences

- Without bubblewrap (the default inside an LXC), all executions share the sandbox
  container's view of the filesystem: code from one conversation could read another
  conversation's workspace. All workspaces belong to the one user, so this is accepted;
  the container is the boundary that protects the app, the host and the network.
- The workspaces volume is shared between the worker and the sandbox (same uid). The
  worker never follows symlinks out of a workspace when reading or attaching files.
- Adding a heavier runner later (gVisor) means a new sandbox image, not new app code.

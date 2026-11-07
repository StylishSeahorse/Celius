# Celius

A self-hosted AI workspace in the style of Claude.ai, for a household or a small team.
Conversations, projects, files and memories live on your own server, sync across the web,
an installable Android app and a Windows app, and are answered by your own model running
on llama.cpp. The model can use tools: it runs code in an isolated sandbox, works with
files, searches the web and calls MCP servers, always within the permissions you set.

- **Server:** FastAPI, PostgreSQL (pgvector) and a background worker, in Docker on a
  Proxmox LXC container
- **Model:** llama.cpp (`llama-server`, OpenAI-compatible) on a separate GPU machine
- **Clients:** web, Android (PWA), Windows (Tauri v2)

**Status:** phases 1–10 of the [backlog](docs/backlog.md) are built and pass CI (API
tests on SQLite and PostgreSQL, browser tests on desktop and phone viewports, Windows
installer build, and a clean-deployment release gate). What is left is checking it on the
real hardware: see [Not yet verified](#not-yet-verified).

## Contents

- [Features](#features)
- [How it fits together](#how-it-fits-together)
- [Install on a server](#install-on-a-server)
- [Configuration](#configuration)
- [Clients](#clients)
- [Development](#development)
- [Tests and CI](#tests-and-ci)
- [Operations](#operations)
- [Security](#security)
- [Repository layout](#repository-layout)
- [Documentation](#documentation)
- [Not yet verified](#not-yet-verified)

## Features

### Chat
- Streaming replies that survive a page reload, a dropped connection or a worker restart
  (they resume from the last event)
- Markdown with code blocks you can copy or download
- Edit a message or regenerate a reply, then switch between the versions
- Stop a reply and keep what it wrote so far
- Thinking on, off or the model's default, per chat
- Pick the model per chat
- Reasoning, tool calls and what the model said between them fold into one panel above
  the answer ("Used 3 tools"), which shows live progress while it works ("Running
  python…"); requests for your approval stay outside it so you can't miss them
- The model is told today's date

### Sync, offline and devices
- Every device sees new messages, renames and deletions without reloading
- Offline: saved chats open, and messages written offline are queued and sent exactly
  once when you reconnect
- Conflicting renames from two devices ask which version to keep
- Notifications on phone and desktop when a reply finishes, fails or waits for approval
  (not for the chat you're looking at)
- Share text, links, photos and files from other Android apps into a new chat
- Windows app: native notifications, closes to the tray, attaches files from folders you
  approve on the PC

### Files
- Attach files and whole folders: text and code, PDF, Word, Excel, PowerPoint, ZIP,
  images, audio and video
- Every upload is checked from its bytes, not its name (a renamed executable is refused),
  and archives are checked for bombs and unsafe paths
- Large files upload in 4 MB pieces and resume after a dropped connection; audio and
  video can be up to 2 GB (`MAX_MEDIA_UPLOAD_BYTES`)
- Documents are read for the model; long ones are sent as their beginning plus the
  passages most relevant to the question, with the full text available to tools
- Images go to vision models
- Previews: images, the first page of PDFs, Office files' embedded preview, a still frame
  of videos
- Audio and video play in the chat, with seeking; the model is told what they are
  (duration, codecs) and tools can process them with ffmpeg. There is no transcription
  yet
- Per-person storage quotas
- Export a conversation (Markdown or JSON) or a whole project (ZIP)

### Projects
- Group chats into projects with their own instructions and knowledge files, given to
  every chat in the project
- Share a project with other people on the server, as "can use" or "can edit"; their
  chats and memories stay private

### Tools
The model can call tools in the conversation's private workspace:

| Tool | What it does |
| --- | --- |
| `list_files`, `read_file`, `write_file` | Work with files in the workspace |
| `run_python`, `run_shell` | Run code in the sandbox (ffmpeg, ImageMagick, pandoc, Node, gcc, scientific and document Python libraries) |
| `git`, `git_clone` | Use Git in the workspace; clone a repository |
| `web_search` | Search the web through [Exa](https://exa.ai): titles, links, dates and the passages most relevant to the question |
| `web_fetch` | Read a web page or download a file |
| `image` | Inspect, resize, crop, rotate, greyscale or convert images |
| `make_document` | Create Word, PDF, Excel, CSV, PowerPoint, Markdown or HTML files |
| `attach_file` | Hand a workspace file to you in the chat |
| `remember`, `search_memory` | Propose or correct a memory (kind, importance, what it is about); search memories by kind or subject, and earlier chats |
| `calendar_events`, `calendar_add`, `calendar_change` | Read the calendar, find free time, add or change events |
| `email_search`, `email_read`, `email_draft`, `email_send` | Search and read mail, write drafts; sending asks every time |
| `automation_create`, `automation_list` | Set up rules that run on their own |
| `task_add`, `task_list`, `task_update`, `daily_briefing` | Manage your task list and plan your day |
| `plan_start`, `plan_update`, `plan_status` | Record and update the plan of a multi-step task (kept across replies) |
| `skill_search`, `skill_read`, `skill_create`, `skill_update`, `skill_validate`, `skill_test`, `skill_publish`, `skill_deprecate`, `skill_history` | Find and follow saved procedures; save and improve them as candidates; test them in the sandbox; ask to publish (your trust setting decides) |
| MCP servers | Tools from MCP servers an administrator registers (streamable HTTP) |

- **Permission profiles** decide what each tool may do: Read only, Standard, Developer,
  Administrator, or your own. Each tool is allowed, asked about or denied by its risk
  (reads, changes files, runs code, uses the web, external service)
- **Approvals** are bound to the exact arguments shown. "Allow once", "Always allow this
  tool in this chat" or "Allow all for this chat"; a profile's "deny" always wins. An
  approvals inbox lists everything waiting
- **Limits:** tool steps per reply (`TOOL_MAX_ITERATIONS`, 25), time per call and per
  reply, output size; a call that already failed isn't retried unchanged
- Tool calls the model writes as text instead of a proper call are recovered, or removed
  with a note: they never show up in the chat as raw markup
- **Scheduled tasks:** run a prompt every N minutes, daily or weekly
- **Activity panel:** every run and background job, with its status

### Memory
- Celius suggests lasting facts about you after a conversation; you approve, edit or
  reject them in a review queue (or switch review off)
- A memory is given to a chat only when it is relevant to your message. Mark a few as
  **Always use** (your name, how you like answers) to give them to every chat
- Recall of excerpts from your earlier chats, within the same project
- Each reply shows which memories and chats it used
- Off switches per person, per project and per chat (incognito, from the first message)
- Delete a memory, a chat's memories or everything, at once
- Embeddings (pgvector) when an embedding server is configured; word matching otherwise
- Four kinds of memory: about you, lasting facts (your systems, projects, decisions),
  events (what was fixed or decided, with the date) and short procedures; each with an
  importance and a confidence (confirmed by you, or inferred)
- Hybrid ranking with configurable weights (meaning, words via PostgreSQL full-text
  search, recency, importance), always behind the relevance check
- Contradictions are flagged for you to resolve; corrections replace the old fact and
  keep it in history
- Entities and relationships (projects, services, machines, people…) linked to memories
- Secrets (passwords, keys, tokens) are refused and redacted before recall indexing
- Re-embedding after you change the embedding model

### Tasks and your day
- Your own task list: subtasks, priorities, deadlines (all-day or timed, in your time
  zone), reminders on your devices, repeats, dependencies, notes, attached files,
  projects
- Ask in a chat: "add a task to finish the memory work", "what are my most urgent
  tasks?", "plan my day". Changing a task asks first under the Standard tool profile
- When you mention something you need to do, Celius can suggest it as a task; nothing
  is added until you accept it (switch off in Today > Settings)
- **Today**: overdue and due tasks, a suggested order, the next 7 days, suggestions,
  Celius's open work and what waits for you (approvals, memories, skills)
- Optional daily briefing at a time you choose (a read-only scheduled task)

### Calendar, email and automations
- Celius's own calendar, plus CalDAV calendars (Nextcloud, iCloud, Fastmail…): see the
  week, add events or block time for a task, find free time and overlaps; today's events
  are in the briefing
- Email over IMAP/SMTP (Gmail and Outlook with an app password): search and read without
  marking mail read, summarise and find action items, save replies to Drafts.
  **Sending asks you every time**, whatever the tool permissions
- Automations that run on their own: "when a task becomes overdue, notify me", "every
  morning, prepare a briefing", "when an invoice email arrives, add a task", "tell me
  when this page changes". Create them in Today > Automations or by asking in a chat;
  each keeps a history, and one that keeps failing switches itself off and tells you
- Passwords are encrypted on the server and never shown again

### Insights and improvement
- **Insights** (Account): completed and failed replies and why, reply time, tool success
  by tool, prompt size per part, multi-step tasks completed, verified and recovered,
  skill reuse and success, memory use and kept suggestions, tasks and automations
- **How much Celius does on its own**: Careful, Balanced or Hands-off sets memory review,
  task suggestions, skill learning and default tool permissions together (sending email
  always asks)
- Weekly memory upkeep: likely duplicates to resolve, old unconfirmed guesses back to
  review
- A skill that keeps failing gets an improved version proposed for your approval
- Benchmarks: `devtools/memory_eval.py` (retrieval precision and recall) and
  `devtools/prompt_eval.py`, both able to save results and compare runs

### Local models
- A capability registry per model: context size, tool calling, images, parallel tool
  calls, JSON output, thinking on/off; from the model server and checks (never from the
  model's name), and overridable by an administrator (Account > Models)
- Prompt profiles: **Lite** for small models (one tool call per turn, fewer tools,
  smaller memory and skill budgets, short rules), **Standard**, **Advanced** for large
  ones; chosen per model or automatically from the context size
- The system prompt is built from modules, and each reply records how many tokens each
  part used
- "Check tool calling" sends a test prompt and remembers the result for every reply
- `devtools/prompt_eval.py` scores scenarios against your model server once per profile:

  ```bash
  cd services/api && uv run python -m devtools.prompt_eval --base-url http://gpu-pc:8080/v1 --model my-model
  ```

### Multi-step tasks
- For work that takes several steps, the model records the objective and a plan
  (`plan_start`) and keeps it current (`plan_update`): step status, findings, blockers and
  verification results
- Celius itself records the files the tools wrote or changed, tool failures, and a
  checkpoint at the end of every reply
- The plan is given back to every reply in the chat, so the work continues even when
  the earlier messages no longer fit the model's context
- A reply that fails, is stopped or uses all its tool steps pauses the task; the next
  message, or **Resume**, picks it up where it stopped
- A task card above the message box shows the plan and progress, with Resume, Mark done
  and Stop task
- Completing a task needs every step done; finishing without a passing check is allowed
  but marked unverified
- After two failed tool calls in a row, the model is told to step back and change approach

### Skills
- Saved procedures in the [Agent Skills](https://agentskills.io) format (SKILL.md plus
  scripts, references, assets), versioned, with rollback
- The model sees the few skills relevant to your message and loads one only when it uses
  it; skill tools appear once you have skills
- After a multi-step task, Celius can propose a skill (or an improvement to the one it
  used); it is validated and tested in the sandbox, and you approve it
- Trust levels: conservative, balanced (default: you approve publishing) or autonomous
  (only versions that pass their test are published automatically)
- Validation blocks secrets and text that tries to override instructions or permissions;
  a skill never grants permissions
- Uses, outcomes and your feedback are recorded per skill; export and import as zip

### People
- Invite people with one-time links; each person has their own chats, files, memories
  and tools
- Administrators disable, re-enable or delete accounts, reset passwords and grant admin
  rights
- Per-person limits: storage, replies per 24 hours, which models and which tool profiles
  they may use
- A second person can't reach the first person's data through any route: this is tested
  for every API route automatically

### Running it
- Encrypted backups (database, files, workspaces, settings) with an off-site copy,
  restore, and a restore drill that proves the latest backup works
- Upgrade and rollback scripts, with a backup taken first
- Health checks, a status page, Prometheus metrics and alerts to a webhook
- Login throttling, revocable sessions, password change and reset, security headers and
  a strict content security policy

## How it fits together

```
 Browser / Android PWA / Windows app
                │  HTTPS (reverse proxy)
                ▼
 ┌──────────────────────── Proxmox LXC (Docker Compose) ────────────────────────┐
 │  api ── FastAPI: REST + SSE streams, serves the web app                       │
 │   │                                                                           │
 │  postgres ── data, run queue, events, memories (pgvector)                     │
 │   │                                                                           │
 │  worker ── generates replies, runs tools and background jobs ─────────────────┼──► llama-server
 │   │                                                                           │    (GPU PC, LAN)
 │  sandbox ── runs the model's code: no secrets, internal network only          │
 │   │                                                                           │
 │  egress ── the sandbox's only way out: public websites, ports 80/443          │
 └───────────────────────────────────────────────────────────────────────────────┘
       worker ──► Exa (web_search), public web (web_fetch), MCP servers, push services
```

- The API stores your message and queues a **run**; the worker claims it with a lease
  (a crashed worker's run is picked up again), streams the reply into run events, and the
  API relays them to every open client over SSE ([ADR 0003](docs/adr/0003-sse-with-persisted-run-events.md),
  [ADR 0005](docs/adr/0005-postgres-run-queue.md))
- Clients keep a local copy and an outbox, and follow a change feed
  ([ADR 0006](docs/adr/0006-sync-and-offline.md))
- Code runs only in the sandbox container, as a non-root user with no capabilities, a
  read-only root and limits, never in the API or worker
  ([ADR 0008](docs/adr/0008-sandbox-and-tools.md))
- The worker's own web access (search, fetch, clone, MCP) refuses private and LAN
  addresses, re-checking every redirect
- Redis is in the Compose stack but no current feature needs it

## Install on a server

The full guide is [docs/deployment/proxmox-lxc.md](docs/deployment/proxmox-lxc.md). In
short, on a Debian 12 or Ubuntu 24.04 LXC with Docker (nesting and keyctl enabled):

```sh
git clone <this repository> celius && cd celius
cp .env.example .env && chmod 600 .env
sed -i "s|REPLACE_WITH_RANDOM_SECRET|$(openssl rand -hex 32)|" .env
sed -i "s|REPLACE_WITH_RANDOM_PASSWORD|$(openssl rand -hex 24)|" .env
sed -i "s|REPLACE_WITH_RANDOM_TOKEN|$(openssl rand -hex 32)|" .env
sed -i "s|REPLACE_WITH_RANDOM_EGRESS_TOKEN|$(openssl rand -hex 32)|" .env
nano .env        # at least APP_PUBLIC_URL, BOOTSTRAP_*, MODEL_BASE_URL, MODEL_API_KEY
make up          # builds the images, migrates, starts
curl -s http://127.0.0.1:8000/api/v1/health/ready
```

Then:
1. Start `llama-server` on the GPU machine with `--jinja` (tool calling and the thinking
   switch need it) and an `--api-key`, and allow only the LXC through its firewall
   ([guide, step 6](docs/deployment/proxmox-lxc.md#6-model-server-on-the-windows-pc))
2. Put the API behind a reverse proxy with HTTPS, with buffering off for SSE
   ([reverse-proxy.md](docs/deployment/reverse-proxy.md)). HTTPS is needed for the
   installable app, offline start and notifications
3. Sign in with the bootstrap account, then remove `BOOTSTRAP_PASSWORD` from `.env`
4. Optional: `EXA_API_KEY` for web search, an embedding server for better memory, backups
   with `BACKUP_PASSPHRASE` and `BACKUP_OFFSITE_TARGET`, an `ALERT_WEBHOOK_URL`

Database migrations run automatically on `make up` and `make upgrade`.

## Configuration

Everything is set in `.env`; [`.env.example`](.env.example) lists every setting with an
explanation. The ones you are most likely to change:

| Setting | Default | What it does |
| --- | --- | --- |
| `APP_PUBLIC_URL` | | The address people use; cookies, links and push depend on it |
| `MODEL_BASE_URL`, `MODEL_API_KEY` | | Your `llama-server` (`http://<gpu-pc>:8080/v1`) |
| `MODEL_PARALLEL_REQUESTS` | 1 | Keep equal to llama-server's `--parallel` |
| `TOOL_DEFAULT_PROFILE` | `standard` | Permission profile for new chats |
| `TOOL_MAX_ITERATIONS` | 25 | Tool steps per reply ("continue" carries on) |
| `TOOL_SANDBOX_NETWORK` | true | Internet for code in the sandbox, through the egress proxy |
| `TOOL_NETWORK_ENABLED`, `TOOL_WEB_ALLOWLIST` | true, empty | Web access for the worker's tools, optionally limited to some sites |
| `EXA_API_KEY` | empty | Turns on `web_search`; searches use Exa credit |
| `MAX_UPLOAD_BYTES`, `MAX_MEDIA_UPLOAD_BYTES` | 50 MB, 2 GB | Largest file; audio and video have their own limit |
| `MAX_USER_STORAGE_BYTES` | 10 GB | Default storage per person (administrators can set each person's) |
| `EMBEDDING_BASE_URL`, `EMBEDDING_MODEL` | empty | Embeddings for memory and recall (word matching without) |
| `MEMORY_PROMPT_MAX_ITEMS`, `MEMORY_MIN_SIMILARITY` | 8, 0.55 | How many relevant memories, and how close they must be |
| `BACKUP_PASSPHRASE`, `BACKUP_OFFSITE_TARGET` | | Backup encryption and the off-site copy (rsync or rclone) |
| `ALERT_WEBHOOK_URL` | empty | Where alerts go: Home Assistant, an ntfy topic (`ALERT_WEBHOOK_FORMAT=ntfy`) or anything that accepts JSON |
| `PUSH_ENABLED` | true | Web Push notifications (needs HTTPS) |

## Clients

All clients use the same account and stay in sync. Details:
[docs/deployment/clients.md](docs/deployment/clients.md).

- **Browser:** open your server's address
- **Android:** open the HTTPS address in Chrome → menu → **Install app**. Turn on
  notifications under **Account → Notifications**
- **Windows:** download the installer from the latest CI run (GitHub → Actions → **ci** →
  `celius-windows-installers`), install, and enter your server's address. `make desktop`
  builds it yourself

## Development

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Node 22 + pnpm, Docker. For
audio and video details, ffmpeg on the `PATH`.

```sh
make install          # API and web dependencies
make dev-deps         # local PostgreSQL and Redis
make migrate
make fake-llama       # a stand-in llama-server on :8080, no GPU needed

# in separate terminals:
BOOTSTRAP_USERNAME=admin BOOTSTRAP_PASSWORD='choose-a-long-password' \
  MODEL_BASE_URL=http://127.0.0.1:8080/v1 make dev-api
MODEL_BASE_URL=http://127.0.0.1:8080/v1 make dev-worker
make dev-web          # http://localhost:5173
```

- API documentation: http://localhost:8000/api/docs (off in production)
- After changing the API, regenerate the TypeScript client with `make gen-api`; CI fails
  when it is out of date
- New migration: `make migration m="what changed"`

## Tests and CI

```sh
make test             # API tests (SQLite, mocked model server, in-process sandbox)
make test-api-pg      # the same on PostgreSQL, plus a migration round-trip
make test-sandbox     # sandbox service tests (bubblewrap ones skip if unavailable)
make test-web         # web unit tests and typecheck
make e2e              # Playwright against a full local stack, desktop and phone viewports
make lint
```

CI (`.github/workflows/ci.yml`) runs all of these, builds the Windows installers and the
Docker images, and runs a release gate in a clean deployment: install, add data, back up,
destroy, restore, restore drill and rollback. A weekly dependency audit
(`audit.yml`) runs pip-audit and pnpm audit. Each phase's exit-gate results, and the
manual checks for the real deployment, are in [docs/acceptance-tests/](docs/acceptance-tests/).

## Operations

| Task | Command | Guide |
| --- | --- | --- |
| Start or rebuild | `make up` | [Install](docs/deployment/proxmox-lxc.md) |
| Health | `make status`, or **Account → System status** | [Monitoring](docs/operations/monitoring.md) |
| Back up | `make backup` | [Backup and restore](docs/operations/backup-restore.md) |
| Restore | `make restore ARCHIVE=backups/celius-….tar.enc` | [Backup and restore](docs/operations/backup-restore.md) |
| Prove a backup restores | `make restore-drill` | [Backup and restore](docs/operations/backup-restore.md) |
| Upgrade | `make upgrade [REV=…]` | [Upgrade and rollback](docs/operations/upgrade-and-rollback.md) |
| Undo an upgrade | `make rollback` | [Upgrade and rollback](docs/operations/upgrade-and-rollback.md) |
| Reset a password from the host | `python -m app.ops.users reset-password` (in the api container) | [Security review](docs/security-review.md) |

## Security

The full review, with the threat model, is in [docs/security-review.md](docs/security-review.md).
The short version:

- The people using it are trusted not to attack each other; the model and everything it
  reads (web pages, documents, MCP tools) are **not**
- Code the model writes runs only in the sandbox: non-root, no capabilities, read-only
  root, memory, CPU and process limits, no secrets, and internet only through the egress
  proxy, which refuses LAN and private addresses
- Tools act only within the profile and approvals; approvals are bound to exact
  arguments; retrieved memories and documents are marked as data, not instructions
- Passwords are hashed with argon2id; sessions are revocable, `HttpOnly`,
  `SameSite=Strict` cookies; logins are throttled; cross-site requests are refused
- Uploads are verified from their bytes; downloads are served with `nosniff` and a
  sandboxing content security policy; native parsers (pdfium, ffmpeg) run in limited
  subprocesses
- Secrets stay on the server (MCP credentials encrypted at rest); backups are encrypted
  with AES-256
- Every API route is tested to require sign-in, and to refuse a second person's access to
  the first person's data

## Repository layout

```
apps/web/             React + Vite + Tailwind web app and PWA (service worker, offline outbox)
apps/desktop/         Tauri v2 Windows app (folders, notifications, tray)
services/api/         FastAPI API, worker, migrations, tests, dev tools (fake llama-server)
services/sandbox/     Code execution service and egress proxy
packages/api-client/  TypeScript types generated from the API
packages/contracts/   Run-event JSON Schema shared with clients
infra/                Dockerfiles, Compose files, operations scripts
docs/                 Backlog, ADRs, deployment, operations, acceptance tests, security review
```

## Documentation

- [Backlog](docs/backlog.md): every feature, its status and acceptance criteria
- Deployment: [Proxmox LXC](docs/deployment/proxmox-lxc.md) ·
  [reverse proxy](docs/deployment/reverse-proxy.md) · [clients](docs/deployment/clients.md)
- Operations: [backup and restore](docs/operations/backup-restore.md) ·
  [upgrade and rollback](docs/operations/upgrade-and-rollback.md) ·
  [monitoring](docs/operations/monitoring.md)
- [Security review](docs/security-review.md)
- [Acceptance tests](docs/acceptance-tests/), phases 2–10
- Architecture decisions:
  - [0001: Separate app, inference and execution](docs/adr/0001-separate-app-inference-execution.md)
  - [0002: Python + FastAPI backend](docs/adr/0002-python-fastapi-backend.md)
  - [0003: SSE with persisted run events](docs/adr/0003-sse-with-persisted-run-events.md)
  - [0004: Server-side sessions](docs/adr/0004-sessions-and-auth.md)
  - [0005: PostgreSQL run queue; the API serves the web app](docs/adr/0005-postgres-run-queue.md)
  - [0006: Change feed, outbox and branches for multi-device use](docs/adr/0006-sync-and-offline.md)
  - [0007: File uploads, storage and processing](docs/adr/0007-file-storage.md)
  - [0008: Sandbox, tool policy and approvals](docs/adr/0008-sandbox-and-tools.md)
  - [0009: Memory and recall](docs/adr/0009-memory-and-recall.md)
- Assistant roadmap: [roadmap and status](docs/implementation/CELIUS_ROADMAP.md) ·
  [audit](docs/implementation/AUDIT.md) ·
  [decisions C-01…C-12](docs/implementation/ARCHITECTURE_DECISIONS.md) ·
  [implementation log](docs/implementation/IMPLEMENTATION_LOG.md)

## Not yet verified

Built and tested in CI, but not yet checked on the real hardware (tracked in the
[backlog](docs/backlog.md)):

- The deployment on the Proxmox host surviving restarts and reboots (F-10)
- The real `llama-server` on the RTX 3060: tool calling, context size, speed and safe
  parallel requests (M-05, T-09)
- The sandbox image on the real host (T-08)
- Memory extraction quality with the deployed model, and choosing an embedding model (R-05)
- The first backup and restore drill on the real host (H-06)
- `web_search` against Exa's live API (the tests use a stand-in for Exa)

Not built: transcription of audio and video, and server-side extraction of archives into
project workspaces.

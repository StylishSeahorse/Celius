# Celius Remote — progress

Updated 2026-11-12. Test-level detail per feature: [TESTING.md](TESTING.md).

| Phase | State | Notes |
|---|---|---|
| 0 Audit | Done | [AUDIT.md](AUDIT.md), [ARCHITECTURE.md](ARCHITECTURE.md), ADR C-16 |
| 1 Infrastructure foundation | Done | Registry, pairing, authorisation, credentials, tokens, WebSocket, heartbeats, state, monitoring; Linux and Windows agent |
| 2 Native remote execution | Done | Dispatch, sessions, folders, commands, structured events, terminal streaming, persistence, reconnection, replay |
| 3 Conversation integration | Done | Target selection, remote conversations, messages, live replies, tool events, terminal, files, approvals, status |
| 4 External agent adapters | Done for Claude Code, with one gap | Agent SDK adapter + canonical events + projection. **Not yet run against a real Claude Code account.** |
| 5 Management UI | Done | Machines, details, session monitor, selector, terminal, files, resources, controls, approvals |
| 6 Reliability and security | Done, with gaps below | Rotation, revocation, permission intersection, replay, recovery, at-most-once execution, size limits, retention, audit, isolation tests, installers, CI |
| 7 Distributed agents | Not started (by design) | Designed for: [ROADMAP.md](ROADMAP.md) |

## Open items

Blocked by this environment (Linux container without Windows, systemd or a Claude account):
- Windows installer executed on a real Windows 10/11 machine (it is parsed in CI; the agent's
  unit tests run on Windows in CI).
- systemd service started under systemd (the installer was run with `--no-service`).
- Claude Code adapter against real Claude Code.

Not implemented (see ROADMAP.md): macOS agent; mid-turn instructions to Claude Code;
Celius attachments copied to machines; `attach_file` on machines; interactive terminal
input; automatic machine selection; self-updating agents; OS-level network restriction;
Windows per-command memory limits; Playwright e2e for remote.

Pre-existing, outside this work: CI's API job fails on `main` since `d6a1f63` because
`services/api/devtools/data/memory_eval.json` is excluded by `.gitignore` and was never
committed; the browser e2e job is skipped as a result.

## Decisions taken along the way

- The end-to-end tests run on PostgreSQL only (SQLite's single writer lock stalls a server,
  worker and agents sharing one file).
- The hub's idle loop was made read-only after it turned out to hold a write transaction
  per machine per poll.
- An oversize event could make the agent resend the same frame forever; agents now cap
  batches and events, and the server's frame limit equals uvicorn's.
- Windows children run with `PYTHONUTF8=1` after CI showed ANSI-code-page output.
- Approval cards no longer offer "always allow" for tools that ask every time (remote
  commands, and sending email before this).

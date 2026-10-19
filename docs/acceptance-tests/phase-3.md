# Phase 3 acceptance tests: client experience and sync

Exit gate: **synchronisation, reconnect and duplicate-submission tests pass.**

## Automated

| Suite | Covers |
| --- | --- |
| `services/api/tests/test_sync.py` | Every write type appears in the feed (create, message, completion, cancel, failure, rename, delete); paging; privacy between accounts; ack never moves backwards; idempotent create with a client id |
| `services/api/tests/test_branches.py` | Regenerate and edit fork branches without overwriting; switching restores whole continuations; editing the first message; appends go to the selected branch; idempotent edit/regenerate; nothing changes while generating; `parent_not_head` reports the head |
| `services/api/tests/test_projects.py` | CRUD, counts, idempotent create, instructions reach the model, deleting keeps chats |
| `apps/web/src/offline/outbox.test.ts` | Order, persistence across reloads, offline retry with the same id, offline-created conversations, waiting for a running reply without blocking others, permanent failures |
| `apps/web/e2e/sync.spec.ts` | **Offline queue delivered exactly once after reconnect**; **lost response retried without a duplicate**; **second device sees messages and renames live**; **conflicting renames: Use mine / Keep theirs** |
| `apps/web/e2e/branches.spec.ts` | Regenerate, edit and version switching in the UI; projects |
| `apps/web/e2e/pwa.spec.ts` | Manifest, icons, no-store service worker; **app opens offline with saved conversations** |

All browser tests run in a desktop window and a Pixel 7 viewport.

## Manual

1. **Phone install:** with HTTPS in place, install from Chrome on Android, turn on
   airplane mode, open the app: saved conversations show; write a message, turn
   airplane mode off: it's sent once and answered.
2. **Two devices:** open the same conversation on the phone and the PC; send from one
   and watch it appear on the other within about 5 s.
3. **Windows app:** install from the CI artifact, connect to the server, sign in, chat;
   use **Change server…** to switch servers.

## Results, 2026-10-19 (local stack on PostgreSQL 16, fake model server)

| Check | Result |
| --- | --- |
| API tests on SQLite | 74 passed (1 skipped: the PostgreSQL-only migration test) |
| API tests on PostgreSQL, plus migration round-trip | 75 passed |
| Web unit tests | 15 passed |
| Playwright, desktop + Pixel 7, fresh database | 24/24 passed |
| Generated API client is reproducible | Generating twice gives byte-identical output |
| Docker web build stage (simulated in a clean directory) | Installs with the frozen lockfile and builds |
| Desktop app on Linux (WebKitGTK, virtual display) | Compiles with no warnings; connect page → server → sign in → chat with a streamed reply → **Change server…** returns with the address filled in |
| Desktop app on Windows | Not run yet: CI builds the NSIS/MSI installers on `windows-latest` |

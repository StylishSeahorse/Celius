# ADR 0006: Change feed, outbox and branch model for multi-device use

Status: accepted · 2026-10-19

## Decisions

### The server is the only source of truth
Clients keep two things in IndexedDB:
- a **cache**: copies of the conversation list, conversations, projects, models and the signed-in user, so the app opens instantly and offline
- an **outbox** of messages not yet delivered

Clients never merge data; they refetch.

### Change feed instead of pushing state
Every durable write records a `sync_events` row in the same transaction. Clients poll
`GET /sync/changes?cursor=N` every ~4 s while visible and online, and immediately on
reconnect or focus. The feed says *what* changed (entity type, id, conversation id);
the client refetches the affected list or conversation. Polling one indexed query is
negligible for a single user and survives proxies better than a long-lived connection.

On a device's first run, or when its stored cursor is ahead of the server's (e.g. after
a database restore), the client takes the server cursor *first* and then reloads
everything, so nothing changed during the reload is missed.

### Exactly-once delivery = idempotent server + persistent outbox
Every send goes through the outbox, online or not. Each item is persisted with its
`client_message_id` before the first attempt. The server deduplicates on
`(owner_id, client_message_id)`, so retrying after a lost response returns the original
message instead of a copy. Conversations and projects accept a client-generated `id`
for the same reason: a conversation started offline is created on reconnect, safely and
idempotently.

Ordering is per conversation: a later message never overtakes an earlier one, and a
message whose conversation is still generating waits (`409 run_in_progress`) without
blocking other conversations. Permanent failures (deleted conversation, validation)
are marked failed and shown with *Retry* / *Discard*.

Edits, regenerations and switching versions need the server and are not queued.

### Conflicts are explicit
Settings changes (title, model) carry `expected_version`. A stale write gets
`409 version_conflict` with the server's current state, and the UI asks
**Keep theirs / Use mine**. "Use mine" retries against the version it was shown.
Appending messages doesn't need this: it merges naturally onto the current head.

### Branches: a message tree with branch heads
Messages form a tree through `parent_id`. Editing a user message or regenerating a reply
creates a sibling under the same parent on a new branch; nothing is overwritten. Each leaf
is the head of exactly one branch. The conversation's `active_branch_id` decides the
displayed path, and `POST /messages/{id}/select` switches to the most recent continuation
under a chosen sibling. Branch operations are refused while a reply is generating.

### Clients
- **Android:** the web app as an installable PWA. A service worker caches the app shell;
  API responses are never cached by the service worker.
- **Windows:** a Tauri v2 shell that navigates to the user's server. The remote page gets
  no IPC access. A bundled connect page stores the server address.

## Consequences
- The active branch is shared state: switching versions on one device switches the others
  (they follow via the feed). Last switch wins.
- Service workers, and therefore PWA installation and offline cold start, need HTTPS
  (or `localhost`). Over plain `http://` on the LAN the app works, but offline it only
  keeps working in a page that is already open.
- Sync latency is up to ~4 s. Live replies stream over SSE as before.

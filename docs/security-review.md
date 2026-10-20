# Security review (Phase 7, H-04)

Scope: one household or small team on a private network, reached through a TLS reverse
proxy. The users are trusted not to attack each other. The model and anything it reads
(web pages, documents, MCP tools) are **not** trusted. Reviewed 2026-10-20 at the Phase 7
revision.

## Threat model

| Adversary | Can | Must not |
| --- | --- | --- |
| Someone on the network or internet without an account | Reach the login page | Guess passwords at speed, use a stolen cookie from another site, read any data |
| A malicious web page or document the model reads (prompt injection) | Steer the model's text and tool requests | Run tools without the policy and approvals allowing them, reach the LAN, read secrets, survive the run |
| Code the model writes (in the sandbox) | Use CPU, memory and disk within limits; reach public websites through the egress proxy | Reach the LAN, the host, Postgres or Redis; read the service's token; keep running after the run; escape its workspace |
| A signed-in user | Use their own data | Read another user's chats, files or memories; administer without being an admin |
| Someone who gets a backup archive | Hold it | Read it without the passphrase |

## Review by area

### Authentication and sessions

- Passwords are hashed with argon2id. A login for an unknown user is checked against a
  dummy hash, so the response time doesn't reveal which names exist.
- Session tokens are 256-bit random values; only their SHA-256 is stored. They expire
  after `APP_SESSION_TTL_HOURS` (14 days), and signing out revokes the token on the server.
- The cookie is `HttpOnly`, `Secure` in production and **`SameSite=Strict`**. Bearer
  tokens are used by the desktop app and scripts.
- **Fixed in this phase**: there was no throttling. Now 10 failed logins per account or
  30 per IP address within 15 minutes give `429` with `Retry-After`, even when the
  password is right. Counting uses the audit log, so it holds across restarts and API
  replicas (F-11).
- **Fixed in this phase**: users can now change their password (`POST /auth/password`
  needs the current one and signs out all other sessions), and an administrator can
  reset one from the host: `python -m app.ops.users reset-password`, which also clears
  the throttle. There is also a list of signed-in devices with "sign out others" (F-12).
- Every route is tested to refuse unauthenticated requests
  (`tests/test_security.py::test_every_api_route_requires_authentication`, generated from
  the OpenAPI schema, so new routes are covered automatically). Public: health, login,
  and metrics, which has its own token and is off by default.

### Cross-site requests and browser hardening

- **Fixed in this phase**: unsafe requests (POST/PUT/PATCH/DELETE) authenticated by cookie
  carrying another site's `Origin` header (which browsers send on cross-site requests) are
  refused with `403 cross_origin`. Requests without `Origin` (non-browser clients) pass;
  `SameSite=Strict` already keeps the cookie off cross-site requests, so this is defence
  in depth against CSRF.
- **Fixed in this phase**: every response carries `X-Content-Type-Options: nosniff`,
  `X-Frame-Options: DENY`, `Referrer-Policy: same-origin` and a `Permissions-Policy`. The
  web app is served with a CSP: `script-src 'self'`, no inline script, no `eval`,
  `frame-ancestors 'none'`, and `connect-src` limited to itself and the Tauri IPC.
- Model output is rendered as Markdown without raw HTML. Downloads of anything that isn't
  an image or PDF are forced to `attachment`.

### Authorization

- Every query for chats, files, projects, memories, tool calls and jobs is filtered by
  owner; cross-user tests exist for each (`test_files.py`, `test_memory.py`,
  `test_tools.py`, `test_sync.py`).
- Administration (MCP servers, the status page, custom tool profiles shared with
  everyone) checks `is_admin`.

### Secrets

- `.env` holds every secret: the database password, `APP_SECRET_KEY`, the sandbox and
  egress tokens, and the backup passphrase. Restore sets it to mode 600. It is passed to
  the api, worker and migrate containers only. The sandbox gets its own token and nothing
  else; the egress proxy gets only the proxy credential (tested in
  `test_compose_policy.py`).
- MCP credentials are encrypted at rest (Fernet, key derived from `APP_SECRET_KEY` by
  HKDF) and never returned by the API.
- Backups are encrypted with AES-256 and PBKDF2 (200 000 iterations). The passphrase is
  given to openssl through the environment, never as an argument, so it doesn't appear in
  `ps`. Archives and work directories are created with `umask 077`.
- *Accepted*: the api and worker can see `BACKUP_PASSPHRASE`, because compose has a single
  env file. Code execution there already means access to the database. Moving the
  passphrase to a separate root-only file is an option if that ever changes.

### Files

- Stored content is addressed by SHA-256 under `blobs/`, so no user-supplied name ever
  becomes a path. Uploads land in quarantine and are verified (size, real type, archive
  bombs and traversal) before being stored.
- Workspace access from tools resolves inside the workspace and never follows symlinks
  (`test_tools_security.py`).
- Project ZIP exports and archive extraction refuse absolute paths, `..` and links
  (`test_files.py`).

### Sandbox and network

- Code runs in a separate container. It is on an internal network with no route out. It
  runs as a non-root uid with all capabilities dropped, a read-only root, and
  `no-new-privileges`; it has pid, memory and CPU limits and no Docker socket. Inside,
  each run is a new session, and with bubblewrap also new namespaces. The whole session is
  killed at the end, and descendants that escaped with `setsid` are found through the
  subreaper and killed too.
- Internet access only goes through the egress proxy. It needs a credential, allows ports
  80 and 443 only, and refuses private, loopback, link-local, CGNAT and IPv4-mapped
  addresses. Every DNS answer must be public, and the connection is pinned to the address
  that was checked. Every destination is logged.
- The sandbox service is non-dumpable, so the code it runs (same uid) can't read its
  token from `/proc`.
- The worker's own web tools (`web_fetch`, `git_clone`) use the same rules. Every redirect
  is re-checked, and the size is capped.
- Escape tests: `services/sandbox/tests/test_runner.py` (background processes, fork bombs,
  memory, output floods, timeouts, workspace escape, environment),
  `tests/test_egress.py` (credential, private destinations, DNS rebinding, ports), and
  `services/api/tests/test_tools_security.py` (SSRF including redirects to the metadata
  address, symlinks, policy).
- **Fixed in this phase**: on Python 3.12, a background child holding the output pipes made
  the sandbox wait for it, indefinitely if it never ended, tying up a slot (CI exposed
  this). The runner now waits for the exit status itself and then kills the session as
  designed.
- *Accepted*: without bubblewrap (when the host forbids user namespaces) runs share the
  container's namespaces. The container boundary and limits above still hold. The tools
  panel shows the mode.

### Tools and prompt injection

- Every tool has a risk level. Profiles decide allow, ask or deny, and denies always win,
  even over "allow all in this chat". Approvals are bound to the exact arguments' hash.
  Scheduled tasks run with the profile chosen when they were created.
- Retrieved memories and documents are marked as data in the prompt. They can't change the
  tool policy, because the policy is enforced by the worker, not the model.

## Not done / out of scope

- No second factor. The deployment is meant to sit behind the reverse proxy, which can add
  SSO or mTLS if needed.
- No automatic dependency audit in CI yet; run `uv run pip-audit` and `pnpm audit` before a
  release.
- The model server on the LAN is trusted. Use `MODEL_API_KEY` if llama-server is on a
  shared network.

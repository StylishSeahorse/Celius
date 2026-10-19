# Phase 5 acceptance tests: tool execution and autonomy

Exit gate: **sandbox isolation, policy enforcement, cancellation and audit tests pass.**

## Automated

| Suite | Covers |
| --- | --- |
| `services/sandbox/tests` | **Isolation**: the environment is scrubbed (the service's own token is invisible); wall-clock timeout, CPU, memory and file-size limits; output capped and floods killed; background processes (`setsid`, `nohup`, fully detached) don't survive the run; invalid workspace ids refused. Under bubblewrap: no network (only `lo`, connections fail), other workspaces and host files hidden, read-only root, own PID namespace. **Cancellation**: cancelling a run kills its process group; a real server kills the code when the caller closes the connection. Token required |
| `services/api/tests/test_tools.py` | **Policy**: profiles decide by risk; grants never override a deny; denied tools aren't offered and are refused if called; unknown tools and malformed or invalid arguments refused. **Approvals**: a run pauses without a worker; approving with a different `args_hash` fails (409) and is audited; approval runs exactly the stored arguments and resumes the run where it stopped; decisions count once; deny and expiry are reported to the model and nothing runs; "allow for this chat" skips later prompts in that chat only. **Limits**: iteration limit forces a final answer without tools; results are capped. **Sandbox**: Python runs, a produced file is attached to the reply; worker secrets are not visible to code. **Cancellation**: cancelling while awaiting approval cancels the calls; cancelling during a running call kills the code (its file is never written) and the model isn't called again; a call interrupted by a worker restart is reported, never re-run. **Endpoints**: catalogue, settings, custom profiles; workspace downloads are always attachments with nosniff and CSP sandbox, and refuse symlinks, traversal and absolute paths. **Audit**: approve, deny, mismatch and refusals recorded |
| `services/api/tests/test_tools_security.py` | **Workspace**: traversal, absolute, drive and NUL paths refused; symlinked files and directories are never followed for reading, writing or copying. **SSRF**: private, loopback, link-local, CGNAT, multicast, IPv4-mapped and metadata addresses refused; non-http(s) schemes, other ports and credentials refused; mixed DNS answers (rebinding) refused; the guarded transport refuses to connect to a private address; redirects to private addresses refused; size cap; allowlist. **MCP**: list and call over streamable HTTP (JSON and SSE answers, session id); wrong credentials and private hosts refused; MCP tools need approval under the standard profile and nothing reaches the server before it; only administrators can register servers; credentials are sealed and never returned; sealed values need the same secret. **Schedules**: time-zone and weekday maths; minimum interval, unknown time zone and quota refused; a due task fires once (a second claim gets nothing) into its own conversation with its profile; runs are marked as scheduled in the activity list. **Tools in the sandbox**: Git works offline and refuses network subcommands; Word and Excel files are created |
| `services/api/tests/test_compose_policy.py` | No service mounts the Docker socket or is privileged; the sandbox has no env file, only its token, only the internal `sandbox` network, no ports, all capabilities dropped, read-only root, non-root user, pid and memory limits, only the workspaces volume; only the worker shares its network; Postgres and Redis have no route out |
| `apps/web` unit tests | Live tool-call state from the stream: a saved step resets the live text; awaiting approval and resumed states |
| `e2e/tools.spec.ts` (desktop and Pixel 7) | A write asks for approval showing its exact arguments, the sidebar badge counts it, **Allow once** runs it once; Python runs in the real sandbox under the developer profile; read only works for reading; deny from the approvals inbox; activity and sandbox status in the Tools panel; create and delete a scheduled task |
| CI `image` job | The sandbox image builds and, with no network and no capabilities, runs Python with pandas, python-docx, openpyxl, python-pptx, fpdf2 and matplotlib importable |

## Results, 2026-10-19

| Check | Result |
| --- | --- |
| Sandbox tests (process and bubblewrap modes) | 44 passed |
| API tests on SQLite | 190 passed (1 skipped: the PostgreSQL-only migration test) |
| API tests on PostgreSQL, plus migration round-trip (0001 → 0004) | 186 passed |
| Web unit tests, typecheck, generated client up to date | 16 passed |
| Playwright, desktop + Pixel 7 (with the real sandbox in process mode) | 45 passed, 1 skipped (folder picking on the phone viewport) |
| Sandbox Docker image | **not built here** (the registry rate-limited this environment); built and smoke-tested by CI |

## Manual (on the real deployment)

1. `make up` after adding `SANDBOX_TOKEN`; **Tools & activity → Permissions** says the
   sandbox is running, and `docker compose logs sandbox` shows its mode.
2. Verify tool calling for your model: `POST /api/v1/models/{id}/verify-tools`. llama-server
   needs `--jinja` and a template with tool support (Qwen 2.5/3, Llama 3.1+, Mistral).
3. In a chat with **Tools: Developer**, ask for a chart of some numbers as a PNG; check
   the code ran, the image is attached and downloads.
4. Under **Standard**, ask it to fetch a web page: approve it, then ask it to fetch
   `http://192.168.0.1/` and check it is refused as a private address.
5. From the phone, approve a pending call from the Tools panel while the desktop app
   shows the same chat; both update.
6. Create a daily scheduled task, use **Run now**, and see it in Activity.

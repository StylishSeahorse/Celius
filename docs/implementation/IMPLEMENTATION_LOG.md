# Implementation log

Newest first. Each entry: what changed, how it was verified, what is left.

## 2026-11-07: Gmail through "Sign in with Google"

**Existing implementation.** IMAP/SMTP mailboxes with sealed passwords (C-11); Gmail
worked only with an app password.

**Changes.** See C-14.
- **Sign-in:** `services/integrations/google.py` (consent with PKCE and a sealed state
  bound to a nonce cookie, code exchange, ID-token checks, token refresh and cache,
  revocation).
- **Account:** a `gmail` email provider with Google's servers fixed; the mailbox signs in
  with XOAUTH2 over IMAP and SMTP.
- **Routes:** `GET /integrations/google`, `POST /integrations/google/start`, and the
  callback.
- **Settings:** `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET` and
  `GOOGLE_OAUTH_REDIRECT_URL`.
- **Web:** the "Sign in with Google (Gmail)" button in Today > Settings; coming back
  from Google opens Settings with the result.
- **Setup guide:** `docs/deployment/google-sign-in.md` (Google Cloud steps, redirect
  rules, the 7-day Testing limit).
- No migration: `provider` is a free string column.

**Verification.**
- `tests/test_google_oauth.py` (9) runs against a fake Google token and revocation
  endpoint that checks PKCE. It covers:
  - not configured;
  - the consent URL (scopes, offline, S256, cookie flags, the secret never sent);
  - connecting, then search, send and check through XOAUTH2, with tokens sealed and
    never returned;
  - refresh when expired, the cache, and revoked access asking to sign in again;
  - reconnecting the same address updates the account and keeps the refresh token;
  - the callback refuses another browser, an altered state, an expired state, cancel,
    an unticked Gmail scope, a token for another app and an unverified address;
  - a consent tied to the user who started it;
  - fixed servers (no config or password edits, no manual creation) and revocation on
    removal.
- e2e `integrations.spec.ts`: the consent round trip, with Google's page intercepted.
- Totals:
  - API suite **641 passed, 7 skipped** (SQLite);
  - Google and integrations tests on PostgreSQL 16: **31 passed**;
  - full e2e **101 passed**.

**Left.** A real Google project and mailbox: run the guide on the server. Google
Calendar over the same sign-in is the next step.

## 2026-11-07: auto-review of tool approvals

**Existing implementation.** Policy profiles (allow, ask or deny by risk), approvals
bound to exact arguments, grants per chat and "Allow all for this chat"; `always_ask`
for sending email; the executor pauses when a call waits for the user.

**Changes.** See C-13.
- `services/tools/review.py`: the hard rules, the reviewer prompt and its answer parser,
  and the user's own words with attachments removed.
- Executor `_auto_review` runs before a pause. It sets `decision_source "reviewer"`,
  writes a reason, audits `tool_call.auto_reviewed` and keeps a per-reply cap.
- Settings: `TOOL_AUTO_REVIEW_DEFAULT`, `TOOL_AUTO_REVIEW_MODEL` and
  `TOOL_AUTO_REVIEW_MAX_PER_RUN`; `auto_review {enabled, risks}` in
  `/tools/settings`.
- The `tool_auto_review` field in the autonomy presets: Hands-off turns it on.
- Web: Tools > Permissions > Auto-review (toggle and risk classes); the tool card shows the
  reviewer's reason; the autonomy summary line.
- The dev fake model server answers review requests (`[review:approve]`).

**Verification.**
- `tests/test_auto_review.py` (8):
  - the hard rules (email, external, cap, secrets, risky commands; a plain `rm -rf build`
    goes to the reviewer);
  - strict parsing;
  - the reviewer sees neither tool results, attachments nor the system prompt;
  - an approval runs the call without pausing, and is audited;
  - "ask", an unclear answer and an HTTP error each leave the call waiting with a reason,
    and it is not reviewed again on resume;
  - off by default;
  - the settings API, validation and presets.
- One fix found by the tests: risky patterns are matched against argument values, not
  their JSON (the closing quote hid `rm -rf /`).
- e2e `tools.spec.ts` "auto-review approves what the request asks for and leaves the
  rest" (desktop and mobile).
- Totals: API suite **630 passed, 7 skipped** (SQLite). On PostgreSQL 16: auto-review,
  tools, isolation and continuous improvement, 46 passed. Full e2e suite **99 passed**.

**Left.** How often the real local model approves or asks on everyday requests isn't
measured yet; `GET /insights` can count `tool_call.auto_reviewed` once there is use.

## 2026-11-07: Phase 7, continuous improvement

**Existing implementation.** Everything needed for measurement was already stored (runs,
tool calls, `run.started` with profile and prompt sections, agent-task events, skill
events, memory statuses and usage parts, personal tasks, automation runs); only
`message.delta` events are pruned. `prompt_eval.py` (Phase 4) existed.

**Changes.** See C-12. `services/analytics.py` and `GET /insights`;
`devtools/memory_eval.py` with `devtools/data/memory_eval.json`; light stemming in
memory keyword matching; `MEMORY_MIN_KEYWORD_SCORE`; `memory/consolidate.py` (weekly
job, queued from the worker's maintenance); `skill.improve` and the failure-cluster rule
(after runs and after negative feedback); `services/autonomy.py` and
`/assistant/autonomy`; `prompt_eval.py --save/--compare`; web: Account > Insights and
"How much Celius does on its own", a review reason on memories sent back to review.

**Measured.** Memory benchmark, keyword-only, bundled set: before precision 0.500 /
recall 0.469 / MRR 0.500 / silence 0.750; after stemming 0.677 / 0.719 / 0.750 / 0.750. A
stemmer variant that over-stemmed ("runs" → "run") measured 0.630 precision and was
reverted. The four weight sets in `--grid` scored identically on this set, so the
weights were left alone.

**Verification.** `tests/test_continuous_improvement.py` (14): insights from real chats
(completion, failure reasons, per-tool success, context by section, privacy and the
admin-only scope), memory/agent/assistant figures, stemming, the benchmark as a regression
guard and its grid, consolidation (duplicates once, stale back to review, approval
confirms, queued once per period), skill improvement (cluster → one job → an improved
version waiting for approval; feedback counts; conservative never), autonomy presets and
an administrator's limit, benchmark comparison. e2e `insights.spec.ts`. Totals: API suite
**622 passed, 7 skipped** (SQLite); Phase 7, memory, skills, attachments, isolation and
migrations on PostgreSQL 16 + pgvector (125 passed); full e2e suite **97 passed**.

**Left.** Real-model and real-embedding measurements on the server.

## 2026-11-05: Phase 6, integrations and automation

**Existing implementation.** Sealed credentials (MCP servers), the SSRF-guarded HTTP
client with a per-server private-network option, Web Push, the job queue, the worker
loop (runs, jobs, schedules, reminders), the personal task service.

**Changes.** See C-11.
- Migration 0015: `integration_accounts`, `calendar_events`, `automations`,
  `automation_runs`, `automation_events`
- `services/integrations/`: `accounts.py`, `ical.py`, `calendar.py` (local + CalDAV,
  conflicts, free slots), `email.py` (pinned IMAP/SMTP, search, read, drafts, send)
- `ToolSpec.always_ask` in the policy and approval paths
- `services/automation/`: `engine.py` (validation, triggers, conditions, actions,
  events, polling jobs, history, failure handling, pruning), `service.py`
- Task service emits `task_status` / `task_created` events; the worker loop runs the
  engine; the briefing includes the calendar
- Tools: `calendar_events`, `calendar_add`, `calendar_change`, `email_search`,
  `email_read`, `email_draft`, `email_send`, `automation_create`, `automation_list`
- API: `/integrations/accounts` (+ check), `/calendar/events` (+ update, delete),
  `/email/messages`, `/email/drafts`, `/email/send`, `/automations` (+ runs)
- Web: Today > Calendar (week, add, delete, overlaps), Automations (recipes, JSON,
  on/off, history, failure reason), Settings > Calendar and email accounts

**Found while testing.** Updating an event in a CalDAV object that held two events
rewrote the object with only the first: the other event was lost. Such objects (and
repeating series) are now read-only from Celius for updates and deletes, and marked so
in listings; the test checks nothing is lost. An isolation-sweep parameter clash
(`{message_id}` already named chat messages) led to renaming the email route's
parameter to `{email_id}`.

**Verification.** `tests/test_integrations.py` (22) with in-process fake CalDAV and
IMAP/SMTP servers (`tests/integration_fakes.py`): iCalendar parsing and round trip,
sealed passwords and connection checks, admin-only local network, local calendar and
conflicts, CalDAV create/list/update/refusals/outage, the briefing's calendar, calendar
tools and approval, email tools appearing with an account, search/read/draft with a
prompt-injection email, sending asking every time (Administrator + "allow all"),
automation validation, overdue once, task events, schedules, email arrivals, page
changes, switching off after failures, `run_prompt` with trigger data, a model-created
rule, privacy. Isolation sweep covers the routes. e2e `integrations.spec.ts`. Totals: API
suite **605 passed, 7 skipped** (SQLite; one Phase 5 test's assertion was too strict
once more tools existed and was corrected); integrations, tasks, tools, isolation,
profiles and migrations on PostgreSQL 16 + pgvector (115 passed); full e2e suite
**95 passed**. No real CalDAV or mail server was used: **connect a real calendar and
mailbox on the server**.

**Left.** OAuth providers, recurring local events, stored email-task links.

## 2026-11-05: Phase 5, personal assistant foundation

**Existing implementation.** Projects, scheduled prompts (`scheduled_tasks`), Web Push
(`push.queue` → `push.notify` job), memory extraction after replies, agent tasks. No
personal tasks, reminders, user time zone, briefing or dashboard.

**Changes.** See C-10.
- Migration 0014: `user_tasks`
- `services/assistant/tasks.py`: create/change rules (ownership, project access, loops,
  dependencies, attachments, secrets redacted), local dates and repeats, next occurrence,
  suggestions, briefing and suggested order, reminders
- Worker loop fires due reminders; sync feed carries `user_task` changes
- Tools `task_add`, `task_list`, `task_update`, `daily_briefing`; `assistant` prompt
  module
- Memory extraction also notices to-dos when allowed (`TASKS_RULE`, `parse_tasks`)
- API: `/tasks` (list, create, get, update, accept, delete), `/assistant/briefing`,
  `/assistant/settings` (time zone, suggestions, daily briefing schedule)
- Web: Today panel (briefing with check-off, suggestions to accept or dismiss, Celius's
  open work, what waits for you; task list with quick add, priorities, subtasks, editor
  with due date, reminder and repeat; settings) and a sidebar entry with a count;
  reminders open `#/today`. Dev fake model: `[todo:…]`

**Found while testing.** Creating a task with dependencies answered `blocked: false`
(the create response didn't load the blockers). The sync feed schema hadn't received
`user_task` (my edit missed a reformatted line): any task change would have broken
`GET /sync/changes`; caught by the web typecheck against the regenerated client, fixed,
and now covered by a test. The full e2e run failed the attachment tests on the 8k-context
fake model a third time. Root cause, now fixed rather than worked around: the attachment
share was taken of the *whole* context window before the system prompt was built, so a
grown prompt plus a long document overflowed small models. Attachments are now sized
from what the system prompt leaves (`executor._prepare`); a regression test fails
without the fix and passes with it. The remaining failure was the tool definitions
themselves (~5,000 of an 8k model's ~5,900 usable tokens): tools now fit a budget of the
context (`TOOL_CONTEXT_SHARE`, C-09), core tools first, the rest cheapest first.

**Verification.** `tests/test_personal_tasks.py` (15): local dates, repeats across DST and
month ends, list order and updates, subtasks/dependencies/validation, repeating
completion, reminders once and only for active tasks (and re-armed), briefing and order,
time zone deciding "today", the daily briefing schedule, suggestions from a chat
(accept, dismiss, no duplicates), suggestions off, task tools in a project chat, Standard
asking before a change, privacy, the sync feed. Isolation sweep covers the routes. e2e
`today.spec.ts` (add, briefing, complete; suggestion accepted). Totals: API suite
**565 passed, 7 skipped** (SQLite); migrations, personal tasks, attachments, profiles,
isolation and sync on PostgreSQL 16 + pgvector (57 passed); full e2e suite **89 passed**.

**Left.** Calendar and automation rules (Phase 6).

## 2026-11-05: Phase 4, local model intelligence

**Existing implementation.** `model_gateway` detects context size and vision from
llama-server `/props` and can probe tool calling, but kept the probe in process memory;
`model_catalog` existed unused; budgets came from global settings; the system prompt was
concatenated in `executor._prepare`.

**Changes.** See C-09.
- Migration 0013: `model_catalog.profile`, `overrides`, `limits`, `notes`
- `services/model_registry.py`: detection recorded (race-safe, throttled), probe stored,
  effective profile with sources; `ModelProfile.run_settings` applies budgets per run
- `services/prompting.py`: sections, order, trusted vs data, per-section sizes
- Executor: profile resolved per run; prompt built from modules; compact toolset and one
  tool call per turn on lite; per-model tool-step and output limits; `run.started`
  records `profile` and `prompt_sections`
- `ChatRequest.response_format`; memory extraction uses JSON mode when supported
- API: `/models` includes each model's effective profile; `GET/PUT /models/registry`
  (administrators); the tool probe persists
- Web: Account > Models (profile, capability overrides with their source, tool steps,
  "Check tool calling")
- `devtools/prompt_eval.py`; the interim `TOOL_COMPACT_BELOW_CONTEXT_TOKENS` setting from
  Phase 2 is replaced by the lite profile (it was never released)

**Found while testing.** In the Models UI, two quick changes to one model raced: the
second save sent the profile as loaded before the first save, undoing it (caught by the
e2e test). The component now keeps local state and builds each save on the last.

**Verification.** `tests/test_model_profiles.py` (12): automatic profile from context,
builder order and sizes, standard prompt unchanged, lite (principles, compact tools, one
call per turn with the refused call allowed next turn), advanced, admin profile/
overrides/limits applied to runs, admin turning tool calling off, the probe reaching
"another process" through the registry, admin-only registry, JSON mode for extraction,
the evaluation harness scoring per profile. e2e `models.spec.ts`. The harness was run
against the development fake server (0/15 by design: it only echoes); **no real-model
results yet**. Totals: API suite **538 passed, 7 skipped** (SQLite); migrations, profiles,
models, chat, memory, agent tasks and isolation on PostgreSQL 16 + pgvector (73 passed);
full e2e suite **85 passed**.

**Left.** Run the harness on the real GPU machine and set profiles from the results.

## 2026-11-05: Phase 3, agent orchestration

**Existing implementation.** The executor's loop persists each model step and tool call,
resumes a run after a worker crash or an approval pause, refuses identical retries of a
failed call, and caps tool steps, time and output per reply. Nothing kept the *task*
across replies.

**Changes.** See C-08.
- Migration 0012: `agent_tasks`, `agent_task_events`
- `services/agent/tasks.py`: lifecycle, plan updates, completion rules, verification,
  tool-call observation (files, failures), run start/checkpoint, prompt block
- Tools `plan_start`, `plan_update`, `plan_status` (`services/tools/plan.py`)
- Executor: task state or planning guidance in the system prompt; tool calls observed;
  checkpoints on completion, failure (`_mark_failed`) and stop (`finish_cancelled`);
  error-recovery hint after consecutive failures
- API `/agent-tasks`: list (open/all/by status, by chat), detail with events, update
  (abandon, mark done, pause), resume (a new reply with the stored state), delete; sync
  feed carries `agent_task` changes
- Web: task card above the composer (plan, progress, blockers, checks, files; Resume,
  Mark done, Stop task); the dev fake model's `[tool:plan]` trigger

**Found while testing.** The identical-retry guard blocked `plan_update(status=completed)`
after the steps had been finished in between: plan updates now reset it. `write_file`
showed as "changed" because the sandbox also lists the file: the tool's own action wins.
The full e2e run then failed the attachment tests on the phone viewport again (prompt over
the fake model's 8k context once skills and the plan tools were both offered). Tool
schemas are now compacted for every tool (`anyOf [X, null]` → X, no `default: null`):
all definitions together cost about 5,100 estimated tokens, less than before Phase 3
added three tools (5,360).

**Verification.** `tests/test_agent_tasks.py` (12): plan recorded and given back every
run, one open task per chat and replacement, completion rules and unverified
completion, blockers, backend file and failure record, recovery hint, pause on tool-step
limit and resume, pause on a failed reply, pause on a stopped reply, state present when
the first message no longer fits the context, resume/abandon/delete via the API, privacy.
Isolation sweep covers the routes. e2e `tasks.spec.ts` (desktop and phone): card appears,
pauses on a failed reply, resumes, stops. Totals: API suite **524 passed, 7 skipped**
(SQLite); migrations, agent tasks, isolation, reliability, chat and skills on PostgreSQL
16 + pgvector (78 passed); full e2e suite **83 passed**.

**Left.** A cross-chat task list in a dashboard (Phase 5); per-task limits.

## 2026-11-05: web UI for memory kinds, conflicts and skills

**Changes.** Memory panel: kind badges (events with their date, "unconfirmed",
"important", entities, "conflicts with another memory"), filter by kind, kind when adding,
a Conflicts tab (keep newer / keep earlier / both true), and which kinds suggestions may
cover. New Skills panel (sidebar, with a review count): list and search, review queue,
skill view with versions (approve & publish, reject, roll back, test), files, warnings,
last test output, history, feedback, edit as a new version, export, deprecate/restore,
delete; settings for trust level and learning from tasks. The sync feed carries `skill`
changes so other windows refresh their counts. API client regenerated.

**Verification.** `pnpm typecheck`, `pnpm test`, `pnpm build`; Playwright on desktop and
phone viewports against the e2e stack (PostgreSQL + fake llama-server + sandbox + worker):
new `e2e/skills.spec.ts` (write/version/rollback/search a skill, trust settings, resolve
a memory conflict) and the existing suite: **81 passed**. API suite on SQLite:
**507 passed, 7 skipped**; skills, memory, tools, isolation and migration round-trip
also on PostgreSQL 16 + pgvector.

**Found by the e2e suite.** With skills saved, the nine skill tool definitions pushed the
attachment tests over the fake model's 8k context ("message too long"). Fixed by shorter
tool descriptions and a compact toolset for small context windows (C-06); the file tests
pass again with skills present, and two tests cover small and large context windows.

**Left.** Entity browser and skill import in the UI.

## 2026-11-05: Phase 2, skills

**Existing implementation.** Nothing for skills. Reused: tool registry and policy, the
sandbox client and per-conversation workspaces, the job queue, embeddings, audit, sync,
the memory secret detector.

**Changes.**
- Migration 0011: `skills`, `skill_versions`, `skill_events`
- `services/skills/`: `format.py` (Agent Skills parsing, YAML frontmatter), `validate.py`
  (format, limits, paths, secrets, injection, risky commands), `registry.py` (lifecycle,
  trust policy, search, usage outcomes, feedback), `testing.py` (sandbox tests),
  `prompt.py` (skill index), `jobs.py` (`skill.propose`, `skill.test`)
- Tools: `skill_search`, `skill_read`, `skill_create`, `skill_update`, `skill_validate`,
  `skill_test`, `skill_publish`, `skill_deprecate`, `skill_history`; read/history/search/
  validate/deprecate offered only once the user has a skill
- Executor: skill index in the system prompt; skill uses settled at the end of each run;
  proposal job queued after multi-step runs
- API `/api/v1/skills…`: list/search, detail, versions, events, create, new version,
  validate, publish (approve / rollback), reject, test, deprecate, restore, feedback,
  delete, export (zip), import (zip), settings (trust, auto-generation)
- PyYAML moved to runtime dependencies (C-07)
- Isolation sweep covers every skills route

**Verification.** `tests/test_skills.py` (31 tests): validation cases, versioning and
rollback, duplicate names, search and feedback, export/import, progressive tools, model
creates → pending → user approves → indexed → read → use counted as success, failure
counted after a failed tool, conservative never generates, autonomous publishes only a
tested version (real sandbox, process mode), failing test reported, generation proposes
for approval, declines, improves a used skill, tests a generated skill in the job,
privacy. Full suite on SQLite; new suites, isolation and migration round-trip on
PostgreSQL 16 + pgvector.

**Left.** Skill manager UI; LLM-graded skill evaluation beyond tests and outcomes.

## 2026-11-05: Phase 1, memory intelligence

**Existing implementation.** ADR 0009 memory: facts with review, scopes, embeddings,
recall excerpts, extraction of user facts, `remember`/`search_memory` tools.

**Changes.** See [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md) C-02…C-05.
Migration 0010 (columns, `memory_conflicts`, `memory_entities`, `memory_entity_links`,
`memory_relations`, `superseded` status, GIN full-text indexes, full downgrade).
New modules `memory/ranking.py`, `memory/sensitive.py`, `memory/entities.py`; extended
`store.py`, `retrieval.py`, `jobs.py` (extraction of four kinds, `memory.reembed`), the
memory API (search, conflicts, entities, relations, re-embed), the `remember` and
`search_memory` tools.

**Verification.** `tests/test_memory_intelligence.py` (31 tests) and the existing 22
memory tests unchanged and passing; isolation sweep extended; full suite
454 passed / 7 skipped on SQLite; memory suites and migrations on PostgreSQL.

**Left.** Memory manager UI for the new fields; periodic LLM consolidation (Phase 7);
measuring retrieval precision/recall on real data before tuning weights.

## 2026-11-05: Phase 0, audit

Read the API, worker, models, migrations, tests and the Hermes Agent sources (memory,
session search, skills tools). Baseline: 414 passed, 7 skipped. Result in
[AUDIT.md](AUDIT.md).

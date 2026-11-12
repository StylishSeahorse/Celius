# Architecture decisions: the assistant roadmap

These continue the project ADRs in [docs/adr](../adr/) (0001–0009), which still hold.
Decisions here are numbered C-01, C-02, … and are referenced from code comments
(`ADR C-03`). Each states the context, the decision, and what was rejected.

---

## C-01: Hermes Agent as a reference, not a dependency

Status: accepted · 2026-11-05

**Context.** The roadmap asks for Hermes Agent's strongest ideas: persistent and procedural
memory, skills, progressive disclosure. Hermes is a single-user CLI agent: memory is two
bounded Markdown files (`MEMORY.md` ~2,200 chars, `USER.md` ~1,375) injected as a frozen
snapshot at session start; session search is SQLite FTS5; skills are directories under
`~/.hermes/skills/` with `SKILL.md`, listed by `skills_list` (name + description) and
loaded by `skill_view` (with `file_path` for supporting files); `skill_manage` creates and
patches them; a background review can create skills after complex tasks, staged for
approval; skill security scanning is opt-in and advisory.

**Decision.** Re-implement the behaviours on Celius's stack (FastAPI, PostgreSQL +
pgvector, the existing worker, tool policy and sandbox). Adopted:
- the split between knowledge given to every chat (Celius: "always use" memories) and
  knowledge fetched on demand (relevant memories, recall, `search_memory`);
- a per-run snapshot: retrieval happens once when a run is prepared (as before);
- procedures belong in skills, not in memory (`procedural` memories are one-line
  pointers; full procedures are skills);
- progressive disclosure: index of names/descriptions → `skill_read` → supporting files;
- background skill creation after multi-step tasks, staged for approval.

**Rejected.** File-based memory (Celius is multi-user, already has reviewed, scoped,
audited DB memory and sync); Hermes's Python dependencies; advisory-only skill scanning
(Celius blocks injection attempts and secrets; see C-06).

---

## C-02: Four kinds of memory in the existing table

Status: accepted · 2026-11-05

**Context.** The roadmap wants user, semantic, episodic and procedural memory. ADR 0009's
`memories` table already has scope, review, provenance, embeddings and deletion
guarantees that are tested.

**Decision.** Extend `memories` (migration 0010) with `category`, `importance` (0–1),
`confidence` (0–1: 1.0 = typed or approved by the user, 0.9 = saved by the model when the
user said it, 0.8/0.5 = extracted and stated/inferred), `occurred_at` (events),
`superseded_by_id` and free-form `attributes`. A new status `superseded` keeps a
corrected fact for history; it is never given to a chat. Extraction asks for all four
kinds (per-user selection in memory settings) and still defaults to "nothing".

**Rejected.** Separate tables per kind (four copies of the same scope/review/deletion
logic); a separate vector store.

---

## C-03: Configurable hybrid ranking, relevance gate unchanged

Status: accepted · 2026-11-05

**Decision.** Relevant memories are ordered by
`(Ws·semantic + Wk·keyword + Wr·recency + Wi·importance) / Σweights_present ×
(0.5 + 0.5·confidence)` with defaults 0.50 / 0.25 / 0.15 / 0.10 (`MEMORY_WEIGHT_*`) and a
30-day recency half-life. Without embeddings the semantic weight leaves the denominator, so
scores stay on 0–1. The relevance gate of ADR 0009 is kept: recency and importance only
order memories that are already relevant, so they can never pull an unrelated fact into a
prompt. Memories under `MEMORY_MIN_CONFIDENCE` (0.3) are never given; unconfirmed ones
(< 0.8) are labelled "(unconfirmed)" in the prompt, events "(episodic, date)".

Keyword candidates come from PostgreSQL full-text search on GIN indexes (`'simple'`
configuration: no stemming, works for any language) instead of `LIKE '%term%'` scans;
SQLite (tests) keeps `LIKE`. Recall excerpts keep the 0.7/0.3 blend their thresholds were
calibrated on.

---

## C-04: Secrets are refused by the backend

Status: accepted · 2026-11-05

**Context.** "Never store secrets" was only an instruction in the extraction prompt.

**Decision.** `memory/sensitive.py` detects private keys, provider key formats (OpenAI,
GitHub, Slack, AWS, Google), JWTs, bearer tokens, `password=`/`token:` assignments,
credentials in URLs and long high-entropy tokens. `add_memory` and `set_content` refuse
them for every source (API 422 `sensitive`, a tool error, extraction skips) and audit the
refusal without the text. Exchanges are redacted before they are indexed for recall.
Skill validation uses the same detector. Detection errs towards refusing.

---

## C-05: Entities and relations in PostgreSQL, no graph database

Status: accepted · 2026-11-05

**Decision.** `memory_entities` (owner, kind, name, normalised key; unique per owner+kind+
key), `memory_entity_links` (memory ↔ entity) and `memory_relations` (entity —relation→
entity, with the memory it was learned from). Extraction and `remember` attach entities;
`search_memory(about=…)` filters by one; the API lists entities with their memories and
relations. Orphaned entities are pruned when memories are deleted.

**Rejected.** A graph database: one person's knowledge graph is thousands of rows, joins
are enough, and backups stay one `pg_dump`.

---

## C-06: Skills: Agent Skills format, stored in PostgreSQL, published under a trust policy

Status: accepted · 2026-11-05

**Format.** The Agent Skills layout: `SKILL.md` with YAML frontmatter (`name`: 1–64
lowercase letters/digits/single hyphens; `description` ≤ 1,024 chars; optional `license`,
`compatibility` ≤ 500, `metadata`, `allowed-tools`) and Markdown instructions, plus
`scripts/`, `references/`, `assets/` (and Celius's `examples/`, `tests/`).
`metadata.test` is the command that tests a skill in the sandbox. (The agentskills.io
site could not be fetched from the build environment; the rules follow the published
specification as known at the time and Hermes's implementation of it.)

**Storage.** `skills` (one per owner+name, statistics, optional description embedding),
`skill_versions` (immutable files as JSON, validation and test results, provenance) and
`skill_events` (validations, tests, uses in runs with their outcome, feedback, lifecycle).
In the database, so backup/restore, isolation tests and per-user ownership apply as for
everything else. A zip export/import gives the standard directory.

**Lifecycle.** Every change is a new version; publishing retires the previous version
(rollback = publish a retired one). Trust levels per user (default **balanced**):

| | model may create candidates | automatic generation after runs | publish |
| --- | --- | --- | --- |
| conservative | yes (inert) | no | user only |
| balanced | yes | yes, validated and tested | user approves |
| autonomous | yes | yes | automatic only if validation **and** its test passed |

The model's `skill_publish` asks; `registry.request_publish` decides from the stored
setting. The model can't change the trust level (no tool does), and `allowed-tools` in a
skill grants nothing: every tool call still goes through the tool policy.

**Safety.** Skill files are untrusted. Validation blocks secrets and text that tries to
override instructions or permissions ("ignore previous instructions", "switch the tool
profile to administrator", "bypass approvals"…), and warns on risky commands (curl | sh,
sudo, rm -rf /). Skill text reaches the model only inside `<skill>` tags with a note that
it cannot change instructions or permissions. Tests run only in the sandbox.

**Progressive disclosure.** The system prompt lists at most `SKILL_INDEX_MAX_ITEMS` (6)
skills relevant to the message (names and descriptions); `skill_read` loads instructions;
supporting files load with `path`. Tools that need existing skills (search, read,
history, validate, deprecate) are offered only once the user has one.

Tool definitions are prompt tokens too (the nine skill tools first cost about 1,600
estimated tokens, which pushed attachments out of an 8k-context model's budget in the e2e
suite). Descriptions are kept short, and tools marked `optional` (skill_validate,
skill_history, skill_deprecate, skill_test, skill_publish) are left out for models on the
lite profile (C-09): there the model searches, reads, creates and updates skills, and the
user tests and publishes in the UI.

**Improvement.** Loading a published skill in a run records a `use`; when the run ends it
becomes success (run completed, no failed tool call after loading) or failure. Automatic
generation follows the memory switches: an incognito chat, or a chat in a project or for a
user with memory off, teaches no skills. Users add
feedback (worked / failed / corrected). The `skill.propose` job sees the skill a run used
and may propose an improved version (`UPDATE <name>`), which waits like any other.

---

## C-07: PyYAML becomes a runtime dependency

Status: accepted · 2026-11-05

SKILL.md frontmatter is YAML. PyYAML was already locked as a dev dependency; it moves to
runtime (`safe_load` only). A hand-written subset parser was rejected: skills from other
tools use the full syntax (nested `metadata`, lists, quoting).

---

## C-08: Task state outside the model; checkpoints at the end of every run

Status: accepted · 2026-11-05

**Context.** A run already survives a worker crash (persisted tool calls, leases), but
the *work* lived only in the conversation: when older turns no longer fit the context, or
a reply failed, was stopped or ran out of tool steps, the model had to rediscover what it
was doing. Small local models are poor at this.

**Decision.** An `agent_tasks` row per piece of multi-step work (one open per chat):
objective, plan (numbered steps with status and note), blockers, verifications, notes,
files, counters, status (`active`, `paused`, `blocked`, `completed`, `failed`,
`abandoned`). `agent_task_events` keeps the history and a checkpoint per run.

- The **model** writes intent: `plan_start` (objective and steps) and `plan_update`
  (step status/note, new steps, findings, blockers, verification results, task status).
  These tools change only Celius's record, so they are `read` risk.
- The **backend** writes facts the model can't be trusted to report: files the tools
  wrote or changed (from tool arguments and the sandbox's changed-files list), tool calls
  and failures, and a checkpoint when each run ends. A failed or stopped reply, or one
  that used all its tool steps with work left, **pauses** the task with the reason; the
  next run in the chat (a user message, or `POST /agent-tasks/{id}/resume`) resumes it
  and is told it was interrupted.
- Every run in the chat gets the state in its system prompt (`<task_state>`, at most
  ~3,500 characters), marked as a record rather than instructions, so the work continues
  even when the history that created it has been trimmed.
- Completion needs every step done or skipped; completing without a passing
  verification is allowed but flagged (`verified: false/None`) and the model is told to
  say so.
- Error recovery: after two failed tool calls in a row, the result tells the model to
  step back, check its assumptions, change approach, or record a blocker. (The existing
  rule refusing an identical retry of a failed call stays; a successful plan update now
  resets it, as the state changed.)

Tool definitions stay cheap: optional fields are sent without JSON Schema null
boilerplate (`ToolSpec.parameters()` compacts every built-in tool's schema; pydantic
still validates the arguments).

**Rejected.** Keeping the plan in message parts only (lost with trimmed history, not
queryable); letting the model report files and outcomes (unreliable); a separate planner
model call per run (doubles inference time on one GPU).

---

## C-09: Model capability registry, prompt profiles and a modular prompt

Status: accepted · 2026-11-05

**Context.** Celius runs local models of very different ability on one GPU. The audit
found a `model_catalog` table nothing wrote to; capabilities came from environment flags
and llama-server's `/props`, and a tool-calling probe was kept in the memory of the
process that ran it (the API's probe never reached the worker, and a restart forgot it).
The system prompt was one string built in `_prepare`.

**Registry.** `model_catalog` (migration 0013) now holds, per provider and model: what
was detected (capabilities, context size, refreshed at most every 5 minutes), the result
of the tool-calling probe (`tool_calling_verified`), and what an administrator set:
profile, capability overrides (tool calling, vision, parallel tool calls, structured
output, thinking toggle, context size), limits (tool steps per reply, output tokens) and
notes. Precedence: **admin > verified > reported > configured**. Nothing is inferred from
a model's name. Each effective value records its source (shown in the UI).

**Profiles.** `lite`, `standard`, `advanced`, set per model by an administrator, else
`MODEL_DEFAULT_PROFILE`, where `auto` uses the measured context window (< 16,384 tokens:
lite; ≥ 65,536: advanced; unknown: standard).

| | lite | standard | advanced |
| --- | --- | --- | --- |
| Principles module | small steps, one tool per turn, short replies, check results, ask when unsure | none (unchanged prompt) | understand, plan, act, check, adjust; parallel independent calls; report what wasn't checked |
| Tool calls per turn | 1 (extra calls refused without counting as failures) | many | many |
| Toolset | compact (optional tools out) | full | full |
| Memories / recall excerpts | ≤ 4 / ≤ 1 | settings | settings |
| Skills listed | ≤ 3 | `SKILL_INDEX_MAX_ITEMS` | 10 |
| Tool steps per reply | ≤ 12 | `TOOL_MAX_ITERATIONS` | `TOOL_MAX_ITERATIONS` |
| Planning guidance | one sentence | full | full |

Budgets are caps on the deployment's settings, so a profile never raises what an
administrator limited, except the advanced skill index. Admin limits and overrides
(e.g. allowing parallel calls on a lite model) win over the profile.

**Modular prompt.** `services/prompting.py` assembles named sections in a fixed order:
identity, principles, project instructions, project files, the data-not-instructions note,
task state, memory, skills, tool rules (last, where small models attend most). Trusted
instructions and data are separate sections, and data sections say they are data. Each
run records its profile and the estimated tokens per section in `run.started`
(`prompt_sections`), the measurements Phase 7 needs. The standard profile renders the
same prompt as before (tested).

**Context budget order.** The system prompt is built first; the newest message's
attachments then take `ATTACHMENT_CONTEXT_SHARE` of what it leaves (previously of the
whole window, which overflowed small models once the prompt grew; found in Phase 5).

**Tool budget.** Tool definitions go with every request. With a known context window
they may use at most `TOOL_CONTEXT_SHARE` (35%) of it: core tools (files, Python,
web_fetch, memory, task_list/task_add, plan_start/plan_update) always stay; the others
are added cheapest first, optional ones last, while they fit. On an 8k model that keeps
about 2,900 tokens of tools instead of 5,800; large models keep everything. Tools left
out for space are not reported to the model as "not available" (they aren't a
permission matter). Found in Phase 5, when the task tools tipped the e2e attachment
tests over an 8k context for the third time: trimming descriptions had only delayed it.

**Tool calling.** One call per turn on lite (configurable per model). `ChatRequest`
gains `response_format`; memory extraction asks for a JSON object when the registry says
the model supports structured output (`MODEL_ENABLE_STRUCTURED_OUTPUT` or per model),
since small models often wrap JSON in prose. Tool schemas stay compact (C-08).

**Evaluation.** `devtools/prompt_eval.py` runs scenarios (plain answer, pick a tool,
remember, plan a multi-step task, write then verify) through the real stack: registry,
prompt builder, executor and tools, in a temporary database, once per profile, against
any OpenAI-compatible server, and reports pass/fail, prompt tokens per section, tools
offered, tool calls and time. It measures; choosing a model's profile stays an
administrator's decision. **No results from a real model are recorded yet**: the harness
has only run against the fake server here.

**Rejected.** Profiles keyed on model-name patterns (wrong as soon as a fine-tune is
renamed); separate prompt templates per model family (unmaintainable); per-chat profile
choice for users (a property of the model, not of the conversation).

---

## C-10: Personal tasks, reminders, suggestions and the daily briefing

Status: accepted · 2026-11-05

**Context.** Celius had projects and scheduled prompts, but nothing that holds the
user's own commitments. Agent tasks (C-08) are Celius's record of its own work; personal
tasks are the user's to-dos. They are kept apart on purpose.

**Decision.**
- `user_tasks` (migration 0014): title, notes, status (`suggested`, `open`,
  `in_progress`, `done`, `cancelled`), priority 0–3, deadline (all-day deadlines stored
  as midnight in the user's time zone), reminder, recurrence (`daily`, `weekdays`,
  `weekly`, `monthly`, `yearly` × interval), subtasks (`parent_id`, loops refused),
  dependencies (`blocked_by`, same owner), tags, attached files (same owner), project
  (one the user can read), source (`user`, `model`, `suggested`) and source chat.
- **Time zone** per user (`assistant.timezone` preference, default `DEFAULT_TIMEZONE`):
  "today", overdue and repeats are computed in it; a weekly 09:00 stays 09:00 across
  daylight-saving changes; monthly repeats clamp to the month's end.
- **Recurrence**: completing a repeating task creates the next occurrence, deadline and
  reminder moved by the same step. A repeating task must have a deadline.
- **Reminders** fire from the worker loop (`fire_due_reminders`), with no model involved,
  through the existing Web Push job. Claimed by a conditional UPDATE, so each fires once
  with several workers; missed ones fire when a worker returns; only for active tasks;
  moving a reminder re-arms it.
- **Tools**: `task_list` and `daily_briefing` read; `task_add` and `task_update` are
  `write`, because they change the user's commitments: the user's tool policy is the
  authorisation (Standard asks first). The tool description also tells the model not to
  change dates or priorities, or cancel, unless asked. `daily_briefing` is optional
  (lite profile: `task_list` covers it). A one-line `assistant` prompt module tells the
  model the task list exists.
- **Suggestions** (proactive, configurable): when the user allows it (default on), the
  memory extraction call also asks for to-dos the user said they must do, with today's
  date for "by Friday". No extra model call. They become `suggested` tasks, deduplicated
  by title, never reminded or listed as to-dos until accepted; dismissing deletes them.
  Incognito chats suggest nothing (extraction doesn't run).
- **Briefing** (`GET /assistant/briefing`, and the `daily_briefing` tool): overdue, due
  today, next 7 days, in progress, blocked, suggestions, Celius's open agent tasks, and
  what waits for the user (approvals, memories, skills). The **suggested order** is a
  deterministic score: overdue +100, due today +60, due within 3 days +30…, priority
  ×10, in progress +15, a little for age; blocked tasks are left out. It is explainable,
  and it changes nothing. "Plan my day" in a chat lets the model turn it into prose.
- **Daily briefing schedule**: an assistant setting creates an ordinary scheduled task
  (daily, the user's time zone) with the **read-only** tool profile: it can read tasks
  and the briefing, never change them.
- Calendar information joins the briefing in Phase 6 (`calendar: null` until then).

**Rejected.** Reusing agent tasks for to-dos (different owner of the intent, different
lifecycle); suggestions as notifications (intrusive); a model-written plan stored as
truth (the plan is computed; the model only explains it); reminders through the model
(they must fire whether or not inference is available).

---

## C-11: Calendar and email providers, the sending rule, and the automation engine

Status: accepted · 2026-11-05

**Accounts.** `integration_accounts` (migration 0015): kind (`calendar`, `email`),
provider (`caldav`, `imap`), non-secret settings, credentials sealed with
`services/secrets.py` (never returned; the API says only whether one is set), enabled,
last error. Like MCP servers, reaching a server on the local network (a home Nextcloud
or mail server) needs `allow_private_network`, which only an administrator can set.

**Calendar.** One interface over Celius's own calendar (`calendar_events`, always
there, events can block time for a task) and CalDAV accounts (Nextcloud, Radicale,
iCloud, Fastmail…). CalDAV through the SSRF-guarded HTTP client; recurring events are
expanded by the server (`calendar-query` with `<expand>`), so RRULE is never evaluated
locally. iCalendar is read and written by a small in-repo module (the needed subset:
folding, escaping, TZID/UTC/floating/all-day, DURATION); a library was not added. A
calendar object holding several events, or a repeat rule, is **read-only** from Celius:
rewriting it would drop the others (a data-loss bug the tests caught). A calendar that
fails doesn't hide the others; its problem is reported. The briefing gains today's
events, overlaps, and timed deadlines that fall inside an event. Recurring local events,
free/busy across other people, and invitations are not implemented.

**Email.** IMAP/SMTP (Gmail, Outlook/Microsoft 365, Fastmail with app passwords; OAuth
providers are not implemented). The standard library clients are subclassed to connect
to an address checked by netguard (TLS still verifies the host name), so a mail host
name can't reach the server's own network. Reading uses `BODY.PEEK` (never marks read);
search criteria are quoted and refuse line breaks; recipients are validated (≤ 20);
messages are built with `EmailMessage` (no header injection). Drafts are appended to the
Drafts folder with `\Draft`. Email content reaches the model inside `<email>` tags with
a note that it is data and must never prompt sending.

**Sending always asks.** `ToolSpec.always_ask` makes `policy.decide` answer "ask" for
every call, whatever the profile, and the approval endpoint neither records "always
allow this tool" for it nor lets "allow all for this chat" approve it. Only
`email_send` has it. The model can't change it; a test runs it under the
Administrator profile with "allow all" granted. Users can still send from the UI.

**Tools.** `calendar_events` (events, overlaps, free time 08–18), `email_search`,
`email_read`, `automation_list` read; `calendar_add`, `calendar_change`, `email_draft`
external; `email_send` external + always_ask; `automation_create` write. Email tools are
offered only once an email account exists; `calendar_change`, `email_send`,
`automation_*` are optional (left out on lite and when the tool budget is tight).

**Automation engine** (`services/automation/`), on the worker loop, no model needed:
- Definitions: trigger + conditions (field/op/value over `task.*`, `email.*`, `page.*`)
  + 1–5 actions; validated server-side whoever writes them (UI JSON, recipes, or the
  model's `automation_create`, which is how natural-language rules are made).
- Triggers: `schedule` (daily/weekly/interval, the user's time zone), `task_overdue`
  and `task_due_soon` (checked every 5 minutes, each task once per deadline),
  `task_status` and `task_created` (events recorded when tasks change; rules' own
  tasks don't emit, so rules can't feed each other), `email_received` (new UIDs since
  the last check; the first check only records the position, so old mail never fires)
  and `web_page_changed` (text hash through the guarded web client; the change is
  quoted). Network checks run as `automation.check` jobs so a slow server never holds
  up the worker loop. Claims are conditional UPDATEs (several workers fire once).
- Actions: `notify` (Web Push), `create_task`, `run_prompt` (a new conversation, the
  read-only tool profile by default and never wider than the user may use; what
  triggered it is appended inside `<trigger_data>` as data).
- History: `automation_runs` (status, reason, what each action did, error), the last
  200 kept. After 5 failures in a row an automation switches itself off with the
  reason and the user is notified; switching it on again resets the count. At most 50
  automations per user.

**Rejected.** A workflow DSL or visual editor (recipes + validated JSON + the model
cover it); polling email with the model; storing provider passwords in environment
variables; letting a profile or grant skip the send confirmation.

---

## C-12: Measure from what is recorded; improve only what a measurement shows

Status: accepted · 2026-11-07

**Analytics** (`services/analytics.py`, `GET /insights`, Account > Insights) are computed
on demand from tables Celius already keeps: runs, tool calls, `run.started` (profile,
prompt tokens per section, dropped history), agent tasks and their events, skill events,
memory statuses and usage parts, personal tasks, automation runs. No new logging, no
model. Per person; administrators can see everyone. Rates are null when nothing was
measured, so "no data" never reads as 0%. Figures cover the spec's list: task completion,
tool success, skill reuse and evaluation success, recovery after interruption, context
consumption, execution duration (memory precision/recall come from the benchmark below,
since real conversations have no labels).

**Memory benchmark** (`devtools/memory_eval.py`, labelled set in `devtools/data/`): the
real retrieval path in a temporary database; precision, recall, MRR and "silence" (an
unrelated question gets nothing); `--grid` compares weight sets; `--save`/`--compare`;
`--embeddings-url` for the real embedding model, `--hash-embeddings` offline.

First result, keyword-only: precision 0.50, recall 0.47, MRR 0.50, silence 0.75, and the
four weight sets scored the same. So the weights weren't the problem (they only order
memories the relevance gate already admits); the misses were word forms ("deploy" /
"deploys") and a length-dampened threshold edge (0.197 vs 0.2). A light suffix stemmer in
`retrieval.terms`/`keyword_score` (shared with attachment excerpts and skill search)
raised it to **precision 0.68, recall 0.72, MRR 0.75, silence 0.75**. A first stemmer
version that cut "runs" to "run" dropped precision to 0.63 and was reverted by the same
benchmark, which now runs in the test suite as a regression guard. The gate thresholds
became settings (`MEMORY_MIN_SIMILARITY`, `MEMORY_MIN_KEYWORD_SCORE`, defaults unchanged).
The weights stay as they were: no measurement here justifies changing them; paraphrase
misses ("graphics card" / "GPU") need the real embedding model, measured on the GPU box.

**Memory upkeep** (`memory/consolidate.py`, weekly per user, `MEMORY_CONSOLIDATION_DAYS`):
likely duplicates that slipped past the add-time checks become "Possibly the same fact"
conflicts (a resolved pair never comes back); inferences never confirmed nor used for
`MEMORY_STALE_DAYS` (90) go back to review with the reason. Nothing is deleted, no model.

**Skill improvement from failure clusters**: when 3 of a skill's last 10 outcomes (uses
and the user's "didn't work"/"had to correct" feedback) failed, a `skill.improve` job
gives the model the skill, the failing tool calls and the feedback, and asks for UPDATE
or NONE; at most once a week per skill; never under conservative trust or with learning
off. The result is an ordinary candidate version (validated, tested, trust policy).

**Autonomy in one place** (`services/autonomy.py`, `/assistant/autonomy`, Account):
Careful / Balanced / Hands-off presets set memory suggestion and review, task
suggestions, skill trust and learning, and the default tool profile together (the
settings still live where they did; "custom" when mixed). Presets never widen an
administrator's limit on tool profiles, and never change that sending email asks.

**Benchmarks over time**: `prompt_eval.py` and `memory_eval.py` save summaries and
compare against a previous file. Neither has run against the real models yet.

## C-13: Auto-review: a reviewer approves tool calls instead of asking

**Context.** Under Standard, every write, code run or web call waits for the user, so a
multi-step task stops at each step. "Allow all for this chat" removes the questions but
also the judgement. Other assistants offer a middle mode where a model checks each call.

**Decision.** An opt-in reviewer (`services/tools/review.py`) decides calls the policy
says to ask about, in the executor just before it would pause (`_auto_review`):

1. **Hard rules, no model.** These go to the user:
   - tools that always ask (sending email);
   - risk classes the user hasn't allowed (default write, execute, network; "external",
     meaning calendar, mail drafts and MCP, is off);
   - arguments that look like a secret;
   - risky shell patterns (`rm -rf /`, `curl | sh`, `git push --force`, …);
   - anything past `TOOL_AUTO_REVIEW_MAX_PER_RUN` approvals in one reply.
2. **The reviewer model** (`TOOL_AUTO_REVIEW_MODEL`, default the chat's model, no tools,
   temperature 0) sees the user's own last three messages, with attachment and project-file
   text replaced, and the proposed call. It never sees tool results, web pages, files or
   emails, because a prompt injection would come from those. It answers JSON:
   approve or ask, with a reason.
3. **Fail closed.** An error, a timeout (60 s) or an unclear answer leaves the call for
   the user.

The reviewer can only turn "ask" into "approve". It never refuses a call, never runs one
the profile denies, and never touches grants. A call the user decided while the review ran
keeps the user's decision. Each decision has `decision_source = "reviewer"` and a reason,
shown on the tool card ("Approved by the reviewer: …" / "Left for you: …"). It is
audited as `tool_call.auto_reviewed`, and a call left for the user isn't reviewed again
when the run resumes.

Settings: `tools.auto_review {enabled, risks}` in the user's preferences (Tools >
Permissions), the default `TOOL_AUTO_REVIEW_DEFAULT` (false), and the Hands-off autonomy
preset (C-12), which turns it on; Careful and Balanced turn it off.

**Consequences.** One extra short model call per reviewed call, which a local server
runs in sequence with the chat. The reviewer judges intent from the user's words
only, so it errs towards asking on vague requests; that is the intended trade-off. It
doesn't make prompt injection impossible, because a user's request can be broad enough
to cover a harmful step. The risk classes and hard rules bound what it can approve.

## C-14: Gmail through "Sign in with Google" (OAuth 2.0 + XOAUTH2)

**Context.** Gmail worked only with an app password (C-11). App passwords need 2-step
verification, are being phased out for many accounts, and give full account access with
nothing to show the user what was granted.

**Decision.**
- **Same mailbox code.** A new email provider, `gmail`, uses the same IMAP/SMTP mailbox
  and signs in with **XOAUTH2** and the `https://mail.google.com/` scope.
  - Reading, drafts and the always-ask sending rule are unchanged.
  - The Gmail REST API would add a second mail implementation, with scopes that differ
    only on paper; IMAP needs the full scope anyway.
- **Consent** (`services/integrations/google.py`):
  - Authorisation-code flow with PKCE (S256), `access_type=offline` and
    `prompt=consent`.
  - The `state` is sealed with the server key. It holds the user, the verifier, a
    10-minute expiry and a hash of a nonce.
  - The nonce goes to the browser in an HttpOnly `SameSite=Lax` cookie on the callback
    path. The session cookie is `Strict`, so it isn't sent on Google's redirect back;
    the callback identifies the user from the state and checks the nonce.
  - So a consent can't be finished in another browser, and can't attach a mailbox to
    another user.
- **Callback:**
  - The code is exchanged server-side with the client secret.
  - The ID token from that response is checked: audience, issuer, verified address.
    No signature check is needed, because it came straight from the token endpoint over
    TLS (OIDC Core 3.1.3.7).
  - The Gmail scope must have been granted (the user can untick it).
  - The same address again updates the existing account. If Google leaves out the
    refresh token on a reconnect, the old one is kept.
- **Tokens:**
  - The refresh token, access token and expiry are sealed like other credentials, and
    never returned.
  - Access tokens are refreshed two minutes before they expire and cached in memory per
    process; the API and the worker each refresh on their own.
  - `invalid_grant` becomes "sign in with Google again", shown by Check and by the tools.
- **Fixed servers:**
  - A Gmail account's config is always Google's servers, whatever is sent.
  - Its servers and password can't be changed, and it can't be created by hand.
  - So the bearer token can't be pointed at another host.
- **Removal** revokes the grant at Google (best effort).
- **Setup:** `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`, and optionally
  `GOOGLE_OAUTH_REDIRECT_URL`. Without them the button doesn't show.

**Consequences.**
- **Redirect address:** Google needs an https redirect on a real domain, or localhost.
  An installation reached only by LAN IP needs a domain, Tailscale HTTPS, or a one-off
  sign-in through localhost.
- **Testing status:** while the Google app is in "Testing", refresh tokens last 7 days.
  Publishing it unverified removes the limit but shows Google's "unverified app" warning,
  for up to 100 users.
- **Not covered yet:**
  - Google Calendar would use the same client and CalDAV with a bearer token.
  - Microsoft 365 would need the same flow against Entra ID.

## C-15: Stop replies that are stuck repeating themselves

**Context.** Local thinking models sometimes fall into a loop while thinking: the same
few sentences ("Okay. Let's write. One detail: …") again and again. The reply never
reaches an answer; it runs until the read timeout or the context fills. The user waits
minutes and gets nothing.

**Decision.** A guard (`services/model_gateway/repetition.py`) is fed the stream as it
arrives, with separate settings for the thinking and for the answer. It trips on any of:

- **Repeated sentences:** at least 90% of the last 40 sentences are ones already written
  twice before. The answer's check is stricter: 95% of 60, and only sentences of 20
  characters or more.
  - Short fillers ("Okay.") don't count.
  - A sentence must be on its third use: writing a draft and then the final copy is
    normal.
- **A verbatim cycle:** the last 1,200+ characters are one block repeated 4 times (5 in
  answers). This catches loops with no sentence breaks.
- **A length cap** (`MODEL_MAX_REASONING_CHARS`, 48,000 ≈ 12k tokens), thinking only.

Measured on samples:
- Real-looking loops trip within about 1,500 characters.
- None of these trip: the project's docs (200k characters) as thinking or answer; 80
  source files as thinking or answer; a code draft written twice.

On a trip the executor closes the stream, which makes llama-server stop generating.
What happens next depends on where the loop was:
- **In the thinking, with no answer or tool call yet:**
  - the thinking is cut back to just before the loop and gets a note;
  - the turn is retried once with `enable_thinking=false`;
  - for that turn only, a short "answer now, directly" note is added to the last
    message.
- **In the answer, or again on the retry:** the text is cut back to before the loop and
  ends with a note suggesting to ask again or turn thinking off. It isn't retried,
  because the text was already shown.

Every case is logged; a run never fails because of it. `MODEL_LOOP_GUARD=false`
switches it off.

**Prevention.** Optional sampling settings are sent with every chat request:
`MODEL_PRESENCE_PENALTY` (Qwen suggests 1.0–1.5 for thinking models that loop),
`MODEL_REPEAT_PENALTY` and `MODEL_DRY_MULTIPLIER` (llama.cpp's DRY sampler). The last
two go only to `llama_cpp` providers. They are off by default, because they change every
model's output.

**Consequences.**
- The thinking shown live includes the loop until the run ends. The stored reply keeps
  only what came before it.
- A model that ignores `enable_thinking` may loop again on the retry; it then ends with
  the note, not a timeout.
- Very long legitimate thinking is cut at the cap; raise or disable it for models that
  need more.

---

## C-16: Celius Remote — execution on registered machines through Central

Status: accepted · 2026-11-12

**Context.** Celius should run agent work on the user's own Windows and Linux machines and
show it as ordinary conversations, including external coding agents (Claude Code). Central
already has durable runs, events, approvals, leases and sync on PostgreSQL; API and
worker share nothing but the database. Details: [docs/remote/ARCHITECTURE.md](../remote/ARCHITECTURE.md).

**Decision.**
- A lightweight Python agent (`services/remote-agent`, one dependency) connects *out* over
  one WebSocket to the API; enrollment and token exchange use HTTPS. No inbound ports.
- PostgreSQL is the bus: `remote_commands` (Central → agent, at-least-once delivery,
  executed at most once thanks to durable command ids on the agent) and
  `remote_session_events` (agent → Central, per-session `seq`, unique, contiguous acks,
  agent-side disk outbox, replay on reconnect). Redis stays unused.
- Native mode keeps orchestration in the existing worker: a run with `execution=remote`
  uses tool specs with the built-in names whose handlers dispatch to the machine. Prompts,
  memory, model profiles, policy, approvals, leases and the never-re-run rule are unchanged.
- External mode (Claude Code via the official Agent SDK, `can_use_tool`) is projected into
  the same conversation: runs with `execution=external` are not claimed by the worker;
  ingested events create message parts, `tool_calls` and run events; permission prompts
  are `tool_calls` in `pending_approval`, decided through the existing routes.
- Machines are not trusted by registration: explicit authorisation, a local policy file on
  the machine (no folders, no shell by default) intersected with Central's per-machine
  config, and remote write/execute tools ask every time unless configured otherwise.

**Rejected.** Running the full Celius stack on each machine (duplicates orchestration and
the source of truth); SSH or exposed ports; terminal-text scraping of Claude Code when the
Agent SDK gives structured messages and a permission callback; a second approval system;
a message broker or new microservice; automatic failover to another machine (risks running
a task twice).

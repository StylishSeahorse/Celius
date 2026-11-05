# Implementation log

Newest first. Each entry: what changed, how it was verified, what is left.

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

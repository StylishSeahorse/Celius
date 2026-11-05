# Implementation log

Newest first. Each entry: what changed, how it was verified, what is left.

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

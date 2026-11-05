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
skill_history, skill_deprecate, skill_test, skill_publish) are left out for models whose
context window is under `TOOL_COMPACT_BELOW_CONTEXT_TOKENS` (16,384): there the model
searches, reads, creates and updates skills, and the user tests and publishes in the UI.
This is the first piece of Phase 4's per-model tool filtering.

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

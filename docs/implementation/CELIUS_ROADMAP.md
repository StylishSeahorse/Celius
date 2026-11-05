# Celius roadmap: from AI workspace to personal assistant

Status of the assistant roadmap (memory, skills, orchestration, local models, personal
assistant, integrations, continuous improvement). The original product backlog (phases
1–10, chat/files/tools/sync/people) is in [docs/backlog.md](../backlog.md) and complete.

Legend: ✅ done and tested · 🟡 partial · ⬜ not started

Related: [audit](AUDIT.md) · [decisions](ARCHITECTURE_DECISIONS.md) ·
[implementation log](IMPLEMENTATION_LOG.md)

## Phase 0: audit ✅

| Item | Status | Where |
| --- | --- | --- |
| Repository map, architecture and dependency map | ✅ | [AUDIT.md](AUDIT.md) §1–2 |
| Feature-to-code mapping | ✅ | AUDIT.md §3 |
| Database assessment | ✅ | AUDIT.md §4 |
| Gaps and plan | ✅ | AUDIT.md §5, this file |

## Phase 1: memory intelligence ✅

| Item | Status | Notes |
| --- | --- | --- |
| Four categories (user, semantic, episodic, procedural) | ✅ | C-02; migration 0010 |
| Importance, confidence, confirmed vs inferred | ✅ | approval and user edits confirm (1.0) |
| Hybrid retrieval: pgvector + full-text + recency + importance, configurable | ✅ | C-03; `MEMORY_WEIGHT_*` |
| Metadata filtering (category, entity, project), scoping | ✅ | `search_memory(kinds, about)`, `GET /memories?category=` |
| Deduplication | ✅ | existing (hash, word overlap, embeddings); superseded facts may return |
| Contradiction detection and resolution | ✅ | heuristic on add; `GET/POST /memory/conflicts` |
| Corrections / consolidation (supersede, keep history) | ✅ | `replaces` in extraction, API, `remember` |
| Extraction of all four kinds, per-user selection | ✅ | `extract_categories` setting |
| Secrets never stored | ✅ | C-04 |
| Entities and relations | ✅ | C-05 |
| Memory search for the memory manager | ✅ | `GET /memories/search` |
| Re-embedding after an embedding model change | ✅ | `POST /memory/reembed` → `memory.reembed` job |
| Source attribution | ✅ | existing (source, conversation, message); shown per reply |
| LLM-checked contradictions (beyond the heuristic) | ⬜ | extraction's `replaces` covers corrections the model sees; a periodic consolidation job is Phase 7 |
| Memory manager UI: kinds (badges, filter, add), conflicts queue, suggestion kinds | ✅ | `MemoryPanel.tsx`; e2e `skills.spec.ts` (conflicts) |
| Entity / relationship browser in the UI | ⬜ | API ready (`/memory/entities`, `/memory/relations`); entities show as badges on memories |

## Phase 2: skill system ✅

| Item | Status | Notes |
| --- | --- | --- |
| Registry, versions, events (migration 0011) | ✅ | C-06 |
| Agent Skills format, validation (format, limits, paths, secrets, injection) | ✅ | `services/skills/validate.py` |
| Tools: skill_search/read/create/update/validate/test/publish/deprecate/history | ✅ | `services/tools/skills.py` |
| Progressive disclosure (prompt index → read → files; tools appear when useful) | ✅ | `services/skills/prompt.py`, `toolset.py` |
| Trust levels conservative / balanced / autonomous | ✅ | `/skills/settings` |
| Automatic generation after multi-step runs (RECOGNISE→…→APPROVE) | ✅ | `skill.propose` job |
| Sandbox tests (tool and API/job) | ✅ | `metadata.test` |
| Usage outcomes, feedback, improvement proposals | ✅ | `skill_events`; `UPDATE <name>` |
| Version history and rollback | ✅ | publish a retired version |
| Export / import (zip of the standard directory) | ✅ | |
| Skill manager UI: list/search, review queue, versions with approve/reject/rollback/test, files, history, feedback, export, trust settings | ✅ | `features/skills/SkillsPanel.tsx`; e2e `skills.spec.ts` |
| Import from the UI | ⬜ | API ready (`POST /skills/import`, zip body) |

## Phase 3: agent orchestration ✅

Existing base kept: runs persist every tool call and resume after a worker crash
(`executor._restore_steps`), with iteration, time and output limits per reply.

| Item | Status | Notes |
| --- | --- | --- |
| Task state outside the model: objective, plan, steps, blockers, files, verification, notes | ✅ | `agent_tasks` (migration 0012), C-08 |
| Plan tools `plan_start`, `plan_update`, `plan_status` | ✅ | `services/tools/plan.py` (`plan_status` optional: compact toolsets) |
| UNDERSTAND → PLAN → EXECUTE → OBSERVE → VERIFY → REPORT guidance | ✅ | short prompt note when no task is open; profile-specific prompts are Phase 4 |
| Backend record: files written/changed, tool calls and failures | ✅ | from tool results, not from the model |
| Checkpoint at the end of every run; pause on failure, stop or tool-step limit | ✅ | `agent_task_events` |
| Recovery: state in every run's system prompt (survives trimmed history), resume API | ✅ | `POST /agent-tasks/{id}/resume` |
| Completion rules: steps finished; unverified completion flagged | ✅ | `verified` per task |
| Error-recovery hint after consecutive tool failures | ✅ | executor `RECOVERY_*` |
| Activity view: task card in the chat (plan, progress, blockers, checks, files; resume, done, stop) | ✅ | `TaskCard.tsx`, e2e `tasks.spec.ts` |
| Task list across chats (dashboard) | ⬜ | API ready (`GET /agent-tasks?status=…`); belongs with the Phase 5 dashboard |
| Per-task limits (replies, tool calls) | ⬜ | per-reply limits exist; counts are recorded per task for a later cap |

## Phase 4: local model intelligence ✅ (measurement on the real GPU pending)

| Item | Status | Notes |
| --- | --- | --- |
| Model capability registry (context, tool calling, vision, parallel tools, structured output, thinking toggle, limits, profile) | ✅ | `model_catalog` (migration 0013), `services/model_registry.py`, C-09 |
| Never from names; precedence admin > verified > reported > configured, with sources | ✅ | |
| Tool-calling probe persisted for every process | ✅ | was in-memory per process |
| Lite / Standard / Advanced profiles, per model or automatic from context size | ✅ | `MODEL_DEFAULT_PROFILE`, `PROFILE_*` |
| Modular system prompt with per-section token accounting | ✅ | `services/prompting.py`; `run.started.prompt_sections` |
| Context optimisation per profile (memory, recall, skills, toolset, tool steps) | ✅ | |
| Tool calling: one call per turn on lite; JSON mode for extraction | ✅ | |
| Admin UI (Account > Models) | ✅ | e2e `models.spec.ts` |
| Prompt evaluation harness | ✅ | `devtools/prompt_eval.py` |
| Results on the real llama-server and a chosen profile per model | ⬜ | run the harness on the GPU machine (see README) |

## Phase 5: personal assistant foundation ⬜

- ⬜ Tasks (subtasks, priority, deadline, dependencies, recurrence, project) with tools
  and API
- ⬜ Deadline/commitment detection from conversations (as proposals)
- ⬜ Reminders on the existing push infrastructure
- ⬜ Daily plan / briefing (scheduled task + tasks + calendar when available)
- ⬜ Assistant dashboard

## Phase 6: integrations and automation ⬜

Existing base: scheduled prompts (`scheduled_tasks`), MCP servers, web push.

- ⬜ Calendar provider abstraction (local, CalDAV first; Google/Microsoft via OAuth later)
- ⬜ Email provider abstraction (IMAP/SMTP first), drafts by default, sending needs approval
- ⬜ Automation engine: triggers (schedule, event, condition), conditions, actions,
  execution history, failure handling, on the job queue (no LLM needed to fire)

## Phase 7: continuous improvement ⬜

- ⬜ Execution analytics (tool success, run duration, context use) from existing tables
- ⬜ Memory quality evaluation (precision/recall on a labelled set) and periodic
  consolidation
- ⬜ Skill improvement from failure clusters; benchmarks; tune ranking weights with data

## Next (continuation point)

1. On the GPU machine: `uv run python -m devtools.prompt_eval --base-url
   http://<gpu>:8080/v1 --model <id>`; record the results in IMPLEMENTATION_LOG.md and set
   each model's profile in Account > Models.
2. Phase 5: personal tasks (subtasks, priorities, deadlines, recurrence), reminders on
   the push infrastructure, daily plan, and a dashboard that also lists agent tasks
   (`GET /agent-tasks`).
3. Small follow-ups: entity browser and skill import in the UI; a periodic memory
   consolidation job; measure retrieval precision/recall before tuning `MEMORY_WEIGHT_*`;
   model ids containing "/" can't be probed via `/models/{id}/verify-tools` (path
   parameter).

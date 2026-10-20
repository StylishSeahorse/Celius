# Phase 6 acceptance tests: memory and retrieval

Exit gate: **retrieval scope, exclusion and deletion tests pass.**

## Automated

| Suite | Covers |
| --- | --- |
| `tests/test_memory.py` | **Review**: extracted facts are proposals, never used before approval, approved (optionally edited) memories reach the next chat's prompt and the reply lists what it used; with review off they are active at once; rejected proposals lose their text and aren't proposed again; duplicates refused. **Scope**: global memories in every chat, project memories only in their project; recall from other unfiled chats, never across the project boundary, never from the chat itself; project chats recall only their project. **Exclusion**: an incognito chat is neither indexed, extracted from nor given memory; turning a chat's memory off removes it from recall at once; a project with memory off gets nothing and gives nothing (and turning it off removes its excerpts); memory off for the user switches everything off, including the memory tools. **Deletion**: a deleted memory is gone from the database and the next prompt; deleting a chat removes its excerpts and, when asked, the memories learned from it (and only then); forget everything needs confirmation and empties both stores. **Embeddings**: stored and used when configured; the embedding server failing falls back to word search. **Safety**: the memory block is marked as data. **Tools**: `remember` proposes; `search_memory` finds approved memories |
| `tests/test_migrations.py` (PostgreSQL) | 0001 → 0005 round-trip including the pgvector extension and columns |
| `e2e/memory.spec.ts` (desktop and Pixel 7) | A suggested memory appears in **To review**, is approved, and the next chat shows "Used 1 memory" listing it; an incognito chat contributes nothing; deleting a chat with "Also forget the 1 memory" removes it; add, edit and delete a memory by hand |

## Results, 2026-10-20

| Check | Result |
| --- | --- |
| API tests on SQLite | 211 passed (1 skipped: the PostgreSQL-only migration test) |
| Memory and migration tests on PostgreSQL (pgvector 0.6) | all passed |
| Web unit tests and typecheck | 16 passed |
| Playwright, desktop + Pixel 7 | see the commit for the latest run |

## Manual (on the real deployment)

1. Have a few chats about something lasting ("I'm planning a trip to Japan in May"); check
   **Memory → To review** gets sensible suggestions and nothing sensitive.
2. Approve one, start a new chat that relies on it, and open "Used … memories" under the
   reply.
3. In a project chat, check that a fact from another project doesn't show up.
4. Start an incognito chat (🧠 crossed out before sending) and confirm nothing from it is
   suggested or recalled later.
5. Optional: start an embedding server and set `MODEL_ENABLE_EMBEDDINGS=true`; ask about
   an earlier chat using different words than it used.

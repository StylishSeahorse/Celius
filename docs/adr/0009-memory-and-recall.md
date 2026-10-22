# ADR 0009: Memory and recall

Status: accepted · 2026-10-20

## Context

Phase 6 lets Celius carry knowledge between chats. There are two kinds:

- **Memories**: short facts the user wants kept ("prefers metric units").
- **Recall**: relevant excerpts of the user's earlier chats.

Both raise the same questions. Who decides what is kept? Where may it appear? What does
deletion really remove? The model runs locally on one GPU and may have no embedding model.

## Decisions

### Two stores, both owned by the user
- `memories`: one row per fact. The scope is **global** (every chat) or **project** (only
  chats in that project). The status is `proposed`, `active` or `rejected`. The source is
  user, model (the `remember` tool) or extraction. The row records the chat it came from.
- `memory_chunks`: excerpts of completed exchanges (user message plus reply, about 1,200
  characters with overlap). They are indexed by a worker job after each reply. Each chunk
  is tagged with its conversation and project.

### Nothing is remembered without consent when review is on
After a reply, a worker job asks the model for up to three lasting facts about the user.
The prompt excludes secrets, sensitive details and other people, and says to ignore any
instructions in the exchange. The facts become **proposals**. A proposal is never used
until the user approves it, possibly after editing it. The `remember` tool goes through
the same path. With review off (a per-user setting), proposals are active at once.

Rejecting a proposal erases its text. Only a hash is kept, so the same fact isn't
proposed again. Duplicates (same normalised text, high word overlap, or close
embeddings) are dropped.

### Scope is a hard rule, checked in one place
`app/services/memory/retrieval.py` decides what a chat receives:

| Chat | Memories | Recall from |
| --- | --- | --- |
| Outside projects | global | other chats outside projects |
| In project P | global and P's | other chats in P |
| Memory off (user, project or chat) | none | none |

A chat never recalls itself. Deleted chats, chats with memory off and projects with
memory off are filtered in the query, even though their excerpts are also removed when
that happens.

### Within scope, only what is relevant (revised 2026-10-22)
At first every memory in scope was given to every chat while there were 30 or fewer. In
use that filled prompts with unrelated facts. Now:
- Memories marked **always use** (who the user is, how they want answers) reach every chat
  in scope, at most 10.
- Any other memory is given only when it is relevant to the message: cosine similarity of
  at least `MEMORY_MIN_SIMILARITY` (0.55) with embeddings, or enough shared words
  (keyword score 0.2) with or without them. At most `MEMORY_PROMPT_MAX_ITEMS` (8). A
  message unrelated to anything remembered gets no memories.
- Extraction is told what is already remembered, that an empty answer is normal, and what
  not to keep (details of the task at hand, what the assistant did, guesses). It proposes
  at most 2 facts, marks each as always use or not, and skips messages under three words.

### Switches at three levels
- **User**: memory on or off, suggestions on or off, review on or off.
- **Project**: memory on or off.
- **Chat**: memory on or off ("incognito"; can be set before the first message).

Off means the chat is not indexed, nothing is extracted from it, and it receives nothing.
Turning a chat or project off removes its excerpts from recall immediately. Memories
already learned from it stay until deleted; the UI says so and offers to delete them.

### Deletion is immediate, and its limits are stated
Deleting a memory deletes the row and its embedding in the same transaction.
Deleting a chat deletes its excerpts, and the memories learned from it when the user
ticks the box (`DELETE /conversations/{id}?forget_memories=true`). "Forget everything"
removes all memories and excerpts. The UI explains what deletion cannot reach:

- replies already written that used the memory;
- exports already downloaded;
- backups made before (they age out with retention);
- the audit log, which records that something was deleted but not its text.

Models are never trained on user data.

### Embeddings are optional
Embeddings come from any OpenAI-compatible `/v1/embeddings` endpoint (for example a
second llama-server with `--embeddings`), set with `EMBEDDING_BASE_URL` and
`EMBEDDING_MODEL`.

- **PostgreSQL**: vectors live in pgvector columns without a fixed dimension, and are
  ranked by cosine distance in SQL. Rows from another embedding model are ignored
  (`embedding_model` is checked).
- **Ranking**: meaning (embeddings) and word overlap, weighted 0.7/0.3.
- **Without embeddings**, or when the embedding server is down, ranking falls back to
  word overlap. Tests cover both.
- **SQLite** (tests): vectors are stored as JSON and ranked in Python.

### Transparency
Every reply that was given memory carries a `memory` part listing the memories and
earlier chats it received. The UI shows it as "Used 2 memories and 1 earlier chat".
Memory text goes into the system prompt in `<memories>` and `<recalled>` blocks, marked
as data, not instructions. These blocks use at most `MEMORY_CONTEXT_SHARE` of the
context.

## Consequences

- Extraction costs a short model call per reply. Users can turn it off, and it is skipped
  for scheduled tasks.
- There is no vector index (dimensions vary by model). For one user's history, exact
  search is fast enough; an HNSW index can be added per model if that changes.
- Deleting a project deletes its project memories and its chats' excerpts. Its chats
  become unfiled and start building global recall from their next exchange.

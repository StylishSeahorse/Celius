"""Memory retrieval benchmark: does a chat get the memories it needs, and only those?

    uv run python -m devtools.memory_eval                       # keyword search only
    uv run python -m devtools.memory_eval --embeddings-url http://gpu-pc:8081/v1
    uv run python -m devtools.memory_eval --grid                # compare ranking weights
    uv run python -m devtools.memory_eval --save results.json --compare previous.json

Runs the real retrieval (services/memory/retrieval.py: relevance gate, hybrid ranking,
budget) over a labelled set (devtools/data/memory_eval.json by default) in a temporary
database, and reports:

  precision   share of the memories given that were needed
  recall      share of the needed memories that were given
  MRR         how high the first needed memory ranked (1 = first)
  silence     share of unrelated questions that got no memory at all

The defaults (MEMORY_WEIGHT_*) were set before any measurement; use this, with your own
labelled set and embedding model, before changing them (ADR C-03, C-12).
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

os.environ.setdefault("APP_ENV", "test")

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

import app.models  # noqa: E402, F401
from app.config import Settings  # noqa: E402
from app.db.base import Base  # noqa: E402
from app.models import Conversation, MemoryStatus, User  # noqa: E402
from app.services.memory.embeddings import Embedder  # noqa: E402
from app.services.memory.retrieval import retrieve  # noqa: E402
from app.services.memory.store import add_memory  # noqa: E402

DATASET = Path(__file__).parent / "data" / "memory_eval.json"
GRID = [
    (0.50, 0.25, 0.15, 0.10),  # the defaults
    (0.70, 0.30, 0.00, 0.00),  # meaning and words only (before Phase 1)
    (0.35, 0.45, 0.10, 0.10),  # words matter more
    (0.60, 0.20, 0.05, 0.15),  # importance matters more
]


class HashEmbedder(Embedder):
    """A deterministic stand-in (hashed bag of words with crude stemming): no server,
    reproducible, and good enough to exercise the semantic path in CI."""

    def __init__(self):
        self.model = "hash-bow-128"

    async def embed(self, texts):
        out = []
        for text in texts:
            vector = [0.0] * 128
            for word in re.findall(r"[a-z]{3,}", text.lower()):
                stem = word[:5]
                vector[int(hashlib.md5(stem.encode()).hexdigest(), 16) % 128] += 1.0
            norm = math.sqrt(sum(v * v for v in vector)) or 1.0
            out.append([v / norm for v in vector])
        return out

    async def aclose(self):
        pass


@dataclass
class Score:
    weights: tuple
    precision: float
    recall: float
    mrr: float
    silence: float
    per_query: list


async def run(
    dataset: dict,
    weights: tuple,
    *,
    embeddings_url: str | None = None,
    embedding_model: str | None = None,
    hash_embeddings: bool = False,
    root: Path,
) -> Score:
    db_path = root / f"memeval-{uuid.uuid4().hex}.db"
    semantic, keyword, recency, importance = weights
    settings = Settings(
        app_env="test",
        database_url=f"sqlite+aiosqlite:///{db_path}",
        memory_require_review=False,
        memory_weight_semantic=semantic,
        memory_weight_keyword=keyword,
        memory_weight_recency=recency,
        memory_weight_importance=importance,
        model_enable_embeddings=bool(embeddings_url),
        embedding_base_url=embeddings_url,
        embedding_model=embedding_model,
    )
    engine = create_async_engine(settings.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    embedder = HashEmbedder() if hash_embeddings else Embedder.from_settings(settings)
    keys: dict[uuid.UUID, str] = {}
    try:
        async with factory() as db:
            user = User(username="eval", password_hash="-")
            db.add(user)
            await db.flush()
            conversation = Conversation(owner_id=user.id, title="eval")
            db.add(conversation)
            await db.flush()
            for item in dataset["memories"]:
                memory = await add_memory(
                    db,
                    user.id,
                    item["text"],
                    settings,
                    source="user",
                    status=MemoryStatus.active,
                    category=item.get("category", "user"),
                    importance=item.get("importance"),
                    embedder=embedder,
                )
                if memory is not None:
                    keys[memory.id] = item["key"]
            await db.commit()
            per_query = []
            for query in dataset["queries"]:
                found = await retrieve(
                    db,
                    conversation,
                    query["text"],
                    settings,
                    embedder,
                    include_always=False,
                    track_use=False,
                )
                given = [keys.get(m.id) for m in found.memories]
                relevant = set(query["relevant"])
                hits = [k for k in given if k in relevant]
                first = next((i for i, k in enumerate(given) if k in relevant), None)
                per_query.append(
                    {
                        "query": query["text"],
                        "given": given,
                        "relevant": sorted(relevant),
                        "precision": len(hits) / len(given)
                        if given
                        else (1.0 if not relevant else 0.0),
                        "recall": len(set(hits)) / len(relevant) if relevant else None,
                        "rr": 1 / (first + 1) if first is not None else 0.0,
                    }
                )
    finally:
        await engine.dispose()
        db_path.unlink(missing_ok=True)
        if embedder is not None:
            await embedder.aclose()
    answerable = [q for q in per_query if q["relevant"]]
    unrelated = [q for q in per_query if not q["relevant"]]
    return Score(
        weights=weights,
        precision=round(sum(q["precision"] for q in answerable) / len(answerable), 3),
        recall=round(sum(q["recall"] for q in answerable) / len(answerable), 3),
        mrr=round(sum(q["rr"] for q in answerable) / len(answerable), 3),
        silence=round(sum(1 for q in unrelated if not q["given"]) / len(unrelated), 3)
        if unrelated
        else 1.0,
        per_query=per_query,
    )


async def evaluate(dataset_path: Path = DATASET, *, grid: bool = False, **options) -> list[Score]:
    dataset = json.loads(await asyncio.to_thread(dataset_path.read_text))
    with tempfile.TemporaryDirectory(prefix="celius-memeval-") as tmp:
        return [
            await run(dataset, weights, root=Path(tmp), **options)
            for weights in (GRID if grid else GRID[:1])
        ]


def report(scores: list[Score], previous: list[dict] | None = None) -> str:
    lines = [
        "| weights (sem, kw, rec, imp) | precision | recall | MRR | silence |",
        "| --- | --- | --- | --- | --- |",
    ]
    before = {tuple(p["weights"]): p for p in previous or []}

    def cell(score: Score, old: dict | None, name: str) -> str:
        value = getattr(score, name)
        return f"{value:.3f}" if old is None else f"{value:.3f} ({value - old[name]:+.3f})"

    for score in scores:
        old = before.get(tuple(score.weights))
        cells = " | ".join(cell(score, old, n) for n in ("precision", "recall", "mrr", "silence"))
        lines.append(f"| {score.weights} | {cells} |")
    misses = [q for q in scores[0].per_query if q["recall"] is not None and q["recall"] < 1]
    if misses:
        lines.append("")
        lines.append("Missed (first weights):")
        lines += [f"- {q['query']}: needed {q['relevant']}, got {q['given']}" for q in misses]
    noisy = [q for q in scores[0].per_query if not q["relevant"] and q["given"]]
    lines += [f"- unrelated question got memories: {q['query']} → {q['given']}" for q in noisy]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dataset", type=Path, default=DATASET)
    parser.add_argument("--embeddings-url", help="An OpenAI-compatible /v1 embeddings server")
    parser.add_argument("--embedding-model")
    parser.add_argument("--hash-embeddings", action="store_true", help="Offline stand-in")
    parser.add_argument("--grid", action="store_true", help="Compare several weight sets")
    parser.add_argument("--save", type=Path, help="Write the scores as JSON")
    parser.add_argument("--compare", type=Path, help="Show changes against saved scores")
    args = parser.parse_args()
    scores = asyncio.run(
        evaluate(
            args.dataset,
            grid=args.grid,
            embeddings_url=args.embeddings_url,
            embedding_model=args.embedding_model,
            hash_embeddings=args.hash_embeddings,
        )
    )
    previous = json.loads(args.compare.read_text()) if args.compare else None
    print(report(scores, previous))
    if args.save:
        args.save.write_text(json.dumps([asdict(s) for s in scores], indent=2))


if __name__ == "__main__":
    sys.exit(main())

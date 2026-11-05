"""Hybrid ranking of memories (ADR-C03).

score = (Ws·semantic + Wk·keyword + Wr·recency + Wi·importance) / (sum of weights used)
        × (0.5 + 0.5·confidence)

- semantic: cosine similarity of embeddings (absent without embeddings; its weight is then
  left out of the denominator, so keyword-only deployments still score on 0..1)
- keyword: share of the message's key words in the memory (retrieval.keyword_score)
- recency: halves every `memory_recency_half_life_days` since last use (else last update)
- importance: 0..1 stored on the memory

Weights come from Settings and are meant to be tuned against tests/test_memory_eval.py.
"""

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings


@dataclass(frozen=True)
class Weights:
    semantic: float = 0.50
    keyword: float = 0.25
    recency: float = 0.15
    importance: float = 0.10
    half_life_days: float = 30.0

    @classmethod
    def from_settings(cls, settings: Settings) -> "Weights":
        return cls(
            semantic=settings.memory_weight_semantic,
            keyword=settings.memory_weight_keyword,
            recency=settings.memory_weight_recency,
            importance=settings.memory_weight_importance,
            half_life_days=settings.memory_recency_half_life_days,
        )


@dataclass(frozen=True)
class Signals:
    semantic: float | None
    keyword: float
    recency: float
    importance: float
    confidence: float = 1.0


def recency(
    when: datetime | None, now: datetime | None = None, half_life_days: float = 30
) -> float:
    """1.0 now, 0.5 after one half-life, towards 0 after that."""
    if when is None:
        return 0.0
    now = now or datetime.now(UTC)
    if when.tzinfo is None:  # SQLite returns naive datetimes
        when = when.replace(tzinfo=UTC)
    age_days = max((now - when).total_seconds() / 86400, 0)
    return math.pow(0.5, age_days / half_life_days)


def combine(signals: Signals, weights: Weights) -> float:
    total = weights.keyword * signals.keyword
    used = weights.keyword
    if signals.semantic is not None:
        total += weights.semantic * max(signals.semantic, 0.0)
        used += weights.semantic
    total += weights.recency * signals.recency + weights.importance * signals.importance
    used += weights.recency + weights.importance
    if used <= 0:
        return 0.0
    confidence = min(max(signals.confidence, 0.0), 1.0)
    return (total / used) * (0.5 + 0.5 * confidence)

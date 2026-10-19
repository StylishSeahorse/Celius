"""Builds the prompt for a run from the branch history within the model's context budget.

Token counts are estimated (≈3 characters per token plus per-message overhead), which
overestimates for English text. Calling the server's /tokenize for every message
would add a round trip per message to every run, and an underestimate only costs
an HTTP 400 from the model server, which is reported as `model_request_rejected`.
"""

import math
from dataclasses import dataclass

from app.errors import AppError
from app.services.model_gateway import ChatMessage

CHARS_PER_TOKEN = 3
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) + MESSAGE_OVERHEAD_TOKENS


@dataclass
class HistoryItem:
    role: str  # "user" | "assistant"
    text: str


@dataclass
class BuiltContext:
    messages: list[ChatMessage]
    estimated_tokens: int
    dropped_messages: int


def context_budget(
    context_tokens: int | None, max_output_tokens: int | None, safety_margin: int
) -> int | None:
    """Tokens available for the prompt, or None when the context size is unknown."""
    if not context_tokens:
        return None
    reserved = max_output_tokens or context_tokens // 4
    return max(context_tokens - reserved - safety_margin, 0)


def _merge_consecutive(history: list[HistoryItem]) -> list[HistoryItem]:
    # Some chat templates (e.g. Mistral's) reject two consecutive turns with the same role,
    # which happens when an assistant turn failed or was cancelled with no output.
    merged: list[HistoryItem] = []
    for item in history:
        if not item.text:
            continue
        if merged and merged[-1].role == item.role:
            merged[-1] = HistoryItem(item.role, f"{merged[-1].text}\n\n{item.text}")
        else:
            merged.append(item)
    return merged


def build_context(
    system_prompt: str, history: list[HistoryItem], budget: int | None
) -> BuiltContext:
    """Keep the system prompt and the newest turns; drop the oldest turns to fit `budget`."""
    turns = _merge_consecutive(history)
    if not turns or turns[-1].role != "user":
        raise AppError(422, "no_user_message", "The prompt must end with a user message")

    system_cost = estimate_tokens(system_prompt) if system_prompt else 0
    kept: list[HistoryItem] = []
    used = system_cost
    for index, item in enumerate(reversed(turns)):
        cost = estimate_tokens(item.text)
        if budget is not None and used + cost > budget:
            if index == 0:
                raise AppError(
                    422,
                    "context_overflow",
                    "The message is too long for the model's context window",
                    estimated_tokens=used + cost,
                    budget_tokens=budget,
                )
            break
        kept.append(item)
        used += cost
    kept.reverse()

    # A history that starts with an assistant turn confuses some templates; trim it.
    while len(kept) > 1 and kept[0].role != "user":
        used -= estimate_tokens(kept.pop(0).text)

    messages = [ChatMessage(role="system", content=system_prompt)] if system_prompt else []
    messages += [ChatMessage(role=item.role, content=item.text) for item in kept]  # type: ignore[arg-type]
    return BuiltContext(
        messages=messages, estimated_tokens=used, dropped_messages=len(turns) - len(kept)
    )

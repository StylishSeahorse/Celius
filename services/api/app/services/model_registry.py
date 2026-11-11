"""Model capability registry and prompt profiles (ADR C-09).

What a model can do is established, in order of trust:

  admin      set by an administrator for this model (wins)
  verified   a live probe succeeded or failed (POST /models/{id}/verify-tools)
  reported   the server said so (llama-server /props: context size, vision)
  configured the deployment's MODEL_ENABLE_* defaults

Never from the model's name. The registry persists in `model_catalog` so the API and
the worker (separate processes) agree, and a verification survives restarts.

A profile sets how much the prompt asks of the model and gives it:

  lite      small context or weak instruction following: one tool call per step, a
            compact toolset, short principles, small memory and skill budgets, fewer
            tool steps per reply
  standard  multi-step planning, memory and skills as usual
  advanced  larger budgets, parallel tool calls, fuller planning guidance

An administrator picks it per model; otherwise MODEL_DEFAULT_PROFILE, where "auto" uses the
measured context window (PROFILE_LITE_BELOW_CONTEXT_TOKENS, PROFILE_ADVANCED_FROM_…).
"""

import logging
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.models import ModelCatalogEntry
from app.services.model_gateway import ModelInfo

log = logging.getLogger(__name__)
REFRESH_EVERY = timedelta(minutes=5)

Profile = Literal["lite", "standard", "advanced"]
PROFILES: tuple[Profile, ...] = ("lite", "standard", "advanced")
BOOL_CAPABILITIES = (
    "tool_calling",
    "vision",
    "parallel_tools",
    "structured_output",
    "reasoning_toggle",
)

# What each profile changes. Budgets are caps on the deployment's settings, never raises
# above what the administrator configured, except where marked.
PROFILE_KNOBS: dict[str, dict] = {
    "lite": {
        "memory_items": 4,
        "recall_chunks": 1,
        "skill_index_items": 3,
        "max_tool_iterations": 12,
        "compact_tools": True,
        "parallel_tools": False,
    },
    "standard": {
        "memory_items": None,  # the deployment's MEMORY_PROMPT_MAX_ITEMS
        "recall_chunks": None,
        "skill_index_items": None,
        "max_tool_iterations": None,  # TOOL_MAX_ITERATIONS
        "compact_tools": False,
        "parallel_tools": True,
    },
    "advanced": {
        "memory_items": None,
        "recall_chunks": None,
        "skill_index_items": 10,  # may exceed SKILL_INDEX_MAX_ITEMS: big contexts afford it
        "max_tool_iterations": None,
        "compact_tools": False,
        "parallel_tools": True,
    },
}


@dataclass
class ModelProfile:
    model_id: str
    provider: str
    profile: Profile
    profile_source: str  # admin | default | auto
    context_tokens: int | None
    tool_calling: bool
    vision: bool
    reasoning_toggle: bool
    parallel_tools: bool
    structured_output: bool
    max_tool_iterations: int
    max_output_tokens: int | None
    reasoning_budget_tokens: int | None
    compact_tools: bool
    memory_items: int
    recall_chunks: int
    skill_index_items: int
    sources: dict[str, str] = field(default_factory=dict)  # capability -> where it came from

    def as_dict(self) -> dict:
        return asdict(self)

    def run_settings(self, settings: Settings) -> Settings:
        """The deployment's settings with this profile's budgets applied, for one run."""
        return settings.model_copy(
            update={
                "memory_prompt_max_items": self.memory_items,
                "memory_recall_chunks": self.recall_chunks,
                "skill_index_max_items": self.skill_index_items,
                "tool_max_iterations": self.max_tool_iterations,
            }
        )


def auto_profile(context_tokens: int | None, settings: Settings) -> Profile:
    if not context_tokens:
        return "standard"  # unknown: neither shrink nor stretch
    if context_tokens < settings.profile_lite_below_context_tokens:
        return "lite"
    if context_tokens >= settings.profile_advanced_from_context_tokens:
        return "advanced"
    return "standard"


async def catalog_entry(db: AsyncSession, provider: str, model_id: str) -> ModelCatalogEntry | None:
    return await db.get(ModelCatalogEntry, (provider, model_id))


def _cap(value: int | None, limit: int | None) -> int:
    if limit is None:
        return value or 0
    return min(value, limit) if value is not None else limit


def build_profile(
    model_id: str,
    provider: str,
    info: ModelInfo | None,
    entry: ModelCatalogEntry | None,
    settings: Settings,
) -> ModelProfile:
    sources: dict[str, str] = {}
    detected = info.capabilities if info else None
    stored = (entry.capabilities if entry else None) or {}
    overrides = (entry.overrides if entry else None) or {}
    limits = (entry.limits if entry else None) or {}

    def capability(name: str, configured: bool) -> bool:
        if isinstance(overrides.get(name), bool):
            sources[name] = "admin"
            return overrides[name]
        if name == "tool_calling" and isinstance(stored.get("tool_calling_verified"), bool):
            sources[name] = "verified"
            return stored["tool_calling_verified"]
        if detected is not None and hasattr(detected, name):
            sources[name] = (
                detected.source
                if name == "tool_calling"
                else ("reported" if detected.source in ("reported", "verified") else "configured")
            )
            return bool(getattr(detected, name))
        sources[name] = "configured"
        return configured

    context = overrides.get("context_tokens")
    if isinstance(context, int) and context > 0:
        sources["context_tokens"] = "admin"
    else:
        context = (info.context_tokens if info else None) or settings.model_default_context_tokens
        sources["context_tokens"] = "reported" if info and info.context_tokens else "configured"

    if entry is not None and entry.profile in PROFILES:
        profile, profile_source = entry.profile, "admin"
    elif settings.model_default_profile in PROFILES:
        profile, profile_source = settings.model_default_profile, "default"
    else:
        profile, profile_source = auto_profile(context, settings), "auto"
    knobs = PROFILE_KNOBS[profile]

    parallel = overrides.get("parallel_tools")
    if isinstance(parallel, bool):
        sources["parallel_tools"] = "admin"
    else:
        parallel = knobs["parallel_tools"]
        sources["parallel_tools"] = "profile"

    max_iterations = limits.get("max_tool_iterations")
    if not isinstance(max_iterations, int) or max_iterations < 1:
        max_iterations = _cap(settings.tool_max_iterations, knobs["max_tool_iterations"])
    max_output = limits.get("max_output_tokens")
    if not isinstance(max_output, int) or max_output < 1:
        max_output = settings.model_max_output_tokens
    budget = limits.get("reasoning_budget_tokens")
    if isinstance(budget, int) and budget > 0:
        sources["reasoning_budget_tokens"] = "admin"
    else:
        budget = settings.model_reasoning_budget_tokens
        sources["reasoning_budget_tokens"] = "configured"

    return ModelProfile(
        model_id=model_id,
        provider=provider,
        profile=profile,
        profile_source=profile_source,
        context_tokens=context,
        tool_calling=capability("tool_calling", settings.model_enable_tool_calling),
        vision=capability("vision", settings.model_enable_vision),
        reasoning_toggle=capability("reasoning_toggle", False),
        parallel_tools=parallel,
        structured_output=capability("structured_output", settings.model_enable_structured_output),
        max_tool_iterations=max_iterations,
        max_output_tokens=max_output,
        reasoning_budget_tokens=budget,
        compact_tools=knobs["compact_tools"],
        memory_items=_cap(settings.memory_prompt_max_items, knobs["memory_items"]),
        recall_chunks=_cap(settings.memory_recall_chunks, knobs["recall_chunks"]),
        skill_index_items=knobs["skill_index_items"]
        if profile == "advanced"
        else _cap(settings.skill_index_max_items, knobs["skill_index_items"]),
        sources=sources,
    )


async def resolve(
    db: AsyncSession,
    gateway,
    model_id: str,
    settings: Settings,
    *,
    info: ModelInfo | None = None,
) -> ModelProfile:
    """The effective profile of `model_id`. Records what the server reports, so the
    registry lists every model that has been used."""
    if info is None:
        info = await gateway.find_model(model_id)
    provider = getattr(gateway, "provider", "unknown")
    key = info.id if info else model_id
    entry = await catalog_entry(db, provider, key)
    if info is not None:
        try:
            async with db.begin_nested():
                entry = await record_detected(db, provider, info, entry)
        except IntegrityError:
            # Another run registered this model at the same moment: use its row.
            entry = await catalog_entry(db, provider, key)
    return build_profile(key, provider, info, entry, settings)


async def record_detected(
    db: AsyncSession, provider: str, info: ModelInfo, entry: ModelCatalogEntry | None = None
) -> ModelCatalogEntry:
    entry = entry or await catalog_entry(db, provider, info.id)
    detected = {
        **info.capabilities.model_dump(),
        "context_tokens": info.context_tokens,
    }
    if entry is None:
        entry = ModelCatalogEntry(
            provider=provider,
            model_id=info.id,
            context_tokens=info.context_tokens,
            capabilities=detected,
            overrides={},
            limits={},
            is_available=True,
            last_checked_at=utcnow(),
        )
        db.add(entry)
        await db.flush()
        return entry
    kept = {k: v for k, v in (entry.capabilities or {}).items() if "_verified" in k}
    changed = {**detected, **kept} != entry.capabilities or (
        entry.context_tokens != info.context_tokens
    )
    if changed:
        entry.capabilities = {**detected, **kept}
        entry.context_tokens = info.context_tokens
    checked = entry.last_checked_at
    if checked is not None and checked.tzinfo is None:
        checked = checked.replace(tzinfo=utcnow().tzinfo)
    if changed or not entry.is_available or checked is None or utcnow() - checked > REFRESH_EVERY:
        entry.is_available = True
        entry.last_checked_at = utcnow()
    return entry


async def record_verified_tools(
    db: AsyncSession, provider: str, model_id: str, supported: bool
) -> ModelCatalogEntry:
    entry = await catalog_entry(db, provider, model_id)
    if entry is None:
        entry = ModelCatalogEntry(
            provider=provider,
            model_id=model_id,
            capabilities={},
            overrides={},
            limits={},
            is_available=True,
        )
        db.add(entry)
    entry.capabilities = {
        **(entry.capabilities or {}),
        "tool_calling_verified": supported,
        "tool_calling_verified_at": utcnow().isoformat(timespec="seconds"),
    }
    entry.last_checked_at = utcnow()
    await db.flush()
    return entry


async def all_entries(db: AsyncSession) -> list[ModelCatalogEntry]:
    return list(
        (
            await db.scalars(
                select(ModelCatalogEntry).order_by(
                    ModelCatalogEntry.provider, ModelCatalogEntry.model_id
                )
            )
        ).all()
    )

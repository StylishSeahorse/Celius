"""How much Celius does on its own, in one place (ADR C-12).

The settings stay where they always were (memory, assistant, skills and tool preferences):
this module reads them together and applies presets. Two things no preset changes:
sending email always asks (C-11), and an administrator's limits on tool profiles win
(a preset never widens what a person may use).
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models import User, UserSettings
from app.services import limits
from app.services.assistant import tasks as assistant
from app.services.audit import audit
from app.services.memory.store import user_prefs as memory_prefs
from app.services.skills.registry import user_skill_prefs
from app.services.tools.policy import tool_preferences
from app.services.tools.review import review_prefs

PRESETS: dict[str, dict[str, Any]] = {
    # Nothing new without asking; nothing learned automatically.
    "careful": {
        "memory_auto_extract": True,
        "memory_require_review": True,
        "suggest_tasks": False,
        "skill_trust": "conservative",
        "skill_auto_generate": False,
        "tool_profile": "read_only",
        "tool_auto_review": False,
    },
    # The defaults: suggestions and learning, everything reviewed before it is used.
    "balanced": {
        "memory_auto_extract": True,
        "memory_require_review": True,
        "suggest_tasks": True,
        "skill_trust": "balanced",
        "skill_auto_generate": True,
        "tool_profile": "standard",
        "tool_auto_review": False,
    },
    # Learns and acts without asking where it is safe to; tests gate skills; tools that
    # change files run freely in the sandbox, the web and outside services still ask.
    "hands_off": {
        "memory_auto_extract": True,
        "memory_require_review": False,
        "suggest_tasks": True,
        "skill_trust": "autonomous",
        "skill_auto_generate": True,
        "tool_profile": "developer",
        # A reviewer decides what would ask (never sending email, never outside services).
        "tool_auto_review": True,
    },
}
FIELDS = tuple(PRESETS["balanced"])


async def current(db: AsyncSession, user: User, settings: Settings) -> dict[str, Any]:
    memory = await memory_prefs(db, user.id, settings)
    skills = await user_skill_prefs(db, user.id, settings)
    tasks = await assistant.user_prefs(db, user.id, settings)
    tools = tool_preferences(await db.get(UserSettings, user.id))
    values = {
        "memory_auto_extract": memory.auto_extract,
        "memory_require_review": memory.require_review,
        "suggest_tasks": tasks.suggest_tasks,
        "skill_trust": skills.trust,
        "skill_auto_generate": skills.auto_generate,
        "tool_profile": tools.get("default_profile") or settings.tool_default_profile,
        "tool_auto_review": review_prefs(settings, await db.get(UserSettings, user.id)).enabled,
    }
    preset = next((name for name, p in PRESETS.items() if p == values), "custom")
    return {
        **values,
        "preset": preset,
        "allowed_tool_profiles": [
            p for p in limits.PROFILE_ORDER if limits.profile_allowed(user, p)
        ],
        "always_asks": ["email_send"],
    }


async def apply(
    db: AsyncSession, user: User, changes: dict[str, Any], settings: Settings
) -> list[str]:
    """Apply a preset ({"preset": name}) and/or single fields. Returns notes, e.g. when an
    administrator's limit narrowed the tool profile."""
    values = dict(PRESETS[changes["preset"]]) if changes.get("preset") in PRESETS else {}
    values.update({k: v for k, v in changes.items() if k in FIELDS and v is not None})
    notes = []
    if "tool_profile" in values and not limits.profile_allowed(user, values["tool_profile"]):
        narrowed = limits.widest_allowed_profile(user, values["tool_profile"])
        notes.append(
            f"Tool permissions set to {narrowed}: {values['tool_profile']} isn't allowed for you"
        )
        values["tool_profile"] = narrowed
    row = await db.get(UserSettings, user.id)
    if row is None:
        row = UserSettings(user_id=user.id, preferences={})
        db.add(row)
    prefs = dict(row.preferences or {})

    def section(name: str) -> dict:
        return dict(prefs.get(name) or {})

    memory, assistant_prefs, skills, tools = (
        section("memory"),
        section("assistant"),
        section("skills"),
        section("tools"),
    )
    if "memory_auto_extract" in values:
        memory["auto_extract"] = bool(values["memory_auto_extract"])
    if "memory_require_review" in values:
        memory["require_review"] = bool(values["memory_require_review"])
    if "suggest_tasks" in values:
        assistant_prefs["suggest_tasks"] = bool(values["suggest_tasks"])
    if "skill_trust" in values:
        skills["trust"] = values["skill_trust"]
    if "skill_auto_generate" in values:
        skills["auto_generate"] = bool(values["skill_auto_generate"])
    if "tool_profile" in values:
        tools["default_profile"] = values["tool_profile"]
    if "tool_auto_review" in values:
        tools["auto_review"] = {
            **(tools.get("auto_review") or {}),
            "enabled": bool(values["tool_auto_review"]),
        }
    row.preferences = {
        **prefs,
        "memory": memory,
        "assistant": assistant_prefs,
        "skills": skills,
        "tools": tools,
    }
    audit(db, user.id, "autonomy.changed", "user", user.id, **values)
    return notes

"""Tool permission policy. Every call is checked here, server-side, whatever the model says.

A profile maps each tool to allow, ask or deny. Built-in profiles decide by the tool's
risk class; user profiles start from a built-in one and override single tools.
"""

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError
from app.models import Conversation, ToolGrant, ToolProfile, UserSettings

Decision = Literal["allow", "ask", "deny"]
Risk = Literal["read", "write", "execute", "network", "external"]

RISKS: tuple[Risk, ...] = ("read", "write", "execute", "network", "external")
DECISIONS: tuple[Decision, ...] = ("allow", "ask", "deny")

BUILTIN_PROFILES: dict[str, dict[str, Any]] = {
    "read_only": {
        "label": "Read only",
        "description": "Can look at workspace files and ask to fetch web pages. Nothing runs.",
        "risks": {
            "read": "allow",
            "write": "deny",
            "execute": "deny",
            "network": "ask",
            "external": "deny",
        },
    },
    "standard": {
        "label": "Standard",
        "description": "Asks before writing files, running code, using the web or MCP tools.",
        "risks": {
            "read": "allow",
            "write": "ask",
            "execute": "ask",
            "network": "ask",
            "external": "ask",
        },
    },
    "developer": {
        "label": "Developer",
        "description": "Writes files and runs code in the sandbox freely; asks for web and MCP.",
        "risks": {
            "read": "allow",
            "write": "allow",
            "execute": "allow",
            "network": "ask",
            "external": "ask",
        },
    },
    "administrator": {
        "label": "Administrator",
        "description": "Everything allowed without asking. Use with care.",
        "risks": {
            "read": "allow",
            "write": "allow",
            "execute": "allow",
            "network": "allow",
            "external": "allow",
        },
    },
}

CUSTOM_PREFIX = "custom:"
# A grant for every tool in a conversation ("Allow all for this chat"). Like any grant it
# only skips the question for tools that ask; a tool the profile denies stays denied.
ALL_TOOLS = "*"


@dataclass
class Profile:
    key: str  # "standard" or "custom:<uuid>"
    label: str
    base: str
    rules: dict[str, Decision] = field(default_factory=dict)

    def decide(self, tool_name: str, risk: Risk) -> Decision:
        if tool_name in self.rules:
            return self.rules[tool_name]
        return BUILTIN_PROFILES[self.base]["risks"][risk]


@dataclass
class Verdict:
    decision: Decision
    source: str  # policy | grant
    reason: str


def canonical_json(arguments: Any) -> str:
    return json.dumps(arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def args_hash(arguments: Any) -> str:
    return hashlib.sha256(canonical_json(arguments).encode()).hexdigest()


def tool_preferences(settings_row: UserSettings | None) -> dict[str, Any]:
    prefs = (settings_row.preferences if settings_row else None) or {}
    return prefs.get("tools") or {}


async def resolve_profile(
    db: AsyncSession, owner_id: uuid.UUID, key: str | None, settings: Settings
) -> Profile:
    """The named profile, else the user's default, else the deployment default.

    A profile that no longer exists falls back to read_only, never to something wider."""
    if key is None:
        prefs = tool_preferences(await db.get(UserSettings, owner_id))
        key = prefs.get("default_profile") or settings.tool_default_profile
    if key in BUILTIN_PROFILES:
        return Profile(key=key, label=BUILTIN_PROFILES[key]["label"], base=key)
    if key.startswith(CUSTOM_PREFIX):
        try:
            profile_id = uuid.UUID(key[len(CUSTOM_PREFIX) :])
        except ValueError:
            profile_id = None
        row = (
            await db.scalar(
                select(ToolProfile).where(
                    ToolProfile.id == profile_id, ToolProfile.owner_id == owner_id
                )
            )
            if profile_id
            else None
        )
        if row is not None:
            return Profile(key=key, label=row.name, base=row.base, rules=dict(row.rules))
    return Profile(key="read_only", label=BUILTIN_PROFILES["read_only"]["label"], base="read_only")


async def validate_profile_key(db: AsyncSession, owner_id: uuid.UUID, key: str | None) -> None:
    if key is None or key in BUILTIN_PROFILES:
        return
    if key.startswith(CUSTOM_PREFIX):
        try:
            profile_id = uuid.UUID(key[len(CUSTOM_PREFIX) :])
        except ValueError:
            profile_id = None
        if profile_id and await db.scalar(
            select(ToolProfile.id).where(
                ToolProfile.id == profile_id, ToolProfile.owner_id == owner_id
            )
        ):
            return
    raise AppError(422, "unknown_profile", "Unknown tool profile")


async def conversation_grants(db: AsyncSession, conversation_id: uuid.UUID) -> set[str]:
    rows = await db.scalars(
        select(ToolGrant.tool_name).where(ToolGrant.conversation_id == conversation_id)
    )
    return set(rows.all())


def decide(profile: Profile, tool_name: str, risk: Risk, grants: set[str]) -> Verdict:
    decision = profile.decide(tool_name, risk)
    if decision == "deny":
        return Verdict("deny", "policy", f"Not allowed by the {profile.label} profile")
    if decision == "ask" and tool_name in grants:
        return Verdict("allow", "grant", "Allowed for this conversation")
    if decision == "ask" and ALL_TOOLS in grants:
        return Verdict("allow", "grant", "All tools allowed for this conversation")
    if decision == "ask":
        return Verdict("ask", "policy", f"The {profile.label} profile asks first")
    return Verdict("allow", "policy", f"Allowed by the {profile.label} profile")


async def conversation_profile(
    db: AsyncSession, conversation: Conversation, settings: Settings
) -> Profile:
    return await resolve_profile(db, conversation.owner_id, conversation.tool_profile, settings)

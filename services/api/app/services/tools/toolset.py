"""The set of tools offered to the model for one run."""

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.tools.builtin  # noqa: F401 - registers the built-in tools
import app.services.tools.websearch  # noqa: F401 - registers web_search
from app.config import Settings
from app.models import Conversation, McpServer, UserSettings
from app.services.memory.store import memory_active_for
from app.services.tools.mcp import server_headers, specs_for
from app.services.tools.policy import (
    Profile,
    conversation_grants,
    conversation_profile,
    tool_preferences,
)
from app.services.tools.registry import BUILTIN, ToolSpec

MEMORY_TOOLS = ("remember", "search_memory")


@dataclass
class Toolset:
    profile: Profile
    # Every tool that exists for this user (so a call to a hidden one is recognised and
    # refused with the right reason), and the subset offered to the model.
    known: dict[str, ToolSpec] = field(default_factory=dict)
    offered: dict[str, ToolSpec] = field(default_factory=dict)
    grants: set[str] = field(default_factory=set)
    unavailable: dict[str, str] = field(default_factory=dict)  # name -> why not offered

    def openai(self) -> list[dict]:
        return [spec.openai() for spec in self.offered.values()]


async def available_tools(
    db: AsyncSession, owner_id: uuid.UUID, settings: Settings, *, sandbox: bool
) -> tuple[dict[str, ToolSpec], dict[str, str]]:
    """All tools for this user, and the reasons some of them can't be used right now."""
    prefs = tool_preferences(await db.get(UserSettings, owner_id))
    disabled = set(prefs.get("disabled_tools") or [])
    known: dict[str, ToolSpec] = dict(BUILTIN)
    unavailable: dict[str, str] = {}
    servers = (
        await db.scalars(
            select(McpServer).where(McpServer.owner_id == owner_id, McpServer.enabled.is_(True))
        )
    ).all()
    for server in servers:
        for spec in specs_for(server, settings, server_headers(settings, server)):
            known[spec.name] = spec
    for name, spec in known.items():
        if name in disabled:
            unavailable[name] = "Turned off in settings"
        elif spec.needs_sandbox and not sandbox:
            unavailable[name] = "The sandbox is not configured"
        elif spec.needs_network and not settings.tool_network_enabled:
            unavailable[name] = "Web access is turned off on this server"
        elif spec.unavailable_on and (reason := spec.unavailable_on(settings)):
            unavailable[name] = reason
    return known, unavailable


async def build_toolset(
    db: AsyncSession, conversation: Conversation, settings: Settings, *, sandbox: bool
) -> Toolset:
    profile = await conversation_profile(db, conversation, settings)
    known, unavailable = await available_tools(db, conversation.owner_id, settings, sandbox=sandbox)
    if not await memory_active_for(db, conversation, settings):
        for name in MEMORY_TOOLS:
            unavailable.setdefault(name, "Memory is off for this chat")
    offered = {
        name: spec
        for name, spec in known.items()
        if name not in unavailable and profile.decide(name, spec.risk) != "deny"
    }
    return Toolset(
        profile=profile,
        known=known,
        offered=offered,
        grants=await conversation_grants(db, conversation.id),
        unavailable=unavailable,
    )

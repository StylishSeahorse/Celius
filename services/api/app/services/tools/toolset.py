"""The set of tools offered to the model for one run."""

import json
import uuid
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.tools.assistant  # noqa: F401 - registers the personal-assistant tools
import app.services.tools.builtin  # noqa: F401 - registers the built-in tools
import app.services.tools.integrations  # noqa: F401 - calendar, email, automations
import app.services.tools.plan  # noqa: F401 - registers the plan tools
import app.services.tools.skills  # noqa: F401 - registers the skill tools
import app.services.tools.websearch  # noqa: F401 - registers web_search
from app.config import Settings
from app.models import Conversation, IntegrationAccount, McpServer, Skill, UserSettings
from app.services.context import estimate_tokens
from app.services.memory.store import memory_active_for
from app.services.tools.integrations import EMAIL_TOOLS
from app.services.tools.mcp import server_headers, specs_for
from app.services.tools.policy import (
    Profile,
    conversation_grants,
    conversation_profile,
    tool_preferences,
)
from app.services.tools.registry import BUILTIN, ToolSpec

MEMORY_TOOLS = ("remember", "search_memory")
# Offered only once the user has a skill: until then they could do nothing, and every
# tool definition costs prompt space on small local models (progressive disclosure).
# skill_create, skill_update, skill_test and skill_publish stay offered: a run that creates
# its first skill needs them right away (the toolset is fixed for the run).
SKILL_TOOLS_NEEDING_SKILLS = (
    "skill_search",
    "skill_read",
    "skill_validate",
    "skill_deprecate",
    "skill_history",
)


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


COMPACT_REASON = "Left out to save space in this model's small context window"
# Always offered when available and allowed, whatever the budget: the tools most tasks need.
CORE_TOOLS = frozenset(
    {
        "list_files",
        "read_file",
        "write_file",
        "run_python",
        "web_fetch",
        "remember",
        "search_memory",
        "task_list",
        "task_add",
        "plan_start",
        "plan_update",
    }
)


def tool_tokens(spec: ToolSpec) -> int:
    return estimate_tokens(json.dumps(spec.openai()))


def fit_to_budget(offered: dict[str, ToolSpec], max_tokens: int) -> dict[str, str]:
    """Which offered tools to leave out so their definitions fit `max_tokens`: core tools
    stay; the others are added cheapest first (optional ones last) while they fit."""
    core = {n: s for n, s in offered.items() if n in CORE_TOOLS}
    used = sum(tool_tokens(s) for s in core.values())
    rest = sorted(
        (n for n in offered if n not in CORE_TOOLS),
        key=lambda n: (offered[n].optional, tool_tokens(offered[n]), n),
    )
    left_out: dict[str, str] = {}
    for name in rest:
        cost = tool_tokens(offered[name])
        if used + cost <= max_tokens:
            used += cost
        else:
            left_out[name] = COMPACT_REASON
    return left_out


NO_EMAIL_REASON = "No email account connected"
NOT_PERMISSION_REASONS = ("No skills saved yet", COMPACT_REASON, NO_EMAIL_REASON)


async def build_toolset(
    db: AsyncSession,
    conversation: Conversation,
    settings: Settings,
    *,
    sandbox: bool,
    compact: bool = False,
    max_tool_tokens: int | None = None,
) -> Toolset:
    profile = await conversation_profile(db, conversation, settings)
    known, unavailable = await available_tools(db, conversation.owner_id, settings, sandbox=sandbox)
    if not await memory_active_for(db, conversation, settings):
        for name in MEMORY_TOOLS:
            unavailable.setdefault(name, "Memory is off for this chat")
    has_skills = await db.scalar(
        select(func.count())
        .select_from(Skill)
        .where(Skill.owner_id == conversation.owner_id, Skill.status != "deprecated")
    )
    if not has_skills:
        for name in SKILL_TOOLS_NEEDING_SKILLS:
            unavailable.setdefault(name, "No skills saved yet")
    has_email = await db.scalar(
        select(func.count())
        .select_from(IntegrationAccount)
        .where(
            IntegrationAccount.owner_id == conversation.owner_id,
            IntegrationAccount.kind == "email",
            IntegrationAccount.enabled.is_(True),
        )
    )
    if not has_email:
        for name in EMAIL_TOOLS:
            unavailable.setdefault(name, NO_EMAIL_REASON)
    if compact:  # the lite profile (model_registry): leave optional tools out
        for name, spec in known.items():
            if spec.optional:
                unavailable.setdefault(name, COMPACT_REASON)
    offered = {
        name: spec
        for name, spec in known.items()
        if name not in unavailable and profile.decide(name, spec.risk) != "deny"
    }
    if max_tool_tokens is not None:
        # Tool definitions are sent with every request: on a small context they must
        # leave room for the conversation (ADR C-09).
        left_out = fit_to_budget(offered, max_tool_tokens)
        unavailable.update(left_out)
        offered = {n: s for n, s in offered.items() if n not in left_out}
    return Toolset(
        profile=profile,
        known=known,
        offered=offered,
        grants=await conversation_grants(db, conversation.id),
        unavailable=unavailable,
    )

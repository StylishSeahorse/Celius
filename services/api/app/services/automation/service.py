"""Creating and changing automations (the engine runs them: engine.py)."""

import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError
from app.models import Automation
from app.services.assistant import tasks as user_tasks
from app.services.audit import audit
from app.services.automation import engine
from app.services.integrations import accounts
from app.services.sync import record_sync
from app.services.tools.policy import validate_profile_key


async def _check(db: AsyncSession, owner_id: uuid.UUID, trigger: dict, actions: list) -> None:
    if trigger["type"] == "email_received":
        await accounts.owned(db, owner_id, trigger["account_id"], kind="email")
    for action in actions:
        if action["type"] == "run_prompt":
            # Never wider than the user may use (validated like a scheduled task's).
            await validate_profile_key(db, owner_id, action["tool_profile"])


async def create(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    name: str,
    trigger: dict,
    conditions: list,
    actions: list,
    settings: Settings,
    created_by: str = "user",
) -> Automation:
    count = await db.scalar(
        select(func.count()).select_from(Automation).where(Automation.owner_id == owner_id)
    )
    if (count or 0) >= engine.MAX_PER_USER:
        raise AppError(409, "too_many", f"At most {engine.MAX_PER_USER} automations")
    trigger, conditions, actions = engine.validate(trigger, conditions, actions)
    await _check(db, owner_id, trigger, actions)
    prefs = await user_tasks.user_prefs(db, owner_id, settings)
    automation = Automation(
        id=uuid.uuid4(),
        owner_id=owner_id,
        name=" ".join(name.split())[:200] or "Automation",
        enabled=True,
        trigger=trigger,
        conditions=conditions,
        actions=actions,
        state={},
        next_check_at=engine.first_check(trigger, prefs.timezone),
        created_by=created_by,
    )
    db.add(automation)
    await db.flush()
    audit(db, owner_id, "automation.created", "automation", automation.id, by=created_by)
    record_sync(db, owner_id, "automation", automation.id)
    return automation


async def update(
    db: AsyncSession, automation: Automation, changes: dict, settings: Settings
) -> None:
    if "name" in changes and changes["name"]:
        automation.name = " ".join(changes["name"].split())[:200]
    if {"trigger", "conditions", "actions"} & set(changes):
        trigger, conditions, actions = engine.validate(
            changes.get("trigger", automation.trigger),
            changes.get("conditions", automation.conditions),
            changes.get("actions", automation.actions),
        )
        await _check(db, automation.owner_id, trigger, actions)
        if trigger != automation.trigger:
            automation.state = {}  # a new trigger starts fresh
        automation.trigger, automation.conditions, automation.actions = (
            trigger,
            conditions,
            actions,
        )
    if "enabled" in changes:
        automation.enabled = bool(changes["enabled"])
        if automation.enabled:
            automation.consecutive_failures = 0
            automation.disabled_reason = None
    if automation.enabled:
        prefs = await user_tasks.user_prefs(db, automation.owner_id, settings)
        automation.next_check_at = engine.first_check(automation.trigger, prefs.timezone)
    else:
        automation.next_check_at = None
    audit(db, automation.owner_id, "automation.updated", "automation", automation.id)
    record_sync(db, automation.owner_id, "automation", automation.id)

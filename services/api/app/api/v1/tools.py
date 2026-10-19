"""Tools: catalogue, settings, profiles, approvals and conversation workspaces."""

import mimetypes
import uuid
from typing import Annotated
from urllib.parse import quote

import anyio
from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError, not_found
from app.models import ToolCall, ToolGrant, ToolProfile, UserSettings
from app.schemas.tools import (
    ApproveToolCall,
    DenyToolCall,
    PendingApproval,
    ToolCallOut,
    ToolInfo,
    ToolProfileCreate,
    ToolProfileOut,
    ToolProfileUpdate,
    ToolSettingsOut,
    ToolSettingsUpdate,
)
from app.services.audit import audit
from app.services.conversations import get_owned_conversation
from app.services.tools import calls as call_svc
from app.services.tools import workspace as ws
from app.services.tools.policy import (
    BUILTIN_PROFILES,
    CUSTOM_PREFIX,
    Profile,
    tool_preferences,
    validate_profile_key,
)
from app.services.tools.registry import BUILTIN
from app.services.tools.sandbox_client import SandboxClient
from app.services.tools.sandbox_status import read_status
from app.services.tools.toolset import available_tools

router = APIRouter(tags=["tools"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
MAX_DOWNLOAD = 200 * 1024 * 1024


def _sandbox_configured(settings: Settings) -> bool:
    return bool(settings.sandbox_url and settings.sandbox_token)


# ---------------------------------------------------------------------- catalogue


@router.get("/tools", response_model=list[ToolInfo])
async def list_tools(auth: CurrentAuth, db: DB, settings: AppSettings) -> list[ToolInfo]:
    known, unavailable = await available_tools(
        db, auth.user.id, settings, sandbox=_sandbox_configured(settings)
    )
    return [
        ToolInfo(
            name=name,
            label=spec.label or name,
            description=spec.description,
            risk=spec.risk,
            source="builtin" if name in BUILTIN else "mcp",
            available=name not in unavailable,
            unavailable_reason=unavailable.get(name),
        )
        for name, spec in known.items()
    ]


async def _user_settings(db: AsyncSession, user_id: uuid.UUID) -> UserSettings:
    row = await db.get(UserSettings, user_id)
    if row is None:
        row = UserSettings(user_id=user_id, preferences={})
        db.add(row)
        await db.flush()
    return row


@router.get("/tools/settings", response_model=ToolSettingsOut)
async def get_tool_settings(auth: CurrentAuth, db: DB, settings: AppSettings) -> ToolSettingsOut:
    prefs = tool_preferences(await db.get(UserSettings, auth.user.id))
    health = None
    if _sandbox_configured(settings):
        # The worker's view (the API itself has no route to the sandbox in production).
        health = await anyio.to_thread.run_sync(read_status, settings)
        if health is None:
            client = SandboxClient.from_settings(settings)
            health = (await client.health() if client else None) or {"status": "unreachable"}
            if client:
                await client.aclose()
    return ToolSettingsOut(
        default_profile=prefs.get("default_profile") or settings.tool_default_profile,
        disabled_tools=prefs.get("disabled_tools") or [],
        sandbox=health,
        network_enabled=settings.tool_network_enabled,
        web_allowlist=settings.web_allowlist,
        limits={
            "max_iterations": settings.tool_max_iterations,
            "max_run_seconds": settings.tool_max_run_seconds,
            "call_timeout_seconds": settings.tool_call_timeout_seconds,
            "max_result_chars": settings.tool_max_result_chars,
            "approval_ttl_minutes": settings.tool_approval_ttl_minutes,
        },
    )


@router.put("/tools/settings", response_model=ToolSettingsOut)
async def update_tool_settings(
    body: ToolSettingsUpdate, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> ToolSettingsOut:
    row = await _user_settings(db, auth.user.id)
    tools = dict(tool_preferences(row))
    if body.default_profile is not None:
        await validate_profile_key(db, auth.user.id, body.default_profile)
        tools["default_profile"] = body.default_profile
    if body.disabled_tools is not None:
        tools["disabled_tools"] = sorted(set(body.disabled_tools))
    row.preferences = {**(row.preferences or {}), "tools": tools}
    audit(
        db,
        auth.user.id,
        "tools.settings_changed",
        "user",
        auth.user.id,
        request.client.host if request.client else None,
        **body.model_dump(exclude_none=True),
    )
    await db.commit()
    return await get_tool_settings(auth, db, settings)


# ---------------------------------------------------------------------- profiles


async def _profile_out(
    db: AsyncSession,
    user_id: uuid.UUID,
    settings: Settings,
    profile: Profile,
    description: str | None,
    builtin: bool,
) -> ToolProfileOut:
    known, _ = await available_tools(db, user_id, settings, sandbox=True)
    return ToolProfileOut(
        key=profile.key,
        label=profile.label,
        description=description,
        builtin=builtin,
        base=profile.base,
        rules=dict(profile.rules),
        decisions={name: profile.decide(name, spec.risk) for name, spec in known.items()},
    )


async def _custom_out(db, user_id, settings, row: ToolProfile) -> ToolProfileOut:
    profile = Profile(
        key=f"{CUSTOM_PREFIX}{row.id}", label=row.name, base=row.base, rules=dict(row.rules)
    )
    return await _profile_out(db, user_id, settings, profile, row.description, False)


@router.get("/tool-profiles", response_model=list[ToolProfileOut])
async def list_profiles(auth: CurrentAuth, db: DB, settings: AppSettings) -> list[ToolProfileOut]:
    out = [
        await _profile_out(
            db,
            auth.user.id,
            settings,
            Profile(key=key, label=info["label"], base=key),
            info["description"],
            True,
        )
        for key, info in BUILTIN_PROFILES.items()
    ]
    rows = (
        await db.scalars(
            select(ToolProfile)
            .where(ToolProfile.owner_id == auth.user.id)
            .order_by(ToolProfile.name)
        )
    ).all()
    out += [await _custom_out(db, auth.user.id, settings, row) for row in rows]
    return out


async def _owned_profile(db: AsyncSession, user_id: uuid.UUID, profile_id: uuid.UUID):
    row = await db.scalar(
        select(ToolProfile).where(ToolProfile.id == profile_id, ToolProfile.owner_id == user_id)
    )
    if row is None:
        raise not_found("Tool profile")
    return row


@router.post("/tool-profiles", response_model=ToolProfileOut, status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: ToolProfileCreate, request: Request, auth: CurrentAuth, db: DB, settings: AppSettings
) -> ToolProfileOut:
    row = ToolProfile(
        owner_id=auth.user.id,
        name=body.name,
        description=body.description,
        base=body.base,
        rules=body.rules,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise AppError(409, "name_taken", "A profile with this name already exists") from exc
    audit(
        db,
        auth.user.id,
        "tool_profile.created",
        "tool_profile",
        row.id,
        request.client.host if request.client else None,
        base=body.base,
        rules=body.rules,
    )
    await db.commit()
    return await _custom_out(db, auth.user.id, settings, row)


@router.patch("/tool-profiles/{profile_id}", response_model=ToolProfileOut)
async def update_profile(
    profile_id: uuid.UUID,
    body: ToolProfileUpdate,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
) -> ToolProfileOut:
    row = await _owned_profile(db, auth.user.id, profile_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(row, key, value)
    try:
        await db.flush()
    except IntegrityError as exc:
        raise AppError(409, "name_taken", "A profile with this name already exists") from exc
    audit(
        db,
        auth.user.id,
        "tool_profile.updated",
        "tool_profile",
        row.id,
        request.client.host if request.client else None,
        **body.model_dump(exclude_unset=True),
    )
    await db.commit()
    return await _custom_out(db, auth.user.id, settings, row)


@router.delete("/tool-profiles/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(
    profile_id: uuid.UUID, request: Request, auth: CurrentAuth, db: DB
) -> None:
    """Conversations using it fall back to read_only (never to something wider)."""
    row = await _owned_profile(db, auth.user.id, profile_id)
    await db.delete(row)
    audit(
        db,
        auth.user.id,
        "tool_profile.deleted",
        "tool_profile",
        profile_id,
        request.client.host if request.client else None,
    )
    await db.commit()


# ---------------------------------------------------------------------- approvals


@router.get("/tool-calls/pending", response_model=list[PendingApproval])
async def list_pending(auth: CurrentAuth, db: DB) -> list[PendingApproval]:
    return await call_svc.pending_approvals(db, auth.user.id)


@router.get("/tool-calls/{call_id}", response_model=ToolCallOut)
async def get_call(call_id: uuid.UUID, auth: CurrentAuth, db: DB) -> ToolCallOut:
    call = await db.scalar(
        select(ToolCall).where(ToolCall.id == call_id, ToolCall.owner_id == auth.user.id)
    )
    if call is None:
        raise not_found("Tool call")
    return call_svc.tool_call_out(call)


@router.post("/tool-calls/{call_id}/approve", response_model=ToolCallOut)
async def approve_call(
    call_id: uuid.UUID, body: ApproveToolCall, request: Request, auth: CurrentAuth, db: DB
) -> ToolCallOut:
    call = await call_svc.approve(
        db,
        auth.user,
        call_id,
        body.args_hash,
        remember=body.remember,
        remember_all=body.remember_all,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return call_svc.tool_call_out(call)


@router.post("/tool-calls/{call_id}/deny", response_model=ToolCallOut)
async def deny_call(
    call_id: uuid.UUID, body: DenyToolCall, request: Request, auth: CurrentAuth, db: DB
) -> ToolCallOut:
    call = await call_svc.deny(
        db,
        auth.user,
        call_id,
        body.reason,
        ip_address=request.client.host if request.client else None,
    )
    await db.commit()
    return call_svc.tool_call_out(call)


# ---------------------------------------------------------------------- per conversation


@router.get("/conversations/{conversation_id}/tool-grants", response_model=list[str])
async def list_grants(conversation_id: uuid.UUID, auth: CurrentAuth, db: DB) -> list[str]:
    await get_owned_conversation(db, auth.user.id, conversation_id)
    rows = await db.scalars(
        select(ToolGrant.tool_name).where(ToolGrant.conversation_id == conversation_id)
    )
    return sorted(rows.all())


@router.delete(
    "/conversations/{conversation_id}/tool-grants", status_code=status.HTTP_204_NO_CONTENT
)
async def clear_grants(
    conversation_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    tool: Annotated[str | None, Query(max_length=200)] = None,
) -> None:
    """Forget "allow for this conversation" (one tool, or all of them)."""
    await get_owned_conversation(db, auth.user.id, conversation_id)
    query = delete(ToolGrant).where(ToolGrant.conversation_id == conversation_id)
    if tool:
        query = query.where(ToolGrant.tool_name == tool)
    await db.execute(query)
    audit(db, auth.user.id, "tool_grants.cleared", "conversation", conversation_id, tool=tool)
    await db.commit()


@router.get("/conversations/{conversation_id}/workspace")
async def list_workspace(
    conversation_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    path: Annotated[str, Query(max_length=1024)] = "",
) -> dict:
    await get_owned_conversation(db, auth.user.id, conversation_id)
    root = ws.workspace_dir(settings, conversation_id)
    try:
        entries, more = await anyio.to_thread.run_sync(ws.list_entries, root, path)
    except ws.WorkspaceError as exc:
        raise AppError(404, "not_found", str(exc)) from exc
    return {"entries": [e.__dict__ for e in entries], "truncated": more}


@router.get("/conversations/{conversation_id}/workspace/file")
async def download_workspace_file(
    conversation_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    path: Annotated[str, Query(min_length=1, max_length=1024)],
) -> Response:
    """Download a file the tools produced. Always as an attachment: workspace content is
    untrusted, so it is never rendered by the browser on this origin."""
    await get_owned_conversation(db, auth.user.id, conversation_id)
    root = ws.workspace_dir(settings, conversation_id)
    try:
        data, size = await anyio.to_thread.run_sync(ws.read_bytes, root, path, MAX_DOWNLOAD + 1)
    except ws.WorkspaceError as exc:
        raise AppError(404, "not_found", str(exc)) from exc
    if size > MAX_DOWNLOAD:
        raise AppError(413, "too_large", "The file is too large to download here")
    name = path.rsplit("/", 1)[-1]
    media_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
    ascii_name = name.encode("ascii", "replace").decode().replace('"', "_")
    return Response(
        content=data,
        media_type=media_type,
        headers={
            "Content-Disposition": f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{_quote(name)}",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Cache-Control": "private, no-store",
        },
    )


def _quote(name: str) -> str:
    return quote(name, safe="")

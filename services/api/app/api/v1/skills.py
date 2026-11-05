"""Skills: browse, author, review generated ones, publish, roll back, deprecate, test,
give feedback, export and import (the standard skill directory as a zip)."""

import io
import uuid
import zipfile
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status
from fastapi.responses import Response
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError, not_found
from app.models import Skill, SkillEvent, SkillVersion, UserSettings
from app.schemas.skills import (
    SkillDetail,
    SkillEventOut,
    SkillFeedback,
    SkillFiles,
    SkillOut,
    SkillSettings,
    SkillSettingsUpdate,
    SkillVersionBrief,
    SkillVersionOut,
)
from app.services.audit import audit
from app.services.jobs import enqueue_job
from app.services.memory.embeddings import Embedder
from app.services.skills import registry
from app.services.skills.format import (
    MAX_FILE_CHARS,
    MAX_FILES,
    SKILL_FILE,
    SkillFormatError,
    parse,
)
from app.services.skills.jobs import count_pending
from app.services.sync import record_sync

router = APIRouter(tags=["skills"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]
MAX_ARCHIVE_BYTES = 2 * 1024 * 1024


async def _pending(db: AsyncSession, skill_ids: list[uuid.UUID]) -> dict[uuid.UUID, list[int]]:
    if not skill_ids:
        return {}
    rows = (
        await db.execute(
            select(SkillVersion.skill_id, SkillVersion.version).where(
                SkillVersion.skill_id.in_(skill_ids), SkillVersion.status == "pending"
            )
        )
    ).all()
    found: dict[uuid.UUID, list[int]] = {}
    for skill_id, version in rows:
        found.setdefault(skill_id, []).append(version)
    return found


def _out(skill: Skill, pending: dict | None = None) -> SkillOut:
    return SkillOut(
        id=skill.id,
        name=skill.name,
        description=skill.description,
        status=skill.status,
        origin=skill.origin,
        tags=skill.tags or [],
        published_version=skill.published_version,
        latest_version=skill.latest_version,
        pending_versions=sorted((pending or {}).get(skill.id, [])),
        use_count=skill.use_count,
        success_count=skill.success_count,
        failure_count=skill.failure_count,
        last_used_at=skill.last_used_at,
        created_at=skill.created_at,
        updated_at=skill.updated_at,
    )


def _brief(version: SkillVersion) -> SkillVersionBrief:
    return SkillVersionBrief(
        version=version.version,
        status=version.status,
        created_by=version.created_by,
        changelog=version.changelog,
        validation=version.validation or {},
        test=version.test or {},
        created_at=version.created_at,
        published_at=version.published_at,
    )


def _full(version: SkillVersion) -> SkillVersionOut:
    return SkillVersionOut(
        **_brief(version).model_dump(),
        files=version.files,
        frontmatter=version.frontmatter or {},
        source_conversation_id=version.source_conversation_id,
    )


async def _owned(db: AsyncSession, owner_id: uuid.UUID, skill_id: uuid.UUID) -> Skill:
    skill = await registry.get_skill(db, owner_id, skill_id)
    if skill is None:
        raise not_found("Skill")
    return skill


async def _version(db: AsyncSession, skill: Skill, number: int) -> SkillVersion:
    version = await registry.get_version(db, skill, number)
    if version is None or version.version != number:
        raise not_found("Skill version")
    return version


async def _detail(db: AsyncSession, skill: Skill) -> SkillDetail:
    versions = (
        await db.scalars(
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill.id)
            .order_by(SkillVersion.version.desc())
        )
    ).all()
    current = await registry.get_version(db, skill)
    return SkillDetail(
        **_out(skill, await _pending(db, [skill.id])).model_dump(),
        versions=[_brief(v) for v in versions],
        current=_full(current) if current else None,
    )


# ---------------------------------------------------------------------- settings


@router.get("/skills/settings", response_model=SkillSettings)
async def get_skill_settings(auth: CurrentAuth, db: DB, settings: AppSettings) -> SkillSettings:
    prefs = await registry.user_skill_prefs(db, auth.user.id, settings)
    return SkillSettings(
        trust=prefs.trust,
        auto_generate=prefs.auto_generate,
        pending=await count_pending(db, auth.user.id),
    )


@router.put("/skills/settings", response_model=SkillSettings)
async def update_skill_settings(
    body: SkillSettingsUpdate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> SkillSettings:
    row = await db.get(UserSettings, auth.user.id)
    if row is None:
        row = UserSettings(user_id=auth.user.id, preferences={})
        db.add(row)
    current = dict(((row.preferences or {}).get("skills")) or {})
    current.update(body.model_dump(exclude_none=True))
    row.preferences = {**(row.preferences or {}), "skills": current}
    audit(db, auth.user.id, "skill.settings_changed", "user", auth.user.id, **current)
    await db.commit()
    return await get_skill_settings(auth, db, settings)


# ---------------------------------------------------------------------- browse


@router.get("/skills", response_model=list[SkillOut])
async def list_skills(
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    status_: Annotated[
        str | None, Query(alias="status", pattern="^(candidate|active|deprecated|pending)$")
    ] = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
) -> list[SkillOut]:
    if q:
        embedder = Embedder.from_settings(settings)
        try:
            found = await registry.search(
                db,
                auth.user.id,
                q,
                embedder=embedder,
                limit=100,
                statuses=("active", "candidate", "deprecated"),
            )
        finally:
            if embedder is not None:
                await embedder.aclose()
        rows = [skill for _, skill in found]
    else:
        query = select(Skill).where(Skill.owner_id == auth.user.id)
        if status_ == "pending":
            query = query.where(
                Skill.id.in_(select(SkillVersion.skill_id).where(SkillVersion.status == "pending"))
            )
        elif status_:
            query = query.where(Skill.status == status_)
        rows = (await db.scalars(query.order_by(Skill.name).limit(500))).all()
    pending = await _pending(db, [s.id for s in rows])
    return [_out(s, pending) for s in rows]


@router.get("/skills/{skill_id}", response_model=SkillDetail)
async def get_skill(skill_id: uuid.UUID, auth: CurrentAuth, db: DB) -> SkillDetail:
    return await _detail(db, await _owned(db, auth.user.id, skill_id))


@router.get("/skills/{skill_id}/versions/{version}", response_model=SkillVersionOut)
async def get_skill_version(
    skill_id: uuid.UUID, version: int, auth: CurrentAuth, db: DB
) -> SkillVersionOut:
    skill = await _owned(db, auth.user.id, skill_id)
    return _full(await _version(db, skill, version))


@router.get("/skills/{skill_id}/events", response_model=list[SkillEventOut])
async def skill_events(
    skill_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[SkillEventOut]:
    skill = await _owned(db, auth.user.id, skill_id)
    rows = (
        await db.scalars(
            select(SkillEvent)
            .where(SkillEvent.skill_id == skill.id)
            .order_by(SkillEvent.created_at.desc())
            .limit(limit)
        )
    ).all()
    return [
        SkillEventOut(
            id=e.id,
            version=e.version,
            kind=e.kind,
            outcome=e.outcome,
            run_id=e.run_id,
            duration_ms=e.duration_ms,
            detail=e.detail or {},
            created_at=e.created_at,
        )
        for e in rows
    ]


# ---------------------------------------------------------------------- authoring


async def _publish_if_valid(db, skill, version, settings) -> None:
    """A skill the user writes is published when it validates; otherwise it stays a
    candidate showing its errors."""
    if not (version.validation or {}).get("ok"):
        return
    embedder = Embedder.from_settings(settings)
    try:
        await registry.request_publish(
            db, skill, version, actor="user", settings=settings, embedder=embedder
        )
    finally:
        if embedder is not None:
            await embedder.aclose()


@router.post("/skills", response_model=SkillDetail, status_code=status.HTTP_201_CREATED)
async def create_skill(
    body: SkillFiles, auth: CurrentAuth, db: DB, settings: AppSettings
) -> SkillDetail:
    skill, version = await registry.create_skill(
        db, auth.user.id, body.files, origin="user", created_by="user", changelog=body.changelog
    )
    await _publish_if_valid(db, skill, version, settings)
    await db.commit()
    return await _detail(db, skill)


@router.post(
    "/skills/{skill_id}/versions", response_model=SkillDetail, status_code=status.HTTP_201_CREATED
)
async def add_skill_version(
    skill_id: uuid.UUID, body: SkillFiles, auth: CurrentAuth, db: DB, settings: AppSettings
) -> SkillDetail:
    skill = await _owned(db, auth.user.id, skill_id)
    version = await registry.add_version(
        db, skill, body.files, created_by="user", changelog=body.changelog
    )
    await _publish_if_valid(db, skill, version, settings)
    await db.commit()
    return await _detail(db, skill)


# ---------------------------------------------------------------------- lifecycle


@router.post("/skills/{skill_id}/versions/{version}/validate", response_model=SkillVersionOut)
async def validate_version(
    skill_id: uuid.UUID, version: int, auth: CurrentAuth, db: DB
) -> SkillVersionOut:
    skill = await _owned(db, auth.user.id, skill_id)
    row = await _version(db, skill, version)
    await registry.revalidate(db, skill, row)
    await db.commit()
    return _full(row)


@router.post("/skills/{skill_id}/versions/{version}/publish", response_model=SkillDetail)
async def publish_version(
    skill_id: uuid.UUID, version: int, auth: CurrentAuth, db: DB, settings: AppSettings
) -> SkillDetail:
    """Approve a pending version, publish a candidate, or roll back to a retired one."""
    skill = await _owned(db, auth.user.id, skill_id)
    row = await _version(db, skill, version)
    embedder = Embedder.from_settings(settings)
    try:
        await registry.request_publish(
            db, skill, row, actor="user", settings=settings, embedder=embedder
        )
    finally:
        if embedder is not None:
            await embedder.aclose()
    await db.commit()
    return await _detail(db, skill)


@router.post("/skills/{skill_id}/versions/{version}/reject", response_model=SkillDetail)
async def reject_version(
    skill_id: uuid.UUID, version: int, auth: CurrentAuth, db: DB
) -> SkillDetail:
    skill = await _owned(db, auth.user.id, skill_id)
    await registry.reject_version(db, skill, await _version(db, skill, version))
    if skill.published_version is None and skill.status == "candidate":
        others = await db.scalar(
            select(SkillVersion.id).where(
                SkillVersion.skill_id == skill.id, SkillVersion.status != "rejected"
            )
        )
        if others is None:
            skill.status = "deprecated"  # nothing left to publish
    await db.commit()
    return await _detail(db, skill)


@router.post("/skills/{skill_id}/versions/{version}/test", status_code=status.HTTP_202_ACCEPTED)
async def test_version(
    skill_id: uuid.UUID, version: int, auth: CurrentAuth, db: DB, settings: AppSettings
) -> dict:
    skill = await _owned(db, auth.user.id, skill_id)
    row = await _version(db, skill, version)
    if not (row.validation or {}).get("ok"):
        raise AppError(422, "validation_failed", "Fix the validation errors before testing")
    if (row.test or {}).get("status") == "not_defined":
        raise AppError(409, "no_test", "This version has no metadata.test command")
    if not settings.sandbox_url:
        raise AppError(409, "no_sandbox", "The sandbox is not configured on this server")
    job = enqueue_job(
        db, auth.user.id, "skill.test", {"skill_id": str(skill.id), "version": row.version}
    )
    await db.commit()
    return {"job_id": str(job.id)}


@router.post("/skills/{skill_id}/deprecate", response_model=SkillDetail)
async def deprecate_skill(
    skill_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> SkillDetail:
    skill = await _owned(db, auth.user.id, skill_id)
    await registry.deprecate(db, skill, actor="user", settings=settings)
    await db.commit()
    return await _detail(db, skill)


@router.post("/skills/{skill_id}/restore", response_model=SkillDetail)
async def restore_skill(skill_id: uuid.UUID, auth: CurrentAuth, db: DB) -> SkillDetail:
    skill = await _owned(db, auth.user.id, skill_id)
    await registry.restore(db, skill)
    await db.commit()
    return await _detail(db, skill)


@router.post("/skills/{skill_id}/feedback", status_code=status.HTTP_204_NO_CONTENT)
async def skill_feedback(
    skill_id: uuid.UUID, body: SkillFeedback, auth: CurrentAuth, db: DB
) -> None:
    skill = await _owned(db, auth.user.id, skill_id)
    await registry.feedback(db, skill, body.outcome, body.note)
    await db.commit()


@router.delete("/skills/{skill_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_skill(skill_id: uuid.UUID, auth: CurrentAuth, db: DB) -> None:
    skill = await _owned(db, auth.user.id, skill_id)
    await db.execute(delete(Skill).where(Skill.id == skill.id))
    audit(db, auth.user.id, "skill.deleted", "skill", skill.id, name=skill.name)
    record_sync(db, auth.user.id, "skill", skill.id, operation="delete")
    await db.commit()


# ---------------------------------------------------------------------- export / import


@router.get("/skills/{skill_id}/export")
async def export_skill(
    skill_id: uuid.UUID,
    auth: CurrentAuth,
    db: DB,
    version: Annotated[int | None, Query(ge=1)] = None,
) -> Response:
    """The skill as a standard directory in a zip: <name>/SKILL.md, <name>/scripts/..."""
    skill = await _owned(db, auth.user.id, skill_id)
    row = await registry.get_version(db, skill, version)
    if row is None:
        raise not_found("Skill version")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in sorted(row.files.items()):
            archive.writestr(f"{skill.name}/{path}", content)
    return Response(
        buffer.getvalue(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{skill.name}-v{row.version}.zip"'},
    )


@router.post("/skills/import", response_model=SkillDetail, status_code=status.HTTP_201_CREATED)
async def import_skill(auth: CurrentAuth, db: DB, request: Request) -> SkillDetail:
    """A zip of one skill directory (SKILL.md at its root or in one top folder), sent as
    the request body (application/zip). Imported skills are untrusted: they are validated
    and stay candidates until the user publishes them."""
    data = bytearray()
    async for piece in request.stream():
        data += piece
        if len(data) > MAX_ARCHIVE_BYTES:
            raise AppError(413, "too_large", "Skill archives are limited to 2 MB")
    files = _read_archive(bytes(data))
    try:
        existing = await registry.get_skill(db, auth.user.id, parse(files).name or "-")
    except SkillFormatError as exc:
        raise AppError(422, "invalid_skill", str(exc)) from exc
    if existing is not None:
        await registry.add_version(
            db, existing, files, created_by="user", changelog="Imported from an archive"
        )
        skill = existing
    else:
        skill, _ = await registry.create_skill(
            db,
            auth.user.id,
            files,
            origin="imported",
            created_by="user",
            changelog="Imported from an archive",
        )
    await db.commit()
    return await _detail(db, skill)


def _read_archive(data: bytes) -> dict[str, str]:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise AppError(422, "invalid_archive", "Not a zip file") from exc
    entries = [i for i in archive.infolist() if not i.is_dir()]
    if len(entries) > MAX_FILES:
        raise AppError(422, "invalid_archive", f"At most {MAX_FILES} files")
    names = [i.filename for i in entries]
    prefix = ""
    if SKILL_FILE not in names:
        tops = {n.split("/", 1)[0] for n in names}
        if len(tops) == 1 and f"{next(iter(tops))}/{SKILL_FILE}" in names:
            prefix = next(iter(tops)) + "/"
        else:
            raise AppError(422, "invalid_archive", "The archive has no SKILL.md")
    files: dict[str, str] = {}
    for info in entries:
        if info.file_size > MAX_FILE_CHARS * 4:
            raise AppError(422, "invalid_archive", f"{info.filename} is too large")
        name = info.filename.removeprefix(prefix)
        try:
            files[name] = archive.read(info).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AppError(422, "invalid_archive", f"{name} is not UTF-8 text") from exc
    return files

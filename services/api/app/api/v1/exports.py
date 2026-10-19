import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.db import get_db
from app.errors import not_found
from app.models import Conversation, Job, Project, StoredFile
from app.schemas.exports import ExportCreate, ExportOut
from app.services.files.service import file_out
from app.services.jobs import enqueue_job

router = APIRouter(prefix="/exports", tags=["exports"])

DB = Annotated[AsyncSession, Depends(get_db)]


async def _export_out(db: AsyncSession, job: Job) -> ExportOut:
    file = None
    if job.result and job.result.get("file_id"):
        stored = await db.get(StoredFile, uuid.UUID(job.result["file_id"]))
        file = file_out(stored) if stored and stored.deleted_at is None else None
    return ExportOut(id=job.id, status=job.status, error=job.error, file=file)


@router.post("", response_model=ExportOut, status_code=status.HTTP_202_ACCEPTED)
async def create_export(body: ExportCreate, auth: CurrentAuth, db: DB) -> ExportOut:
    """Queue an export. Poll GET /exports/{id} until it's completed, then download the file."""
    model = Conversation if body.scope == "conversation" else Project
    owned = await db.scalar(
        select(model.id).where(model.id == body.id, model.owner_id == auth.user.id)
    )
    if owned is None:
        raise not_found(body.scope.capitalize())
    job = enqueue_job(db, auth.user.id, "export", body.model_dump(mode="json"))
    await db.commit()
    return await _export_out(db, job)


@router.get("/{export_id}", response_model=ExportOut)
async def get_export(export_id: uuid.UUID, auth: CurrentAuth, db: DB) -> ExportOut:
    job = await db.scalar(
        select(Job).where(Job.id == export_id, Job.owner_id == auth.user.id, Job.kind == "export")
    )
    if job is None:
        raise not_found("Export")
    return await _export_out(db, job)

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.dependencies import CurrentAuth
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import AppError
from app.models import StoredFile
from app.schemas.files import (
    FileOut,
    FileText,
    StorageUsage,
    UploadCreate,
    UploadOut,
    UploadProgress,
)
from app.services.access import deletable_file, readable_file, require_project
from app.services.files import quota
from app.services.files import service as svc
from app.services.files.sniff import MEDIA
from app.services.limits import storage_limit

router = APIRouter(prefix="/files", tags=["files"])

DB = Annotated[AsyncSession, Depends(get_db)]
AppSettings = Annotated[Settings, Depends(get_settings)]

# Types a browser may display in place. Everything else is always a download.
INLINE_SAFE = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"} | {
    media_type for _, media_type, _ in MEDIA.values()
}


@router.post("/uploads", response_model=UploadOut, status_code=status.HTTP_201_CREATED)
async def create_upload(
    body: UploadCreate, auth: CurrentAuth, db: DB, settings: AppSettings
) -> UploadOut:
    """Start an upload. The declared size is reserved against the storage quota."""
    session = await svc.create_upload(db, auth.user, body, settings)
    return UploadOut(
        id=session.id,
        upload_url=f"/api/v1/files/uploads/{session.id}/content",
        expires_at=session.expires_at,
    )


@router.put("/uploads/{upload_id}/content")
async def upload_content(
    upload_id: uuid.UUID,
    request: Request,
    auth: CurrentAuth,
    db: DB,
    settings: AppSettings,
    content_range: Annotated[str | None, Header()] = None,
) -> dict:
    """The raw file bytes as the request body. Sending the whole file again replaces the
    previous attempt. With `Content-Range: bytes first-last/total` the body is one chunk;
    chunks resume an interrupted upload (see `GET /uploads/{id}`)."""
    session = await svc.receive_content(
        db,
        auth.user,
        upload_id,
        request.stream(),
        settings,
        content_range=svc.parse_content_range(content_range),
    )
    return {"received_bytes": session.received_bytes}


@router.get("/uploads/{upload_id}", response_model=UploadProgress)
async def upload_progress(upload_id: uuid.UUID, auth: CurrentAuth, db: DB) -> UploadProgress:
    """How much of an upload has arrived, to resume it after a dropped connection."""
    session = await svc.get_upload(db, auth.user, upload_id)
    return UploadProgress(
        id=session.id,
        status=session.status.value,
        declared_size=session.declared_size,
        received_bytes=session.received_bytes,
        expires_at=session.expires_at,
    )


@router.post(
    "/uploads/{upload_id}/complete", response_model=FileOut, status_code=status.HTTP_201_CREATED
)
async def complete_upload(
    upload_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> FileOut:
    """Verify the upload (size, real type, optional malware scan) and store it."""
    return svc.file_out(await svc.complete_upload(db, auth.user, upload_id, settings))


@router.get("", response_model=list[FileOut])
async def list_files(
    auth: CurrentAuth, db: DB, project_id: uuid.UUID | None = None
) -> list[FileOut]:
    if project_id is not None:
        # A project's files, whoever added them (projects can be shared).
        await require_project(db, auth.user.id, project_id)
        query = select(StoredFile).where(
            StoredFile.project_id == project_id, StoredFile.deleted_at.is_(None)
        )
    else:
        query = select(StoredFile).where(
            StoredFile.owner_id == auth.user.id, StoredFile.deleted_at.is_(None)
        )
    if project_id is not None:
        query = query.order_by(StoredFile.relative_path, StoredFile.filename)
    else:
        query = query.order_by(StoredFile.created_at.desc()).limit(200)
    return [svc.file_out(f) for f in (await db.scalars(query)).all()]


@router.get("/usage", response_model=StorageUsage)
async def storage_usage(auth: CurrentAuth, db: DB, settings: AppSettings) -> StorageUsage:
    return StorageUsage(
        used_bytes=await quota.usage(db, auth.user.id),
        limit_bytes=storage_limit(auth.user, settings),
        max_upload_bytes=settings.max_upload_bytes,
        max_media_upload_bytes=max(settings.max_media_upload_bytes, settings.max_upload_bytes),
    )


@router.get("/{file_id}", response_model=FileOut)
async def get_file(file_id: uuid.UUID, auth: CurrentAuth, db: DB) -> FileOut:
    return svc.file_out(await readable_file(db, auth.user.id, file_id))


@router.get("/{file_id}/text", response_model=FileText)
async def get_file_text(file_id: uuid.UUID, auth: CurrentAuth, db: DB) -> FileText:
    file = await readable_file(db, auth.user.id, file_id)
    text = await db.scalar(select(StoredFile.text_content).where(StoredFile.id == file.id))
    return FileText(text=text or "", truncated=file.text_truncated)


def _protect(response: FileResponse, *, pdf_inline: bool = False) -> FileResponse:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Cache-Control"] = "private, max-age=3600"
    response.headers["Referrer-Policy"] = "no-referrer"
    if not pdf_inline:
        # Even if a browser renders the file, it gets no script, no same-origin access.
        response.headers["Content-Security-Policy"] = "sandbox; default-src 'none'; img-src 'self'"
    return response


@router.get("/{file_id}/download", response_class=FileResponse)
async def download(
    file_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings, inline: bool = False
) -> FileResponse:
    """Authenticated download. Never a public URL: every request checks access."""
    file = await readable_file(db, auth.user.id, file_id)
    path = svc.storage_for(settings).blob(file.sha256)
    if not path.is_file():
        raise AppError(404, "file_missing", "The file's content is missing from storage")
    show_inline = inline and file.media_type in INLINE_SAFE
    media_type = (
        file.media_type if show_inline or file.kind != "text" else "text/plain; charset=utf-8"
    )
    response = FileResponse(
        path,
        media_type=media_type,
        filename=file.filename,
        content_disposition_type="inline" if show_inline else "attachment",
    )
    return _protect(response, pdf_inline=show_inline and file.media_type == "application/pdf")


@router.get("/{file_id}/thumbnail", response_class=FileResponse)
async def thumbnail(
    file_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings
) -> FileResponse:
    file = await readable_file(db, auth.user.id, file_id)
    path = svc.storage_for(settings).thumbnail(file.id)
    if not file.has_thumbnail or not path.is_file():
        raise AppError(404, "no_thumbnail", "This file has no thumbnail")
    return _protect(FileResponse(path, media_type="image/webp"))


@router.delete("/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(file_id: uuid.UUID, auth: CurrentAuth, db: DB, settings: AppSettings) -> None:
    file = await deletable_file(db, auth.user.id, file_id)
    await svc.delete_file(db, file, settings)

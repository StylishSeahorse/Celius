"""Upload lifecycle: reserve quota → stream to quarantine → verify → store → process.

    POST /files/uploads              reserve the declared size; create a session
    PUT  /files/uploads/{id}/content stream the bytes into quarantine (hash as we go)
    POST /files/uploads/{id}/complete check size, type and (optionally) malware; store;
                                     queue text extraction / thumbnail
Nothing in quarantine is ever served. Every step is idempotent for a retrying client.
"""

import hashlib
import re
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from functools import partial

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.errors import AppError, not_found
from app.models import FileKind, FileStatus, StoredFile, UploadSession, UploadStatus, User
from app.schemas.files import FileOut, UploadCreate
from app.services import limits
from app.services.access import require_project
from app.services.files import quota
from app.services.files.archives import ArchiveLimits, inspect_zip
from app.services.files.paths import UnsafePath, safe_filename, safe_relative_path
from app.services.files.scan import ScanUnavailable, clamd_scan
from app.services.files.sniff import (
    IMAGE_TYPES,
    MEDIA,
    OFFICE,
    TEXT_EXTENSIONS,
    FileRejected,
    detect,
    extension_of,
)
from app.services.files.storage import Storage
from app.services.jobs import enqueue_job
from app.services.sync import record_sync

ZIP_KINDS = {FileKind.archive, FileKind.document, FileKind.spreadsheet, FileKind.presentation}
KNOWN_EXTENSIONS = (
    TEXT_EXTENSIONS | set(IMAGE_TYPES) | set(OFFICE) | set(MEDIA) | {"pdf", "zip", "txt", ""}
)


def max_size_for(filename: str, settings: Settings) -> int:
    """Audio and video have their own, larger limit; the real type is checked at the end."""
    if extension_of(filename) in MEDIA:
        return max(settings.max_media_upload_bytes, settings.max_upload_bytes)
    return settings.max_upload_bytes


def storage_for(settings: Settings) -> Storage:
    return Storage(settings.storage_root)


def archive_limits(settings: Settings) -> ArchiveLimits:
    return ArchiveLimits(
        max_entries=settings.archive_max_entries,
        max_total_bytes=settings.archive_max_total_bytes,
        max_ratio=settings.archive_max_ratio,
    )


def file_out(file: StoredFile) -> FileOut:
    return FileOut(
        id=file.id,
        filename=file.filename,
        relative_path=file.relative_path,
        media_type=file.media_type,
        kind=file.kind,
        size_bytes=file.size_bytes,
        sha256=file.sha256,
        status=file.status,
        error=file.error,
        text_chars=file.text_chars,
        text_truncated=file.text_truncated,
        has_thumbnail=file.has_thumbnail,
        meta=file.meta,
        project_id=file.project_id,
        created_at=file.created_at,
    )


async def get_owned_file(db: AsyncSession, owner_id: uuid.UUID, file_id: uuid.UUID) -> StoredFile:
    file = await db.scalar(
        select(StoredFile).where(
            StoredFile.id == file_id,
            StoredFile.owner_id == owner_id,
            StoredFile.deleted_at.is_(None),
        )
    )
    if file is None:
        raise not_found("File")
    return file


async def _owned_upload(db: AsyncSession, user: User, upload_id: uuid.UUID) -> UploadSession:
    session = await db.scalar(
        select(UploadSession)
        .where(UploadSession.id == upload_id, UploadSession.owner_id == user.id)
        .with_for_update()
    )
    if session is None:
        raise not_found("Upload")
    return session


async def get_upload(db: AsyncSession, user: User, upload_id: uuid.UUID) -> UploadSession:
    session = await db.scalar(
        select(UploadSession).where(
            UploadSession.id == upload_id, UploadSession.owner_id == user.id
        )
    )
    if session is None:
        raise not_found("Upload")
    return session


def _require_open(session: UploadSession) -> None:
    expires_at = session.expires_at
    if expires_at.tzinfo is None:  # SQLite drops tz info
        expires_at = expires_at.replace(tzinfo=utcnow().tzinfo)
    if session.status != UploadStatus.open or expires_at <= utcnow():
        raise AppError(410, "upload_closed", "This upload has finished or expired; start a new one")


async def create_upload(
    db: AsyncSession, user: User, data: UploadCreate, settings: Settings
) -> UploadSession:
    try:
        filename = safe_filename(data.filename)
        relative_path = safe_relative_path(data.relative_path)
    except UnsafePath as exc:
        raise AppError(422, "unsafe_path", str(exc)) from exc
    limit = max_size_for(filename, settings)
    if data.size > limit:
        raise AppError(
            413,
            "file_too_large",
            f"Files like this can be at most {limit} bytes",
            max_upload_bytes=limit,
        )
    if extension_of(filename) not in KNOWN_EXTENSIONS:
        raise AppError(
            415, "unsupported_type", f"Files of type .{extension_of(filename)} are not supported"
        )
    if data.project_id is not None:
        await require_project(db, user.id, data.project_id, "write")

    await quota.reserve(db, user.id, data.size, limits.storage_limit(user, settings))
    session = UploadSession(
        id=uuid.uuid4(),
        owner_id=user.id,
        project_id=data.project_id,
        filename=filename,
        relative_path=relative_path,
        declared_size=data.size,
        expires_at=utcnow() + timedelta(minutes=settings.upload_session_ttl_minutes),
    )
    db.add(session)
    await db.commit()
    return session


CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+)")


def parse_content_range(header: str | None) -> tuple[int, int, int] | None:
    """`bytes <first>-<last>/<total>` → (first, last, total); None when absent."""
    if header is None:
        return None
    match = CONTENT_RANGE.fullmatch(header.strip())
    if not match:
        raise AppError(400, "bad_content_range", "Content-Range must be 'bytes first-last/total'")
    first, last, total = (int(g) for g in match.groups())
    if last < first or last >= total:
        raise AppError(400, "bad_content_range", "Content-Range is out of order")
    return first, last, total


async def receive_content(
    db: AsyncSession,
    user: User,
    upload_id: uuid.UUID,
    body: AsyncIterator[bytes],
    settings: Settings,
    content_range: tuple[int, int, int] | None = None,
) -> UploadSession:
    """Stream the body into quarantine.

    Without Content-Range the body is the whole file and replaces any earlier attempt.
    With it, the body is one chunk starting at `first`, which may be at most what has
    been received so far: a client that lost the response to a chunk re-sends it (or
    asks `GET /uploads/{id}` where to continue) and nothing is duplicated.
    """
    session = await _owned_upload(db, user, upload_id)
    _require_open(session)
    if content_range is not None:
        return await _receive_chunk(db, session, body, settings, content_range)
    path = storage_for(settings).quarantine(session.id)
    digest = hashlib.sha256()
    received = 0
    async with await anyio.open_file(path, "wb") as handle:
        async for chunk in body:
            received += len(chunk)
            if received > session.declared_size:
                break
            digest.update(chunk)
            await handle.write(chunk)
    if received > session.declared_size:
        path.unlink(missing_ok=True)
        raise AppError(413, "upload_too_large", "More data was sent than the declared size")
    session.received_bytes = received
    session.sha256 = digest.hexdigest()
    await db.commit()
    return session


async def _receive_chunk(
    db: AsyncSession,
    session: UploadSession,
    body: AsyncIterator[bytes],
    settings: Settings,
    content_range: tuple[int, int, int],
) -> UploadSession:
    first, _last, total = content_range
    if total != session.declared_size:
        raise AppError(
            400, "bad_content_range", f"The upload was declared as {session.declared_size} bytes"
        )
    if first > session.received_bytes:
        raise AppError(
            409,
            "upload_offset_mismatch",
            f"Continue from byte {session.received_bytes}",
            received_bytes=session.received_bytes,
        )
    path = storage_for(settings).quarantine(session.id)
    written = 0
    mode = "r+b" if path.exists() else "wb"
    async with await anyio.open_file(path, mode) as handle:
        await handle.truncate(first)  # drop anything after the resume point
        await handle.seek(first)
        async for chunk in body:
            if first + written + len(chunk) > total:
                raise AppError(413, "upload_too_large", "More data was sent than the declared size")
            await handle.write(chunk)
            written += len(chunk)
    session.received_bytes = first + written
    session.sha256 = None  # hashed once, when the upload is completed
    # A slow link keeps its upload alive as long as chunks keep arriving.
    session.expires_at = utcnow() + timedelta(minutes=settings.upload_session_ttl_minutes)
    await db.commit()
    return session


def _hash_file(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


async def _reject(
    db: AsyncSession, session: UploadSession, settings: Settings, error: FileRejected
):
    storage_for(settings).quarantine(session.id).unlink(missing_ok=True)
    session.status = UploadStatus.rejected
    await quota.release(db, session.owner_id, session.declared_size)
    await db.commit()
    raise AppError(422, error.code, str(error))


async def complete_upload(
    db: AsyncSession, user: User, upload_id: uuid.UUID, settings: Settings
) -> StoredFile:
    session = await _owned_upload(db, user, upload_id)
    if session.status == UploadStatus.completed and session.file_id:
        return await get_owned_file(db, user.id, session.file_id)  # a retried "complete"
    _require_open(session)
    path = storage_for(settings).quarantine(session.id)
    if session.received_bytes != session.declared_size or not path.exists():
        raise AppError(
            422,
            "size_mismatch",
            f"Received {session.received_bytes} of {session.declared_size} bytes; "
            "send the rest of the content",
            received_bytes=session.received_bytes,
        )
    if session.sha256 is None:  # sent in chunks
        session.sha256 = await anyio.to_thread.run_sync(_hash_file, path)

    try:
        detected = await anyio.to_thread.run_sync(detect, path, session.filename)
        if detected.kind in ZIP_KINDS:
            await anyio.to_thread.run_sync(partial(inspect_zip, path, archive_limits(settings)))
        if settings.clamav_host:
            signature = await clamd_scan(path, settings.clamav_host, settings.clamav_port)
            if signature:
                raise FileRejected(
                    "malware_detected", f"The file was flagged as malware ({signature})"
                )
    except FileRejected as error:
        await _reject(db, session, settings, error)
    except ScanUnavailable as exc:
        raise AppError(
            503, "scan_unavailable", "The malware scanner is unavailable; try again shortly"
        ) from exc

    await anyio.to_thread.run_sync(storage_for(settings).commit, path, session.sha256)
    file = StoredFile(
        id=uuid.uuid4(),
        owner_id=user.id,
        project_id=session.project_id,
        filename=session.filename,
        relative_path=session.relative_path,
        media_type=detected.media_type,
        kind=detected.kind,
        size_bytes=session.received_bytes,
        sha256=session.sha256,
        status=FileStatus.processing,
        meta={},
    )
    db.add(file)
    await db.flush()
    session.status = UploadStatus.completed
    session.file_id = file.id
    enqueue_job(db, user.id, "file.process", {"file_id": str(file.id)})
    record_sync(
        db,
        user.id,
        "file",
        file.id,
        payload={"project_id": str(file.project_id) if file.project_id else None},
    )
    await db.commit()
    return file


async def delete_file(db: AsyncSession, file: StoredFile, settings: Settings) -> None:
    """Delete a file: frees its quota; the bytes go once no other file uses the same content."""
    file.deleted_at = utcnow()
    await quota.release(db, file.owner_id, file.size_bytes)
    record_sync(
        db,
        file.owner_id,
        "file",
        file.id,
        operation="delete",
        payload={"project_id": str(file.project_id) if file.project_id else None},
    )
    still_used = await db.scalar(
        select(StoredFile.id)
        .where(
            StoredFile.sha256 == file.sha256,
            StoredFile.deleted_at.is_(None),
            StoredFile.id != file.id,
        )
        .limit(1)
    )
    await db.commit()
    store = storage_for(settings)
    if still_used is None:
        store.remove_blob(file.sha256)
    store.thumbnail(file.id).unlink(missing_ok=True)


async def cleanup_expired_uploads(factory: SessionFactory, settings: Settings) -> int:
    """Expire abandoned uploads: release their reserved quota and delete partial data."""
    async with factory() as db:
        expired = (
            await db.scalars(
                select(UploadSession)
                .where(
                    UploadSession.status == UploadStatus.open, UploadSession.expires_at < utcnow()
                )
                .with_for_update(skip_locked=True)
            )
        ).all()
        for session in expired:
            session.status = UploadStatus.expired
            await quota.release(db, session.owner_id, session.declared_size)
            storage_for(settings).quarantine(session.id).unlink(missing_ok=True)
        await db.commit()
        return len(expired)

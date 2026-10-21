"""The `file.process` job: extract text, metadata and a thumbnail after upload."""

import uuid
from functools import partial

import anyio
from sqlalchemy import update

from app.config import Settings
from app.db import SessionFactory
from app.models import FileStatus, Job, StoredFile
from app.services.files.extract import ExtractionFailed, extract
from app.services.files.service import archive_limits, storage_for
from app.services.files.sniff import FileRejected
from app.services.jobs import job_handler
from app.services.sync import record_sync


@job_handler("file.process")
async def process_file(factory: SessionFactory, settings: Settings, job: Job) -> dict:
    file_id = uuid.UUID(job.payload["file_id"])
    async with factory() as db:
        file = await db.get(StoredFile, file_id)
        if file is None or file.deleted_at is not None:
            return {"skipped": True}
        kind, sha256, owner_id, project_id = file.kind, file.sha256, file.owner_id, file.project_id

    store = storage_for(settings)
    values: dict = {}
    try:
        result = await anyio.to_thread.run_sync(
            partial(
                extract,
                store.blob(sha256),
                kind,
                max_chars=settings.max_extracted_text_chars,
                limits=archive_limits(settings),
                max_pixels=settings.image_max_pixels,
                previews=settings.file_previews,
                media=settings.media_probe,
            )
        )
        if result.thumbnail:
            path = store.thumbnail(file_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            await anyio.Path(path).write_bytes(result.thumbnail)
        values = {
            "status": FileStatus.ready,
            "text_content": result.text,
            "text_chars": len(result.text or ""),
            "text_truncated": result.truncated,
            "meta": result.meta,
            "has_thumbnail": result.thumbnail is not None,
            "error": None,
        }
    except (ExtractionFailed, FileRejected) as exc:
        values = {"status": FileStatus.failed, "error": str(exc)[:500]}

    async with factory() as db:
        await db.execute(update(StoredFile).where(StoredFile.id == file_id).values(**values))
        record_sync(
            db,
            owner_id,
            "file",
            file_id,
            payload={"project_id": str(project_id) if project_id else None},
        )
        await db.commit()
    return {"status": values["status"].value}

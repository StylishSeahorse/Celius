"""Exports: a conversation as Markdown, JSON or ZIP, and a project as ZIP.

Built by the `export` job; the result is an ordinary stored file (kind=export), downloaded
through the same authenticated endpoint as everything else. Exports count towards storage.
"""

import hashlib
import json
import re
import tempfile
import uuid
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import anyio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.models import (
    Conversation,
    FileKind,
    FileLink,
    FileStatus,
    Job,
    Message,
    MessagePart,
    MessageRole,
    Project,
    StoredFile,
)
from app.services.conversations import branch_path, load_parts
from app.services.files import quota
from app.services.files.paths import UnsafePath, safe_filename, safe_relative_path
from app.services.files.service import storage_for
from app.services.jobs import JobFailed, job_handler
from app.services.sync import record_sync

FORMAT = "celius.conversation.v1"


def _slug(text: str | None, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:60]
    return slug or fallback


def _text(parts: list[MessagePart], kind: str = "text") -> str:
    return "".join(p.text or "" for p in parts if p.type == kind)


class _Conversation:
    """Everything an export needs about one conversation, loaded up front."""

    def __init__(self, conversation, path, all_messages, parts, files):
        self.conversation = conversation
        self.path = path
        self.all_messages = all_messages
        self.parts = parts
        self.files = files  # message id -> [StoredFile]


async def _load(db: AsyncSession, conversation: Conversation) -> _Conversation:
    path = await branch_path(db, conversation)
    all_messages = list(
        (
            await db.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.created_at)
            )
        ).all()
    )
    parts = await load_parts(db, all_messages)
    rows = (
        await db.execute(
            select(FileLink.message_id, StoredFile)
            .join(StoredFile, StoredFile.id == FileLink.file_id)
            .where(FileLink.message_id.in_([m.id for m in all_messages] or [uuid.uuid4()]))
            .order_by(FileLink.position)
        )
    ).all()
    files: dict[uuid.UUID, list[StoredFile]] = {}
    for message_id, file in rows:
        files.setdefault(message_id, []).append(file)
    return _Conversation(conversation, path, all_messages, parts, files)


def to_markdown(data: _Conversation) -> str:
    conversation = data.conversation
    lines = [f"# {conversation.title or 'Untitled conversation'}", ""]
    lines.append(f"_Exported from Celius on {datetime.now(UTC):%Y-%m-%d %H:%M} UTC._")
    lines.append("")
    for message in data.path:
        who = "You" if message.role == MessageRole.user else "Assistant"
        lines.append(f"## {who}")
        lines.append("")
        reasoning = _text(data.parts[message.id], "reasoning")
        if reasoning:
            lines += ["<details><summary>Reasoning</summary>", "", reasoning, "", "</details>", ""]
        lines.append(_text(data.parts[message.id]) or "_(no text)_")
        attached = data.files.get(message.id, [])
        if attached:
            lines.append("")
            lines.append(
                "Attachments: " + ", ".join(f"`{f.relative_path or f.filename}`" for f in attached)
            )
        lines.append("")
    return "\n".join(lines)


def to_json(data: _Conversation) -> dict:
    conversation = data.conversation
    return {
        "format": FORMAT,
        "exported_at": datetime.now(UTC).isoformat(),
        "conversation": {
            "id": str(conversation.id),
            "title": conversation.title,
            "project_id": str(conversation.project_id) if conversation.project_id else None,
            "model_override": conversation.model_override,
            "created_at": conversation.created_at.isoformat(),
            "updated_at": conversation.updated_at.isoformat(),
        },
        "active_path": [str(m.id) for m in data.path],
        # Every message, including other branches (edits and regenerated replies).
        "messages": [
            {
                "id": str(m.id),
                "parent_id": str(m.parent_id) if m.parent_id else None,
                "role": m.role.value,
                "status": m.status.value,
                "model_id": m.model_id,
                "created_at": m.created_at.isoformat(),
                "text": _text(data.parts[m.id]),
                "reasoning": _text(data.parts[m.id], "reasoning") or None,
                "attachments": [
                    {
                        "id": str(f.id),
                        "filename": f.filename,
                        "path": f.relative_path,
                        "media_type": f.media_type,
                        "size_bytes": f.size_bytes,
                        "sha256": f.sha256,
                    }
                    for f in data.files.get(m.id, [])
                ],
            }
            for m in data.all_messages
        ],
    }


def _zip_name(*parts: str) -> str:
    try:
        return safe_relative_path("/".join(p for p in parts if p)) or "file"
    except UnsafePath:
        return safe_filename(parts[-1])


def _add_conversation(
    zf: zipfile.ZipFile, data: _Conversation, folder: str, settings: Settings
) -> None:
    base = f"{_slug(data.conversation.title, 'conversation')}-{str(data.conversation.id)[:8]}"
    zf.writestr(_zip_name(folder, f"{base}.md"), to_markdown(data))
    zf.writestr(_zip_name(folder, f"{base}.json"), json.dumps(to_json(data), indent=2))
    store = storage_for(settings)
    for attached in data.files.values():
        for file in attached:
            blob = store.blob(file.sha256)
            if file.deleted_at is None and blob.is_file():
                zf.write(
                    blob,
                    _zip_name(folder, base, "attachments", file.relative_path or file.filename),
                )


async def _owned(db, model, owner_id, object_id):
    obj = await db.get(model, object_id)
    if obj is None or obj.owner_id != owner_id or getattr(obj, "deleted_at", None) is not None:
        raise JobFailed("The item to export no longer exists")
    return obj


def _build(out: Path, scope: str, fmt: str, settings: Settings, payload: dict) -> tuple[str, str]:
    """Write the export to `out` (runs in a thread). Returns (filename, media type)."""
    if scope == "conversation":
        data: _Conversation = payload["data"]
        name = _slug(data.conversation.title, "conversation")
        if fmt == "markdown":
            out.write_text(to_markdown(data))
            return f"{name}.md", "text/markdown"
        if fmt == "json":
            out.write_text(json.dumps(to_json(data), indent=2))
            return f"{name}.json", "application/json"
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
            _add_conversation(zf, data, "", settings)
        return f"{name}.zip", "application/zip"

    project: Project = payload["project"]
    store = storage_for(settings)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "project.json",
            json.dumps(
                {
                    "name": project.name,
                    "description": project.description,
                    "instructions": project.instructions,
                    "exported_at": datetime.now(UTC).isoformat(),
                },
                indent=2,
            ),
        )
        zf.writestr("instructions.md", project.instructions or "")
        for data in payload["conversations"]:
            _add_conversation(zf, data, "conversations", settings)
        for file in payload["files"]:
            blob = store.blob(file.sha256)
            if blob.is_file():
                zf.write(blob, _zip_name("files", file.relative_path or file.filename))
    return f"{_slug(project.name, 'project')}.zip", "application/zip"


def _hash_and_size(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest(), path.stat().st_size


def _temp_file(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=directory, delete=False) as handle:
        return Path(handle.name)


@job_handler("export")
async def export_job(factory: SessionFactory, settings: Settings, job: Job) -> dict:
    scope, fmt = job.payload["scope"], job.payload["format"]
    object_id = uuid.UUID(job.payload["id"])

    async with factory() as db:
        if scope == "conversation":
            conversation = await _owned(db, Conversation, job.owner_id, object_id)
            payload = {"data": await _load(db, conversation)}
        else:
            project = await _owned(db, Project, job.owner_id, object_id)
            conversations = (
                await db.scalars(
                    select(Conversation).where(
                        Conversation.project_id == project.id, Conversation.deleted_at.is_(None)
                    )
                )
            ).all()
            files = (
                await db.scalars(
                    select(StoredFile).where(
                        StoredFile.project_id == project.id, StoredFile.deleted_at.is_(None)
                    )
                )
            ).all()
            payload = {
                "project": project,
                "conversations": [await _load(db, c) for c in conversations],
                "files": files,
            }

        out = await anyio.to_thread.run_sync(_temp_file, Path(settings.storage_root) / "tmp")
        try:
            filename, media_type = await anyio.to_thread.run_sync(
                _build, out, scope, fmt, settings, payload
            )
            sha256, size = await anyio.to_thread.run_sync(_hash_and_size, out)
            try:
                await quota.reserve(db, job.owner_id, size, settings.max_user_storage_bytes)
            except Exception as exc:
                raise JobFailed("Not enough storage space left for the export") from exc
            await anyio.to_thread.run_sync(storage_for(settings).write_new, out, sha256)
            file = StoredFile(
                id=uuid.uuid4(),
                owner_id=job.owner_id,
                filename=safe_filename(filename),
                media_type=media_type,
                kind=FileKind.export,
                size_bytes=size,
                sha256=sha256,
                status=FileStatus.ready,
                meta={"scope": scope, "format": fmt, "source_id": str(object_id)},
            )
            db.add(file)
            record_sync(db, job.owner_id, "file", file.id)
            await db.commit()
            return {"file_id": str(file.id)}
        finally:
            await anyio.Path(out).unlink(missing_ok=True)

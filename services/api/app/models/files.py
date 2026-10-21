import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey


class FileStatus(enum.StrEnum):
    processing = "processing"  # stored; text extraction / thumbnail pending
    ready = "ready"
    failed = "failed"  # stored and downloadable, but its content couldn't be read


class FileKind(enum.StrEnum):
    image = "image"
    pdf = "pdf"
    document = "document"  # .docx
    spreadsheet = "spreadsheet"  # .xlsx
    presentation = "presentation"  # .pptx
    text = "text"  # plain text, markdown, code, csv, json…
    archive = "archive"  # .zip
    audio = "audio"  # mp3, m4a, wav, ogg, opus, flac, aac, webm audio
    video = "video"  # mp4, mov, webm, mkv, avi, ogv
    export = "export"  # produced by the export job


class StoredFile(UUIDPrimaryKey, Timestamps, Base):
    """File metadata. Bytes live on disk under STORAGE_ROOT, addressed by SHA-256."""

    __tablename__ = "files"
    __table_args__ = (Index("ix_files_owner_id_created_at", "owner_id", "created_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Set for project knowledge files (included in every chat of the project).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE"), index=True
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    # Folder uploads keep their relative path (validated, normalised, never absolute).
    relative_path: Mapped[str | None] = mapped_column(String(1024))
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    kind: Mapped[FileKind] = mapped_column(Enum(FileKind, name="file_kind"), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, nullable=False)
    status: Mapped[FileStatus] = mapped_column(
        Enum(FileStatus, name="file_status"), default=FileStatus.processing, nullable=False
    )
    error: Mapped[str | None] = mapped_column(String(500))
    # Deferred: listings never load extracted text; read it explicitly when needed.
    text_content: Mapped[str | None] = mapped_column(Text, deferred=True)
    text_chars: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    text_truncated: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    has_thumbnail: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class UploadStatus(enum.StrEnum):
    open = "open"
    completed = "completed"
    rejected = "rejected"
    expired = "expired"


class UploadSession(UUIDPrimaryKey, Timestamps, Base):
    """An upload in progress. Its declared size is reserved against the quota up front."""

    __tablename__ = "upload_sessions"

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("projects.id", ondelete="CASCADE")
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    relative_path: Mapped[str | None] = mapped_column(String(1024))
    declared_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    received_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[UploadStatus] = mapped_column(
        Enum(UploadStatus, name="upload_status"), default=UploadStatus.open, nullable=False
    )
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("files.id", ondelete="SET NULL")
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class FileLink(Base):
    """A file attached to a message."""

    __tablename__ = "file_links"

    message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("files.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)


class QuotaUsage(Base):
    """Bytes stored or reserved per user. Changed only by atomic conditional updates."""

    __tablename__ = "quota_usage"

    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    storage_bytes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)


class JobStatus(enum.StrEnum):
    queued = "queued"
    running = "running"
    completed = "completed"
    failed = "failed"


class Job(UUIDPrimaryKey, Timestamps, Base):
    """Durable background work (file processing, exports), claimed by workers with a lease."""

    __tablename__ = "jobs"
    __table_args__ = (Index("ix_jobs_status_created_at", "status", "created_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, name="job_status"), default=JobStatus.queued, nullable=False
    )
    payload: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    result: Mapped[dict | None] = mapped_column(JSONType)
    error: Mapped[str | None] = mapped_column(String(500))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    worker_id: Mapped[str | None] = mapped_column(String(128))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

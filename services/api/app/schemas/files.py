import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.models import FileKind, FileStatus


class UploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=1024)
    size: int = Field(ge=0)
    # Path inside an uploaded folder, e.g. "src/app/main.py".
    relative_path: str | None = Field(default=None, max_length=2048)
    # Upload as a project knowledge file.
    project_id: uuid.UUID | None = None


class UploadOut(BaseModel):
    id: uuid.UUID
    upload_url: str
    expires_at: datetime


class FileOut(BaseModel):
    id: uuid.UUID
    filename: str
    relative_path: str | None
    media_type: str
    kind: FileKind
    size_bytes: int
    sha256: str
    status: FileStatus
    error: str | None
    text_chars: int
    text_truncated: bool
    has_thumbnail: bool
    meta: dict[str, Any]
    project_id: uuid.UUID | None
    created_at: datetime


class FileText(BaseModel):
    text: str
    truncated: bool


class StorageUsage(BaseModel):
    used_bytes: int
    limit_bytes: int
    max_upload_bytes: int

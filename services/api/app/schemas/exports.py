import uuid
from typing import Literal

from pydantic import BaseModel, model_validator

from app.models import JobStatus
from app.schemas.files import FileOut


class ExportCreate(BaseModel):
    scope: Literal["conversation", "project"]
    id: uuid.UUID
    format: Literal["markdown", "json", "zip"]

    @model_validator(mode="after")
    def _project_is_zip(self) -> "ExportCreate":
        if self.scope == "project" and self.format != "zip":
            raise ValueError("Projects export as zip")
        return self


class ExportOut(BaseModel):
    id: uuid.UUID
    status: JobStatus
    error: str | None
    file: FileOut | None

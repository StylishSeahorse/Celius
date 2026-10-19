"""ORM models. Importing this package registers every table on Base.metadata."""

from app.models.conversation import (
    Branch,
    Conversation,
    Message,
    MessagePart,
    MessageRevision,
    MessageRole,
    MessageStatus,
    Project,
)
from app.models.files import (
    FileKind,
    FileLink,
    FileStatus,
    Job,
    JobStatus,
    QuotaUsage,
    StoredFile,
    UploadSession,
    UploadStatus,
)
from app.models.generation import (
    TERMINAL_RUN_STATUSES,
    ModelCatalogEntry,
    Run,
    RunEvent,
    RunStatus,
)
from app.models.identity import (
    AuditEvent,
    DeviceSyncState,
    Session,
    SyncEvent,
    User,
    UserSettings,
)

__all__ = [
    "TERMINAL_RUN_STATUSES",
    "AuditEvent",
    "Branch",
    "Conversation",
    "DeviceSyncState",
    "FileKind",
    "FileLink",
    "FileStatus",
    "Job",
    "JobStatus",
    "QuotaUsage",
    "StoredFile",
    "UploadSession",
    "UploadStatus",
    "Message",
    "MessagePart",
    "MessageRevision",
    "MessageRole",
    "MessageStatus",
    "ModelCatalogEntry",
    "Project",
    "Run",
    "RunEvent",
    "RunStatus",
    "Session",
    "SyncEvent",
    "User",
    "UserSettings",
]

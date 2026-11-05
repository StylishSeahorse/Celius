"""Skills: reusable procedures in the Agent Skills format (SKILL.md + supporting files),
versioned, validated and published under a trust policy (ADR C-06)."""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    Uuid,
)
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey, utcnow
from app.models.memory import EmbeddingType

# Skill status (the skill as a whole):
#   candidate   generated or proposed; no version published yet; never offered to the model
#   active      a published version exists and is offered (search, index, read)
#   deprecated  kept for history; not offered
SKILL_STATUSES = ("candidate", "active", "deprecated")
# Version status:
#   candidate   just written; validation pending or failed
#   validated   passed validation (and its test, if it has one, unless still untested)
#   pending     waiting for the user's approval to publish (trust policy)
#   published   the version the skill uses now
#   retired     a previously published version (rollback target)
#   rejected    refused by the user or failed validation for good
VERSION_STATUSES = ("candidate", "validated", "pending", "published", "retired", "rejected")


class Skill(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "skills"
    __table_args__ = (
        Index("uq_skills_owner_id_name", "owner_id", "name", unique=True),
        Index("ix_skills_owner_id_status", "owner_id", "status"),
    )

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(1024), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    # user (written in the UI), model (skill_create), generated (learned after a run),
    # imported (uploaded archive)
    origin: Mapped[str] = mapped_column(String(16), nullable=False)
    tags: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    published_version: Mapped[int | None] = mapped_column(Integer)
    latest_version: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    # Usage statistics (ADR C-06: improvement is driven by recorded outcomes).
    use_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    success_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failure_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    embedding = deferred(mapped_column(EmbeddingType, nullable=True))
    embedding_model: Mapped[str | None] = mapped_column(String(255))


class SkillVersion(UUIDPrimaryKey, Timestamps, Base):
    __tablename__ = "skill_versions"
    __table_args__ = (
        Index("uq_skill_versions_skill_id_version", "skill_id", "version", unique=True),
    )

    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="candidate", nullable=False)
    # {"SKILL.md": "...", "scripts/check.sh": "...", "references/notes.md": "..."}
    files: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    frontmatter: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changelog: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(String(16), nullable=False)  # user|model|generated
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL")
    )
    source_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("conversations.id", ondelete="SET NULL")
    )
    # {"ok": bool, "errors": [...], "warnings": [...]}
    validation: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    # {"status": "passed|failed|not_defined|untested", "output": "...", "at": iso}
    test: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SkillEvent(UUIDPrimaryKey, Base):
    """What happened to a skill: validations, tests, uses in runs and their outcomes, user
    feedback. The evaluation history the UI shows and improvement proposals read."""

    __tablename__ = "skill_events"
    __table_args__ = (Index("ix_skill_events_skill_id_created_at", "skill_id", "created_at"),)

    skill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("skills.id", ondelete="CASCADE"), nullable=False
    )
    version: Mapped[int | None] = mapped_column(Integer)
    # validation | test | use | feedback | lifecycle
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # passed | failed | pending | success | failure | corrected | info
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )

"""Calendar and email accounts, the local calendar, and automations (ADR C-11)."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, JSONType, Timestamps, UUIDPrimaryKey, utcnow

ACCOUNT_KINDS = ("calendar", "email")
ACCOUNT_PROVIDERS = {"calendar": ("caldav",), "email": ("imap",)}


class IntegrationAccount(UUIDPrimaryKey, Timestamps, Base):
    """A connection to the user's calendar or mailbox. Credentials are encrypted at rest
    (services/secrets.py) and never returned by the API."""

    __tablename__ = "integration_accounts"
    __table_args__ = (Index("ix_integration_accounts_owner_id_kind", "owner_id", "kind"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # calendar | email
    provider: Mapped[str] = mapped_column(String(16), nullable=False)  # caldav | imap
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Non-secret settings: URLs, hosts, ports, user name, folders, from address.
    config: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    credentials: Mapped[str | None] = mapped_column(Text)  # sealed
    # Allow a server on the local network (a home Nextcloud or mail server).
    allow_private_network: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_error: Mapped[str | None] = mapped_column(String(500))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CalendarEvent(UUIDPrimaryKey, Timestamps, Base):
    """An event in Celius's own calendar (always available, no account needed)."""

    __tablename__ = "calendar_events"
    __table_args__ = (Index("ix_calendar_events_owner_id_starts_at", "owner_id", "starts_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    all_day: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    location: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str | None] = mapped_column(Text)
    # Time blocked for one of the user's tasks.
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("user_tasks.id", ondelete="SET NULL")
    )


class Automation(UUIDPrimaryKey, Timestamps, Base):
    """When <trigger> (and <conditions>), do <actions>. Evaluated by the worker without a
    model; only a `run_prompt` action uses one."""

    __tablename__ = "automations"
    __table_args__ = (Index("ix_automations_enabled_next_check_at", "enabled", "next_check_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    trigger: Mapped[dict] = mapped_column(JSONType, nullable=False)
    conditions: Mapped[list] = mapped_column(JSONType, default=list, nullable=False)
    actions: Mapped[list] = mapped_column(JSONType, nullable=False)
    # Engine state: what has already fired (task ids, a page hash, the last email UID…).
    state: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str | None] = mapped_column(String(16))
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    disabled_reason: Mapped[str | None] = mapped_column(String(300))
    created_by: Mapped[str] = mapped_column(String(16), default="user", nullable=False)


class AutomationRun(UUIDPrimaryKey, Base):
    """One firing: what triggered it, what each action did, and how it ended."""

    __tablename__ = "automation_runs"
    __table_args__ = (
        Index("ix_automation_runs_automation_id_created_at", "automation_id", "created_at"),
    )

    automation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("automations.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)  # ok | failed | skipped
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    detail: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    error: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )


class AutomationEvent(UUIDPrimaryKey, Base):
    """Something that happened in Celius that automations may react to (a task changed
    status, a task was created). Consumed by the engine; kept briefly."""

    __tablename__ = "automation_events"
    __table_args__ = (Index("ix_automation_events_processed_at", "processed_at"),)

    owner_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    data: Mapped[dict] = mapped_column(JSONType, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

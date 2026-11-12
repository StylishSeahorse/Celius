"""Retention for Celius Remote (ADR C-16), run by the worker's maintenance pass.

- Terminal output of sessions is the bulk of the event log: kept REMOTE_OUTPUT_RETENTION_DAYS.
  Other session events (commands, files, approvals) stay with the conversation and go
  when it is deleted.
- Finished commands (including fetched file contents) are kept 30 days for the audit
  trail and then removed.
- Unused pairing codes are removed a day after they expire.
"""

from datetime import timedelta

from sqlalchemy import delete

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.models import RemoteCommand, RemoteEnrollment, RemoteSessionEvent
from app.models.remote import FINAL_COMMAND_STATUSES

COMMAND_RETENTION_DAYS = 30


async def prune(factory: SessionFactory, settings: Settings) -> dict[str, int]:
    now = utcnow()
    async with factory() as db:
        output = await db.execute(
            delete(RemoteSessionEvent).where(
                RemoteSessionEvent.type == "terminal.output",
                RemoteSessionEvent.created_at
                < now - timedelta(days=settings.remote_output_retention_days),
            )
        )
        commands = await db.execute(
            delete(RemoteCommand).where(
                RemoteCommand.status.in_(FINAL_COMMAND_STATUSES),
                RemoteCommand.updated_at < now - timedelta(days=COMMAND_RETENTION_DAYS),
            )
        )
        codes = await db.execute(
            delete(RemoteEnrollment).where(RemoteEnrollment.expires_at < now - timedelta(days=1))
        )
        await db.commit()
    return {
        "terminal_output": output.rowcount or 0,
        "commands": commands.rowcount or 0,
        "pairing_codes": codes.rowcount or 0,
    }

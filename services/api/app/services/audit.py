import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.base import utcnow
from app.models import AuditEvent


def audit(
    db: AsyncSession,
    user_id: uuid.UUID | None,
    action: str,
    target_type: str | None = None,
    target_id: Any = None,
    ip_address: str | None = None,
    **detail: Any,
) -> None:
    """Record a security-relevant action in the same transaction as the change itself."""
    db.add(
        AuditEvent(
            user_id=user_id,
            action=action,
            target_type=target_type,
            target_id=str(target_id) if target_id is not None else None,
            detail=detail,
            ip_address=ip_address,
            created_at=utcnow(),
        )
    )

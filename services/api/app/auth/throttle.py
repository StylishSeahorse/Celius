"""Login throttling from the audit log: no extra state, and it survives restarts.

Too many failures in the window, for the same username or from the same address, and
further attempts are refused (429) until the window passes, even with the right password.
"""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.models import AuditEvent

SCAN_LIMIT = 2000


async def login_blocked(
    db: AsyncSession, settings: Settings, username: str, ip: str | None
) -> int | None:
    """Seconds until another attempt is allowed, or None if it is allowed now."""
    window = timedelta(minutes=settings.login_window_minutes)
    since = utcnow() - window
    rows = (
        await db.execute(
            select(AuditEvent.created_at, AuditEvent.ip_address, AuditEvent.detail)
            .where(AuditEvent.action == "auth.login_failed", AuditEvent.created_at >= since)
            .order_by(AuditEvent.created_at.desc())
            .limit(SCAN_LIMIT)
        )
    ).all()
    by_user = [at for at, _, detail in rows if (detail or {}).get("username") == username]
    by_ip = [at for at, address, _ in rows if ip and address == ip]
    for times, limit in (
        (by_user, settings.login_max_failures_per_user),
        (by_ip, settings.login_max_failures_per_ip),
    ):
        if len(times) >= limit:
            oldest = sorted(times)[-limit]
            if oldest.tzinfo is None:
                oldest = oldest.replace(tzinfo=since.tzinfo)
            return max(1, int((oldest + window - utcnow()).total_seconds()))
    return None

"""Machine identity (ADR C-16): pairing codes, credentials and short-lived access tokens.

- A user creates a one-time pairing code; the machine trades it for an agent id and a
  credential (256 random bits, shown once, only its SHA-256 stored).
- A new machine is `pending` until the user authorises it; only `active` machines get
  access tokens.
- Access tokens are HMAC-signed (key derived from APP_SECRET_KEY), valid for minutes and
  never stored; every use re-checks that the machine is still active, so revocation takes
  effect at once.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.errors import AppError
from app.models import RemoteAgent, RemoteAgentCredential, RemoteCommand, RemoteEnrollment
from app.services.audit import audit

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no 0/O, 1/I
CREDENTIAL_PREFIX = "cra_"
TOKEN_PREFIX = "crt_"


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def new_pairing_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(12))  # ~60 bits
    return f"{raw[:4]}-{raw[4:8]}-{raw[8:]}"


async def create_enrollment(
    db: AsyncSession, owner_id: uuid.UUID, name: str | None, settings: Settings
) -> tuple[str, RemoteEnrollment]:
    code = new_pairing_code()
    enrollment = RemoteEnrollment(
        id=uuid.uuid4(),
        owner_id=owner_id,
        code_hash=_sha256(normalize_code(code)),
        name=name,
        created_at=utcnow(),
        expires_at=utcnow() + timedelta(minutes=settings.remote_enrollment_ttl_minutes),
    )
    db.add(enrollment)
    audit(db, owner_id, "remote_enrollment.created", "remote_enrollment", enrollment.id)
    return code, enrollment


def new_credential() -> str:
    return CREDENTIAL_PREFIX + secrets.token_urlsafe(32)


def _aware(moment):
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=utcnow().tzinfo)


async def enroll(
    db: AsyncSession, code: str, facts: dict, ip_address: str | None
) -> tuple[RemoteAgent, str]:
    """Consume a pairing code: create the machine (pending) and its first credential."""
    enrollment = await db.scalar(
        select(RemoteEnrollment)
        .where(RemoteEnrollment.code_hash == _sha256(normalize_code(code)))
        .with_for_update()
    )
    if (
        enrollment is None
        or enrollment.used_at is not None
        or _aware(enrollment.expires_at) <= utcnow()
    ):
        raise AppError(401, "invalid_code", "This pairing code is invalid or has expired")
    agent = RemoteAgent(
        id=uuid.uuid4(),
        owner_id=enrollment.owner_id,
        name=(enrollment.name or facts.get("hostname") or "Remote machine")[:100],
        hostname=facts.get("hostname"),
        os=facts.get("os"),
        os_version=facts.get("os_version"),
        arch=facts.get("arch"),
        agent_version=facts.get("agent_version"),
        protocol_version=facts.get("protocol_version"),
        status="pending",
        capabilities={},
        local_policy={},
        config=default_config(),
        last_ip=ip_address,
    )
    db.add(agent)
    await db.flush()
    credential = new_credential()
    db.add(
        RemoteAgentCredential(
            id=uuid.uuid4(),
            agent_id=agent.id,
            secret_hash=_sha256(credential),
            created_at=utcnow(),
        )
    )
    enrollment.used_at = utcnow()
    enrollment.agent_id = agent.id
    audit(
        db,
        enrollment.owner_id,
        "remote_agent.enrolled",
        "remote_agent",
        agent.id,
        ip_address,
        hostname=agent.hostname,
        os=agent.os,
    )
    return agent, credential


def default_config() -> dict:
    """Central's policy for a new machine: nothing beyond what the machine itself allows,
    and every change or command asks first."""
    return {
        "allow_shell": True,  # still needs allow_shell in the machine's own policy file
        "allow_write": True,
        "approval": "always",  # always | profile
        "adapters": ["celius", "claude_code"],
        "max_sessions": 4,
        "network": "unrestricted",  # recorded; see docs/remote/ARCHITECTURE.md §8
    }


async def authenticate_credential(
    db: AsyncSession, credential: str, settings: Settings
) -> tuple[RemoteAgent, RemoteAgentCredential]:
    row = await db.scalar(
        select(RemoteAgentCredential).where(
            RemoteAgentCredential.secret_hash == _sha256(credential)
        )
    )
    now = utcnow()
    if (
        row is None
        or row.revoked_at is not None
        or (row.expires_at is not None and _aware(row.expires_at) <= now)
    ):
        raise AppError(401, "invalid_credential", "This machine credential is not valid")
    agent = await db.get(RemoteAgent, row.agent_id)
    if agent is None or agent.status == "revoked":
        raise AppError(401, "agent_revoked", "This machine has been revoked")
    row.last_used_at = now
    if row.expires_at is None:
        # The current credential is in use: credentials it replaced stop working now.
        await db.execute(
            update(RemoteAgentCredential)
            .where(
                RemoteAgentCredential.agent_id == agent.id,
                RemoteAgentCredential.id != row.id,
                RemoteAgentCredential.revoked_at.is_(None),
            )
            .values(revoked_at=now)
        )
    return agent, row


async def rotate_credential(
    db: AsyncSession, agent: RemoteAgent, settings: Settings, ip_address: str | None = None
) -> str:
    now = utcnow()
    grace = now + timedelta(minutes=settings.remote_credential_grace_minutes)
    await db.execute(
        update(RemoteAgentCredential)
        .where(
            RemoteAgentCredential.agent_id == agent.id,
            RemoteAgentCredential.revoked_at.is_(None),
            RemoteAgentCredential.expires_at.is_(None),
        )
        .values(expires_at=grace)
    )
    credential = new_credential()
    db.add(
        RemoteAgentCredential(
            id=uuid.uuid4(), agent_id=agent.id, secret_hash=_sha256(credential), created_at=now
        )
    )
    audit(
        db, agent.owner_id, "remote_agent.credential_rotated", "remote_agent", agent.id, ip_address
    )
    return credential


# ---------------------------------------------------------------------- access tokens


def _token_key(settings: Settings) -> bytes:
    return hmac.new(
        settings.app_secret_key.get_secret_value().encode(),
        b"celius-remote-agent-token-v1",
        hashlib.sha256,
    ).digest()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def issue_token(agent_id: uuid.UUID, settings: Settings) -> tuple[str, int]:
    expires = int(time.time()) + settings.remote_token_ttl_seconds
    body = _b64(
        json.dumps(
            {"a": str(agent_id), "e": expires, "n": secrets.token_hex(8)}, separators=(",", ":")
        ).encode()
    )
    signature = _b64(hmac.new(_token_key(settings), body.encode(), hashlib.sha256).digest())
    return f"{TOKEN_PREFIX}{body}.{signature}", expires


@dataclass
class TokenClaims:
    agent_id: uuid.UUID
    expires: int


def verify_token(token: str, settings: Settings) -> TokenClaims | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    body, _, signature = token[len(TOKEN_PREFIX) :].partition(".")
    expected = _b64(hmac.new(_token_key(settings), body.encode(), hashlib.sha256).digest())
    if not signature or not hmac.compare_digest(signature, expected):
        return None
    try:
        claims = json.loads(_unb64(body))
        agent_id = uuid.UUID(claims["a"])
        expires = int(claims["e"])
    except (ValueError, KeyError, TypeError):
        return None
    if expires <= time.time():
        return None
    return TokenClaims(agent_id, expires)


async def agent_for_token(db: AsyncSession, token: str | None, settings: Settings) -> RemoteAgent:
    claims = verify_token(token or "", settings)
    if claims is None:
        raise AppError(401, "invalid_token", "Missing, invalid or expired agent token")
    agent = await db.get(RemoteAgent, claims.agent_id)
    if agent is None or agent.status != "active":
        raise AppError(401, "agent_not_active", "This machine is not authorised")
    return agent


# ---------------------------------------------------------------------- user actions


async def authorize(db: AsyncSession, agent: RemoteAgent, user_id: uuid.UUID, ip: str | None):
    if agent.status == "revoked":
        raise AppError(409, "agent_revoked", "A revoked machine can't be authorised again")
    if agent.status == "pending":
        agent.status = "active"
        agent.authorized_at = utcnow()
        audit(db, user_id, "remote_agent.authorized", "remote_agent", agent.id, ip)


async def revoke(db: AsyncSession, agent: RemoteAgent, user_id: uuid.UUID, ip: str | None):
    now = utcnow()
    agent.status = "revoked"
    agent.revoked_at = agent.revoked_at or now
    await db.execute(
        update(RemoteAgentCredential)
        .where(
            RemoteAgentCredential.agent_id == agent.id, RemoteAgentCredential.revoked_at.is_(None)
        )
        .values(revoked_at=now)
    )
    # Nothing still queued for it may run later.
    await db.execute(
        update(RemoteCommand)
        .where(
            RemoteCommand.agent_id == agent.id,
            RemoteCommand.status.in_(["pending", "delivered", "running"]),
        )
        .values(status="cancelled", error="The machine was revoked", completed_at=now)
    )
    audit(db, user_id, "remote_agent.revoked", "remote_agent", agent.id, ip)

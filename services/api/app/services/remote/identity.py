"""Machine identity (ADR C-16, C-17): device sign-in, credentials and access tokens.

- `celius login` on a device starts a sign-in request (like the OAuth 2.0 device
  authorization grant, RFC 8628): the device keeps a secret device code and polls; the
  person sees a short user code there and accepts the request in Celius. Accepting creates
  the machine for that person; the device then receives its credential (256 random bits,
  shown once, only its SHA-256 stored).
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

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db.base import utcnow
from app.errors import AppError
from app.models import (
    RemoteAgent,
    RemoteAgentCredential,
    RemoteCommand,
    RemoteDeviceRequest,
    User,
)
from app.services.audit import audit

CODE_ALPHABET = "BCDFGHJKLMNPQRSTVWXZ23456789"  # no vowels (no words), no 0/O, 1/I
CREDENTIAL_PREFIX = "cra_"
TOKEN_PREFIX = "crt_"
DEVICE_CODE_PREFIX = "cdc_"
POLL_INTERVAL_SECONDS = 3
MAX_PENDING_PER_ADDRESS = 10  # per hour
MAX_PENDING_PER_USER = 10


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def normalize_code(code: str) -> str:
    return "".join(c for c in code.upper() if c.isalnum())


def format_user_code(code: str) -> str:
    return f"{code[:4]}-{code[4:]}"


def _aware(moment):
    return moment if moment is None or moment.tzinfo else moment.replace(tzinfo=utcnow().tzinfo)


def new_credential() -> str:
    return CREDENTIAL_PREFIX + secrets.token_urlsafe(32)


# ---------------------------------------------------------------------- device sign-in


async def start_device_login(
    db: AsyncSession,
    *,
    username: str | None,
    name: str | None,
    facts: dict,
    ip_address: str | None,
    settings: Settings,
) -> tuple[str, RemoteDeviceRequest]:
    """A device asks to sign in. Returns the secret device code and the request."""
    now = utcnow()
    recent = await db.scalar(
        select(func.count())
        .select_from(RemoteDeviceRequest)
        .where(
            RemoteDeviceRequest.ip_address == ip_address,
            RemoteDeviceRequest.created_at > now - timedelta(hours=1),
        )
    )
    if ip_address and recent >= MAX_PENDING_PER_ADDRESS:
        raise AppError(429, "slow_down", "Too many sign-in requests from this address")
    target = None
    if username:
        # An unknown name is not revealed: the request simply isn't listed for anyone,
        # and can still be accepted by typing its code.
        target = await db.scalar(
            select(User).where(User.username == username.strip().lower(), User.is_active)
        )
        if target is not None:
            waiting = await db.scalar(
                select(func.count())
                .select_from(RemoteDeviceRequest)
                .where(
                    RemoteDeviceRequest.target_user_id == target.id,
                    RemoteDeviceRequest.status == "pending",
                    RemoteDeviceRequest.expires_at > now,
                )
            )
            if waiting >= MAX_PENDING_PER_USER:
                raise AppError(429, "slow_down", "Too many devices are waiting to be accepted")
    for _ in range(10):
        user_code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))  # ~38 bits
        taken = await db.scalar(
            select(RemoteDeviceRequest.id).where(
                RemoteDeviceRequest.user_code == user_code,
                RemoteDeviceRequest.status == "pending",
            )
        )
        if taken is None:
            break
    device_code = DEVICE_CODE_PREFIX + secrets.token_urlsafe(32)
    request = RemoteDeviceRequest(
        id=uuid.uuid4(),
        device_code_hash=_sha256(device_code),
        user_code=user_code,
        target_user_id=target.id if target else None,
        name=(name or facts.get("hostname") or "")[:100] or None,
        facts=facts,
        ip_address=ip_address,
        status="pending",
        created_at=now,
        expires_at=now + timedelta(minutes=settings.remote_device_login_ttl_minutes),
    )
    db.add(request)
    if target is not None:
        audit(
            db,
            target.id,
            "remote_device.login_requested",
            "remote_device_request",
            request.id,
            ip_address,
            hostname=facts.get("hostname"),
        )
    return device_code, request


def _expired(request: RemoteDeviceRequest) -> bool:
    return _aware(request.expires_at) <= utcnow()


async def poll_device_login(db: AsyncSession, device_code: str) -> tuple[RemoteAgent, str]:
    """The device asks whether it was accepted. Errors follow RFC 8628 (`authorization_
    pending`, `access_denied`, `expired_token`); on success it gets its credential, once."""
    request = await db.scalar(
        select(RemoteDeviceRequest)
        .where(RemoteDeviceRequest.device_code_hash == _sha256(device_code))
        .with_for_update()
    )
    if request is None or request.status == "consumed":
        raise AppError(400, "invalid_grant", "Unknown or already used sign-in request")
    if request.status == "denied":
        raise AppError(403, "access_denied", "The sign-in was refused in Celius")
    if request.status == "pending":
        if _expired(request):
            request.status = "expired"
            raise AppError(410, "expired_token", "The sign-in request expired; run login again")
        raise AppError(428, "authorization_pending", "Waiting for you to accept it in Celius")
    if request.status != "approved" or request.agent_id is None:
        raise AppError(410, "expired_token", "The sign-in request expired; run login again")
    agent = await db.get(RemoteAgent, request.agent_id)
    if agent is None or agent.status != "active":
        raise AppError(403, "access_denied", "This machine was removed in Celius")
    credential = new_credential()
    db.add(
        RemoteAgentCredential(
            id=uuid.uuid4(), agent_id=agent.id, secret_hash=_sha256(credential), created_at=utcnow()
        )
    )
    request.status = "consumed"
    audit(
        db,
        agent.owner_id,
        "remote_agent.signed_in",
        "remote_agent",
        agent.id,
        request.ip_address,
        hostname=agent.hostname,
    )
    return agent, credential


async def requests_for(db: AsyncSession, user: User) -> list[RemoteDeviceRequest]:
    """Devices waiting for this person (named them at login)."""
    return list(
        (
            await db.scalars(
                select(RemoteDeviceRequest)
                .where(
                    RemoteDeviceRequest.target_user_id == user.id,
                    RemoteDeviceRequest.status == "pending",
                    RemoteDeviceRequest.expires_at > utcnow(),
                )
                .order_by(RemoteDeviceRequest.created_at.desc())
            )
        ).all()
    )


async def _pending_by_code(db: AsyncSession, user: User, user_code: str) -> RemoteDeviceRequest:
    request = await db.scalar(
        select(RemoteDeviceRequest)
        .where(
            RemoteDeviceRequest.user_code == normalize_code(user_code),
            RemoteDeviceRequest.status == "pending",
        )
        .with_for_update()
    )
    if (
        request is None
        or _expired(request)
        # Named someone else at login: not this person's to accept.
        or (request.target_user_id is not None and request.target_user_id != user.id)
    ):
        raise AppError(
            404, "unknown_code", "No device is waiting with this code (it may have expired)"
        )
    return request


async def approve_device(
    db: AsyncSession, user: User, user_code: str, name: str | None, ip_address: str | None
) -> RemoteAgent:
    request = await _pending_by_code(db, user, user_code)
    facts = request.facts or {}
    agent = RemoteAgent(
        id=uuid.uuid4(),
        owner_id=user.id,
        name=((name or "").strip() or request.name or facts.get("hostname") or "Remote machine")[
            :100
        ],
        hostname=facts.get("hostname"),
        os=facts.get("os"),
        os_version=facts.get("os_version"),
        arch=facts.get("arch"),
        agent_version=facts.get("agent_version"),
        protocol_version=facts.get("protocol_version"),
        status="active",
        capabilities={},
        local_policy={},
        config=default_config(),
        last_ip=request.ip_address,
        authorized_at=utcnow(),
    )
    db.add(agent)
    await db.flush()
    request.status = "approved"
    request.agent_id = agent.id
    request.decided_by = user.id
    request.decided_at = utcnow()
    audit(
        db,
        user.id,
        "remote_agent.accepted",
        "remote_agent",
        agent.id,
        ip_address,
        hostname=agent.hostname,
        device_ip=request.ip_address,
    )
    return agent


async def deny_device(db: AsyncSession, user: User, user_code: str, ip_address: str | None) -> None:
    request = await _pending_by_code(db, user, user_code)
    request.status = "denied"
    request.decided_by = user.id
    request.decided_at = utcnow()
    audit(
        db,
        user.id,
        "remote_device.refused",
        "remote_device_request",
        request.id,
        ip_address,
        hostname=(request.facts or {}).get("hostname"),
    )


def default_config() -> dict:
    """Central's policy for a new machine: nothing beyond what the machine itself allows,
    and every change or command asks first."""
    return {
        "allow_shell": True,  # still needs allow_shell in the machine's own policy file
        "allow_write": True,
        "approval": "always",  # always | profile
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
    if agent is None or agent.status != "active":
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

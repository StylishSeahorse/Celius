"""Calendar and email accounts: validated settings, sealed credentials (ADR C-11).

Credentials (passwords, app passwords) are encrypted with services/secrets.py and never
returned; the API says only whether they are set. Network access goes through the same
guard as MCP and web tools: public addresses only, unless the user allows the local
network for this account (a home Nextcloud or mail server).
"""

import uuid
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.errors import AppError, not_found
from app.models import ACCOUNT_PROVIDERS, IntegrationAccount
from app.services.audit import audit
from app.services.secrets import seal, unseal

PURPOSE = "integration-credentials"

# Required non-secret settings per provider, and their defaults.
CONFIG = {
    "caldav": {"url": None, "username": None},
    "imap": {
        "imap_host": None,
        "imap_port": 993,
        "smtp_host": None,
        "smtp_port": 465,
        "username": None,
        "from_address": None,
        "drafts_folder": "Drafts",
        "inbox_folder": "INBOX",
        "smtp_starttls": False,
    },
}


def clean_config(provider: str, config: dict[str, Any]) -> dict[str, Any]:
    if provider not in CONFIG:
        raise AppError(422, "invalid_provider", f"Unknown provider {provider!r}")
    out = {}
    for key, default in CONFIG[provider].items():
        value = config.get(key, default)
        if value is None or value == "":
            raise AppError(422, "invalid_config", f"{key} is required")
        out[key] = value
    if provider == "caldav":
        parts = urlsplit(str(out["url"]))
        if parts.scheme not in ("https", "http") or not parts.hostname:
            raise AppError(422, "invalid_config", "The calendar URL must be an http(s) URL")
        out["url"] = str(out["url"]).rstrip("/") + "/"
    else:
        for key in ("imap_port", "smtp_port"):
            if not isinstance(out[key], int) or not 1 <= out[key] <= 65535:
                raise AppError(422, "invalid_config", f"{key} must be a port number")
        if "@" not in str(out["from_address"]):
            raise AppError(422, "invalid_config", "from_address must be an email address")
    return out


def credentials(settings: Settings, account: IntegrationAccount) -> dict[str, Any]:
    if not account.credentials:
        return {}
    value = unseal(settings, account.credentials, PURPOSE)
    if value is None:
        raise AppError(
            409,
            "credentials_unreadable",
            f"The password for {account.name!r} can't be read (the server key changed); "
            "enter it again",
        )
    return value


async def create(
    db: AsyncSession,
    owner_id: uuid.UUID,
    *,
    kind: str,
    provider: str,
    name: str,
    config: dict,
    password: str | None,
    allow_private_network: bool,
    settings: Settings,
) -> IntegrationAccount:
    if provider not in ACCOUNT_PROVIDERS.get(kind, ()):
        raise AppError(422, "invalid_provider", f"{provider!r} isn't a {kind} provider")
    account = IntegrationAccount(
        id=uuid.uuid4(),
        owner_id=owner_id,
        kind=kind,
        provider=provider,
        name=name.strip()[:200] or provider,
        config=clean_config(provider, config),
        credentials=seal(settings, {"password": password}, PURPOSE) if password else None,
        allow_private_network=allow_private_network,
        enabled=True,
    )
    db.add(account)
    await db.flush()
    audit(db, owner_id, "integration.created", "integration", account.id, kind=kind)
    return account


async def owned(
    db: AsyncSession, owner_id: uuid.UUID, account_id: uuid.UUID | str, kind: str | None = None
) -> IntegrationAccount:
    try:
        account_id = uuid.UUID(str(account_id))
    except ValueError as exc:
        raise not_found("Account") from exc
    account = await db.scalar(
        select(IntegrationAccount).where(
            IntegrationAccount.id == account_id, IntegrationAccount.owner_id == owner_id
        )
    )
    if account is None or (kind and account.kind != kind):
        raise not_found("Account")
    return account


async def accounts_of(
    db: AsyncSession, owner_id: uuid.UUID, kind: str, *, enabled_only: bool = True
) -> list[IntegrationAccount]:
    query = select(IntegrationAccount).where(
        IntegrationAccount.owner_id == owner_id, IntegrationAccount.kind == kind
    )
    if enabled_only:
        query = query.where(IntegrationAccount.enabled.is_(True))
    return list((await db.scalars(query.order_by(IntegrationAccount.created_at))).all())


def set_password(settings: Settings, account: IntegrationAccount, password: str | None) -> None:
    account.credentials = seal(settings, {"password": password}, PURPOSE) if password else None

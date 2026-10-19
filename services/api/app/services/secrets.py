"""Encryption at rest for stored credentials (e.g. MCP server tokens).

The key is derived from APP_SECRET_KEY, so rotating that key makes stored credentials
unreadable: they must be entered again (the UI reports them as needing re-entry).
"""

import base64
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.config import Settings


def _fernet(settings: Settings, purpose: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"celius", info=purpose.encode()).derive(
        settings.app_secret_key.get_secret_value().encode()
    )
    return Fernet(base64.urlsafe_b64encode(key))


def seal(settings: Settings, value: Any, purpose: str = "credentials") -> str:
    return _fernet(settings, purpose).encrypt(json.dumps(value).encode()).decode()


def unseal(settings: Settings, token: str, purpose: str = "credentials") -> Any | None:
    try:
        return json.loads(_fernet(settings, purpose).decrypt(token.encode()))
    except (InvalidToken, ValueError):
        return None

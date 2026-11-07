"""Gmail through "Sign in with Google" (OAuth 2.0), ADR C-14.

The mailbox is still IMAP and SMTP (services/integrations/email.py), authenticated with
XOAUTH2 instead of a password, so reading, drafts and the always-ask sending rule are
unchanged. What this module adds:

- **Start:** a Google consent URL with PKCE (S256), `access_type=offline` and a sealed
  `state` holding the user, the PKCE verifier, an expiry and a hash of a random nonce.
  The nonce goes to the browser in a short-lived HttpOnly cookie (SameSite=Lax: the
  session cookie is Strict, so it is not sent when Google redirects back). The callback
  needs both, so a consent started in one browser or for one user can't be finished in
  another.
- **Callback:** the code is exchanged at Google's token endpoint over TLS; the ID token
  from that response gives the verified address (its audience and issuer are checked; a
  token received directly from the endpoint needs no signature check, OIDC Core 3.1.3.7).
- **Tokens:** the refresh token (and the current access token) are sealed like other
  credentials and never returned. Access tokens are refreshed when they are about to
  expire, cached per process; a revoked or expired grant asks the user to sign in again.
- **Fixed servers:** a Gmail account always talks to imap.gmail.com and smtp.gmail.com,
  whatever a later settings change says, so the token can't be sent to another host.
"""

import base64
import hashlib
import json
import secrets
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.services.secrets import seal, unseal

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
ISSUERS = ("https://accounts.google.com", "accounts.google.com")
SCOPES = ("openid", "email", "https://mail.google.com/")
STATE_PURPOSE = "google-oauth-state"
STATE_SECONDS = 600
NONCE_COOKIE = "celius_google_oauth"
CALLBACK_PATH = "/api/v1/integrations/google/callback"
TIMEOUT = 20
EARLY_REFRESH_SECONDS = 120

# The account's servers: fixed for Gmail (see the module docstring).
GMAIL_CONFIG = {
    "imap_host": "imap.gmail.com",
    "imap_port": 993,
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "smtp_starttls": False,
    "inbox_folder": "INBOX",
    "drafts_folder": "[Gmail]/Drafts",
}

# Tests replace this with httpx.MockTransport.
TRANSPORT: httpx.AsyncBaseTransport | None = None

# account id -> (refresh-token hash, access token, expiry as a unix time)
_ACCESS: dict[uuid.UUID, tuple[str, str, float]] = {}


class GoogleError(Exception):
    """Something the user should see as is (and possibly fix by signing in again)."""


@dataclass
class Grant:
    email: str
    refresh_token: str | None
    access_token: str
    expires_at: float


def configured(settings: Settings) -> bool:
    return bool(settings.google_oauth_client_id and settings.google_oauth_client_secret)


def redirect_uri(settings: Settings) -> str:
    return settings.google_oauth_redirect_url or (
        settings.app_public_url.rstrip("/") + CALLBACK_PATH
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT)


# ---------------------------------------------------------------------- consent


def start(settings: Settings, user_id: uuid.UUID, login_hint: str | None = None):
    """The consent URL and the nonce for the browser cookie."""
    if not configured(settings):
        raise GoogleError("Sign in with Google isn't set up on this server")
    verifier = secrets.token_urlsafe(48)
    nonce = secrets.token_urlsafe(24)
    state = seal(
        settings,
        {
            "u": str(user_id),
            "v": verifier,
            "n": _hash(nonce),
            "exp": int(time.time()) + STATE_SECONDS,
        },
        STATE_PURPOSE,
    )
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    )
    params = {
        "client_id": settings.google_oauth_client_id,
        "redirect_uri": redirect_uri(settings),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",  # always returns a refresh token, also on reconnecting
        "include_granted_scopes": "true",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{AUTH_URL}?{urlencode(params)}", nonce


def check_state(settings: Settings, state: str, nonce: str | None) -> tuple[uuid.UUID, str]:
    """The user who started the consent and the PKCE verifier, or GoogleError."""
    data = unseal(settings, state or "", STATE_PURPOSE)
    if not isinstance(data, dict):
        raise GoogleError("The sign-in link isn't valid; start again")
    if data.get("exp", 0) < time.time():
        raise GoogleError("The sign-in took too long; start again")
    if not nonce or not secrets.compare_digest(_hash(nonce), str(data.get("n"))):
        raise GoogleError("Finish signing in in the same browser you started in")
    return uuid.UUID(data["u"]), data["v"]


def _claims(id_token: str, settings: Settings) -> dict:
    try:
        payload = id_token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise GoogleError("Google's answer couldn't be read") from exc
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if settings.google_oauth_client_id not in audiences or claims.get("iss") not in ISSUERS:
        raise GoogleError("Google's answer wasn't meant for this server")
    if not claims.get("email") or claims.get("email_verified") is not True:
        raise GoogleError("Google didn't confirm the address of this account")
    return claims


async def _token_request(settings: Settings, data: dict) -> dict:
    secret = settings.google_oauth_client_secret
    body = {
        "client_id": settings.google_oauth_client_id,
        "client_secret": secret.get_secret_value() if secret else "",
        **data,
    }
    try:
        async with _client() as client:
            response = await client.post(TOKEN_URL, data=body)
    except httpx.HTTPError as exc:
        raise GoogleError(f"Google couldn't be reached ({exc.__class__.__name__})") from exc
    try:
        answer = response.json()
    except ValueError:
        answer = {}
    if response.status_code != 200:
        if answer.get("error") == "invalid_grant":
            raise GoogleError("Google access has expired or was revoked: sign in with Google again")
        raise GoogleError(f"Google refused ({answer.get('error') or response.status_code})")
    return answer


async def exchange(settings: Settings, code: str, verifier: str) -> Grant:
    answer = await _token_request(
        settings,
        {
            "grant_type": "authorization_code",
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": redirect_uri(settings),
        },
    )
    granted = set(str(answer.get("scope") or "").split())
    if "https://mail.google.com/" not in granted:
        raise GoogleError("Celius needs access to Gmail: tick the Gmail box when you sign in")
    claims = _claims(str(answer.get("id_token") or ""), settings)
    return Grant(
        email=str(claims["email"]).lower(),
        refresh_token=answer.get("refresh_token"),
        access_token=str(answer["access_token"]),
        expires_at=time.time() + int(answer.get("expires_in") or 3600),
    )


# ---------------------------------------------------------------------- tokens


def sealed_credentials(grant: Grant, previous: dict | None = None) -> dict:
    """What is stored (sealed by accounts.py). Google may leave out the refresh token on
    a reconnect; the previous one is kept then."""
    refresh = grant.refresh_token or (previous or {}).get("refresh_token")
    if not refresh:
        raise GoogleError(
            "Google didn't grant offline access; remove Celius in your Google "
            "account's third-party access page and sign in again"
        )
    return {
        "refresh_token": refresh,
        "access_token": grant.access_token,
        "expires_at": grant.expires_at,
    }


async def access_token(settings: Settings, account_id: uuid.UUID, stored: dict) -> str:
    """A valid access token for the account, refreshed when it is about to expire."""
    refresh = stored.get("refresh_token")
    if not refresh:
        raise GoogleError("This Gmail account isn't signed in: sign in with Google again")
    key = _hash(refresh)
    now = time.time()
    cached = _ACCESS.get(account_id)
    if cached and cached[0] == key and cached[2] - EARLY_REFRESH_SECONDS > now:
        return cached[1]
    if (
        stored.get("access_token")
        and float(stored.get("expires_at") or 0) - EARLY_REFRESH_SECONDS > now
    ):
        _ACCESS[account_id] = (key, stored["access_token"], float(stored["expires_at"]))
        return stored["access_token"]
    answer = await _token_request(
        settings, {"grant_type": "refresh_token", "refresh_token": refresh}
    )
    token = str(answer["access_token"])
    _ACCESS[account_id] = (key, token, now + int(answer.get("expires_in") or 3600))
    return token


def forget(account_id: uuid.UUID) -> None:
    _ACCESS.pop(account_id, None)


async def revoke(stored: dict) -> None:
    """Best effort: tell Google to drop the grant when the account is removed."""
    token = stored.get("refresh_token") or stored.get("access_token")
    if not token:
        return
    try:
        async with _client() as client:
            await client.post(REVOKE_URL, data={"token": token})
    except httpx.HTTPError:
        pass


def xoauth2(user: str, token: str) -> str:
    return f"user={user}\x01auth=Bearer {token}\x01\x01"

"""Meta (Facebook Login) for the event finder, ADR C-17.

The user signs in on Meta's own page; Celius never sees the password. What it keeps:

- the Page access token of the Facebook Page linked to the user's Instagram
  Professional account (it doesn't expire while the user keeps the app), plus the
  long-lived user token (about 60 days) as a fallback;
- the Instagram account's id, which Business Discovery is called on to read *other*
  Business/Creator accounts' recent posts.

Tokens are sealed like other credentials, never returned, and every Graph call carries
an `appsecret_proof`, so a leaked token is useless without the app secret.

The consent is bound to the user and the browser like Google's (a sealed state plus an
HttpOnly nonce cookie on the callback path). Endpoint shapes follow Meta's Graph API;
`META_GRAPH_VERSION` picks the version.
"""

import hashlib
import hmac
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.services.secrets import seal, unseal

STATE_PURPOSE = "meta-oauth-state"
STATE_SECONDS = 600
NONCE_COOKIE = "celius_meta_oauth"
CALLBACK_PATH = "/api/v1/integrations/meta/callback"
# Business Discovery needs instagram_basic; the Page list (and its Instagram account)
# needs pages_show_list, pages_read_engagement and, for Pages in a business portfolio,
# business_management.
SCOPES = ("instagram_basic", "pages_show_list", "pages_read_engagement", "business_management")
TIMEOUT = 30
USERNAME = re.compile(r"[A-Za-z0-9._]{1,30}")

# Tests replace this with httpx.MockTransport.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class MetaError(Exception):
    """Shown to the user as is."""


class MetaAuthError(MetaError):
    """The token no longer works: sign in with Meta again."""


def configured(settings: Settings) -> bool:
    return bool(settings.meta_app_id and settings.meta_app_secret)


def redirect_uri(settings: Settings) -> str:
    return settings.meta_redirect_url or settings.app_public_url.rstrip("/") + CALLBACK_PATH


def graph_url(settings: Settings, path: str) -> str:
    return f"https://graph.facebook.com/{settings.meta_graph_version}/{path.lstrip('/')}"


def _secret(settings: Settings) -> str:
    return settings.meta_app_secret.get_secret_value() if settings.meta_app_secret else ""


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def proof(settings: Settings, token: str) -> str:
    """appsecret_proof: HMAC-SHA256 of the token with the app secret."""
    return hmac.new(_secret(settings).encode(), token.encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------- consent


def start(settings: Settings, user_id: uuid.UUID) -> tuple[str, str]:
    if not configured(settings):
        raise MetaError("Sign in with Meta isn't set up on this server")
    nonce = secrets.token_urlsafe(24)
    state = seal(
        settings,
        {"u": str(user_id), "n": _hash(nonce), "exp": int(time.time()) + STATE_SECONDS},
        STATE_PURPOSE,
    )
    params = {
        "client_id": settings.meta_app_id,
        "redirect_uri": redirect_uri(settings),
        "state": state,
        "response_type": "code",
        "scope": ",".join(SCOPES),
    }
    return (
        f"https://www.facebook.com/{settings.meta_graph_version}/dialog/oauth?{urlencode(params)}",
        nonce,
    )


def check_state(settings: Settings, state: str, nonce: str | None) -> uuid.UUID:
    data = unseal(settings, state or "", STATE_PURPOSE)
    if not isinstance(data, dict):
        raise MetaError("The sign-in link isn't valid; start again")
    if data.get("exp", 0) < time.time():
        raise MetaError("The sign-in took too long; start again")
    if not nonce or not secrets.compare_digest(_hash(nonce), str(data.get("n"))):
        raise MetaError("Finish signing in in the same browser you started in")
    return uuid.UUID(data["u"])


async def _get(settings: Settings, path: str, params: dict, token: str | None = None) -> dict:
    query = dict(params)
    if token:
        query["access_token"] = token
        query["appsecret_proof"] = proof(settings, token)
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT) as client:
            response = await client.get(graph_url(settings, path), params=query)
    except httpx.HTTPError as exc:
        raise MetaError(f"Meta couldn't be reached ({exc.__class__.__name__})") from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code == 200 and isinstance(body, dict) and "error" not in body:
        return body
    error = (body or {}).get("error") or {}
    code = error.get("code")
    message = str(error.get("message") or response.status_code)[:300]
    if code == 190 or (error.get("type") == "OAuthException" and code in (102, 463, 467)):
        raise MetaAuthError("Meta access has expired or was removed: sign in with Meta again")
    if code in (4, 17, 32, 613) or response.status_code == 429:
        raise MetaError("Meta's rate limit was reached; the next scan will try again")
    raise MetaError(f"Meta refused: {message}")


async def connect(settings: Settings, code: str) -> tuple[dict, dict]:
    """The code from the callback → (config, credentials) for the account."""
    short = await _get(
        settings,
        "oauth/access_token",
        {
            "client_id": settings.meta_app_id,
            "client_secret": _secret(settings),
            "redirect_uri": redirect_uri(settings),
            "code": code,
        },
    )
    long = await _get(
        settings,
        "oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": settings.meta_app_id,
            "client_secret": _secret(settings),
            "fb_exchange_token": short["access_token"],
        },
    )
    user_token = str(long["access_token"])
    expires_in = int(long.get("expires_in") or 60 * 24 * 3600)
    pages = await _get(
        settings,
        "me/accounts",
        {"fields": "name,access_token,instagram_business_account{id,username}", "limit": 100},
        user_token,
    )
    linked = [p for p in pages.get("data") or [] if p.get("instagram_business_account")]
    if not linked:
        raise MetaError(
            "No Instagram Professional account is linked to a Facebook Page you manage: "
            "switch Instagram to a Professional account, link it to a Page, and try again"
        )
    page = linked[0]
    instagram = page["instagram_business_account"]
    config = {
        "ig_user_id": str(instagram["id"]),
        "ig_username": instagram.get("username") or "",
        "page_id": str(page.get("id") or ""),
        "page_name": page.get("name") or "",
    }
    credentials = {
        "page_token": page.get("access_token"),
        "user_token": user_token,
        "user_expires_at": time.time() + expires_in,
    }
    return config, credentials


def token_of(credentials: dict) -> str:
    token = credentials.get("page_token") or credentials.get("user_token")
    if not token:
        raise MetaAuthError("This Meta account isn't signed in: sign in with Meta again")
    return token


# ---------------------------------------------------------------------- reading


def _time(value) -> datetime | None:
    if not value:
        return None
    text = str(value)
    if re.search(r"[+-]\d{4}$", text):  # Meta writes +0000
        text = f"{text[:-2]}:{text[-2:]}"
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def instagram_posts(
    settings: Settings, config: dict, credentials: dict, username: str, limit: int
) -> list[dict]:
    """Recent posts of another Business/Creator account (Business Discovery), newest
    first: {id, url, text, images, posted_at, author}."""
    username = username.strip().lstrip("@")
    if not USERNAME.fullmatch(username):
        raise MetaError(f"{username!r} isn't an Instagram username")
    media = (
        f"media.limit({limit}){{id,caption,media_type,media_url,thumbnail_url,permalink,"
        f"timestamp,children{{media_type,media_url}}}}"
    )
    body = await _get(
        settings,
        config["ig_user_id"],
        {"fields": f"business_discovery.username({username}){{username,name,{media}}}"},
        token_of(credentials),
    )
    found = body.get("business_discovery") or {}
    posts = []
    for item in (found.get("media") or {}).get("data") or []:
        images = []
        if item.get("media_type") == "CAROUSEL_ALBUM":
            for child in (item.get("children") or {}).get("data") or []:
                if child.get("media_type") == "IMAGE" and child.get("media_url"):
                    images.append(child["media_url"])
        elif item.get("media_type") == "VIDEO":
            if item.get("thumbnail_url"):
                images.append(item["thumbnail_url"])
        elif item.get("media_url"):
            images.append(item["media_url"])
        posts.append(
            {
                "id": f"ig:{item.get('id')}",
                "url": item.get("permalink"),
                "text": item.get("caption") or "",
                "images": images[:3],
                "posted_at": _time(item.get("timestamp")),
                "author": f"@{found.get('username') or username}",
            }
        )
    return posts


async def check(settings: Settings, credentials: dict) -> None:
    await _get(settings, "me", {"fields": "id"}, token_of(credentials))

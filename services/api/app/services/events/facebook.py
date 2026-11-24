"""Facebook Pages' public posts, through an Apify actor (ADR C-17).

Meta offers no open API for reading other Pages' posts (Page Public Content Access needs
business verification and app review), so a hosted scraper reads them: Celius sends the
Page URL to the actor and reads back its dataset. The Apify token is a server setting,
sent in a header, and never reaches the model. The actor's output varies between
versions, so fields are read leniently.
"""

import re
from datetime import UTC, datetime, timedelta

import httpx

from app.config import Settings

TIMEOUT = 180  # the run-sync endpoint waits for the actor to finish
PAGE_URL = re.compile(r"https://(www\.|m\.|web\.)?facebook\.com/[^\s?#]+/?", re.IGNORECASE)

# Tests replace this with httpx.MockTransport.
TRANSPORT: httpx.AsyncBaseTransport | None = None


class FacebookError(Exception):
    pass


def configured(settings: Settings) -> bool:
    return settings.apify_token is not None and bool(settings.apify_token.get_secret_value())


def clean_page_url(value: str) -> str:
    value = value.strip()
    if not value.lower().startswith("http"):
        value = f"https://www.facebook.com/{value.lstrip('/')}"
    if not PAGE_URL.fullmatch(value):
        raise FacebookError("Use the Page's address, like https://www.facebook.com/yourvenue")
    return value.rstrip("/")


def _time(value) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(seconds, UTC)
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _images(item: dict) -> list[str]:
    found: list[str] = []
    for media in item.get("media") or []:
        if not isinstance(media, dict):
            continue
        for candidate in (
            media.get("thumbnail"),
            (media.get("photo_image") or {}).get("uri"),
            (media.get("image") or {}).get("uri"),
            media.get("url") if str(media.get("url", "")).startswith("https://scontent") else None,
        ):
            if isinstance(candidate, str) and candidate.startswith("https://"):
                found.append(candidate)
                break
    for key in ("image", "imageUrl", "thumbnail"):
        if isinstance(item.get(key), str) and item[key].startswith("https://"):
            found.append(item[key])
    return list(dict.fromkeys(found))[:3]


async def page_posts(
    settings: Settings, page_url: str, since: datetime | None, limit: int
) -> list[dict]:
    """Recent posts of a public Page: {id, url, text, images, posted_at, author}."""
    if not configured(settings):
        raise FacebookError("Reading Facebook Pages isn't set up on this server (APIFY_TOKEN)")
    since = since or datetime.now(UTC) - timedelta(days=21)
    body = {
        "startUrls": [{"url": clean_page_url(page_url)}],
        "resultsLimit": limit,
        "onlyPostsNewerThan": since.date().isoformat(),
    }
    url = (
        f"https://api.apify.com/v2/acts/{settings.apify_facebook_actor}/run-sync-get-dataset-items"
    )
    token = settings.apify_token.get_secret_value() if settings.apify_token else ""
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT, transport=TRANSPORT) as client:
            response = await client.post(
                url, json=body, headers={"Authorization": f"Bearer {token}"}
            )
    except httpx.HTTPError as exc:
        raise FacebookError(f"Apify couldn't be reached ({exc.__class__.__name__})") from exc
    if response.status_code == 401:
        raise FacebookError("Apify refused the token (APIFY_TOKEN)")
    if response.status_code >= 400:
        raise FacebookError(f"Reading the Page failed (Apify {response.status_code})")
    try:
        items = response.json()
    except ValueError as exc:
        raise FacebookError("Apify's answer couldn't be read") from exc
    posts = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or item.get("error"):
            continue
        text = item.get("text") or item.get("message") or item.get("postText") or ""
        link = item.get("url") or item.get("postUrl") or item.get("facebookUrl")
        posted = _time(item.get("time") or item.get("timestamp") or item.get("date"))
        posts.append(
            {
                "id": f"fb:{item.get('postId') or link or hash(text)}",
                "url": link,
                "text": str(text),
                "images": _images(item),
                "posted_at": posted,
                "author": item.get("pageName") or page_url,
            }
        )
    return posts

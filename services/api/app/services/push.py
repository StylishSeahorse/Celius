"""Web Push: tell a device that a reply is ready, even when Celius isn't open.

Messages are encrypted for the browser (RFC 8291, aes128gcm) and the request is signed
with a VAPID key (RFC 8292), so the push service (Google, Mozilla, Apple, Microsoft) can't
read them. The VAPID key is derived from APP_SECRET_KEY: nothing new to store or back up.
Rotating the secret invalidates every subscription; devices re-subscribe on their next
visit, because the public key they subscribed with no longer matches.

Only endpoints on the known push services are accepted, so a subscription can't make the
worker send requests anywhere else.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from urllib.parse import urlsplit

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.db import SessionFactory
from app.db.base import utcnow
from app.models import Job, PushSubscription
from app.services.jobs import enqueue_job, job_handler

log = logging.getLogger(__name__)

RECORD_SIZE = 4096
MAX_PAYLOAD = 3000  # one record: payload + delimiter + 16-byte tag must fit in 4096
CURVE = ec.SECP256R1()
CURVE_ORDER = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
DEFAULT_PUSH_HOSTS = (
    "fcm.googleapis.com",  # Chrome, Edge on Android, most Chromium browsers
    "push.services.mozilla.com",  # Firefox
    "push.apple.com",  # Safari, iOS home-screen apps
    "notify.windows.com",  # Edge (WNS)
)


class InvalidSubscription(ValueError):
    pass


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64url_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _hmac(key: bytes, data: bytes) -> bytes:
    return hmac.new(key, data, hashlib.sha256).digest()


def _public_bytes(key: ec.EllipticCurvePublicKey) -> bytes:
    return key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )


# ---------------------------------------------------------------------- RFC 8291


def encrypt(
    payload: bytes,
    ua_public: str,
    auth_secret: str,
    *,
    sender_key: ec.EllipticCurvePrivateKey | None = None,
    salt: bytes | None = None,
) -> bytes:
    """The aes128gcm body for one push message (a single record)."""
    if len(payload) > MAX_PAYLOAD:
        raise ValueError("push payload too large")
    ua_public_bytes = b64url_decode(ua_public)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(CURVE, ua_public_bytes)
    sender_key = sender_key or ec.generate_private_key(CURVE)
    sender_public = _public_bytes(sender_key.public_key())
    salt = salt or os.urandom(16)

    shared = sender_key.exchange(ec.ECDH(), ua_key)
    prk_key = _hmac(b64url_decode(auth_secret), shared)
    ikm = _hmac(prk_key, b"WebPush: info\x00" + ua_public_bytes + sender_public + b"\x01")
    prk = _hmac(salt, ikm)
    cek = _hmac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = _hmac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    ciphertext = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)  # 0x02: last record
    header = salt + RECORD_SIZE.to_bytes(4, "big") + bytes([len(sender_public)]) + sender_public
    return header + ciphertext


# ---------------------------------------------------------------------- RFC 8292


def vapid_key(settings: Settings) -> ec.EllipticCurvePrivateKey:
    seed = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=b"celius", info=b"web-push-vapid"
    ).derive(settings.app_secret_key.get_secret_value().encode())
    scalar = int.from_bytes(seed, "big") % (CURVE_ORDER - 1) + 1
    return ec.derive_private_key(scalar, CURVE)


def public_key(settings: Settings) -> str:
    """The applicationServerKey browsers subscribe with."""
    return b64url(_public_bytes(vapid_key(settings).public_key()))


def vapid_headers(endpoint: str, settings: Settings, now: float | None = None) -> dict[str, str]:
    key = vapid_key(settings)
    parts = urlsplit(endpoint)
    claims = {
        "aud": f"{parts.scheme}://{parts.netloc}",
        "exp": int((now or time.time()) + 12 * 3600),
        "sub": settings.push_contact or settings.app_public_url,
    }
    header = b64url(json.dumps({"typ": "JWT", "alg": "ES256"}, separators=(",", ":")).encode())
    body = b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{header}.{body}".encode()
    r, s = decode_dss_signature(key.sign(signing_input, ec.ECDSA(hashes.SHA256())))
    signature = b64url(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
    token = f"{header}.{body}.{signature}"
    return {"Authorization": f"vapid t={token}, k={public_key(settings)}"}


# ---------------------------------------------------------------------- subscriptions


def check_endpoint(endpoint: str, settings: Settings) -> str:
    parts = urlsplit(endpoint)
    host = (parts.hostname or "").lower()
    allowed = [h.strip().lower() for h in settings.push_allowed_hosts.split(",") if h.strip()]
    allowed = allowed or list(DEFAULT_PUSH_HOSTS)
    if parts.scheme != "https" or not host:
        raise InvalidSubscription("Push endpoints must be https URLs")
    if not any(host == h or host.endswith("." + h) for h in allowed):
        raise InvalidSubscription(f"{host} is not a known push service")
    return endpoint


def check_keys(p256dh: str, auth: str) -> None:
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(CURVE, b64url_decode(p256dh))
        if len(b64url_decode(auth)) < 16:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise InvalidSubscription("The subscription's keys are not valid") from exc


async def subscribe(
    db: AsyncSession,
    user_id: uuid.UUID,
    settings: Settings,
    *,
    endpoint: str,
    p256dh: str,
    auth: str,
    user_agent: str | None,
) -> PushSubscription:
    check_endpoint(endpoint, settings)
    check_keys(p256dh, auth)
    existing = await db.scalar(
        select(PushSubscription).where(PushSubscription.endpoint == endpoint)
    )
    if existing is not None and existing.user_id != user_id:
        await db.delete(existing)  # the device now belongs to someone else's session
        existing = None
    subscription = existing or PushSubscription(id=uuid.uuid4(), user_id=user_id, endpoint=endpoint)
    subscription.p256dh = p256dh
    subscription.auth = auth
    subscription.user_agent = (user_agent or "")[:300] or None
    subscription.failures = 0
    if existing is None:
        db.add(subscription)
    await db.commit()
    return subscription


async def unsubscribe(db: AsyncSession, user_id: uuid.UUID, endpoint: str) -> bool:
    result = await db.execute(
        delete(PushSubscription).where(
            PushSubscription.user_id == user_id, PushSubscription.endpoint == endpoint
        )
    )
    await db.commit()
    return bool(result.rowcount)


def queue(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    title: str,
    body: str,
    url: str = "/",
    tag: str | None = None,
    urgency: str = "normal",
) -> Job:
    """Send a notification to all of the user's devices, from a background job."""
    return enqueue_job(
        db,
        user_id,
        "push.notify",
        {"title": title, "body": body, "url": url, "tag": tag, "urgency": urgency},
    )


async def send(
    subscription: PushSubscription,
    message: dict,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """POST one encrypted message; returns the push service's status code."""
    check_endpoint(subscription.endpoint, settings)
    payload = json.dumps(message, ensure_ascii=False).encode()
    if len(payload) > MAX_PAYLOAD:
        message = {**message, "body": message.get("body", "")[:500]}
        payload = json.dumps(message, ensure_ascii=False).encode()
    body = encrypt(payload, subscription.p256dh, subscription.auth)
    headers = {
        **vapid_headers(subscription.endpoint, settings),
        "Content-Encoding": "aes128gcm",
        "Content-Type": "application/octet-stream",
        "TTL": str(settings.push_ttl_seconds),
        "Urgency": message.get("urgency") or "normal",
    }
    async with httpx.AsyncClient(timeout=15, transport=transport, follow_redirects=False) as client:
        response = await client.post(subscription.endpoint, content=body, headers=headers)
    return response.status_code


# The transport is swappable for tests.
_transport: httpx.AsyncBaseTransport | None = None


@job_handler("push.notify")
async def notify_job(factory: SessionFactory, settings: Settings, job: Job) -> dict:
    if not settings.push_enabled:
        return {"sent": 0, "skipped": "push disabled"}
    message = {k: v for k, v in job.payload.items() if v is not None}
    sent = removed = failed = 0
    async with factory() as db:
        subscriptions = (
            await db.scalars(
                select(PushSubscription).where(PushSubscription.user_id == job.owner_id)
            )
        ).all()
        for subscription in subscriptions:
            try:
                status = await send(subscription, message, settings, _transport)
            except (httpx.HTTPError, InvalidSubscription, ValueError) as exc:
                log.warning("Push to %s failed: %s", urlsplit(subscription.endpoint).hostname, exc)
                status = 0
            if 200 <= status < 300:
                sent += 1
                subscription.failures = 0
                subscription.last_success_at = utcnow()
            elif status in (403, 404, 410):
                # Gone, or subscribed with a key we no longer have: the device re-subscribes.
                await db.delete(subscription)
                removed += 1
            else:
                failed += 1
                subscription.failures += 1
                if subscription.failures >= 20:
                    await db.delete(subscription)
                    removed += 1
        await db.commit()
    return {"sent": sent, "removed": removed, "failed": failed}

"""Phase 9: Web Push (RFC 8291 encryption, RFC 8292 VAPID), subscriptions, notifications."""

import hashlib
import hmac
import json
import os

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import func, select

from app.models import Job, PushSubscription
from app.services import push
from tests.fakes import FakeLlama, drain_jobs, drain_runs

FCM = "https://fcm.googleapis.com/fcm/send/device-token-123"


def _private(b64: str) -> ec.EllipticCurvePrivateKey:
    return ec.derive_private_key(int.from_bytes(push.b64url_decode(b64), "big"), push.CURVE)


def decrypt(body: bytes, ua_key: ec.EllipticCurvePrivateKey, auth_secret: bytes) -> bytes:
    """The browser's side of RFC 8291."""
    salt, keylen = body[:16], body[20]
    sender_public = body[21 : 21 + keylen]
    ciphertext = body[21 + keylen :]
    sender = ec.EllipticCurvePublicKey.from_encoded_point(push.CURVE, sender_public)
    ua_public = ua_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    shared = ua_key.exchange(ec.ECDH(), sender)

    def mac(key, data):
        return hmac.new(key, data, hashlib.sha256).digest()

    ikm = mac(mac(auth_secret, shared), b"WebPush: info\x00" + ua_public + sender_public + b"\x01")
    prk = mac(salt, ikm)
    cek = mac(prk, b"Content-Encoding: aes128gcm\x00\x01")[:16]
    nonce = mac(prk, b"Content-Encoding: nonce\x00\x01")[:12]
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


class Device:
    """A browser that subscribed: its keys, and what it receives."""

    def __init__(self, endpoint=FCM):
        self.key = ec.generate_private_key(push.CURVE)
        self.auth = os.urandom(16)
        self.endpoint = endpoint
        self.received: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.status = 201

    def subscription(self) -> dict:
        public = self.key.public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        return {
            "endpoint": self.endpoint,
            "keys": {"p256dh": push.b64url(public), "auth": push.b64url(self.auth)},
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.received.append(json.loads(decrypt(request.content, self.key, self.auth)))
        return httpx.Response(self.status)


# ---------------------------------------------------------------------- crypto


def test_encryption_matches_the_rfc_8291_example():
    body = push.encrypt(
        b"When I grow up, I want to be a watermelon",
        "BCVxsr7N_eNgVRqvHtD0zTZsEc6-VV-JvLexhqUzORcxaOzi6-AYWXvTBHm4bjyPjs7Vd8pZGH6SRpkNtoIAiw4",
        "BTBZMqHH6r4Tts7J_aSIgg",
        sender_key=_private("yfWPiYE-n46HLnH0KqZOF1fJJU3MYrct3AELtAQ-oRw"),
        salt=push.b64url_decode("DGv6ra1nlYgDCS1FRnbzlw"),
    )
    assert push.b64url(body) == (
        "DGv6ra1nlYgDCS1FRnbzlwAAEABBBP4z9KsN6nGRTbVYI_c7VJSPQTBtkgcy27mlmlMoZIIgDll6e3vC"
        "YLocInmYWAmS6TlzAC8wEqKK6PBru3jl7A_yl95bQpu6cVPTpK4Mqgkf1CXztLVBSt2Ks3oZwbuwXPXL"
        "WyouBWLVWGNWQexSgSxsj_Qulcy4a-fN"
    )


def test_encryption_round_trips_with_fresh_keys():
    device = Device()
    sub = device.subscription()["keys"]
    body = push.encrypt(b'{"title":"Hi"}', sub["p256dh"], sub["auth"])
    assert decrypt(body, device.key, device.auth) == b'{"title":"Hi"}'
    with pytest.raises(ValueError):
        push.encrypt(b"x" * (push.MAX_PAYLOAD + 1), sub["p256dh"], sub["auth"])


def test_vapid_key_is_stable_and_the_token_verifies(settings):
    assert push.public_key(settings) == push.public_key(settings)
    header = push.vapid_headers(FCM, settings, now=1_000_000)["Authorization"]
    token, key = header.removeprefix("vapid t=").split(", k=")
    assert key == push.public_key(settings)
    head, claims, signature = token.split(".")
    payload = json.loads(push.b64url_decode(claims))
    assert payload["aud"] == "https://fcm.googleapis.com"
    assert payload["exp"] == 1_000_000 + 12 * 3600
    raw = push.b64url_decode(signature)
    der = encode_dss_signature(int.from_bytes(raw[:32], "big"), int.from_bytes(raw[32:], "big"))
    public = ec.EllipticCurvePublicKey.from_encoded_point(push.CURVE, push.b64url_decode(key))
    public.verify(der, f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256()))  # raises if bad

    settings.app_secret_key = settings.app_secret_key.__class__("another-secret-of-enough-length")
    assert push.public_key(settings) != key  # a new secret means new subscriptions


# ---------------------------------------------------------------------- subscriptions


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://fcm.googleapis.com/fcm/send/x",  # not https
        "https://evil.example/collect",  # not a push service
        "https://fcm.googleapis.com.evil.example/x",  # look-alike
        "https://192.168.0.204/x",  # the LAN
    ],
)
async def test_only_push_services_are_accepted(client, auth_headers, endpoint):
    device = Device(endpoint)
    response = await client.post(
        "/api/v1/push/subscriptions", json=device.subscription(), headers=auth_headers
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_subscription"


async def test_subscribe_status_and_unsubscribe(client, auth_headers):
    device = Device()
    bad = device.subscription()
    bad["keys"]["p256dh"] = push.b64url(b"\x04" + b"\x01" * 64)  # not a point on the curve
    assert (
        await client.post("/api/v1/push/subscriptions", json=bad, headers=auth_headers)
    ).status_code == 422

    sub = device.subscription()
    assert (
        await client.post("/api/v1/push/subscriptions", json=sub, headers=auth_headers)
    ).status_code == 204
    # Subscribing again (same device) doesn't duplicate.
    await client.post("/api/v1/push/subscriptions", json=sub, headers=auth_headers)
    info = (
        await client.get("/api/v1/push", params={"endpoint": device.endpoint}, headers=auth_headers)
    ).json()
    assert info["devices"] == 1 and info["subscribed"] and info["public_key"]

    await client.post(
        "/api/v1/push/subscriptions/remove",
        json={"endpoint": device.endpoint},
        headers=auth_headers,
    )
    assert (await client.get("/api/v1/push", headers=auth_headers)).json()["devices"] == 0


# ---------------------------------------------------------------------- notifications


@pytest.fixture
def fake() -> FakeLlama:
    return FakeLlama()


@pytest.fixture
def model_handler(fake):
    return fake.handler


async def test_a_finished_reply_notifies_every_device(
    client, auth_headers, sessionmaker, gateway, settings, fake, monkeypatch
):
    phone, laptop = Device(), Device("https://updates.push.services.mozilla.com/wpush/v2/abc")
    for device in (phone, laptop):
        await client.post(
            "/api/v1/push/subscriptions", json=device.subscription(), headers=auth_headers
        )

    def route(request):
        target = phone if request.url.host == "fcm.googleapis.com" else laptop
        return target.handler(request)

    monkeypatch.setattr(push, "_transport", httpx.MockTransport(route))
    fake.reply = "Tomatoes go in after the last frost, usually mid-May."
    conv = (
        await client.post("/api/v1/conversations", json={"title": "Garden"}, headers=auth_headers)
    ).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "When?"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    await drain_jobs(sessionmaker, settings)

    for device in (phone, laptop):
        (message,) = device.received
        assert message["title"] == "Garden"
        assert message["body"].startswith("Tomatoes go in")
        assert message["url"] == f"/#/c/{conv['id']}"
        assert message["tag"] == conv["id"]
        request = device.requests[0]
        assert request.headers["content-encoding"] == "aes128gcm"
        assert request.headers["authorization"].startswith("vapid t=")
        assert int(request.headers["ttl"]) > 0


async def test_gone_devices_are_forgotten_and_nobody_subscribed_means_no_jobs(
    client, auth_headers, sessionmaker, gateway, settings, fake, monkeypatch
):
    device = Device()
    device.status = 410
    await client.post(
        "/api/v1/push/subscriptions", json=device.subscription(), headers=auth_headers
    )
    monkeypatch.setattr(push, "_transport", httpx.MockTransport(device.handler))
    await client.post("/api/v1/push/test", headers=auth_headers)
    await drain_jobs(sessionmaker, settings)
    assert len(device.received) == 1
    async with sessionmaker() as db:
        assert await db.scalar(select(func.count()).select_from(PushSubscription)) == 0

    # With no device subscribed, a finished reply queues nothing.
    conv = (await client.post("/api/v1/conversations", json={}, headers=auth_headers)).json()
    await client.post(
        f"/api/v1/conversations/{conv['id']}/messages",
        json={"client_message_id": os.urandom(8).hex(), "content": "Hi"},
        headers=auth_headers,
    )
    await drain_runs(sessionmaker, gateway, settings)
    async with sessionmaker() as db:
        push_jobs = await db.scalar(
            select(func.count()).select_from(Job).where(Job.kind == "push.notify")
        )
    assert push_jobs == 1  # only the test notification

"""Phase 7 security review: authentication on every route, throttling, password changes,
response headers and cross-site requests."""

import re

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.ops.users import reset_password
from tests.conftest import PASSWORD

# Routes that must work without signing in.
PUBLIC = {
    "/api/v1/health/live",
    "/api/v1/health/ready",
    "/api/v1/auth/login",
    "/api/v1/auth/register",  # needs a valid invitation token
    "/api/v1/auth/invitations/{token}",
    "/api/v1/metrics",  # its own bearer token (METRICS_TOKEN); 404 without it
}
FAKE_ID = "00000000-0000-4000-8000-000000000000"


def api_routes():
    """Every operation the API publishes (from its OpenAPI schema)."""
    for path, operations in create_app().openapi()["paths"].items():
        for method in operations:
            if method.upper() not in ("HEAD", "OPTIONS"):
                yield method.upper(), path


@pytest.mark.parametrize(("method", "path"), sorted(api_routes()))
async def test_every_api_route_requires_authentication(client, method, path):
    if path in PUBLIC:
        pytest.skip("public by design")
    url = re.sub(r"\{[^}]+\}", FAKE_ID, path)
    response = await client.request(method, url, json={})
    assert response.status_code == 401, (method, path, response.status_code)


async def test_login_is_throttled_after_repeated_failures(client, user, settings, sessionmaker):
    settings.login_max_failures_per_user = 3
    for _ in range(3):
        failed = await client.post(
            "/api/v1/auth/login", json={"username": "owner", "password": "wrong password!"}
        )
        assert failed.status_code == 401
    blocked = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
    )
    assert blocked.status_code == 429  # even with the right password
    assert int(blocked.headers["retry-after"]) > 0
    # Other accounts aren't affected by one name's failures.
    other = await client.post(
        "/api/v1/auth/login", json={"username": "someone", "password": "whatever-123"}
    )
    assert other.status_code == 401

    # The owner resets the password from the command line: throttling is cleared.
    await reset_password("owner", "a brand new password", factory=sessionmaker)
    ok = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": "a brand new password"}
    )
    assert ok.status_code == 200


async def test_login_is_throttled_per_address(client, user, settings):
    settings.login_max_failures_per_ip = 4
    for i in range(4):
        await client.post(
            "/api/v1/auth/login", json={"username": f"guess{i}", "password": "nope nope nope"}
        )
    blocked = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
    )
    assert blocked.status_code == 429


async def test_changing_the_password_signs_out_other_devices(client, user, auth_headers):
    other = (
        await client.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})
    ).json()
    client.cookies.clear()
    other_headers = {"Authorization": f"Bearer {other['access_token']}"}
    sessions = (await client.get("/api/v1/auth/sessions", headers=auth_headers)).json()
    assert len(sessions) == 2 and sum(s["current"] for s in sessions) == 1

    wrong = await client.post(
        "/api/v1/auth/password",
        headers=auth_headers,
        json={"current_password": "nope", "new_password": "x" * 12},
    )
    assert wrong.status_code == 403
    short = await client.post(
        "/api/v1/auth/password",
        headers=auth_headers,
        json={"current_password": PASSWORD, "new_password": "short"},
    )
    assert short.status_code == 422
    changed = await client.post(
        "/api/v1/auth/password",
        headers=auth_headers,
        json={"current_password": PASSWORD, "new_password": "correct horse battery 2"},
    )
    assert changed.status_code == 204
    assert (await client.get("/api/v1/auth/me", headers=auth_headers)).status_code == 200
    assert (await client.get("/api/v1/auth/me", headers=other_headers)).status_code == 401
    old = await client.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})
    assert old.status_code == 401


async def test_revoke_other_sessions(client, user, auth_headers):
    other = (
        await client.post("/api/v1/auth/login", json={"username": "owner", "password": PASSWORD})
    ).json()
    client.cookies.clear()
    await client.post("/api/v1/auth/sessions/revoke-others", headers=auth_headers)
    me = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {other['access_token']}"}
    )
    assert me.status_code == 401


async def test_security_headers(client, user, auth_headers):
    response = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "same-origin"
    assert "content-security-policy" not in response.headers  # API JSON


async def test_the_web_app_gets_a_strict_csp(tmp_path):
    (tmp_path / "index.html").write_text("<!doctype html><title>Celius</title>")
    app = create_app(
        Settings(
            app_env="test", static_root=str(tmp_path), database_url="sqlite+aiosqlite:///:memory:"
        )
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as c:
        page = await c.get("/some/client/route")
    assert page.status_code == 200
    csp = page.headers["content-security-policy"]
    assert "script-src 'self';" in csp and "frame-ancestors 'none'" in csp
    assert "unsafe-eval" not in csp and "img-src 'self' data: blob:;" in csp


async def test_cross_site_cookie_requests_are_refused(client, user):
    login = await client.post(
        "/api/v1/auth/login", json={"username": "owner", "password": PASSWORD}
    )
    assert login.status_code == 200 and "celius_session" in client.cookies
    evil = await client.post(
        "/api/v1/conversations", json={}, headers={"Origin": "https://evil.example"}
    )
    assert evil.status_code == 403
    assert evil.json()["error"]["code"] == "cross_origin"
    same = await client.post(
        "/api/v1/conversations", json={}, headers={"Origin": "http://testserver"}
    )
    assert same.status_code == 201
    # Bearer tokens (desktop app, scripts) aren't sent automatically: no check needed.
    bearer = await client.post(
        "/api/v1/conversations",
        json={},
        headers={
            "Origin": "tauri://localhost",
            "Authorization": f"Bearer {login.json()['access_token']}",
        },
    )
    assert bearer.status_code == 201
